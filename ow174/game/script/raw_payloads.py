"""Strict raw payload tools for two verified 1.74 layouts, outside the runtime writers.

Field names and branches come from the codec table. Values retain their wire meaning:
the seven optional f32 fields have no assigned purpose, and 861D9015's v0..v6 are
unsigned bit patterns. A link's index is an owner state index or a remote-sync index,
depending on H; this module never translates one into the other.
"""

import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass

from ow174.game.bits import BitReader, BitWriter
from ow174.game.script import codecs

_SUPPORTED = frozenset({"9935FBC8", "861D9015"})


class PayloadError(ValueError):
    """Incomplete, out-of-range, or unsupported raw payload input."""


@dataclass(frozen=True)
class LinkMetadata:
    """Caller-supplied wire widths and allowed raw indexes for one known instance.

    Use the graph's actual widths, including zero when appropriate. Explicit index
    sets can exclude missing graph entries; an empty set permits no links for that H.
    Both contexts are validated, even when only one is being encoded or decoded.
    """

    owner_bits: int
    owner_indices: frozenset[int]
    remote_bits: int
    remote_indices: frozenset[int]


def _uint(value, width: int, path: str, *, flag: bool = False) -> int:
    if type(value) is not int and not (flag and type(value) is bool):
        raise PayloadError(f"{path}: expected an unsigned {width}-bit integer")
    if not 0 <= value < 1 << width:
        raise PayloadError(f"{path}: {value} does not fit in {width} bits")
    return int(value)


def _float32(value, path: str) -> float:
    if type(value) not in (int, float):
        raise PayloadError(f"{path}: expected a finite f32 number")
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite number")
        result = struct.unpack("<f", struct.pack("<f", number))[0]
    except (OverflowError, ValueError, struct.error) as exc:
        raise PayloadError(f"{path}: expected a finite number in f32 range") from exc
    if not math.isfinite(result):
        raise PayloadError(f"{path}: expected a finite number in f32 range")
    return result


def _entity(value, path: str) -> int:
    number = _uint(value, 32, path)
    if number and (not number & 0x80000000 or not number & 0x1FFFFFFF):
        raise PayloadError(f"{path}: expected 0 or a canonical entity id (high bit set, low 29 bits nonzero)")
    return number


def _metadata(links) -> dict[int, LinkMetadata]:
    if links is None:
        return {}
    if not isinstance(links, Mapping):
        raise PayloadError("links: expected a mapping of instance ids to LinkMetadata")
    result = {}
    for instance, meta in links.items():
        _uint(instance, 16, "links instance")
        if not isinstance(meta, LinkMetadata):
            raise PayloadError(f"links[{instance}]: expected LinkMetadata")
        checked = []
        for context in ("owner", "remote"):
            width = getattr(meta, f"{context}_bits")
            if type(width) is not int or not 0 <= width <= 32:
                raise PayloadError(f"links[{instance}].{context}_bits: expected a width from 0 to 32")
            indices = getattr(meta, f"{context}_indices")
            if not isinstance(indices, (set, frozenset)):
                raise PayloadError(f"links[{instance}].{context}_indices: expected an explicit set")
            checked.extend(
                (
                    width,
                    frozenset(
                        _uint(index, width, f"links[{instance}].{context}_indices") for index in indices
                    ),
                )
            )
        result[instance] = LinkMetadata(*checked)
    return result


def _context(cls: str, owner: bool, full: bool, links):
    if type(owner) is not bool or type(full) is not bool:
        raise PayloadError("owner and full must be explicit bool values")
    item = codecs.codec(cls) if isinstance(cls, str) else None
    if item is None or item.hash not in _SUPPORTED:
        raise PayloadError(f"{cls!r}: raw payload tools only support STU_9935FBC8 and STU_861D9015")
    if not item.verified:
        raise PayloadError(f"{cls}: the codec table does not mark its layout VERIFIED")
    return item.layout, _metadata(links)


def _condition(condition: str, fields: dict, owner: bool, full: bool) -> bool:
    if condition == "H&full":
        return owner and full
    if condition not in fields:
        raise PayloadError(f"layout condition {condition!r} names no earlier field")
    return bool(fields[condition])


def _link_meta(instance: int, links: dict, owner: bool):
    if instance not in links:
        raise PayloadError(f"link: instance {instance} has no explicit link metadata")
    meta = links[instance]
    return (meta.owner_bits, meta.owner_indices) if owner else (meta.remote_bits, meta.remote_indices)


def _link(value, links: dict, owner: bool, path: str):
    if value is None:
        return None
    if not isinstance(value, tuple) or len(value) != 2:
        raise PayloadError(f"{path}: expected None or (instance, raw_index)")
    instance = _uint(value[0], 16, f"{path}.instance")
    width, indices = _link_meta(instance, links, owner)
    index = _uint(value[1], width, f"{path}.index")
    if index not in indices:
        context = "owner" if owner else "remote"
        raise PayloadError(f"{path}: index {index} is not in the supplied {context} set")
    return instance, index


