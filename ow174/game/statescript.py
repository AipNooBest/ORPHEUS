"""Statescript on ch1: frames that put graph instances on an entity and set their states and
variables. The client runs the graphs; the server says which instances exist and what is on.

Read in the client (the chunk reader 0x7FF7898A6450, the frame 0x7FF78991E3C0):
- A chunk is `w_u32 first | w_var span | bit fragmented | w_var size in bits | payload`. first 0 is a
  full frame. The client keeps the highest `last` (first + span) it applied and drops chunks at or
  below it, so an unchanged chunk can be sent again safely.
- The payload starts `bit lead | bit H`; with H (an owner frame): `bit C | w_var CmFD`, and in a delta
  also `w_var LBSS` (the body's length). An owner frame is skipped unless its CmFD is larger than the
  last one (unsigned, so 0 never applies).
- A full frame lists the instances (w_u16 index steps, each with a descriptor: 16 bits graph, bit other
  entity, bit flag, bit parent -> w_var parent instance and w_var parent STATE index, bit references),
  then the entity's variables, then per instance its variables, one bit per state, and in an owner
  frame its event list; then the frame's event list. It destroys the network instances it does not
  list and first clears the variables the network set, so every full frame carries everything.
- In an owner frame the states are the graph's m_states minus the client-only and server-only ones.
  A SubScript state that is on carries its child's instance id (w_u16) and a bit.
- Instances the server makes never run their own Entry nodes (only client-only ones run), so the
  server sends what those would have done: the states that are on and the variables.
"""

import struct
from dataclasses import dataclass, field

from ow174.game.bits import BitWriter
from ow174.game.script import codecs
from ow174.game.world import EntityUpdate


@dataclass(frozen=True)
class Graph:
    index: int  # the 01B graph's 16-bit index
    owner_states: int  # state bits in an owner frame
    sync_vars: int  # per-instance variable presence bits


# From the graph data (the counts were tested live).
CONTROLLER = Graph(0x13C1, 43, 12)  # the player's controller in the Practice Range mode
PVP_CONTROLLER = Graph(0x0C90, 118, 29)  # the controller of both teams in the PvP modes
HUD = Graph(0x20E4, 1, 0)  # 13C1's weapon and ability HUD (0C90 starts its own)
HERO_SELECT_HOST = Graph(0x288A, 6, 0)
HERO_SELECT = Graph(0x288B, 30, 4)  # presents the hero select screen 008C.05A
TEAM_ENTRY = Graph(0x288D, 0, 0)  # client-only: posts its entity to the local player's team list


class Value:
    tag: int

    def write(self, out: BitWriter) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class Bool(Value):
    value: bool

    def write(self, out: BitWriter) -> None:
        out.bits(0, 4)
        out.bit(self.value)


@dataclass(frozen=True)
class Int(Value):
    value: int

    def write(self, out: BitWriter) -> None:
        out.bits(4, 4)
        out.bits(self.value & 0xFFFFFFFF, 32)


@dataclass(frozen=True)
class Float(Value):
    value: float

    def write(self, out: BitWriter) -> None:
        out.bits(8, 4)
        out.bits(struct.unpack("<I", struct.pack("<f", self.value))[0], 32)


@dataclass(frozen=True)
class Asset(Value):
    """A GUID read as an asset (a hero, for example): its type bits and its low 32 bits."""

    guid: int

    def write(self, out: BitWriter) -> None:
        out.bits(9, 4)
        out.bit(1)
        out.bits(self.guid >> 48, 16)
        if self.guid >> 48:
            out.w_var(self.guid & 0xFFFFFFFF)


@dataclass(frozen=True)
class Entity(Value):
    entity: int

    def write(self, out: BitWriter) -> None:
        out.bits(13, 4)
        out.bit(1)
        out.bits(self.entity & 0xFFFFFFFF, 32)


def write_variable(out: BitWriter, value: Value) -> None:
    value.write(out)
    out.bit(0)  # no bindings


def subscript(child: int) -> BitWriter:
    """The payload of a SubScript state that is on: its child instance."""
    out = BitWriter()
    out.w_u16(child)
    out.bit(0)
    return out


@dataclass
class Instance:
    index: int
    graph: Graph
    parent: tuple[int, int] | None = None  # (parent instance, the parent graph's state index)
    presence: dict[int, Value] = field(default_factory=dict)  # presence bit -> value
    extra: dict[int, Value] = field(default_factory=dict)  # variable id -> value
    active: dict[int, BitWriter | None] = field(default_factory=dict)  # owner state bit -> payload


