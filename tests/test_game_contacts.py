"""The physics engine's contacts for the character's capsule (ow174/game/contacts.py), ported from the
client. Bit-exact checks against the client's own code need its image and live under the research scratch
folder; these are the engine's numbers on stand-in walls."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import contacts as c

f32 = c.f32
REST = 4.495  # a wall at 5: the capsule's core stops 0.5 + 5 mm from it, not at its radius


def wall_z(z: float) -> c.MeshShape:
    """A wall facing -z: two triangles sharing a diagonal (wing vertex numbers across it)."""
    v = [(-10.0, -1.0, z), (10.0, -1.0, z), (10.0, 10.0, z), (-10.0, 10.0, z)]
    return c.MeshShape(v, [(0, 2, 1, 3, -1, -1, 1, 0), (0, 3, 2, -1, -1, 1, 1, 0)])


def wall_x(x: float) -> c.MeshShape:
    """A wall facing -x."""
    v = [(x, -1.0, -10.0), (x, -1.0, 10.0), (x, 10.0, 10.0), (x, 10.0, -10.0)]
    return c.MeshShape(v, [(0, 1, 2, 3, -1, -1, 1, 0), (0, 2, 3, -1, -1, 1, 1, 0)])


def floor(y: float, half: float = 2.0) -> c.MeshShape:
    """A floor facing +y."""
    v = [(-half, y, -half), (half, y, -half), (half, y, half), (-half, y, half)]
    return c.MeshShape(v, [(0, 2, 1, -1, -1, -1, 1, 0), (0, 3, 2, -1, -1, -1, 1, 0)])


DISC = [(0.4, 0.015, 0.0), (0.0, 0.015, 0.4), (-0.4, 0.015, 0.0), (0.0, 0.015, -0.4), (0.0, 0.0, 0.0)]


def walk(meshes, start, step, ticks: int):
    world = c.StaticWorld([c.StaticShape(mesh, order=k) for k, mesh in enumerate(meshes)])
    body = c.CharacterBody(0.5, 1.5, world=world, category=1)
    body.teleport(start)
    result = None
    for _ in range(ticks):
        result = body.move(tuple(f32(x) for x in step), f32(0.016), 20)
    return result


class ContactsTest(unittest.TestCase):
    def test_wall_rests_five_millimetres_out(self):
        final, planes, touching = walk([wall_z(5.0)], (0.0, 0.0, 3.0), (0.0, 0.0, 0.2), 20)
        self.assertAlmostEqual(final[2], REST, delta=1e-6)
        self.assertEqual((final[0], final[1]), (0.0, 0.0))
        self.assertTrue(touching)
        self.assertEqual([n for n, _w in planes], [(0.0, 0.0, -1.0)])

    def test_square_corner(self):
        final, planes, touching = walk([wall_z(5.0), wall_x(5.0)], (3.0, 0.0, 3.0), (0.2, 0.0, 0.2), 20)
        self.assertAlmostEqual(final[0], REST, delta=1e-6)
        self.assertAlmostEqual(final[2], REST, delta=1e-6)
        self.assertEqual((len(planes), touching), (2, True))

    def test_slide_along_a_wall(self):
        final, _planes, touching = walk([wall_z(5.0)], (-5.0, 0.0, 4.0), (0.1, 0.0, 0.05), 40)
        self.assertAlmostEqual(final[0], -1.0, delta=1e-4)
        self.assertAlmostEqual(final[2], REST, delta=1e-6)
        self.assertTrue(touching)

    def test_free_move_is_exact(self):
        final, planes, touching = walk([wall_z(5.0)], (0.0, 0.0, 0.0), (0.1, 0.0, -0.1), 3)
        self.assertEqual(final, (f32(f32(f32(0.1) + f32(0.1)) + f32(0.1)), 0.0, -final[0]))
        self.assertEqual((planes, touching), ([], False))

    def test_clip_velocity(self):
        self.assertEqual(c.clip_velocity((1.0, 0.0, 2.0), [((0.0, 0.0, -1.0), 0.0)]), (1.0, 0.0, 0.0))
        self.assertEqual(c.clip_velocity((1.0, 0.0, -2.0), [((0.0, 0.0, -1.0), 0.0)]), (1.0, 0.0, -2.0))
        both = [((0.0, 0.0, -1.0), 0.0), ((-1.0, 0.0, 0.0), 0.0)]
        self.assertEqual(c.clip_velocity((1.0, 3.0, 2.0), both), (0.0, 3.0, 0.0))

    def test_gjk_and_box(self):
        d = c.gjk([(0.0, 0.0, 0.0)], [(3.0, 4.0, 0.0)])
        self.assertEqual((d.distance, d.point_b), (5.0, (3.0, 4.0, 0.0)))
        tri = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        self.assertTrue(c.triangle_box(*tri, (0.2, 0.2, -0.1), (0.4, 0.4, 0.1)))
        self.assertFalse(c.triangle_box(*tri, (0.8, 0.8, -0.1), (1.0, 1.0, 0.1)))

    def test_closest_point_on_a_triangle(self):
        tri = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 2.0))
        self.assertEqual(c.closest_on_triangle(*tri, (0.5, 1.0, 0.5)), (0.5, 0.0, 0.5))  # over the face
        self.assertEqual(c.closest_on_triangle(*tri, (-1.0, 0.3, -1.0)), (0.0, 0.0, 0.0))  # past a corner
        self.assertEqual(c.closest_on_triangle(*tri, (1.0, -0.5, -1.0)), (1.0, 0.0, 0.0))  # past an edge

    def test_a_disc_swept_onto_a_floor_stops_on_it_and_reports_a_point_1_cm_out(self):
        # The probe's sweep: the disc's lowest point stops 1.5 cm over the floor (the radii 0.015 + 0.01 less
        # the 1 cm skin); the point is the floor's witness pushed out by its 1 cm radius.
        world = c.StaticWorld([c.StaticShape(floor(1.0))])
        hit = c.scene_sweep(world, (0.0, 2.0, 0.0), (0.0, 0.5, 0.0), DISC, f32(0.015), 1)
        fraction, point, normal, shape, triangle = hit
        self.assertAlmostEqual(f32(2.0 - fraction * 1.5), 1.015, places=5)
        self.assertAlmostEqual(point[1], 1.01, places=6)
        self.assertAlmostEqual(normal[1], 1.0, places=6)
        self.assertIn(triangle, (0, 1))
        self.assertIs(shape, world.shapes[0])

    def test_the_sweep_takes_the_nearest_shape_and_turns_a_props_hit_back(self):
        # A prop's mesh is swept in its own frame: here a floor at local y 0.2, turned a quarter round y and
        # lifted 1 m, over the map's floor at 1 m.
        quarter = (0.0, f32(0.70710677), 0.0, f32(0.70710677))
        prop = c.StaticShape(floor(0.2, 0.5), ((0.3, 1.0, 0.0), quarter), order=1)
        world = c.StaticWorld([c.StaticShape(floor(1.0), order=0), prop])
        hit = c.scene_sweep(world, (0.0, 2.0, 0.0), (0.0, 0.5, 0.0), DISC, f32(0.015), 1)
        self.assertIs(hit[3], prop)
        self.assertAlmostEqual(hit[1][1], 1.21, places=5)
        self.assertAlmostEqual(hit[2][1], 1.0, places=5)
        self.assertIsNone(c.scene_sweep(world, (0.0, 2.0, 0.0), (0.0, 1.5, 0.0), DISC, f32(0.015), 1))


if __name__ == "__main__":
    unittest.main()
