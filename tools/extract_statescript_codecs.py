"""Statescript payload codecs of the 1.74 PC client (build 104319), read from the client code. An IDA script
that writes data/statescript_codecs_174.json: every statescript state class the client registers, the class it
builds for each node class, and what that class reads from a frame (its property block and its class reader).

Run it in IDA with a dump of the client's runtime image (base 0x7FF788F10000) open:
    File > Script file... > tools/extract_statescript_codecs.py
It only reads the image (no database changes) and needs capstone (IDA's Python has it). It also reads, when
present, the server's graph data (data/game_graphs_174.json.gz: class names and usage counts) and OWLib's STU
types (the folder TankLib/STU/Types of an OWLib checkout, named by the OWLIB_TYPES environment variable). It
prints a short summary (about 15 s).

What it does (addresses are the 1.74 image's):
- The registration list (head 0x7FF78C1A69D8, insert 0x7FF78AA9FE40, filled by CRT initializers) holds
  records {+0 handler factory, +8 construct, +0x10 destroy, +0x18 typeinfo, +0x20 priority}. The builder
  0x7FF78AA9FE60 walks it from the head and replaces an entry when the record's priority is >= the entry's
  (`cmp [rcx+20h], eax; jl` at 0x7FF78AAA007D): the highest priority wins, and on a tie the record nearer the
  list's end. The builder runs from the game's init (0x7FF7894ADF79), after the initializers.
- A node class (an STU type) finds its class by its node typeinfo (node vt+8), then by the typeinfo's parents
  (lookup 0x7FF78AAA02B0); with none, the client builds a plain State (0x7FF78AAA1460).
- A state's payload is its property block (state vt+0x168, read by 0x7FF78AA81930) and then its class reader
  (state vt+0x158), called by 0x7FF78AA82060 as (state, reader, H, full). Block fields are read in order with
  no presence bits; a field is skipped when its flags have 8, or 0x10 without H, or 0x20 without full
  (0x7FF78AA82F50). H = an owner frame; full = a full frame (deltas pass 0, 0x7FF78991C94F).
- StEC classes (0x7FF789E6E960, by node class hash) carry X/StAc instead of A.
The reader layouts below were read by hand in this image (addresses in each entry's evidence); the script
checks that every reader the registry gives has one.
"""

import contextlib
import gzip
import json
import os
import re
import struct
from collections import Counter
from pathlib import Path

import capstone
import ida_bytes

BASE = 0x7FF788F10000
TEXT = (0x7FF788F11000, 0x7FF78B400000)
DATA = (0x7FF78B400000, 0x7FF78C6A9000)
LIST_HEAD = 0x7FF78C1A69D8
STATE_TYPEINFO = 0x7FF78BCE53F0  # STUStatescriptState's node typeinfo
TYPE_VT = 0x7FF78B4F1640  # STU reflection type objects
NULLSUB = 0x7FF78ABF5640
STEC_PREDICATE = 0x7FF789E6E96A  # after `call [rax+28h]` (the node's STU type)
PRIM_GETTER = 0x7FF78AAEDC70  # type vt+0x30: mov eax, [rcx+28h]; ret (kind 0, the primitive id)
KIND_GETTER = 0x7FF789B315B0  # type vt+0x38: vt+0x30 >> 32
# Primitive ids are CRC32s of the type names (predicates 0x7FF78AAE5860...; sizes 0x7FF78AAF1C90). The block
# reader reads a primitive as its bytes in memory order, 8 bits each (0x7FF78AAF0490); u64 with field
# flags2 & 2 and strings take other paths (0x7FF78AAF0300) and are not used by any state class here.
PRIMITIVES = {
    0x3B9327D2: ("u8", 8),
    0x6DC98054: ("s8", 8),
    0xA49CE182: ("u16", 16),
    0xA0119D30: ("s16", 16),
    0x91C74719: ("u32", 32),
    0x954A3BAB: ("s32", 32),
    0x05D31669: ("u64", 64),
    0x015E6ADB: ("s64", 64),
    0x8FA75A30: ("f32", 32),
    0x1BB30B40: ("f64", 64),
}
BLOCK_OFFSETS = {  # block function -> the state sub-object it lists (lea rax, [rcx+off])
    0x7FF789C54BF0: 0x1C0,
    0x7FF7897FFA90: 0x1E8,
    0x7FF7897F9C00: 0x1C8,
    0x7FF7897F58C0: 0x2C8,
    0x7FF789807CD0: 0x200,
}


# --- layout ops (data/statescript_codecs_174.json "layout"; read by ow174/game/script/codecs.py) -----------


def bits(f, n=1):
    return {"op": "bits", "n": n, "f": f}


def f32(f):
    return {"op": "f32", "f": f}


def w_u16(f):
    return {"op": "w_u16", "f": f}


def var_b(f):
    return {"op": "var_b", "f": f}


def signed_var(f):
    return {"op": "signed_var", "f": f}


def entity(f):
    return {"op": "entity_id", "f": f}


def value(f):
    return {"op": "value", "f": f}


def link(f="link"):
    return {"op": "link", "f": f}


def when(cond, then, otherwise=()):
    return {"op": "if", "c": cond, "then": list(then), "else": list(otherwise)}


