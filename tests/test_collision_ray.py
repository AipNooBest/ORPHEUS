"""Static collision ray witnesses without original map data."""

import math
import unittest

from ow174.game.collision import MOVER, NOT_GROUND, TEAM0, TEAM1, World
from ow174.game.combat import ray_world


def face(z):
    return (-2, -1, z, 2, -1, z, 0, 2, z)


class CollisionRayTests(unittest.TestCase):
    def setUp(self):
        self.world = World(
            [face(6), face(2), face(4), face(6)],
            [TEAM1, 0x80, TEAM0, TEAM1],
            [2, NOT_GROUND, 4, 8],
        )

    def test_nearest_hit_respects_category_and_keeps_triangle_metadata(self):
        for category, distance, mask, surface in (
            (0x80, 2.0, 0x80, NOT_GROUND),
            (MOVER, 4.0, TEAM0, 4),
            (TEAM1, 6.0, TEAM1, 2),
        ):
            with self.subTest(category=category):
                hit = self.world.cast_ray((0, 0, 0), (0, 0, 1), 8, category)
                self.assertEqual(hit.distance, distance)
                self.assertEqual(hit.point, (0.0, 0.0, distance))
                self.assertEqual(hit.normal, (0.0, 0.0, -1.0))
                self.assertEqual((hit.mask, hit.surface), (mask, surface))
                self.assertEqual(ray_world(self.world, (0, 0, 0), (0, 0, 1), 8, category), distance)

    def test_back_face_normal_and_end_of_segment(self):
        hit = self.world.cast_ray((0, 0, 7), (0, 0, -1), 1, TEAM1)
        self.assertEqual(hit.distance, 1.0)
        self.assertEqual(hit.point, (0.0, 0.0, 6.0))
        self.assertEqual(hit.normal, (0.0, 0.0, 1.0))
        self.assertIsNone(self.world.cast_ray((0, 0, 7), (0, 0, -1), 0.99, TEAM1))

    def test_vertical_rays_and_invalid_inputs(self):
        floor = World([(-2, 1, -2, 2, 1, -2, 0, 1, 2)])
        hit = floor.cast_ray((0, 3, 0), (0, -1, 0), 2)
        self.assertEqual((hit.distance, hit.point, hit.normal), (2.0, (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)))
        self.assertIsNone(floor.cast_ray((0, 3, 0), (0, 0, 0), 2))
        self.assertIsNone(floor.cast_ray((0, 3, 0), (0, -1, 0), math.inf))
        self.assertIsNone(floor.cast_ray((math.nan, 3, 0), (0, -1, 0), 2))


if __name__ == "__main__":
    unittest.main()
