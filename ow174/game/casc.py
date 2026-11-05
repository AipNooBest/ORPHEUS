"""Reads single files out of the local game install (Blizzard's CASC storage), by their keys.

Only what the server needs to pull a map's collision out of the player's own copy of the game:
    - the local index `data\\casc\\data\\XX*.idx` of bucket XX (the 9 first bytes of the encoding key
      xored together, folded to 4 bits; the newest version of each bucket wins, as in TACTLib), whose
      18-byte entries are {9 bytes of the key, 5 bytes big endian archive << 30 | offset, u32 size};
    - the archive `data.NNN` at that offset: a 30-byte header {the key reversed, u32 size, ...} and the
      file in BLTE form: 'BLTE', u32 header size, then blocks that are stored ('N') or zlib ('Z').
The content key is the MD5 of the file, which checks what was read. Files are found by their encoding
key, not by name: the key tables (data/collision_keys_174.json) are build 1.74.0.0.104319's.
"""

import hashlib
import struct
import zlib
from pathlib import Path

INDEX_VERSION = 7
ENTRY = 18  # bytes per index entry
KEY = 9  # bytes of the encoding key the index keeps


class CascError(Exception):
    pass


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