def repeat(count, f, body):
    return {"op": "repeat", "count": count, "f": f, "body": list(body)}


# Shared readers: bit 0x7FF789697BE0, bits(n) 0x7FF78969BDB0 (LSB first), w_u16 0x7FF78A76BA50 and
# 0x7FF78AA85190, var_b 0x7FF78A76B800 (0+8, 10+16, 11+32), signed_var 0x7FF78AA836A0 (sign, then 0+8, 10+16,
# 11+32), optional w_u16 0x7FF78A76BB20, optional f32 0x7FF789BB6170, entity_id 0x7FF789CC0F70 (2 type bits +
# var_b), tagged value 0x7FF789C663A0 -> 0x7FF789C67DE0, link field 0x7FF78AA81D80 -> 0x7FF78AA837D0 (a bit;
# 1: w_u16 instance id, then the state index in the linked instance's graph width: m_statesBitCount with H,
# else m_remoteSyncNodesBitCount).
TIME = when("has_time", [bits("time_set"), when("time_set", [var_b("time")])])
CHASE = [
    bits("vector"),
    bits("has_remaining"),
    bits("reached"),
    f32("x"),
    when("vector", [f32("y"), f32("z")]),
    when("has_remaining", [bits("big"), when("big", [bits("remaining", 32)], [bits("remaining", 16)])]),
    bits("has_last"),
    when("has_last", [var_b("t")]),
]
ABILITY = [
    link(),
    bits("has_cur"),
    when("has_cur", [f32("cur"), bits("has_rate"), when("has_rate", [f32("rate")]), var_b("t")]),
]
VOLLEY = [
    when(
        "H",
        [signed_var("t1"), bits("has_offset"), when("has_offset", [signed_var("offset"), w_u16("subindex")])],
    ),
    bits("has_volleys"),
    when("has_volleys", [w_u16("volleys")]),
    bits("counter", 6),
]
MESSAGE = [
    when("H", [bits("has_sender"), when("has_sender", [bits("sender_kind"), var_b("sender")])]),
    bits("stacked"),
]
TARGETS = [
    bits("has_list"),
    when("has_list", [w_u16("n"), repeat("n", "targets", [bits("entity", 32), var_b("age")])]),
]
LINKED_TARGET = [
    link(),
    when(
        "H&full",
        [
            bits("has_entity"),
            when("has_entity", [entity("entity")]),
            repeat(7, "values", [bits("set"), when("set", [f32("value")])]),
        ],
    ),
]
HIT = [
    value("value"),
    entity("entity"),
    when("valid:entity", [var_b("extra")]),
    w_u16("w"),
    bits("a"),
    bits("b"),
]
SHOCKWAVE = [
    bits("has_list"),
    when(
        "has_list", [w_u16("n"), repeat("n", "items", [w_u16("a"), bits("b", 16), bits("c", 32)]), f32("f")]
    ),
]
EXTRA = [entity("entity"), bits("has_extra"), when("has_extra", [var_b("extra")])]
LAYOUT_861D9015 = [
    bits("a"),
    when(
        "a",
        [
            bits("v0", 16),
            bits("v1", 16),
            bits("v2", 32),
            bits("v3", 32),
            bits("v4", 32),
            bits("v5", 16),
            bits("v6", 16),
            bits("has_entity"),
            when("has_entity", EXTRA),
            bits("g"),
        ],
    ),
]
STACK = [when("H", [bits("top"), bits("under")])]
# reader -> (codec, layout or None, status, evidence)
READERS = {
    NULLSUB: ("none", [], "VERIFIED", "nullsub: reads nothing"),
    0x7FF78AAA6460: (
        "stack",
        STACK,
        "VERIFIED",
        "0x7FF78AAA647E: only with H; two bits into the sub-object +0x78/+0x79 "
        "(0x7FF78AAA64F6, 0x7FF78AAA6560)",
    ),
    0x7FF78AAA6F20: (
        "stack_counter",
        [*STACK, w_u16("counter")],
        "VERIFIED",
        "as stack, then w_u16 into the activation counter +0x44 (0x7FF78AAA7045)",
    ),
    0x7FF78AAA92D0: (
        "switch",
        [bits("current")],
        "VERIFIED",
        "one bit into currentlyTrue +0x238 (0x7FF78AAA9330)",
    ),
    0x7FF789BCD370: (
        "anim",
        [link(), bits("counter", 3), bits("has_time"), TIME],
        "VERIFIED",
        "link 0x7FF789BCD380, bits(3) counter +0x44, bit, bit, var_b time +0x348 (0x7FF789BCD452)",
    ),
    0x7FF78AAA8400: (
        "chase",
        CHASE,
        "VERIFIED",
        "3 bits +0x9C..+0x9E, f32 x[,y,z] (0x7FF78AAA858C), remaining 32/16 bits +0x98 (0x7FF78AAA8678), "
        "var_b t: last tick = frame length * (frame + 1) - t (0x7FF78AAA86F7)",
    ),
    0x7FF789894540: (
        "counter2",
        [bits("counter", 2)],
        "VERIFIED",
        "bits(2) (0x7FF789894561) = the activation counter & 3; stored to +0x44/+0x260 when active or full",
    ),
    0x7FF789898050: (
        "counter2",
        [bits("counter", 2)],
        "VERIFIED",
        "bits(2) (0x7FF789898071), as 0x7FF789894540",
    ),
    0x7FF78AAAC380: (
        "button",
        [when("H", [bits("counter", 4)])],
        "VERIFIED",
        "only with H: bits(4) (0x7FF78AAAC3A1), the client snaps its counter +0x44 to within 8",
    ),
    0x7FF789C5BAC0: (
        "send",
        [bits("has_reply"), when("has_reply", [w_u16("reply")])],
        "VERIFIED",
        "optional w_u16 (0x7FF78A76BB20) into +0x204",
    ),
    0x7FF789910BE0: (
        "message",
        MESSAGE,
        "VERIFIED",
        "0x7FF789C5B970: with H a bit, then a bit (0xA0000000 / 0x80000000) and var_b sender; "
        "then a bit +0x7C",
    ),
    0x7FF789B94AC0: (
        "ability",
        ABILITY,
        "VERIFIED",
        "link 0x7FF789B94AD5, bit, f32 cur +0x268 (0x7FF789B94B5C), bit, f32 rate (0x7FF789B94BE1), var_b t "
        "(0x7FF789B94C2F)",
    ),
    0x7FF789BA13A0: (
        "targets",
        TARGETS,
        "VERIFIED",
        "bit, w_u16 n (0x7FF789BA1426), n x (bits(32) entity, var_b age) (0x7FF789BA14A9)",
    ),
    0x7FF789C70400: (
        "volley",
        VOLLEY,
        "VERIFIED",
        "with H: signed_var t1 (start = frame length * frame - t1), bit [signed_var, w_u16]; then the volley "
        "count (0x7FF789C70580: bit [w_u16], else 1) and bits(6) +0x2C8 (0x7FF789C70558)",
    ),
    0x7FF78AAB1A30: (
        "subscript",
        [when("H", [w_u16("child"), bits("flag")])],
        "VERIFIED",
        "only with H: w_u16 child +0x1C8 (0x7FF78AAB1A4C), bit +0x1CA",
    ),
    0x7FF78AA81D80: ("link", [link()], "VERIFIED", "the link field alone (0x7FF78AA837D0)"),
    0x7FF78990D760: (
        "pulser",
        [bits("fresh"), bits("count", 8)],
        "VERIFIED",
        "bit, bits(8) count +0x24C (0x7FF78990D7DC); then +0x248 from the count (0x7FF78990D7F5)",
    ),
    0x7FF789B983D0: (
        "linked_target",
        LINKED_TARGET,
        "VERIFIED",
        "link, then only with H and full: bit [entity_id], 7 optional f32 (0x7FF789BB6170)",
    ),
    0x7FF789806D90: ("link", [link()], "VERIFIED", "link, then 0x7FF7898093C0 without the reader"),
    0x7FF789822B10: ("link", [link()], "VERIFIED", "link, then 0x7FF789826B00 without the reader"),
    0x7FF789822A30: ("link", [link()], "VERIFIED", "link, then 0x7FF7898267C0 without the reader"),
    0x7FF789806DB0: ("none", [], "VERIFIED", "no reads: 0x7FF78980C1C0(state, frame)"),
    0x7FF789C56A00: (
        "hits",
        [w_u16("n"), repeat("n", "hits", HIT), bits("flag")],
        "VERIFIED",
        "w_u16 n, per hit a tagged value (0x7FF789C663A0), entity_id, var_b when the entity is valid, w_u16, "
        "two bits; then a bit +0x3B4",
    ),
    0x7FF789B97670: (
        "value_slot",
        [bits("which"), bits("value", 32)],
        "VERIFIED",
        "bit, bits(32) into +0x1C8 or +0x1CC",
    ),
    0x7FF789C3D0E0: (
        "flags13",
        [bits("low", 6), bits("more"), when("more", [bits("high", 7)])],
        "VERIFIED",
        "bits(6), bit, bits(7) << 6 into +0x238",
    ),
    0x7FF789BA11B0: (
        "shockwave",
        SHOCKWAVE,
        "VERIFIED",
        "bit; 1: w_u16 n, n x (w_u16, bits(16), bits(32)), f32 (0x7FF789BA1335)",
    ),
    0x7FF7897F4520: (
        "two_vectors",
        [bits("a"), bits("b"), when("b", [repeat(6, "v", [f32("x")])])],
        "VERIFIED",
        "0x7FF789B97F00: bit +0x1FB, bit; 1: 6 x f32",
    ),
    0x7FF78AAA9D40: ("two_bits", [bits("a"), when("!a", [bits("b")])], "VERIFIED", "bit; 0: a second bit"),
    0x7FF789BA1150: (
        "start",
        [when("H", [signed_var("t")])],
        "VERIFIED",
        "only with H: signed_var t into +0x48",
    ),
    0x7FF7897FEEF0: ("time16", [bits("time", 16)], "VERIFIED", "bits(16) (0x7FF7897FEF04)"),
    0x7FF7897F77D0: ("blink", [when("!H", [bits("value", 32)])], "VERIFIED", "only without H: bits(32)"),
    0x7FF78988FA30: (
        "861D9015",
        LAYOUT_861D9015,
        "VERIFIED",
        "0x7FF789C1F760: bit; 1: 16, 16, 32, 32, 32, 16, 16 bits, bit [entity_id, bit [var_b]], bit +0x1F9",
    ),
    0x7FF7899E7050: ("bit", [bits("value")], "VERIFIED", "one bit into +0x470"),
    0x7FF789C559D0: (
        "63B7ADFD",
        [bits("a"), when("a", [bits("b"), var_b("value")])],
        "VERIFIED",
        "bit; 1: bit, var_b",
    ),
    0x7FF78AAAC2F0: ("239B1E64", [var_b("value"), bits("flag")], "VERIFIED", "var_b +0x1C0, bit +0x1C4"),
    0x7FF78AAAC3F0: (
        "9E3A8497",
        None,
        "UNVERIFIED",
        "w_u16 n, bit, bit, then n entries of bitlen(n-1) bits and one more: not machine-readable here "
        "(no graph uses it)",
    ),
    0x7FF7898A54E0: (
        "026ED8D3",
        None,
        "UNVERIFIED",
        "entity_id, bits(4), bit, lists of 16-bit ids: not read to the end (no graph uses it)",
    ),
    0x7FF78AAA8BF0: (
        "C72FF969",
        None,
        "UNVERIFIED",
        "with H: 0x7FF78A76B980 (an optional signed value), two bits (no graph uses it)",
    ),
}
# Codec names of a whole payload (block fields + reader) where the block sends something, and names for the
# sent block fields the server writes (by field hash; the others are m_<hash>).
BLOCK_CODECS = {"C38B92B4": "ability", "D74D4F47": "frames"}
BLOCK_FIELD_NAMES = {"7EEFB57A": "flags", "3D70B9F1": "target"}
# Real frames: the minidumps of the two live Soldier crashes. Each crash frame was rebuilt from the server's
# code and read with this table; the client's reader stopped where the table's does (crash 1: 2026-10-01, bit
# 2842, 0x7FF78991D6DB; crash 2: 2026-10-02 00:52:18 J, bit 857 in the variable store, and 00:52:32 R, bit 441
# in the owner event list). The classes read in those frames before the crash:
DUMPS = {"1": "crash 1 (bit 2842)", "J": "crash 2 dump J (bit 857)", "R": "crash 2 dump R (bit 441)"}
DUMP_CLASSES = {
    "01178155": "1", "23A661BD": "1", "2F4E2E3F": "1", "316CFEF2": "1", "37D754C8": "1", "594C2D80": "1JR",
    "60FC201F": "1", "6540C278": "1R", "691BFA55": "JR", "6AE5D1A1": "1J", "76F0CF47": "1J", "7A7F2732": "1",
    "7C37840C": "1", "8CC3BD5A": "1", "90C3BCEA": "1", "96F1DBCF": "1", "9D6CF8AC": "1", "9D7BF987": "1",
    "B480A974": "1J", "BD02E168": "1", "C38B92B4": "1R", "CD46AF93": "1", "D74D4F47": "1", "D9843704": "1",
    "DEBD057B": "1", "E13B30A8": "1", "E56BA8D8": "1", "EED6D952": "J", "F5A99B2A": "1",
}  # fmt: skip
HEX_KEYS = {"record", "construct", "destroy", "typeinfo", "vtable", "reader", "block", "writer"}
HEX_KEYS |= {"block_vtable", "block_type"}