def _descriptor(out: BitWriter, instance: Instance) -> None:
    out.bits(instance.graph.index, 16)
    out.bit(0)  # the instance lives on this entity
    out.bit(0)  # flag
    if instance.parent is None:
        out.bit(0)
    else:
        out.bit(1)
        out.w_var(instance.parent[0])
        out.w_var(instance.parent[1])
    out.bit(0)  # no references


def _variables(out: BitWriter, variables: dict[int, Value]) -> None:
    for number, value in variables.items():
        out.w_var(number)
        write_variable(out, value)
    out.w_var(0)


def owner_full_frame(cmfd: int, instances: list[Instance], entity_vars: dict[int, Value]) -> BitWriter:
    """An owner (H=1) full frame. `cmfd` must be larger than any CmFD sent to this entity before."""
    if cmfd < 1:
        raise ValueError("CmFD 0 is always stale")
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(1)  # H
    out.bit(0)  # C
    out.w_var(cmfd)
    last = 0
    for instance in sorted(instances, key=lambda item: item.index):
        out.w_u16(instance.index - last)
        last = instance.index
        _descriptor(out, instance)
    out.w_u16(0)
    if entity_vars:
        out.bit(1)
        _variables(out, entity_vars)
    else:
        out.bit(0)
    for instance in sorted(instances, key=lambda item: item.index):
        for bit in range(instance.graph.sync_vars):
            value = instance.presence.get(bit)
            out.bit(value is not None)
            if value is not None:
                write_variable(out, value)
        if instance.extra:
            out.bit(1)
            _variables(out, instance.extra)
        else:
            out.bit(0)
        for bit in range(instance.graph.owner_states):
            on = bit in instance.active
            out.bit(on)
            payload = instance.active.get(bit)
            if on and payload is not None:
                out.append(payload)
        out.bits(0b11, 2)  # the instance's event list ends (code 3)
    out.bits(0, 2)  # the frame's event lists: none
    return out


def variables_frame(entity_vars: dict[int, Value]) -> BitWriter:
    """A plain (H=0) full frame with no instances, only entity variables: how an entity without
    graphs of its own, such as the game mode entity, gets values that other graphs read from it."""
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(0)  # H
    out.w_u16(0)  # no instances
    if entity_vars:
        out.bit(1)
        _variables(out, entity_vars)
    else:
        out.bit(0)
    out.bits(0, 2)  # the frame's event lists: none
    return out


def owner_ack(cmfd: int) -> BitWriter:
    """An owner delta that only moves CmFD on: the client then replays fewer of its own commands when it
    rolls its prediction back."""
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(1)  # H
    out.bit(0)  # C
    out.w_var(cmfd)
    out.w_var(7)  # LBSS: the body below, up to the "more" bit
    out.w_u16(0)  # no instances
    out.bit(0)  # no entity variables
    out.bit(0)  # no instance events
    out.bit(0)  # no more sub-frames
    out.bits(0, 2)  # no frame events
    return out


def chunk(payload: BitWriter, first: int, span: int) -> BitWriter:
    out = BitWriter()
    out.w_u32(first)
    out.w_var(span)
    out.bit(0)  # not fragmented
    out.w_var(payload.count)
    out.append(payload)
    return out


class Stream:
    """One entity's statescript as one client gets it: the frame numbers of the chunks sent and
    delivered, and the last CmFD. The client applies a chunk only when its last frame is new to it and
    drops a queued chunk whose range a newer chunk covers, so every chunk gets the next number and a
    chunk with data must arrive before an ack-only delta may cover it."""

    def __init__(self, entity: int) -> None:
        self.entity = entity
        self.last = 0  # the last frame number sent
        self.delivered = 0  # the highest last frame the client has
        self.data_last = 0  # the last chunk that carries data, not only an ack
        self.cmfd = 0
        self.next_ack = 0.0  # when the next ack-only delta may go

    def next_cmfd(self, newest: int) -> int:
        """The client skips an owner frame whose CmFD is not larger than the last one."""
        self.cmfd = max(newest, self.cmfd + 1)
        return self.cmfd

    @property
    def data_in_flight(self) -> bool:
        return self.delivered < self.data_last

    def full_frame(self, frame: BitWriter) -> EntityUpdate:
        """A full frame; sent again unchanged when its datagram is lost."""
        self.last += 1
        self.data_last = self.last
        return self._update(chunk(frame, 0, self.last), resend=True)

    def ack_delta(self, frame: BitWriter) -> EntityUpdate:
        """A delta that only moves CmFD on. A later one covers it, so a lost one is not sent again."""
        first = self.delivered + 1
        self.last += 1
        return self._update(chunk(frame, first, self.last - first), resend=False)

    def arrived(self, last: int) -> None:
        self.delivered = max(self.delivered, last)

    def _update(self, data: BitWriter, resend: bool) -> EntityUpdate:
        update = EntityUpdate(self.entity, chunk=data)
        update.chunk_last = self.last
        update.stream = self
        update.resend = resend
        return update


