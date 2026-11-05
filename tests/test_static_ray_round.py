"""Analytic round hits for expanded static ray geometry."""

import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_collision_ray_cache import EXTRA, FORWARD, IDENTIFIER, MAP, MODE, ORIGIN, map_chunk

from ow174.game import collision, props, static_ray


class RoundHitTests(unittest.TestCase):
    def assert_hit(self, actual, distance, normal):
        self.assertIsNotNone(actual)
        actual_distance, actual_normal = actual
        self.assertAlmostEqual(actual_distance, distance)
        for actual_axis, axis in zip(actual_normal, normal, strict=True):
            self.assertAlmostEqual(actual_axis, axis)

    def test_sphere_tangent_inside_and_finite_segment(self):
        center = (0.0, 0.0, 5.0)
        hit = static_ray.round_hit
        self.assert_hit(hit(ORIGIN, FORWARD, 4, center, center, 1), 4, (0.0, 0.0, -1.0))
        self.assertIsNone(hit(ORIGIN, FORWARD, 3.99, center, center, 1))
        self.assert_hit(hit((1.0, 0.0, 0.0), FORWARD, 5, center, center, 1), 5, (1.0, 0.0, 0.0))
        self.assertIsNone(hit((1.01, 0.0, 0.0), FORWARD, 8, center, center, 1))
        self.assert_hit(hit(center, FORWARD, 2, center, center, 1), 1, (0.0, 0.0, -1.0))
        self.assertIsNone(hit((0.0, 0.0, 4.0), (0.0, 0.0, -1.0), 8, center, center, 1))

    def test_capsule_side_caps_and_parallel_ray(self):
        a, b = (0.0, -1.0, 5.0), (0.0, 1.0, 5.0)
        hit = static_ray.round_hit
        self.assert_hit(hit((0.6, 0.0, 0.0), FORWARD, 8, a, b, 1), 4.2, (0.6, 0.0, -0.8))
        self.assert_hit(hit((0.0, 1.6, 0.0), FORWARD, 8, a, b, 1), 4.2, (0.0, 0.6, -0.8))
        self.assert_hit(hit((0.6, -5.0, 5.0), (0.0, 1.0, 0.0), 8, a, b, 1), 3.2, (0.6, -0.8, 0.0))
        self.assert_hit(hit((0.0, 0.0, 5.0), FORWARD, 2, a, b, 1), 1, (0.0, 0.0, -1.0))
        self.assertIsNone(hit((0.0, 2.01, 0.0), FORWARD, 8, a, b, 1))

    def test_rejects_invalid_ray_or_primitive(self):
        center = (0.0, 0.0, 5.0)
        hit = static_ray.round_hit
        for args in (
            (ORIGIN, ORIGIN, 8, center, center, 1),
            (ORIGIN, FORWARD, math.inf, center, center, 1),
            (ORIGIN, FORWARD, 0, center, center, 1),
            (ORIGIN, FORWARD, 8, center, center, 0),
            (ORIGIN, FORWARD, 8, center, (math.nan, 0, 5), 1),
        ):
            with self.subTest(args=args):
                self.assertIsNone(hit(*args))


class RoundRayCacheTests(unittest.TestCase):
    def test_expanded_round_shapes_keep_masks_transforms_and_mode(self):
        sphere_mask = collision.TEAM0 | EXTRA
        models = [
            [("sphere", sphere_mask, ORIGIN, 0.5)],
            [("capsule", collision.TEAM1, (-1.0, -1.0, 0.0), (-1.0, 1.0, 0.0), 0.5)],
        ]
        placed = [
            (0, 1, (0.0, 0.0, 2.0), (2.0, 2.0, 2.0), (0.0, 0.0, 0.0, 1.0)),
            (1, 0, (2.0, 0.0, 4.0), (1.0, 1.0, 1.0), (0.0, 0.0, 1.0, 0.0)),
        ]
        key = {"ckey": "11" * 16}
        source = {"map": f"0x{MAP:016X}", "chunk": f"0x{collision.chunk_guid(MAP):016X}", **key}
        legacy_meshes, legacy_hulls = collision.parse_chunk(map_chunk())
        ray_meshes, ray_hulls = collision.parse_chunk(map_chunk(), collision.ALL_CATEGORIES)
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(collision, "collision_keys", return_value={MAP: key}),
            patch.object(
                props,
                "props_data",
                return_value={
                    "modes": {f"0x{MODE:016X}": [f"0x{IDENTIFIER:016X}"]},
                },
            ),
            patch.object(collision, "CACHE_DIR", Path(folder)),
        ):
            legacy = collision.cache_path(MAP)
            legacy.write_bytes(
                collision.pack(
                    "synthetic",
                    legacy_meshes,
                    legacy_hulls,
                    source,
                    models,
                    placed,
                    [IDENTIFIER],
                )
            )
            expanded = collision.ray_cache_path(MAP)
            expanded.write_bytes(
                collision.pack(
                    "synthetic",
                    ray_meshes,
                    ray_hulls,
                    dict(
                        source,
                        geometry=collision.RAY_GEOMETRY,
                        category_mask=collision.ALL_CATEGORIES,
                        prop_scope=collision.RAY_PROP_SCOPE,
                    ),
                    models,
                    placed,
                    [IDENTIFIER],
                )
            )
            mover = collision.World.load(legacy, frozenset({IDENTIFIER}))
            core = collision.RayWorld.load(MAP)
            mode = collision.RayWorld.load(MAP, MODE)

        origin = (0.0, 0.8, 0.0)
        self.assertEqual(core.cast_ray(origin, FORWARD, 8, EXTRA).distance, 3.0)
        hit = mode.cast_ray(origin, FORWARD, 8, EXTRA)
        self.assertAlmostEqual(hit.distance, 1.4)
        self.assertAlmostEqual(hit.point[2], 1.4)
        self.assertAlmostEqual(hit.normal[1], 0.8)
        self.assertAlmostEqual(hit.normal[2], -0.6)
        self.assertEqual((hit.mask, hit.surface), (sphere_mask, 0))
        tie = mode.cast_ray((0.0, 0.0, 2.0), FORWARD, 1, EXTRA)
        self.assertEqual((tie.distance, tie.mask, tie.surface), (1.0, EXTRA, 5))

        # The flat end of the movement prism blocks this segment before the sphere would.
        near_top = (0.0, 0.95, 0.0)
        self.assertIsNotNone(mover.cast_ray(near_top, FORWARD, 1.2, collision.TEAM0))
        self.assertIsNone(mode.cast_ray(near_top, FORWARD, 1.2, collision.TEAM0))

        capsule_origin = (3.3, 0.0, 0.0)
        hit = core.cast_ray(capsule_origin, FORWARD, 8, collision.TEAM1)
        self.assertAlmostEqual(hit.distance, 3.6)
        self.assertAlmostEqual(hit.normal[0], 0.6)
        self.assertAlmostEqual(hit.normal[2], -0.8)
        self.assertEqual((hit.mask, hit.surface), (collision.TEAM1, 0))
        self.assertIsNone(core.cast_ray(capsule_origin, FORWARD, 8, EXTRA))


if __name__ == "__main__":
    unittest.main()
