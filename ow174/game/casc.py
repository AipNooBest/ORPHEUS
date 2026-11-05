"""Reads single files out of the local game install (Blizzard's CASC storage), by their keys.

Only what the server needs to pull a map's collision out of the player's own copy of the game:
    - the local index `data\\casc\\data\\XX*.idx` of bucket XX (the 9 first bytes of the encoding key
      xored together, folded to 4 bits; the newest version of each bucket wins, as in TACTLib), whose
      18-byte entries are {9 bytes of the key, 5 bytes big endian archive << 30 | offset, u32 size};
    - the archive `data.NNN` at that offset: a 30-byte header {the key reversed, u32 size, ...} and the
      file in BLTE form: 'BLTE', u32 header size, then blocks that are stored ('N') or zlib ('Z').
The content key is the MD5 of the file, which checks what was read. Files are found by their encoding
key, not by name: the key tables (data/collision_keys_174.json) are build 1.74.0.0.104319's.
An asset stored inside a bundle has its own content key and size, with the containing bundle's keys,
size and the asset's offset in a nested `bundle` record. Both contents are checked before use.
"""

import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

INDEX_VERSION = 7
ENTRY = 18  # bytes per index entry
KEY = 9  # bytes of the encoding key the index keeps
RECORD_CATALOG_SCHEMA = "orpheus-casc-records-v1"
RECORD_CATALOG_BUILD = "1.74.0.0.104319"


class CascError(Exception):
    pass


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise CascError(f"duplicate record catalog key {key}")
        result[key] = value
    return result


def _record_layout(record: dict) -> tuple:
    if not isinstance(record, dict):
        raise CascError("invalid storage record")
    bundled = "bundle" in record
    if bundled and "ekey" in record:
        raise CascError("bundled storage record cannot have an outer encoding key")
    source = record["bundle"] if bundled else record
    if not isinstance(source, dict):
        raise CascError("invalid storage record")
    try:
        ekey = bytes.fromhex(source["ekey"])
        source_ckey = bytes.fromhex(source["ckey"])
        ckey = bytes.fromhex(record["ckey"])
    except (KeyError, TypeError, ValueError) as error:
        raise CascError("invalid storage record keys") from error
    if any(len(key) != 16 for key in (ekey, source_ckey, ckey)):
        raise CascError("storage record keys must be 16 bytes")

    def size(value, label: str) -> int:
        if type(value) is not int or value < 0:
            raise CascError(f"invalid {label}: expected a non-negative integer")
        return value

    if bundled:
        return (ekey, source_ckey, ckey, size(source.get("size"), "bundle size"),
                size(source.get("offset"), "bundle offset"), size(record.get("size"), "slice size"))
    expected = size(record["size"], "file size") if "size" in record else None
    return ekey, source_ckey, ckey, expected, None, None


@dataclass(frozen=True)
class RecordCatalog:
    """Local build-specific definition and model records for the offline geometry builder."""

    records: dict[int, dict]
    sha256: str

    @classmethod
    def load(cls, path: str | Path) -> "RecordCatalog":
        raw = Path(path).read_bytes()
        try:
            data = json.loads(raw, object_pairs_hook=_unique_object)
        except ValueError as error:
            raise CascError("invalid record catalog JSON") from error
        if (
            type(data) is not dict
            or set(data) != {"schema", "build", "records"}
            or data["schema"] != RECORD_CATALOG_SCHEMA
            or data["build"] != RECORD_CATALOG_BUILD
            or type(data["records"]) is not dict
        ):
            raise CascError("record catalog must describe build 1.74.0.0.104319")
        records = {}
        for text, record in data["records"].items():
            if not re.fullmatch(r"0x(?:0400|0D00)[0-9A-F]{12}", text):
                raise CascError(f"invalid definition or model GUID {text}")
            if type(record) is not dict or "size" not in record:
                raise CascError(f"invalid storage record for {text}")
            if "bundle" in record:
                if (
                    set(record) != {"bundle", "ckey", "size"}
                    or type(record["bundle"]) is not dict
                    or set(record["bundle"]) != {"ekey", "ckey", "size", "offset"}
                ):
                    raise CascError(f"invalid bundled storage record for {text}")
            elif set(record) != {"ekey", "ckey", "size"}:
                raise CascError(f"invalid direct storage record for {text}")
            _, _, _, size, offset, length = _record_layout(record)
            if offset is not None and (offset > size or length > size - offset):
                raise CascError(f"asset slice for {text} runs past its bundle")
            records[int(text, 16)] = record
        return cls(records, hashlib.sha256(raw).hexdigest())