# --- Owner frames of a body the server runs (ow174/game/script) ----------------------------------
#
# Read in the client:
# - A delta (first != 0) lists only what changed: instances (w_u16 index steps; a bit "gone", else a bit
#   "full descriptor follows"), the entity variables, and per listed instance a mask over its m_syncVars
#   entries plus an id list (0x7FF78991CA20), then a mask over all its m_states (0x7FF78991C620): a
#   LogicalButton / WeaponVolley / Effect state gets X then StAc and its payload, every other state A and
#   its payload when A = 1. Then, in an owner frame, the event lists (EIns), "more" and the frame's events.
# - Masks (0x7FF789C630E0): up to 8 entries as plain bits, else one bit per group of 8 entries and the
#   groups that have a bit set.
# - A variable is a tagged value and its bindings (0x7FF78991D4F0): the states whose output the variable
#   takes while they are active (the client's Stack entries), each with its instance, state index (the
#   bound instance's m_statesBitCount bits), output slot and its priority.

# What the client reads for each state class comes from its codec table (ow174/game/script/codecs.py,
# data/statescript_codecs_174.json, read in the client code): whether it is an StEC class and its codec. The
# writers below cover these codecs; tests/test_statescript_codecs.py reads every writer back with the table.
STEC_CLASSES = codecs.stec_classes()
WRITERS = {
    "none", "switch", "stack", "anim", "chase", "button", "ability", "targets", "volley", "subscript",
    "message", "send", "link", "hits", "flags13", "frames", "counter2", "pulser",
}  # fmt: skip


def family(cls: str) -> str:
    """The payload writer of a state class: its codec in the client's table, "unknown" when we have no writer
    for it (or the table has no verified layout)."""
    item = codecs.codec(cls)
    if item is None or not item.verified or item.codec not in WRITERS:
        return "unknown"
    return item.codec


def sendable(cls: str, active: bool) -> bool:
    """Whether a state of this class can go into an owner frame: one whose payload we cannot write only when
    it is off and not an StEC class (an StEC state carries its payload when it is off too)."""
    return family(cls) != "unknown" or (not active and cls not in STEC_CLASSES)


def signed_u16(out: BitWriter, value: int) -> None:
    """sign(1) then w_u16 (0x7FF789C63290): instance ids and instance steps."""
    out.bit(value < 0)
    out.w_u16(abs(value))


def signed_var(out: BitWriter, value: int) -> None:
    """sign(1) then w_var (0x7FF78AA836A0): the volley's times."""
    out.bit(value < 0)
    out.w_var(abs(value))


def count(out: BitWriter, value: int) -> None:
    """0 / 1 0 / 1 1 0 / 1 1 1 + w_u16 (0x7FF789C63400)."""
    if value == 0:
        out.bit(0)
    elif value == 1:
        out.bits(0b01, 2)
    elif value == 2:
        out.bits(0b011, 3)
    else:
        out.bits(0b111, 3)
        out.w_u16(value)


def mask(out: BitWriter, size: int, chosen: set[int]) -> None:
    """A mask over `size` entries (0x7FF789C630E0)."""
    if size <= 8:
        for index in range(size):
            out.bit(index in chosen)
        return
    groups = ((size - 1) >> 3) + 1
    present = [any(index in chosen for index in range(8 * g, min(size, 8 * g + 8))) for g in range(groups)]
    for flag in present:
        out.bit(flag)
    for g in range(groups):
        if present[g]:
            for index in range(8 * g, min(size, 8 * g + 8)):
                out.bit(index in chosen)


def _f32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", float(value)))[0]