def _fields(layout: list, source, owner: bool, full: bool, links: dict, path: str) -> dict:
    if not isinstance(source, Mapping):
        raise PayloadError(f"{path}: expected a mapping of explicit fields")
    source = dict(source)
    result = {}

    def visit(ops):
        for op in ops:
            kind = op["op"]
            if kind == "if":
                visit(op["then"] if _condition(op["c"], result, owner, full) else op["else"])
                continue
            name = op["f"]
            field_path = f"{path}.{name}"
            if name not in source:
                raise PayloadError(f"{field_path}: missing required field for the current branch")
            value = source[name]
            if kind == "bits":
                result[name] = _uint(value, op["n"], field_path, flag=op["n"] == 1)
            elif kind == "f32":
                result[name] = _float32(value, field_path)
            elif kind == "entity_id":
                result[name] = _entity(value, field_path)
            elif kind == "var_b":
                result[name] = _uint(value, 32, field_path)
            elif kind == "link":
                result[name] = _link(value, links, owner, field_path)
            elif kind == "repeat":
                count = op["count"]
                if not isinstance(count, int) or not isinstance(value, (list, tuple)) or len(value) != count:
                    raise PayloadError(f"{field_path}: expected exactly {count} explicit items")
                result[name] = [
                    _fields(op["body"], item, owner, full, links, f"{field_path}[{index}]")
                    for index, item in enumerate(value)
                ]
            else:
                raise PayloadError(f"unsupported raw layout operation {kind!r}")

    visit(layout)
    unexpected = source.keys() - result.keys()
    if unexpected:
        names = ", ".join(sorted(map(repr, unexpected)))
        raise PayloadError(f"{path}: unexpected fields for the current branch: {names}")
    return result


def encode_payload(
    cls: str, fields: Mapping, *, owner: bool, full: bool, links: Mapping[int, LinkMetadata] | None = None
) -> BitWriter:
    """Return a fresh payload bit stream, after validating every active field.

    Missing fields, inactive-branch fields, extra repeat items, lossy integer coercion,
    nonfinite/overflowing f32 values, and links without valid metadata are rejected.
    No output is published or caller-owned writer changed on failure.
    """
    layout, metadata = _context(cls, owner, full, links)
    checked = _fields(layout, fields, owner, full, metadata, cls)

    def write_link(out, value):
        out.bit(value is not None)
        if value is not None:
            instance, index = value
            width, _ = _link_meta(instance, metadata, owner)
            out.w_u16(instance)
            out.bits(index, width)

    out = BitWriter()
    codecs.write(out, layout, checked, owner, full, link=write_link)
    return out


def _read(reader, layout, owner, full, links) -> dict:
    fields = {}

    def read_link():
        if not reader.bit():
            return None
        instance = codecs.w_u16(reader)
        width, _ = _link_meta(instance, links, owner)
        return instance, reader.bits(width)

    def visit(ops):
        for op in ops:
            kind = op["op"]
            if kind == "if":
                visit(op["then"] if _condition(op["c"], fields, owner, full) else op["else"])
                continue
            name = op["f"]
            if kind == "bits":
                fields[name] = reader.bits(op["n"])
            elif kind == "f32":
                fields[name] = codecs.f32(reader)
            elif kind == "var_b":
                fields[name] = codecs.var_b(reader)
            elif kind == "entity_id":
                entity_kind = reader.bits(2)
                low = _uint(codecs.var_b(reader), 29, f"{name} low entity bits")
                fields[name] = (0x80000000 | entity_kind << 29 | low) if low else 0
            elif kind == "link":
                fields[name] = read_link()
            elif kind == "repeat":
                fields[name] = [_read(reader, op["body"], owner, full, links) for _ in range(op["count"])]
            else:
                raise PayloadError(f"unsupported raw layout operation {kind!r}")

    visit(layout)
    return fields


def decode_payload(
    reader: BitReader, cls: str, *, owner: bool, full: bool, links: Mapping[int, LinkMetadata] | None = None
) -> dict:
    """Read raw active fields; advance reader.pos only after the entire payload validates.

    Respect reader.size as an exact bit limit when set by the caller. Trailing bits
    belong to the next field; byte padding is never consumed as part of the payload.
    """
    layout, metadata = _context(cls, owner, full, links)
    if not isinstance(reader, BitReader):
        raise PayloadError("reader: expected BitReader")
    if (
        type(reader.pos) is not int
        or type(reader.size) is not int
        or not 0 <= reader.pos <= reader.size <= len(reader.data) * 8
    ):
        raise PayloadError("reader: invalid position or bit limit")
    working = BitReader(reader.data, reader.pos)
    working.size = reader.size
    try:
        fields = _read(working, layout, owner, full, metadata)
    except EOFError as exc:
        raise PayloadError("truncated raw payload") from exc
    checked = _fields(layout, fields, owner, full, metadata, cls)
    reader.pos = working.pos
    return checked