def game_root(executable: str | Path) -> Path:
    """The install folder of an Overwatch.exe (it sits in _retail_)."""
    path = Path(executable)
    for folder in (path.parent, *path.parents):
        if (folder / "data" / "casc" / "data").is_dir():
            return folder
    raise CascError(f"no game data next to {path}")


def bucket(key: bytes) -> int:
    value = 0
    for byte in key[:KEY]:
        value ^= byte
    return (value & 0xF) ^ (value >> 4)


def blte(data: bytes) -> bytes:
    """The content of a BLTE-encoded file."""
    if data[:4] != b"BLTE":
        raise CascError("not a BLTE file")
    header = struct.unpack_from(">I", data, 4)[0]
    if header == 0:
        sizes, position = [len(data) - 8], 8
    else:
        count = int.from_bytes(data[9:12], "big")
        sizes = [struct.unpack_from(">I", data, 12 + 24 * n)[0] for n in range(count)]
        position = header
    out = bytearray()
    for size in sizes:
        block = data[position : position + size]
        position += size
        mode = block[:1]
        if mode == b"N":
            out += block[1:]
        elif mode == b"Z":
            out += zlib.decompress(block[1:])
        else:
            raise CascError(f"BLTE block of mode {mode!r}")
    return bytes(out)


class Storage:
    """The local storage of one game install."""

    def __init__(self, root: str | Path) -> None:
        self.data = Path(root) / "data" / "casc" / "data"
        if not self.data.is_dir():
            raise CascError(f"no {self.data}")

    def _index(self, number: int) -> Path:
        files = list(self.data.glob(f"{number:02x}*.idx"))
        if not files:
            raise CascError(f"no index {number:02x} in {self.data}")
        return max(files, key=lambda path: int(path.stem[2:], 16))

    def locate(self, ekey: bytes) -> tuple[int, int, int]:
        """(archive number, offset, size) of the file with this encoding key."""
        number = bucket(ekey)
        raw = self._index(number).read_bytes()
        layout = struct.unpack_from("<HBBBBB", raw, 8)  # version, bucket, extra, size, offset, key bytes
        if layout != (INDEX_VERSION, number, 0, 4, 5, KEY):
            raise CascError(f"unknown index layout {layout}")
        entries = struct.unpack_from("<I", raw, 32)[0]
        start = 40
        wanted = ekey[:KEY]
        for position in range(start, start + entries - entries % ENTRY, ENTRY):
            if raw[position : position + KEY] != wanted:
                continue
            high = raw[position + 9]
            low = struct.unpack_from(">I", raw, position + 10)[0]
            archive = high << 2 | low >> 30
            offset = low & 0x3FFFFFFF
            size = struct.unpack_from("<I", raw, position + 14)[0]
            path = self.data / f"data.{archive:03d}"
            if path.is_file() and offset < path.stat().st_size:
                return archive, offset, size
        raise CascError(f"key {ekey.hex()} is not in the local storage")

    def read(self, ekey: bytes, ckey: bytes | None = None) -> bytes:
        archive, offset, size = self.locate(ekey)
        with open(self.data / f"data.{archive:03d}", "rb") as file:
            file.seek(offset)
            record = file.read(size)
        if len(record) != size or struct.unpack_from("<I", record, 16)[0] != size:
            raise CascError(f"archive entry of {ekey.hex()} is damaged")
        content = blte(record[30:])
        if ckey is not None and hashlib.md5(content).digest() != ckey:
            raise CascError(f"{ekey.hex()} does not match its content key")
        return content

    def read_record(self, record: dict) -> bytes:
        """Read a key-table asset, either a file or a checked slice of its containing bundle.

        A direct record names `ekey`, `ckey` and optionally `size`. A bundled asset instead names its
        own `ckey` and `size`, and `bundle` holds the container's `ekey`, `ckey`, `size` and `offset`.
        The bundle's encoding key never stands in for an encoding key of the asset itself.
        """
        ekey, source_ckey, ckey, expected, offset, length = _record_layout(record)
        content = self.read(ekey, source_ckey)
        if expected is not None and len(content) != expected:
            raise CascError(f"storage record size {expected}, read {len(content)} bytes")
        if offset is not None:
            if offset > len(content) or length > len(content) - offset:
                raise CascError("asset slice runs past its bundle")
            content = content[offset : offset + length]
            if hashlib.md5(content).digest() != ckey:
                raise CascError("bundle slice does not match its content key")
        return content