def write_value(out: BitWriter, value) -> None:
    """A tagged value (0x7FF789C67DE0), in the smallest tag that keeps its type."""
    if value is None:
        out.bits(15, 4)
        out.bits(3, 3)
    elif isinstance(value, bool):
        out.bits(0, 4)
        out.bit(value)
    elif isinstance(value, int):
        _write_int(out, value)
    elif isinstance(value, float):
        if value.is_integer() and 0 <= value < 16:
            out.bits(5, 4)
            out.bits(int(value), 4)
        elif value.is_integer() and -128 <= value < 128:
            out.bits(6, 4)
            out.signed(int(value), 8)
        else:
            out.bits(8, 4)
            out.bits(_f32_bits(value), 32)
    elif isinstance(value, tuple):
        out.bits(15, 4)
        out.bits(6, 3)  # an array, its count only
        out.w_u16(len(value))
        out.bit(0)  # not keyed
        if value:
            out.bit(0)  # every element tagged
            for item in value:
                write_value(out, item)
    elif isinstance(value, str):
        out.bits(15, 4)
        out.bits(2, 3)
        for byte in value.encode("utf-8") + b"\0":
            out.bits(byte, 8)
    else:
        _write_object(out, value)


def _write_int(out: BitWriter, value: int) -> None:
    if 0 <= value < 16:
        out.bits(1, 4)
        out.bits(value, 4)
    elif -128 <= value < 128:
        out.bits(2, 4)
        out.signed(value, 8)
    elif -32768 <= value < 32768:
        out.bits(3, 4)
        out.signed(value, 16)
    else:
        out.bits(4, 4)
        out.signed(value, 32)


def _write_object(out: BitWriter, value) -> None:
    from ow174.game.script import expr

    if isinstance(value, expr.Asset):
        Asset(value.guid).write(out)
    elif isinstance(value, expr.Entity):
        out.bits(13, 4)
        out.bit(value.id != 0)
        if value.id:
            out.bits(value.id & 0xFFFFFFFF, 32)
    elif isinstance(value, expr.Handle):
        out.bits(14, 4)
        out.bits(value.kind & 7, 3)
        out.w_var(value.instance)
        out.w_u16(value.state)
        out.w_u16(value.extra)
    elif isinstance(value, expr.Vec3):
        out.bits(12, 4)
        parts = (value.x, value.y, value.z)
        for part in parts:
            out.bit(part != 0.0)
        for part in parts:
            if part != 0.0:
                out.bits(_f32_bits(part), 32)
    else:
        raise ValueError(f"cannot send the value {value!r}")


@dataclass(frozen=True)
class Binding:
    """A state whose output a variable takes while it is active: a Stack entry."""

    instance: int
    state: int  # m_states index
    slot: int = 0
    priority: float = 0.0
    above: bool = True


def write_bindings(out: BitWriter, bindings, own: int, graphs: dict) -> None:
    """A variable's binding list. A state of the variable's own instance takes the short form (bit 0: the
    client takes the instance from the variable's bag, 0x7FF78991D6DB); any other names its instance."""
    count(out, len(bindings))
    for item in bindings:
        count(out, 0)  # no path
        if item.instance == own:
            out.bit(0)
        else:
            out.bit(1)
            signed_u16(out, item.instance)
        out.bits(item.state, graphs[item.instance].states_bits)
        if item.slot:
            out.bit(1)
            out.w_var(item.slot)
        else:
            out.bit(0)
        if item.priority == 0.0 and item.above:
            out.bit(0)  # weight 0, above
        else:
            out.bit(1)
            if float(item.priority).is_integer():
                out.bit(1)
                out.bit(item.priority < 0)
                out.w_var(int(abs(item.priority)))
            else:
                out.bit(0)
                out.bits(_f32_bits(item.priority), 32)
            out.bit(item.above)
        out.bit(0)  # flag A: no re-resolve every frame


def write_var(out: BitWriter, value, bindings, own: int, graphs: dict) -> None:
    write_value(out, value)
    write_bindings(out, bindings, own, graphs)


