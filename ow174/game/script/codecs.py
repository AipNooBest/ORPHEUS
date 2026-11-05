"""What the 1.74 client reads for each statescript state class in a frame, from
data/statescript_codecs_174.json (made in IDA by tools/extract_statescript_codecs.py from the client code).

Per state class (STU hash): the class the client builds for it (registry winner), whether it is an StEC class
(X then StAc, else A), its codec name and the layout of its payload: the property block's fields, then the
class reader's fields. A layout is a list of ops, read in order, all bits LSB first:
- bits n, f32, w_u16, var_b, signed_var, entity_id, value (a tagged value), link (the shared linked-state
  field);
- if c then else: c is H, !H, full, H&full, a field read before (non-zero), !field or valid:field (a valid
  entity);
- repeat count body: count is a field read before or a number; the items are a list of dicts.
H = an owner frame, full = a full frame (a delta reads with full = 0).
"""

import json
import struct
from dataclasses import dataclass
from functools import cache

from ow174.paths import DATA_DIR

CODECS_PATH = DATA_DIR / "statescript_codecs_174.json"


@dataclass(frozen=True)
class Codec:
    hash: str  # the node class's STU hash, 8 hex digits
    name: str  # the class name the graph data uses
    codec: str | None  # the payload's name (the same name = the same layout)
    layout: list | None  # None: not read to the end in the client code
    stec: bool
    status: str  # VERIFIED or UNVERIFIED

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED" and self.layout is not None

    @property
    def empty(self) -> bool:
        return self.layout == []


@cache
def table() -> dict:
    return json.loads(CODECS_PATH.read_text(encoding="utf-8"))


@cache
def _index() -> dict[str, Codec]:
    found = {}
    for key, entry in table()["classes"].items():
        item = Codec(key, entry["name"], entry["codec"], entry["layout"], entry["stec"], entry["status"])
        found[key] = found[entry["name"]] = found[f"STU_{key}"] = item
    return found


def codec(cls: str) -> Codec | None:
    """The codec of a node class by its graph-data name or its hash."""
    return _index().get(cls)


def stec_classes() -> set[str]:
    return {item.name for item in _index().values() if item.stec}


# --- the readers the layouts use (the client's) ------------------------------------------------------------


def _widths(reader, small: int, middle: int, large: int) -> int:
    if not reader.bit():
        return reader.bits(small)
    if not reader.bit():
        return reader.bits(middle)
    return reader.bits(large)


def w_u16(reader) -> int:
    """0+4, 10+8, 11+16 (0x7FF78A76BA50)."""
    return _widths(reader, 4, 8, 16)


def w_var(reader) -> int:
    """0+8, 10+16, 11+32 (0x7FF789C631B0); var_b (0x7FF78A76B800) has the same widths."""
    return _widths(reader, 8, 16, 32)


var_b = w_var


def signed_var(reader) -> int:
    """sign, then 0+8, 10+16, 11+32 (0x7FF78AA836A0)."""
    negative = reader.bit()
    value = w_var(reader)
    return -value if negative else value


def f32(reader) -> float:
    return struct.unpack("<f", struct.pack("<I", reader.bits(32)))[0]


def entity_id(reader) -> int:
    """2 type bits and var_b (0x7FF789CC0F70): 0x80000000 | type << 29 | value,
    0 when the low 29 bits are 0."""
    kind = reader.bits(2)
    entity = (0x80000000 | kind << 29 | var_b(reader)) & 0xFFFFFFFF
    return entity if entity & 0x1FFFFFFF else 0


class LayoutError(ValueError):
    pass


def _true(cond: str, fields: dict, owner: bool, full: bool) -> bool:
    if cond == "H":
        return owner
    if cond == "!H":
        return not owner
    if cond == "full":
        return full
    if cond == "H&full":
        return owner and full
    if cond.startswith("valid:"):
        entity = fields.get(cond[6:], 0)
        return bool(entity) and entity & 0x1FFFFFFF != 0x1FFFFFFF
    if cond.startswith("!"):
        return not fields.get(cond[1:])
    if cond in fields:
        return bool(fields[cond])
    raise LayoutError(f"condition {cond!r} names no field read before it")


