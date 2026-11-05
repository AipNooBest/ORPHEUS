"""Opt-in ray caches retain category geometry independently of mover caches."""

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ow174.game import collision, props
from ow174.paths import LOBBY_RUNTIME_DATA, MATCH_RUNTIME_DATA
from tools.prepare_server_runtime import prepare

MAP = 0x0800000000000688
OTHER_MAP = 0x0800000000000CD3
MODE = 0x0230000000000014
PLACEABLE = 0x0400000000000012
IDENTIFIER = 0x0D80000000000007
EXTRA = 0x80
ORIGIN = (0.0, 0.0, 0.0)
FORWARD = (0.0, 0.0, 1.0)


def face(z):
    return [(-2.0, -1.0, z), (2.0, -1.0, z), (0.0, 2.0, z)]


def map_chunk():
    mesh_at, points_at = 48, 48 + 56
    triangles_at = points_at + 6 * 16
    data = bytearray(triangles_at + 2 * 32)
    struct.pack_into("<4I4Q", data, 0, 0, 0, 0, 1, 0, 0, 0, mesh_at)
    struct.pack_into("<3Q3I", data, mesh_at, 0, points_at, triangles_at, 0, 6, 2)
    for index, point in enumerate(face(5.0) + face(3.0)):
        struct.pack_into("<4f", data, points_at + 16 * index, *point, 0.0)
    struct.pack_into("<3I3i4H", data, triangles_at, 0, 1, 2, -1, -1, -1, 0, 3, collision.TEAM0, 0)
    struct.pack_into("<3I3i4H", data, triangles_at + 32, 3, 4, 5, -1, -1, -1, 0, 5, EXTRA, 0)
    return bytes(data)


def placeable_chunk():
    offset, size = 12, 24 + 112
    data = bytearray(offset + size)
    struct.pack_into("<3I", data, 0, 1, 0, offset)
    struct.pack_into("<I", data, offset + 20, size)
    body = offset + 24
    struct.pack_into("<2Q", data, body, PLACEABLE, IDENTIFIER)
    struct.pack_into("<10f", data, body + 24, 0, 0, 1, 1, 1, 1, 0, 0, 0, 1)
    struct.pack_into("<H", data, body + 104, 1)
    return bytes(data)


class RayCacheTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(collision, "CACHE_DIR", Path(folder)))
        self.keys = {"ckey": "11" * 16}
        self.enterContext(
            patch.object(
                collision,
                "collision_keys",
                return_value={
                    MAP: self.keys,
                    OTHER_MAP: self.keys,
                },
            )
        )
        self.enterContext(
            patch.object(
                props,
                "props_data",
                return_value={
                    "maps": {f"0x{MAP:016X}": {"ckey": "placeables"}},
                    "defs": {f"0x{PLACEABLE:016X}": "model"},
                    "models": {"model": {"ckey": "model"}},
                    "modes": {f"0x{MODE:016X}": [f"0x{IDENTIFIER:016X}"]},
                },
            )
        )
        self.blobs = {self.keys["ckey"]: map_chunk(), "placeables": placeable_chunk(), "model": b"model"}
        self.enterContext(patch.object(props, "model_shapes", side_effect=self.model_shapes))

    def read_record(self, record):
        return self.blobs[record["ckey"]]

    def model_shapes(self, data, category=collision.MOVER):
        self.assertEqual(data, b"model")
        return [("mesh", EXTRA, face(0.0), [(0, 1, 2, -1, -1, -1, EXTRA, 7)])] if category & EXTRA else []

    def test_opt_in_preserves_legacy_and_retains_extra_core_and_mode_prop_surfaces(self):
        legacy = collision.build(MAP, self, "synthetic")
        legacy_bytes = legacy.read_bytes()
        legacy_hit = collision.World.load(legacy).cast_ray(ORIGIN, FORWARD, 8)
        self.assertFalse(collision.ray_cache_path(MAP).exists())

        expanded = collision.build(MAP, self, "synthetic", ray_geometry=True)
        self.assertEqual(expanded, collision.ray_cache_path(MAP))
        self.assertEqual(expanded.name, "0688.rays.col")
        self.assertEqual(collision.cache_version(expanded), collision.VERSION)
        self.assertEqual(legacy.read_bytes(), legacy_bytes)
        self.assertEqual(collision.World.load(legacy).cast_ray(ORIGIN, FORWARD, 8), legacy_hit)
        self.assertEqual(
            (legacy_hit.distance, legacy_hit.mask, legacy_hit.surface),
            (5.0, collision.TEAM0, 3),
        )
        with self.assertRaises(collision.CollisionError):
            collision.World.load(expanded)

        core = collision.RayWorld.load(MAP)
        mode = collision.RayWorld.load(MAP, mode=MODE)
        self.assertEqual(core.cast_ray(ORIGIN, FORWARD, 8, collision.MOVER), legacy_hit)
        hit = core.cast_ray(ORIGIN, FORWARD, 8, EXTRA)
        self.assertEqual((hit.distance, hit.mask, hit.surface), (3.0, EXTRA, 5))
        hit = mode.cast_ray(ORIGIN, FORWARD, 8, EXTRA)
        self.assertEqual((hit.distance, hit.mask, hit.surface), (1.0, EXTRA, 7))
        self.assertIsNone(core.cast_ray(ORIGIN, FORWARD, 2, EXTRA))

    def test_explicit_loader_rejects_missing_legacy_wrong_map_and_wrong_content(self):
        legacy = collision.build(MAP, self, "synthetic")
        with self.assertRaises(collision.CollisionError):
            collision.RayWorld.load(MAP)

        expanded = collision.ray_cache_path(MAP)
        expanded.write_bytes(legacy.read_bytes())
        with self.assertRaises(collision.CollisionError):
            collision.RayWorld.load(MAP)

        collision.build(MAP, self, "synthetic", ray_geometry=True)
        collision.ray_cache_path(OTHER_MAP).write_bytes(expanded.read_bytes())
        with self.assertRaises(collision.CollisionError):
            collision.RayWorld.load(OTHER_MAP)

        with (
            patch.object(collision, "collision_keys", return_value={MAP: {"ckey": "22" * 16}}),
            self.assertRaises(collision.CollisionError),
        ):
            collision.RayWorld.load(MAP)


class RuntimePreparationTests(unittest.TestCase):
    def test_copies_and_hashes_sidecars_only_for_selected_maps(self):
        selected = {
            "0688.col": b"selected mover cache",
            "0688.rays.col": b"selected ray cache",
            "0F00.col": b"selected legacy-only cache",
        }
        unselected = {
            "0CD3.col": b"unselected mover cache",
            "0CD3.rays.col": b"unselected ray cache",
        }
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            destination = Path(folder) / "runtime"
            data = source / "data"
            data.mkdir(parents=True)
            for name in LOBBY_RUNTIME_DATA + MATCH_RUNTIME_DATA:
                (data / name).write_bytes(b"synthetic runtime data")
            descriptor = source / "ow174" / "bnet" / "descriptors.pb"
            descriptor.parent.mkdir(parents=True)
            descriptor.write_bytes(b"synthetic descriptor")
            cache = source / "cache" / "collision"
            cache.mkdir(parents=True)
            for name, content in (selected | unselected).items():
                (cache / name).write_bytes(content)

            prepare(source, destination, ["0688", "0f00"])

            copied = {path.name: path.read_bytes() for path in (destination / "collision").iterdir()}
            self.assertEqual(copied, selected)
            manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))["sha256"]
            collision_hashes = {
                name: digest for name, digest in manifest.items() if name.startswith("collision/")
            }
            expected_hashes = {
                f"collision/{name}": hashlib.sha256(content).hexdigest() for name, content in selected.items()
            }
            self.assertEqual(collision_hashes, expected_hashes)


if __name__ == "__main__":
    unittest.main()