def write_payload(out: BitWriter, cls: str, payload: dict, frame_time: int, frame_ms: int) -> None:
    """A state's payload in an owner frame (H = 1, a delta). `frame_time` is 16 * CmFD: the client takes
    the volley's start and the chase's and ability's last tick relative to it."""
    kind = family(cls)
    if kind == "switch":
        out.bit(payload.get("current", False))
    elif kind == "stack":
        out.bit(payload.get("top", False))
        out.bit(payload.get("under", False))
    elif kind == "button":
        out.bits(payload.get("counter", 0) & 15, 4)
    elif kind == "volley":
        _write_volley(out, payload, frame_time)
    elif kind == "anim":
        out.bit(0)  # no linked state
        out.bits(payload.get("counter", 0) & 7, 3)
        out.bit(0)  # no animation time
    elif kind == "chase":
        _write_chase(out, payload, frame_time + frame_ms)
    elif kind == "ability":
        _write_ability(out, payload, frame_time + frame_ms)
    elif kind == "targets":
        out.bit(1)  # an empty target list
        out.w_u16(0)
    elif kind == "subscript":
        out.w_u16(payload.get("child", 0))
        out.bit(0)
    elif kind == "message":
        out.bit(0)  # no sender
        out.bit(payload.get("stacked", False))
    elif kind in ("send", "link"):
        out.bit(0)
    elif kind == "hits":
        out.w_u16(0)  # no hits: the server does not detect them
        out.bit(payload.get("flag", False))
    elif kind == "flags13":
        value = payload.get("value", 0)
        out.bits(value & 63, 6)
        out.bit(value >> 6 != 0)
        if value >> 6:
            out.bits(value >> 6 & 127, 7)
    elif kind == "frames":
        out.bits(payload.get("target", 0) & 0xFFFFFFFF, 32)
    elif kind == "counter2":  # Effect: the activation counter & 3 (0x7FF789894540)
        out.bits(payload.get("counter", 0) & 3, 2)
    elif kind == "pulser":  # ClientOnlyPulser (0x7FF78990D760)
        out.bit(payload.get("fresh", False))
        out.bits(payload.get("count", 0) & 0xFF, 8)
    elif kind == "unknown":
        raise ValueError(f"no payload writer for {cls}")


def _write_volley(out: BitWriter, payload: dict, frame_time: int) -> None:
    """t1 = 16 * CmFD - start (the client sets start = 16 * CmFD - t1), then the time offset and re-volley
    index (b), the volley counter (c, 1 when not sent) and the activation counter (q, 6 bits)."""
    signed_var(out, frame_time - payload.get("start", frame_time))
    offset = payload.get("offset", 0)
    if offset:
        out.bit(1)
        signed_var(out, offset)
        out.w_u16(payload.get("subindex", 0))
    else:
        out.bit(0)
    volleys = payload.get("volleys", 1) & 0xFF
    if volleys == 1:
        out.bit(0)
    else:
        out.bit(1)
        out.w_u16(volleys)
    out.bits(payload.get("counter", 0) & 63, 6)


def _write_chase(out: BitWriter, payload: dict, next_frame_time: int) -> None:
    cur = payload.get("cur", 0.0)
    vector = hasattr(cur, "x")
    remaining = payload.get("remaining")
    out.bit(vector)
    out.bit(remaining is not None)
    out.bit(payload.get("reached", False))
    for part in (cur.x, cur.y, cur.z) if vector else (cur,):
        out.bits(_f32_bits(part), 32)
    if remaining is not None:
        if remaining >= 1 << 16:
            out.bit(1)
            out.bits(remaining & 0xFFFFFFFF, 32)
        else:
            out.bit(0)
            out.bits(max(0, remaining), 16)
    _write_last(out, payload.get("last"), next_frame_time)


def _write_ability(out: BitWriter, payload: dict, next_frame_time: int) -> None:
    out.bits(payload.get("flags", 0) & 0xFF, 8)
    out.bit(0)  # no linked state
    cur = payload.get("cur", 0.0)
    if cur <= 0:
        out.bit(0)
        return
    out.bit(1)
    out.bits(_f32_bits(cur), 32)
    rate = payload.get("rate", 1.0)
    out.bit(rate != 1.0)
    if rate != 1.0:
        out.bits(_f32_bits(rate), 32)
    out.var_b(max(0, next_frame_time - payload.get("last", next_frame_time)))


def _write_last(out: BitWriter, last: int | None, next_frame_time: int) -> None:
    """A last tick as t, with lastTick = 16 * (CmFD + 1) - t on the client (0x7FF78AAA8400)."""
    if last is None or next_frame_time - last < 0:
        out.bit(0)
        return
    out.bit(1)
    out.var_b(next_frame_time - last)