# --- image helpers ---------------------------------------------------------------------------------------

_cs = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
_text = ida_bytes.get_bytes(TEXT[0], TEXT[1] - TEXT[0])
_data = ida_bytes.get_bytes(DATA[0], DATA[1] - DATA[0])


def q(ea):
    if DATA[0] <= ea < DATA[1] - 8:
        return struct.unpack_from("<Q", _data, ea - DATA[0])[0]
    return ida_bytes.get_qword(ea)


def d(ea):
    return ida_bytes.get_dword(ea)


def decode(ea):
    for ins in _cs.disasm(ida_bytes.get_bytes(ea, 16), ea):
        return ins
    return None


def rip_target(ins):
    return ins.address + ins.size + int(ins.op_str.split("rip + ")[1].split("]")[0], 16)


def stub_target(ea):
    """`lea rax, [rip+x]; ret` -> x"""
    b = ida_bytes.get_bytes(ea, 8)
    if b and b[:3] == b"\x48\x8d\x05" and b[7:8] == b"\xc3":
        return ea + 7 + struct.unpack("<i", b[3:7])[0]
    return None


_PAIR = {
    "ja": "jbe",
    "jbe": "ja",
    "jb": "jae",
    "jae": "jb",
    "je": "jne",
    "jne": "je",
    "jo": "jno",
    "jno": "jo",
    "js": "jns",
    "jns": "js",
    "jp": "jnp",
    "jnp": "jp",
    "jl": "jge",
    "jge": "jl",
    "jg": "jle",
    "jle": "jg",
}
_FLAGS = {
    "jo": ("OF", 1),
    "jno": ("OF", 0),
    "jb": ("CF", 1),
    "jae": ("CF", 0),
    "je": ("ZF", 1),
    "jne": ("ZF", 0),
    "js": ("SF", 1),
    "jns": ("SF", 0),
}
_KEEP_FLAGS = {"lea", "nop", "push", "pop", "xchg", "bswap", "not"}
_SHIFTS = {"shl", "shr", "sal", "sar", "rol", "ror", "rcl", "rcr"}


