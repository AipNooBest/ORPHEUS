"""External CASC records resolve omitted static roots for opt-in ray caches."""

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_collision_ray_cache import (
    EXTRA,
    FORWARD,
    IDENTIFIER,
    MAP,
    MODE,
    ORIGIN,
    PLACEABLE,
    face,
    map_chunk,
    placeable_chunk,
)

from ow174.game import casc, collision, props

EXTRA_DEF = 0x0400000000000013
BASE_MODEL = 0x0D00000000000022
EXTRA_MODEL = 0x0D00000000000023
CATALOG_SCHEMA = "orpheus-casc-records-v1"
BUILD = "1.74.0.0.104319"


def key_record(data):
    return {"ekey": "11" * 16, "ckey": hashlib.md5(data).hexdigest(), "size": len(data)}


def map_placeables():
    chunk = placeable_chunk()
    base, added = bytearray(chunk[12:]), bytearray(chunk[12:])
    struct.pack_into("<f", base, 24 + 32, 2.0)
    struct.pack_into("<Q", added, 24, EXTRA_DEF)
    return struct.pack("<3I", 2, 0, 12) + base + added


class PropCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(collision, "CACHE_DIR", self.directory))
        self.blobs = {}
        core = self.store(map_chunk())
        self.placements = self.store(map_placeables())
        base_model = self.store(b"base model")
        self.records = {
            EXTRA_DEF: self.store(b"extra definition"),
            EXTRA_MODEL: self.store(b"extra model"),
        }
        self.enterContext(patch.object(collision, "collision_keys", return_value={MAP: core}))
        self.enterContext(
            patch.object(
                props,
                "props_data",
                return_value={
                    "maps": {f"0x{MAP:016X}": self.placements},
                    "defs": {f"0x{PLACEABLE:016X}": f"0x{BASE_MODEL:016X}"},
                    "models": {f"0x{BASE_MODEL:016X}": base_model},
                    "modes": {f"0x{MODE:016X}": [f"0x{IDENTIFIER:016X}"]},
                },
            )
        )
        self.enterContext(patch.object(props, "model_shapes", side_effect=self.model_shapes))

    def store(self, data):
        record = key_record(data)
        self.blobs[record["ckey"]] = data
        return record

    def read_record(self, record):
        return self.blobs[record["ckey"]]

    def model_shapes(self, data, category=collision.MOVER):
        mask, surface = {
            b"base model": (collision.MOVER | EXTRA, 7),
            b"extra model": (EXTRA, 9),
        }[data]
        return [("mesh", mask, face(0.0), [(0, 1, 2, -1, -1, -1, mask, surface)])] if category & mask else []

    def catalog_path(self, entries, **overrides):
        document = {
            "schema": CATALOG_SCHEMA,
            "build": BUILD,
            "records": {f"0x{guid:016X}": record for guid, record in entries.items()},
        } | overrides
        path = self.directory / "records.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def entity_model_refs(self, data):
        self.assertEqual(data, b"extra definition")
        return [(0, False), (EXTRA_MODEL, False)], []

    def test_supplement_uses_mode_gating_and_invalidates_prior_readiness(self):
        legacy = collision.build(MAP, self, "synthetic")
        legacy_bytes = legacy.read_bytes()
        legacy_view = collision.World.load(legacy, frozenset({IDENTIFIER}))
        self.assertEqual(legacy_view.cast_ray(ORIGIN, FORWARD, 6).distance, 2.0)
        collision.build(MAP, self, "synthetic", ray_geometry=True)
        self.assertTrue(collision.ray_cache_ready(MAP))
        baseline = collision.RayWorld.load(MAP, MODE)
        self.assertEqual(baseline.cast_ray(ORIGIN, FORWARD, 6, EXTRA).distance, 2.0)

        catalog_path = self.catalog_path(self.records)
        catalog = casc.RecordCatalog.load(catalog_path)
        self.assertEqual(catalog.records, self.records)
        self.assertFalse(collision.ray_cache_ready(MAP, prop_records=catalog))
        with patch.object(props, "entity_model_refs", side_effect=self.entity_model_refs) as resolve:
            expanded = collision.build(MAP, self, "synthetic", ray_geometry=True, prop_records=catalog)
            resolve.assert_called_once_with(b"extra definition")
        header, *_ = collision.unpack(expanded.read_bytes())
        self.assertEqual(header["prop_records_sha256"], catalog.sha256)
        self.assertEqual(header["prop_placement_ckey"], self.placements["ckey"])
        self.assertTrue(collision.ray_cache_ready(MAP, prop_records=catalog))
        changed = {**self.records, EXTRA_MODEL: self.store(b"revised extra model")}
        revised = casc.RecordCatalog.load(self.catalog_path(changed))
        self.assertNotEqual(catalog.sha256, revised.sha256)
        self.assertFalse(collision.ray_cache_ready(MAP, prop_records=revised))

        catalog_path.unlink()
        self.assertEqual(legacy.read_bytes(), legacy_bytes)
        updated = collision.RayWorld.load(MAP, MODE)
        hit = updated.cast_ray(ORIGIN, FORWARD, 6, EXTRA)
        self.assertEqual((hit.distance, hit.surface), (1.0, 9))
        hit = updated.cast_ray((0.0, 0.0, 2.5), (0.0, 0.0, -1.0), 2, EXTRA)
        self.assertEqual((hit.distance, hit.surface), (0.5, 7))
        self.assertEqual(collision.RayWorld.load(MAP).cast_ray(ORIGIN, FORWARD, 6, EXTRA).distance, 3.0)

    def test_missing_definition_or_model_preserves_existing_ray_cache(self):
        path = collision.build(MAP, self, "synthetic", ray_geometry=True)
        original = path.read_bytes()
        for missing in (EXTRA_DEF, EXTRA_MODEL):
            with self.subTest(missing=f"{missing:016X}"):
                records = {guid: record for guid, record in self.records.items() if guid != missing}
                catalog = casc.RecordCatalog.load(self.catalog_path(records))
                with (
                    patch.object(props, "entity_model_refs", side_effect=self.entity_model_refs),
                    self.assertRaisesRegex(collision.CollisionError, f"(?i){missing:x}"),
                ):
                    collision.build(MAP, self, "synthetic", ray_geometry=True, prop_records=catalog)
                self.assertEqual(path.read_bytes(), original)

    def test_catalog_rejects_wrong_schema_build_and_record_types(self):
        for overrides in (
            {"schema": "other"},
            {"build": "1.74.0.0.104318"},
            {"records": []},
            {"records": {f"0x{EXTRA_DEF:016X}": dict(self.records[EXTRA_DEF], size=True)}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(casc.CascError):
                casc.RecordCatalog.load(self.catalog_path(self.records, **overrides))


if __name__ == "__main__":
    unittest.main()