@dataclass
class StateWire:
    cls: str
    active: bool
    payload: dict = field(default_factory=dict)


@dataclass
class EventWire:
    time: int  # absolute ms
    state: int  # m_states index
    finish: bool = False  # a finish request, else a timer
    param: int = 1


@dataclass
class InstanceWire:
    """One instance in a body frame. In a delta only what changed is set; `events` None leaves the client's
    queue of that instance alone, a list replaces its networked events."""

    id: int
    graph: object  # ow174.game.script.graph.Graph
    parent: tuple[int, int] | None = None
    descriptor: bool = False
    gone: bool = False
    vars: dict = field(default_factory=dict)  # var id -> (value, bindings)
    states: dict[int, StateWire] = field(default_factory=dict)
    events: list[EventWire] | None = None

    @property
    def listed(self) -> bool:
        return self.gone or self.descriptor or bool(self.vars) or bool(self.states)


def _descriptor_of(out: BitWriter, item: InstanceWire) -> None:
    out.bits(item.graph.index, 16)
    out.bit(0)  # this entity
    out.bit(0)  # flag
    if item.parent is None:
        out.bit(0)
    else:
        out.bit(1)
        out.w_var(item.parent[0])
        out.w_var(item.parent[1])
    out.bit(0)  # no references


def _entity_vars(out: BitWriter, variables: dict, graphs: dict) -> None:
    if not variables:
        out.bit(0)
        return
    out.bit(1)
    for var in sorted(variables):
        value, bindings = variables[var]
        out.w_var(var)
        write_var(out, value, bindings, 0, graphs)
    out.w_var(0)


def _sync_slots(item: InstanceWire) -> dict[int, int]:
    """m_syncVars entry -> var id for the instance variables this frame sends through the list."""
    slots: dict[int, int] = {}
    for entry in item.graph.sync_vars:
        wanted = entry.var is not None and entry.scope == 0 and entry.var in item.vars
        if wanted and entry.var not in slots.values():
            slots[entry.index] = entry.var
    return slots


def _instance_vars(out: BitWriter, item: InstanceWire, graphs: dict, presence_only: bool) -> None:
    """The per-instance variables: the m_syncVars entries (a presence bit per instance entry with an id in a
    full frame, a mask over the whole list in a delta), then the other ids."""
    slots = _sync_slots(item)
    if presence_only:
        for entry in item.graph.presence_vars():
            present = slots.get(entry.index) == entry.var
            out.bit(present)
            if present:
                write_var(out, *item.vars[entry.var], item.id, graphs)
    else:
        mask(out, len(item.graph.sync_vars), set(slots))
        for index in sorted(slots):
            write_var(out, *item.vars[slots[index]], item.id, graphs)
    extra = sorted(var for var in item.vars if var not in slots.values())
    if not extra:
        out.bit(0)
        return
    out.bit(1)
    for var in extra:
        out.w_var(var)
        write_var(out, *item.vars[var], item.id, graphs)
    out.w_var(0)


def _instance_states(out: BitWriter, item: InstanceWire, frame_time: int, frame_ms: int) -> None:
    nodes = item.graph.states
    mask(out, len(nodes), set(item.states))
    for index in sorted(item.states):
        state = item.states[index]
        node = nodes[index]
        if node is None or node.client_only or node.server_only:
            raise ValueError(f"instance {item.id} st{index} is not a networked state")
        if state.cls in STEC_CLASSES:
            out.bit(1)  # X
            out.bit(state.active)
            write_payload(out, state.cls, state.payload, frame_time, frame_ms)
        else:
            out.bit(state.active)
            if state.active:
                write_payload(out, state.cls, state.payload, frame_time, frame_ms)


def write_events(out: BitWriter, events: list[EventWire], frame_time: int, states_bits: int) -> None:
    """One instance's owner event list (0x7FF78991BA30): 2-bit codes, times from 16 * CmFD on, each the
    step from the one before."""
    last = frame_time
    for event in sorted(events, key=lambda item: item.time):
        if event.finish:
            out.bits(2, 2)
        elif event.param == 1:
            out.bits(0, 2)
        else:
            out.bits(1, 2)
            out.bit(event.param == 0)
            if event.param:
                out.w_u16(event.param)
        out.bits(event.state, states_bits)
        delta = event.time - last
        out.bit(delta == 0)
        if delta:
            out.bit(delta > 0)
            out.w_var(abs(delta))
        last = event.time
    out.bits(3, 2)