def trace(start, maxblocks=80, maxins=900):
    """The real instructions from `start`, following jumps through this image's opaque predicates (a jcc right
    after test/and/or/xor or clc/stc decides on known flags; a jcc X followed by the opposite jcc X is a jmp).
    Blocks in visiting order; calls are not followed."""
    todo, seen, out, count = [start], set(), [], 0
    while todo and len(out) < maxblocks and count < maxins:
        ea = todo.pop(0)
        block, flags = [], {}
        while count < maxins and ea not in seen:
            seen.add(ea)
            ins = decode(ea)
            if ins is None:
                break
            count += 1
            block.append(ins)
            m, nxt = ins.mnemonic, ea + ins.size
            if m in ("ret", "int3", "ud2", "hlt"):
                break
            if m == "jmp":
                try:
                    ea, flags = int(ins.op_str, 16), {}
                    continue
                except ValueError:
                    break
            if m in _PAIR:
                target = int(ins.op_str, 16)
                after = decode(nxt)
                if after is not None and after.mnemonic == _PAIR[m] and after.op_str == ins.op_str:
                    ea, flags = target, {}
                    continue
                if m in _FLAGS and _FLAGS[m][0] in flags:
                    ea = target if flags[_FLAGS[m][0]] == _FLAGS[m][1] else nxt
                    continue
                todo.append(target)
                ea, flags = nxt, {}
                continue
            if m.startswith("j"):
                with contextlib.suppress(ValueError):
                    todo.append(int(ins.op_str, 16))
                ea, flags = nxt, {}
                continue
            if m == "clc":
                flags = {"CF": 0}
            elif m == "stc":
                flags = {"CF": 1}
            elif m in ("test", "and", "or", "xor"):
                flags = {"CF": 0, "OF": 0}
            elif m in _SHIFTS and ins.op_str.endswith(", 0"):
                pass
            elif not (m in _KEEP_FLAGS or m.startswith(("mov", "cmov", "set"))):
                flags = {}
            ea = nxt
        out.append(block)
    return out