def read(reader, layout: list, owner: bool, full: bool, link=None, value=None) -> dict:
    """The fields of one payload. `link(reader)` reads the linked-state field, `value(reader)` a tagged value
    (both need the frame's context, decode.py gives them)."""
    fields: dict = {}
    _read(reader, layout, owner, full, link, value, fields)
    return fields


def _read(reader, layout, owner, full, link, value, fields) -> None:
    for op in layout:
        kind = op["op"]
        if kind == "bits":
            fields[op["f"]] = reader.bits(op["n"])
        elif kind == "f32":
            fields[op["f"]] = f32(reader)
        elif kind == "w_u16":
            fields[op["f"]] = w_u16(reader)
        elif kind == "var_b":
            fields[op["f"]] = var_b(reader)
        elif kind == "signed_var":
            fields[op["f"]] = signed_var(reader)
        elif kind == "entity_id":
            fields[op["f"]] = entity_id(reader)
        elif kind == "value":
            if value is None:
                raise LayoutError("a tagged value needs a value reader")
            fields[op["f"]] = value(reader)
        elif kind == "link":
            if link is None:
                raise LayoutError("a linked-state field needs a link reader")
            fields[op["f"]] = link(reader)
        elif kind == "if":
            branch = op["then"] if _true(op["c"], fields, owner, full) else op["else"]
            _read(reader, branch, owner, full, link, value, fields)
        elif kind == "repeat":
            count = op["count"] if isinstance(op["count"], int) else fields[op["count"]]
            items = []
            for _ in range(count):
                item: dict = {}
                _read(reader, op["body"], owner, full, link, value, item)
                items.append(item)
            fields[op["f"]] = items
        else:
            raise LayoutError(f"unknown layout op {kind!r}")


# --- the writer: the same layouts, the other way (for the frames the server writes) -------------------------


def write(out, layout: list, fields: dict, owner: bool, full: bool, link=None, value=None) -> None:
    """Write one payload's fields by a layout, as `read` reads them. A missing field writes 0; `link(out,
    field)` writes the linked-state field (default: none), `value(out, field)` a tagged value."""
    for op in layout:
        kind = op["op"]
        name = op.get("f")
        if kind == "bits":
            out.bits(int(fields.get(name, 0)) & ((1 << op["n"]) - 1), op["n"])
        elif kind == "f32":
            out.bits(struct.unpack("<I", struct.pack("<f", float(fields.get(name, 0.0))))[0], 32)
        elif kind == "w_u16":
            out.w_u16(int(fields.get(name, 0)))
        elif kind == "var_b":
            out.var_b(int(fields.get(name, 0)))
        elif kind == "signed_var":
            number = int(fields.get(name, 0))
            out.bit(number < 0)
            out.w_var(abs(number))
        elif kind == "entity_id":
            out.entity_id(fields.get(name) or 0)
        elif kind == "value":
            if value is None:
                raise LayoutError("a tagged value needs a value writer")
            value(out, fields.get(name))
        elif kind == "link":
            if link is None:
                out.bit(0)  # no linked state
            else:
                link(out, fields.get(name))
        elif kind == "if":
            branch = op["then"] if _written(op["c"], fields, owner, full) else op["else"]
            write(out, branch, fields, owner, full, link, value)
        elif kind == "repeat":
            items = list(fields.get(name) or [])
            count = op["count"] if isinstance(op["count"], int) else int(fields.get(op["count"], 0))
            if len(items) < count:
                raise LayoutError(f"{name}: {count} items to write, {len(items)} given")
            for item in items[:count]:
                write(out, op["body"], item, owner, full, link, value)
        else:
            raise LayoutError(f"unknown layout op {kind!r}")


def _written(cond: str, fields: dict, owner: bool, full: bool) -> bool:
    """A layout condition for the writer: a field it was not given counts as 0."""
    if cond in ("H", "!H", "full", "H&full") or cond.startswith("valid:"):
        return _true(cond, fields, owner, full)
    if cond.startswith("!"):
        return not fields.get(cond[1:])
    return bool(fields.get(cond))