def owner_delta(
    cmfd: int,
    correction: bool,
    instances: list[InstanceWire],
    entity_vars: dict,
    graphs: dict,
    frame_ms: int = 16,
) -> BitWriter:
    """An owner (H = 1) delta frame of a body. `graphs` maps every instance id of the entity to its graph
    (a binding names the bound instance's state with that graph's width)."""
    if cmfd < 1:
        raise ValueError("CmFD 0 is always stale")
    frame_time = cmfd * frame_ms
    body = BitWriter()
    ordered = sorted(instances, key=lambda item: item.id)
    last = 0
    for item in ordered:
        if not item.listed:
            continue
        body.w_u16(item.id - last)
        last = item.id
        body.bit(item.gone)
        if not item.gone:
            body.bit(item.descriptor)
            if item.descriptor:
                _descriptor_of(body, item)
    body.w_u16(0)
    _entity_vars(body, entity_vars, graphs)
    for item in ordered:
        if item.listed and not item.gone:
            _instance_vars(body, item, graphs, presence_only=False)
            _instance_states(body, item, frame_time, frame_ms)
    lists = [item for item in ordered if item.events is not None and not item.gone]
    body.bit(bool(lists))
    if lists:
        last = 0
        for item in lists:
            signed_u16(body, item.id - last)
            last = item.id
            write_events(body, item.events, frame_time, item.graph.states_bits)
        signed_u16(body, 0)
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(1)  # H
    out.bit(correction)
    out.w_var(cmfd)
    out.w_var(body.count)  # LBSS: the body up to the "more" bit
    out.append(body)
    out.bit(0)  # no more sub-frames
    out.bits(0, 2)  # no frame events
    return out


def body_full_frame(instances: list[InstanceWire], entity_vars: dict, graphs: dict) -> BitWriter:
    """A plain (H = 0) full frame of a body: every instance with its descriptor and variables, and every
    remote-synced state off (X = 0). The client rebinds the instances it built itself (same index, graph,
    no parent), makes the others, and destroys the network instances the frame does not list."""
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(0)  # H
    last = 0
    ordered = sorted(instances, key=lambda item: item.id)
    for item in ordered:
        out.w_u16(item.id - last)
        last = item.id
        _descriptor_of(out, item)
    out.w_u16(0)
    _entity_vars(out, entity_vars, graphs)
    for item in ordered:
        _instance_vars(out, item, graphs, presence_only=True)
        for _ in item.graph.remote_states():
            out.bit(0)  # X = 0: off
    out.bits(0, 2)  # no frame events
    return out


# --- Plain (H = 0) frames with states: what every other client gets of an entity (observers.py, the Biotic
# Field entity in projectiles.py) ------------------------------------------------------------------------
#
# Read in the client (0x7FF78991BE30 / 0x7FF78991C620): an H = 0 frame
# walks the graph's m_remoteSyncNodes. In a full frame each state that costs bits (a networked state) has X:
# 0 aborts it, 1 then A (an StEC class: Y, then StAc) and, when on, its payload with H = 0 and full = 1. A
# delta has a mask over the whole raw list and per set bit A (StEC: X, then StAc) and the payload with full
# = 0. After a full frame the client aborts every active state of the instance that is neither remote-synced
# nor client-only. Payloads: the codec table's layouts with H = 0 (codecs.write).