_VOLATILE = {"rax", "rcx", "rdx", "r8", "r9", "r10", "r11"}


def this_events(fn):
    """A constructor's stores and calls on `this` (rcx at entry, followed through moves): ("vt", 0, value) for
    a `mov [this], reg` of a rip address, ("sub", off, callee) for `lea rcx, [this+off]; call callee`,
    ("base", 0, callee) for a call with rcx = this."""
    alias, regs, events, sub = {"rcx"}, {}, [], None
    for block in trace(fn):
        for ins in block:
            m, ops = ins.mnemonic, [o.strip() for o in ins.op_str.split(",")] if ins.op_str else []
            if m == "call":
                try:
                    target = int(ops[0], 16)
                except ValueError:
                    target = None
                if target and sub is not None:
                    events.append(("sub", sub, target))
                elif target and "rcx" in alias:
                    events.append(("base", 0, target))
                alias -= _VOLATILE
                for reg in _VOLATILE:
                    regs.pop(reg, None)
                sub = None
                continue
            if m == "lea" and len(ops) == 2:
                if ops[1].startswith("[rip + "):
                    regs[ops[0]] = rip_target(ins)
                    alias.discard(ops[0])
                else:
                    found = re.match(r"\[(\w+) \+ (0x[0-9a-f]+)\]$", ops[1])
                    if found and found.group(1) in alias and ops[0] == "rcx":
                        sub = int(found.group(2), 16)
                    elif ops[0] == "rcx":
                        sub = None
                    alias.discard(ops[0])
                    regs.pop(ops[0], None)
                continue
            if m == "mov" and len(ops) == 2:
                dst, src = ops
                if dst.startswith("qword ptr [") and dst.endswith("]"):
                    if dst[11:-1] in alias and src in regs:
                        events.append(("vt", 0, regs[src]))
                    continue
                if not dst.startswith(("qword", "dword", "word", "byte", "xmm")):
                    if src in alias:
                        alias.add(dst)
                    else:
                        alias.discard(dst)
                    if src in regs:
                        regs[dst] = regs[src]
                    else:
                        regs.pop(dst, None)
                    if dst == "rcx":
                        sub = None
                    continue
            if (
                ops
                and m not in ("cmp", "test", "push")
                and not ops[0].startswith(("qword", "dword", "word", "byte"))
            ):
                alias.discard(ops[0])
                regs.pop(ops[0], None)
                if ops[0] in ("rcx", "ecx"):
                    sub = None
    return events