def remote_fields(cls: str, payload: dict) -> dict:
    """A state's payload (the VM's values, as write_payload takes them) as the codec layout's fields of an
    H = 0 frame. Times are relative to a CmFD the frame does not have, so none is sent (unverified)."""
    kind = family(cls)
    get = payload.get
    if kind == "volley":
        volleys = get("volleys", 1) & 0xFF
        return {"has_volleys": volleys != 1, "volleys": volleys, "counter": get("counter", 0) & 63}
    if kind == "anim":
        return {"counter": get("counter", 0) & 7, "has_time": 0}
    if kind == "chase":
        cur = get("cur", 0.0)
        vector = hasattr(cur, "x")
        remaining = get("remaining")
        fields = {"vector": vector, "reached": get("reached", False), "has_remaining": remaining is not None}
        if vector:
            fields.update(x=cur.x, y=cur.y, z=cur.z)
        else:
            fields["x"] = cur
        if remaining is not None:
            fields.update(big=remaining >= 1 << 16, remaining=max(0, remaining) & 0xFFFFFFFF)
        return fields
    if kind == "ability":
        cur = get("cur", 0.0)
        fields = {"flags": get("flags", 0) & 0xFF, "has_cur": cur > 0}
        if cur > 0:
            rate = get("rate", 1.0)
            fields.update(cur=cur, has_rate=rate != 1.0, rate=rate, t=0)
        return fields
    if kind == "switch":
        return {"current": get("current", False)}
    if kind in ("button", "counter2"):
        return {"counter": get("counter", 0)}
    if kind == "message":
        return {"stacked": get("stacked", False)}
    if kind == "pulser":
        return {"fresh": get("fresh", False), "count": get("count", 0) & 0xFF}
    if kind == "hits":
        return {"n": 0, "flag": get("flag", False)}
    if kind == "flags13":
        value = get("value", 0)
        return {"value": value}
    if kind in ("none", "stack", "send", "link", "subscript", "targets", "frames"):
        return dict(payload) if kind == "frames" else {}
    raise ValueError(f"no H=0 payload for {cls}")


def write_remote_payload(out: BitWriter, cls: str, payload: dict, full: bool) -> None:
    item = codecs.codec(cls)
    if item is None or not item.verified:
        raise ValueError(f"no verified codec for {cls}")
    codecs.write(out, item.layout, remote_fields(cls, payload), owner=False, full=full, value=write_value)


def _remote_state(out: BitWriter, state: "StateWire | None", full: bool) -> None:
    """One remote-synced state: in a full frame X, then Y for an StEC class (off goes as X = 0); in a delta X
    for an StEC class; then StAc (StEC) or A and the payload."""
    stec = state is not None and state.cls in STEC_CLASSES
    if full:
        if state is None or not (state.active or stec):
            out.bit(0)  # X = 0: off
            return
        out.bit(1)  # X
        if stec:
            out.bit(1)  # Y
    elif stec:
        out.bit(1)  # X
    if stec:
        out.bit(state.active)  # StAc
        write_remote_payload(out, state.cls, state.payload, full)
    else:
        out.bit(state.active)  # A
        if state.active:
            write_remote_payload(out, state.cls, state.payload, full)


def remote_full_frame(instances: list[InstanceWire], entity_vars: dict, graphs: dict) -> BitWriter:
    """A plain (H = 0) full frame with the remote-synced states that are on (item.states by m_states index):
    every instance with its descriptor and variables, then per instance its states in m_remoteSyncNodes
    order. The client rebinds its own instances, makes the others and destroys the network instances the
    frame does not list."""
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(0)  # H
    last = 0
    ordered = sorted(instances, key=lambda item: item.id)
    for item in ordered:
        out.w_u16(item.id - last)
        last = item.id
        _descriptor_of(out, item)
    out.w_u16(0)
    _entity_vars(out, entity_vars, graphs)
    for item in ordered:
        _instance_vars(out, item, graphs, presence_only=True)
        for node in item.graph.remote_states():
            _remote_state(out, item.states.get(node.state), full=True)
    out.bits(0, 2)  # no frame events
    return out


def remote_delta(instances: list[InstanceWire], entity_vars: dict, graphs: dict) -> BitWriter:
    """A plain (H = 0) delta: the instances that changed, the variables, and per listed instance a mask over
    its raw m_remoteSyncNodes list with the states that changed (item.states by m_states index)."""
    out = BitWriter()
    out.bit(0)  # lead
    out.bit(0)  # H
    ordered = sorted(instances, key=lambda item: item.id)
    last = 0
    for item in ordered:
        if not item.listed:
            continue
        out.w_u16(item.id - last)
        last = item.id
        out.bit(item.gone)
        if not item.gone:
            out.bit(item.descriptor)
            if item.descriptor:
                _descriptor_of(out, item)
    out.w_u16(0)
    _entity_vars(out, entity_vars, graphs)
    for item in ordered:
        if not item.listed or item.gone:
            continue
        _instance_vars(out, item, graphs, presence_only=False)
        nodes = item.graph.remote_nodes
        chosen = {}
        for index, node in enumerate(nodes):
            if node is not None and node.state in item.states and node in item.graph.remote_states():
                chosen[index] = item.states[node.state]
        mask(out, len(nodes), set(chosen))
        for index in sorted(chosen):
            _remote_state(out, chosen[index], full=False)
    out.bits(0, 2)  # no frame events
    return out