def looks_like_state_vtable(vt):
    return all(TEXT[0] <= q(vt + 8 * k) < TEXT[1] for k in range(0x170 // 8))


def primary_vtable(construct):
    """The last vtable a construct function stores to the object (the most derived class's)."""
    stores = [v for kind, _, v in this_events(construct) if kind == "vt" and looks_like_state_vtable(v)]
    return stores[-1] if stores else None


def sub_object(construct, offset, depth=0):
    """(sub-object vtable, its STU type) of the property sub-object at `offset`."""
    events = this_events(construct)
    found = [callee for kind, off, callee in events if kind == "sub" and off == offset]
    if found:
        for vt in reversed([v for kind, _, v in this_events(found[-1]) if kind == "vt"]):
            target = stub_target(q(vt + 0x28)) if TEXT[0] <= q(vt + 0x28) < TEXT[1] else None
            if target and q(target) == TYPE_VT:
                return vt, target
        return None, None
    if depth < 4:
        for kind, _, callee in events:
            if kind == "base":
                result = sub_object(callee, offset, depth + 1)
                if result[1]:
                    return result
    return None, None


def stec(node_hash):
    """Evaluate the StEC predicate's compare tree for a node class hash."""
    ea, flags, al = STEC_PREDICATE, (False, False), 0
    for _ in range(600):
        ins = decode(ea)
        m, op, nxt = ins.mnemonic, ins.op_str, ea + ins.size
        if m == "mov" and op.startswith("ecx, dword ptr [rax + 0xb0]"):
            ea = nxt
        elif m == "cmp" and op.startswith("ecx, "):
            v = int(op.split(", ")[1], 16) & 0xFFFFFFFF
            flags, ea = (node_hash > v, node_hash == v), nxt
        elif m in ("ja", "je", "jne", "jb", "jae", "jbe", "jmp"):
            taken = {
                "ja": flags[0],
                "je": flags[1],
                "jne": not flags[1],
                "jb": not (flags[0] or flags[1]),
                "jae": flags[0] or flags[1],
                "jbe": not flags[0],
                "jmp": True,
            }[m]
            ea = int(op, 16) if taken else nxt
        elif m == "xor" and op == "al, al":
            al, ea = 0, nxt
        elif m == "mov" and op == "al, 1":
            al, ea = 1, nxt
        elif m == "add":
            ea = nxt
        elif m == "ret":
            return bool(al)
        else:
            raise RuntimeError(f"StEC predicate: unexpected {m} {op} at {ea:#x}")
    raise RuntimeError("StEC predicate: no return")


def field_layout(type_ea):
    """The block fields of an STU type: what the block reader reads for each."""
    out, problems = [], []
    array = q(type_ea + 0x28)
    for i in range(d(type_ea + 0x90)):
        entry = array + 56 * i
        offset, ftype, flags, flags2 = d(entry + 8), q(entry + 16), d(entry + 0x18), d(entry + 0x20)
        field = {"hash": f"{d(entry + 0x30):08X}", "offset": offset, "flags": flags}
        vt = q(ftype)
        if vt and q(vt + 0x30) == PRIM_GETTER and q(vt + 0x38) == KIND_GETTER:
            prim = d(ftype + 0x28)
            name, size = PRIMITIVES.get(prim, (f"prim {prim:08X}", None))
            field["type"] = name
            if flags & 8:
                field["sent"] = "never"
            elif size is None or (name == "u64" and flags2 & 2):
                field["sent"] = "unknown"
                problems.append(f"field {field['hash']}: type {name} not decoded")
            else:
                field["sent"] = "H only" if flags & 0x10 else ("full only" if flags & 0x20 else "always")
                field["bits"] = size
        else:
            field["type"] = f"type object {ftype:#x}"
            field["sent"] = "never" if flags & 8 else "unknown"
            if not flags & 8:
                problems.append(f"field {field['hash']}: not a primitive")
        out.append(field)
    return out, problems


def block_ops(fields):
    ops = []
    for field in fields:
        if field["sent"] in ("never", "unknown"):
            continue
        op = bits(BLOCK_FIELD_NAMES.get(field["hash"], f"m_{field['hash']}"), field["bits"])
        op["block"] = field["hash"]
        if field["sent"] == "H only":
            op = when("H", [op])
        elif field["sent"] == "full only":
            op = when("full", [op])
        ops.append(op)
    return ops


# --- names and usage (optional inputs) -------------------------------------------------------------------


def owlib_names():
    names = {}
    root = Path(os.environ.get("OWLIB_TYPES", ""))
    if not os.environ.get("OWLIB_TYPES") or not root.is_dir():
        return names
    for path in root.glob("*.cs"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\[STU\(0x([0-9A-Fa-f]{8})[^\]]*\)\]\s*public class (\w+)", text):
            names.setdefault(m.group(1).upper(), m.group(2))
    return names


def graph_usage(repo):
    """Per class hash: the graph data's class name and its state counts (all graphs, the 32 hero bodies'
    graphs, Soldier: 76's)."""
    path = repo / "data" / "game_graphs_174.json.gz"
    if not path.exists():
        return {}, {}
    data = json.loads(gzip.decompress(path.read_bytes()))
    graphs = {g["index"]: g for g in data["graphs"]}
    owl = {v: k for k, v in owlib_names().items()}

    def hash_of(cls):
        return cls[4:].upper() if cls.startswith("STU_") and len(cls) == 12 else owl.get(cls)

    def refs(item, found):
        if isinstance(item, str) and re.fullmatch(r"0x[0-9A-F]{16}", item) and int(item, 16) >> 48 == 0x0580:
            found.add(int(item, 16) & 0xFFFFFFFFFFFF)
        elif isinstance(item, dict):
            for part in item.values():
                refs(part, found)
        elif isinstance(item, list):
            for part in item:
                refs(part, found)

    def closure(roots):
        seen, todo = set(), [r for r in roots if r is not None]
        while todo:
            index = todo.pop()
            if index in seen or index not in graphs:
                continue
            seen.add(index)
            found = set()
            refs(graphs[index]["fields"], found)
            for node in graphs[index]["nodes"]:
                refs(node["fields"], found)
            todo.extend(found - seen)
        return seen

    heroes = {}
    for body in data["bodies"].values():
        roots = [item.get("graph") for item in body["graphs"]]
        roots += [body.get("manager"), *(body.get("weapons") or [])]
        heroes[body["name"]] = closure(roots)
    soldier = next(v for k, v in heroes.items() if k.startswith("Soldier"))
    union = set().union(*heroes.values())
    usage, names = {}, {}
    for index, g in graphs.items():
        states = {pos for pos in g["states"] if pos is not None}
        for node in g["nodes"]:
            if node["pos"] not in states:
                continue
            h = hash_of(node["class"])
            if h is None:
                continue
            names[h] = node["class"]
            net = not node["client_only"] and not node["server_only"]
            u = usage.setdefault(
                h, {"states": 0, "networked": 0, "hero_networked": 0, "soldier_networked": 0}
            )
            u["states"] += 1
            u["networked"] += net
            u["hero_networked"] += net and index in union
            u["soldier_networked"] += net and index in soldier
    return names, usage


# --- the table -------------------------------------------------------------------------------------------


def node_typeinfos():
    """node typeinfo -> STU type (descriptor), from node vtables: slot 1 returns the typeinfo, slot 5 the
    type."""
    stubs = {}
    for m in re.finditer(rb"\x48\x8D\x05(....)\xC3", _text, re.S):
        ea = TEXT[0] + m.start()
        stubs[ea] = ea + 7 + struct.unpack("<i", m.group(1))[0]
    found = {}
    for off in range(0, len(_data) - 0x30, 8):
        s1 = struct.unpack_from("<Q", _data, off + 8)[0]
        if s1 not in stubs:
            continue
        s5 = struct.unpack_from("<Q", _data, off + 0x28)[0]
        if s5 in stubs:
            ti, desc = stubs[s1], stubs[s5]
            if q(desc) == TYPE_VT and q(ti) == ti:
                found.setdefault(ti, desc)
    return found


def typeinfo_chain(ti):
    out = []
    while ti and ti not in out and len(out) < 20:
        out.append(ti)
        parent = q(ti + 8)
        ti = parent if parent != ti else 0
    return out


def registration_records(typeinfos):
    records, node = [], q(LIST_HEAD)
    while node and len(records) < 5000:
        rec = q(node + 8)
        ti = q(rec + 0x18)
        records.append(
            {
                "index": len(records),
                "record": rec,
                "construct": q(rec + 8),
                "destroy": q(rec + 0x10),
                "typeinfo": ti,
                "priority": d(rec + 0x20),
                "hash": f"{d(typeinfos[ti] + 0xB0):08X}" if ti in typeinfos else None,
                "state": STATE_TYPEINFO in typeinfo_chain(ti),
            }
        )
        node = q(node)
    return records


def class_entry(h, c, records, winners, usage, codecs):
    """One class of the table from its winning record: the block's fields and the reader's layout."""
    r = records[winners[c["via"]]]
    entry = {
        "winner": {
            "record": r["index"],
            "via": "own" if c["via"] == c["typeinfo"] else f"parent typeinfo {c['via']:#x}",
            "priority": r["priority"],
            "construct": f"{r['construct']:#x}",
            "vtable": f"{r['vtable']:#x}",
        },
        "reader": f"{r['reader']:#x}",
    }
    reader = READERS.get(r["reader"])
    if reader is None:
        entry.update({"codec": None, "layout": None, "status": "UNVERIFIED", "evidence": "reader not read"})
        return entry, [f"{h}: reader {r['reader']:#x} has no layout"]
    codec, reader_ops, status, why = reader
    evidence, problems, ops = [f"reader {r['reader']:#x}: {why}"], [], []
    if r["block"] != NULLSUB:
        stype = r.get("block_type")
        fields, bad = field_layout(stype) if stype else ([], ["no type"])
        entry["block"] = {
            "function": f"{r['block']:#x}",
            "offset": r["block_offset"],
            "type": f"{stype:#x}" if stype else None,
            "type_hash": f"{d(stype + 0xB0):08X}" if stype else None,
            "fields": fields,
        }
        ops = block_ops(fields)
        sent = [f for f in fields if f["sent"] != "never"]
        listed = ", ".join(f"{f['hash']} {f['type']}" for f in sent) or "none (all flag 8)"
        evidence.insert(
            0,
            f"block {r['block']:#x}: sub-object +{r['block_offset']:#x} of type {entry['block']['type']}, "
            f"fields sent: {listed}",
        )
        if bad:
            status = "UNVERIFIED"
            evidence.append("block: " + "; ".join(bad))
        if ops:
            types = "+".join(f["type"] for f in fields if f["sent"] not in ("never", "unknown"))
            codec = BLOCK_CODECS.get(h, f"{codec}+{types}")
    layout = None if reader_ops is None else ops + reader_ops
    entry.update({"codec": codec, "layout": layout, "status": status if layout is not None else "UNVERIFIED"})
    entry["evidence"] = "; ".join(evidence)
    codecs[codec] = sorted({*codecs.get(codec, []), f"{r['reader']:#x}"})
    if h in usage:
        entry["usage"] = usage[h]
    return entry, problems


def build(repo):
    typeinfos = node_typeinfos()
    records = registration_records(typeinfos)
    winners = {}
    for item in records:  # the builder's rule
        held = winners.get(item["typeinfo"])
        if held is None or item["priority"] >= records[held]["priority"]:
            winners[item["typeinfo"]] = item["index"]
    names_data, usage = graph_usage(repo)
    owl = owlib_names()
    state_records = [r for r in records if r["state"]]
    problems = []
    for r in state_records:
        r["winner"] = winners[r["typeinfo"]] == r["index"]
        vt = primary_vtable(r["construct"])
        if vt is None:
            problems.append(f"record {r['index']} ({r['hash']}): no vtable")
            continue
        r["vtable"], r["reader"], r["block"], r["writer"] = vt, q(vt + 0x158), q(vt + 0x168), q(vt + 0x150)
        if r["block"] != NULLSUB:
            offset = BLOCK_OFFSETS.get(r["block"])
            subvt, stype = sub_object(r["construct"], offset) if offset is not None else (None, None)
            r["block_offset"], r["block_vtable"], r["block_type"] = offset, subvt, stype
            if stype is None:
                problems.append(f"record {r['index']} ({r['hash']}): block {r['block']:#x} type not found")
    classes = {}
    for ti, desc in typeinfos.items():
        chain = typeinfo_chain(ti)
        if STATE_TYPEINFO not in chain:
            continue
        via = next((t for t in chain if t in winners), None)
        own = [r["index"] for r in state_records if r["typeinfo"] == ti]
        classes[f"{d(desc + 0xB0):08X}"] = {"typeinfo": ti, "descriptor": desc, "records": own, "via": via}
    table, codecs = {}, {}
    for h, c in sorted(classes.items()):
        entry = {"name": names_data.get(h) or owl.get(h) or f"STU_{h}", "stec": stec(int(h, 16))}
        entry["records"] = c["records"]
        if c["via"] is None:
            entry.update({"winner": None, "codec": "none", "layout": [], "status": "VERIFIED"})
            entry["evidence"] = "no registration on its typeinfo chain: the client builds a plain State"
        else:
            found, bad = class_entry(h, c, records, winners, usage, codecs)
            entry.update(found)
            problems += bad
        if h in usage:
            entry.setdefault("usage", usage[h])
        if h in DUMP_CLASSES:
            entry["data"] = "read the table's way in " + ", ".join(DUMPS[key] for key in DUMP_CLASSES[h])
        table[h] = entry
    out_records = [
        {k: (f"{v:#x}" if isinstance(v, int) and k in HEX_KEYS else v) for k, v in r.items() if k != "state"}
        for r in state_records
    ]
    return {
        "build": 104319,
        "image_base": f"{BASE:#x}",
        "generator": "tools/extract_statescript_codecs.py (IDA, the client's runtime image)",
        "rule": "registration list 0x7FF78C1A69D8 walked from the head by 0x7FF78AA9FE60; a record "
        "replaces the entry when its priority is >= the entry's (0x7FF78AAA007D), so the highest priority "
        "wins and on a tie the record nearer the list's end; lookup 0x7FF78AAA02B0 by node typeinfo, then "
        "its parents; none: a plain State (0x7FF78AAA1460)",
        "payload": "property block (state vt+0x168, 0x7FF78AA81930: fields in order, no presence bits; "
        "skipped when flags & 8, flags & 0x10 without H, flags & 0x20 without full) then the class reader "
        "(state vt+0x158), called by 0x7FF78AA82060 as (state, reader, H, full); full = a full frame "
        "(deltas pass 0, 0x7FF78991C94F; full lists 1, 0x7FF78991C07C)",
        "stec": "StEC node classes (0x7FF789E6E960) carry X then StAc and the payload; the others A and the "
        "payload when A = 1 (0x7FF78991C620)",
        "ops": "bits(n, LSB first) f32 w_u16 var_b signed_var entity_id value link; if c: H, !H, full, "
        "H&full, field, !field, valid:field; repeat count field or number",
        "records": out_records,
        "codecs": codecs,
        "classes": table,
        "problems": problems,
        "counts": dict(Counter(e["status"] for e in table.values())),
    }


def main():
    script = Path(globals().get("__file__") or "tools/extract_statescript_codecs.py")
    repo = script.resolve().parents[1]
    table = build(repo)
    out = repo / "data" / "statescript_codecs_174.json"
    out.write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
    counts = f"{len(table['records'])} state records, {len(table['classes'])} state classes"
    print(f"{out}: {counts}, {table['counts']}, problems {len(table['problems'])}")
    for line in table["problems"]:
        print("  ", line)


if __name__ == "__main__":
    main()
