"""Collision asset identities cover probe data, engine props, aliases, and validated lazy caches."""

import copy
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import collision, contacts
from ow174.game.collision_fingerprint import CollisionFingerprintError, WorldRegistry, world_fingerprint


def scene():
    vertices = [(-2.0, 0.0, -2.0), (-2.0, 0.0, 2.0), (2.0, 0.0, 2.0), (2.0, 0.0, -2.0)]
    mesh = contacts.MeshShape(vertices, [(0, 1, 2, -1, -1, 3, 24, 0), (0, 2, 3, 1, -1, -1, 24, 0)])
    hull = contacts.Hull((0.0, 0.0, 0.0), [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                         [(0.0, 0.0, 1.0, 0.0)], [0], [(0, 0, 0, 0)], 24)
    sphere = contacts.Sphere((0.0, 0.0, 0.0), 1.0, 24)
    capsule = contacts.Capsule((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), 0.5, 24)
    frame = ((3.0, 1.0, 4.0), (0.0, 0.0, 0.0, 1.0))
    shapes = [contacts.StaticShape(geometry, frame, index)
              for index, geometry in enumerate((mesh, hull, sphere, capsule, mesh))]
    engine = contacts.StaticWorld(shapes)
    return collision.World([
        (-2.0, 0.0, -2.0, -2.0, 0.0, 2.0, 2.0, 0.0, 2.0),
        (-2.0, 0.0, -2.0, 2.0, 0.0, 2.0, 2.0, 0.0, -2.0),
    ], name="explicit complete fixture", engine=engine)


def warm(world):
    world.column(0, 0)
    mesh = world.engine.shapes[0].geometry
    mesh.query((-2.1, -0.1, -2.1), (2.1, 0.1, 2.1), 24)
    mesh.triangle(0).neighbour_normals()
    return mesh


class FingerprintTests(unittest.TestCase):
    def test_fresh_objects_with_the_same_complete_asset_have_the_same_fingerprint(self):
        self.assertEqual(world_fingerprint(scene()), world_fingerprint(scene()))

    def test_query_cache_occupancy_is_validated_without_changing_the_asset_identity_or_caches(self):
        world = scene()
        cold = world_fingerprint(world)
        mesh = warm(world)
        columns = list(world.columns.items())
        triangles = dict(mesh.cache)
        boxes = dict(mesh.boxes)
        sides = mesh.cache[0].sides
        readonly = AssertionError("must stay read-only")
        with mock.patch.object(collision.World, "column", side_effect=readonly), \
             mock.patch.object(contacts.MeshShape, "triangle", side_effect=readonly), \
             mock.patch.object(contacts.MeshShape, "query", side_effect=readonly):
            self.assertEqual(world_fingerprint(world), cold)
        self.assertEqual(list(world.columns.items()), columns)
        self.assertEqual(mesh.cache, triangles)
        self.assertEqual(mesh.boxes, boxes)
        self.assertIs(mesh.cache[0].sides, sides)

    def test_independent_static_fields_change_the_asset_identity(self):
        mutations = {
            "world name": lambda w: setattr(w, "name", "different name"),
            "probe coordinates": lambda w: w.data.__setitem__(0, w.data[0] + 1.0),
            "probe mask": lambda w: w.masks.__setitem__(0, 8),
            "probe surface": lambda w: w.surfaces.__setitem__(0, 1),
            "probe lowest": lambda w: setattr(w, "lowest", -1.0),
            "probe grid": lambda w: w.grid[(0, 0)].append(0),
            "probe large": lambda w: w.large.append(0),
            "probe large tuples": lambda w: w.large_tuples.append(w._triangle(0)),
            "mesh coordinates": lambda w: w.engine.shapes[0].geometry.vertices.__setitem__(
                0, (0.0, 0.0, 0.0)),
            "mesh raw": lambda w: setattr(w.engine.shapes[0].geometry, "raw", [(0.0, 0.0, 0.0)]),
            "mesh scale": lambda w: setattr(w.engine.shapes[0].geometry, "scale", (2.0, 1.0, 1.0)),
            "mesh inverse scale": lambda w: setattr(w.engine.shapes[0].geometry, "inverse_scale",
                                                    (0.5, 1.0, 1.0)),
            "mesh mask": lambda w: setattr(w.engine.shapes[0].geometry, "mask", 8),
            "mesh corners": lambda w: w.engine.shapes[0].geometry.corners.__setitem__(0, 1),
            "mesh wing": lambda w: w.engine.shapes[0].geometry.corners.__setitem__(3, 2),
            "mesh triangle mask": lambda w: w.engine.shapes[0].geometry.masks.__setitem__(0, 8),
            "mesh triangle surface": lambda w: w.engine.shapes[0].geometry.surfaces.__setitem__(0, 1),
            "mesh bounds": lambda w: w.engine.shapes[0].geometry.bounds.__setitem__(0, 10.0),
            "mesh grid": lambda w: w.engine.shapes[0].geometry.grid[(0, 0)].append(0),
            "mesh large": lambda w: w.engine.shapes[0].geometry.large.append(0),
            "mesh low": lambda w: setattr(w.engine.shapes[0].geometry, "low", (1.0, 0.0, 0.0)),
            "mesh high": lambda w: setattr(w.engine.shapes[0].geometry, "high", (1.0, 1.0, 1.0)),
            "shape frame": lambda w: setattr(w.engine.shapes[0], "frame", None),
            "shape matrix": lambda w: setattr(w.engine.shapes[0], "matrix", None),
            "shape inverse": lambda w: setattr(w.engine.shapes[0], "inverse", None),
            "shape absolute": lambda w: setattr(w.engine.shapes[0], "absolute", None),
            "shape order": lambda w: setattr(w.engine.shapes[0], "order", 8),
            "shape mask": lambda w: setattr(w.engine.shapes[0], "mask", 8),
            "shape kind": lambda w: setattr(w.engine.shapes[0], "kind", 9),
            "shape fat low": lambda w: setattr(w.engine.shapes[0], "fat_low", (0.0, 0.0, 0.0)),
            "shape fat high": lambda w: setattr(w.engine.shapes[0], "fat_high", (0.0, 0.0, 0.0)),
            "static huge": lambda w: w.engine.huge.append(w.engine.shapes[0]),
            "static grid": lambda w: next(iter(w.engine.grid.values())).append(w.engine.shapes[0]),
            "hull centroid": lambda w: setattr(w.engine.shapes[1].geometry, "centroid", (1.0, 1.0, 1.0)),
            "hull vertices": lambda w: w.engine.shapes[1].geometry.vertices.append((1.0, 1.0, 1.0)),
            "hull planes": lambda w: w.engine.shapes[1].geometry.planes.append((1.0, 1.0, 1.0, 1.0)),
            "hull faces": lambda w: w.engine.shapes[1].geometry.faces.append(0),
            "hull edges": lambda w: w.engine.shapes[1].geometry.edges.append((1, 0, 0, 0)),
            "hull mask": lambda w: setattr(w.engine.shapes[1].geometry, "mask", 8),
            "sphere centre": lambda w: setattr(w.engine.shapes[2].geometry, "centre", (1.0, 0.0, 0.0)),
            "sphere radius": lambda w: setattr(w.engine.shapes[2].geometry, "radius", 2.0),
            "sphere mask": lambda w: setattr(w.engine.shapes[2].geometry, "mask", 8),
            "capsule first endpoint": lambda w: setattr(w.engine.shapes[3].geometry, "a", (0.0, -1.0, 0.0)),
            "capsule endpoint": lambda w: setattr(w.engine.shapes[3].geometry, "b", (0.0, 2.0, 0.0)),
            "capsule radius": lambda w: setattr(w.engine.shapes[3].geometry, "radius", 1.0),
            "capsule mask": lambda w: setattr(w.engine.shapes[3].geometry, "mask", 8),
        }
        baseline = world_fingerprint(scene())
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                world = scene()
                mutate(world)
                self.assertNotEqual(world_fingerprint(world)["sha256"], baseline["sha256"])

    def test_shape_aliases_and_geometry_sharing_are_part_of_the_identity(self):
        shared = scene()
        separated = scene()
        separated.engine.shapes[4].geometry = copy.deepcopy(separated.engine.shapes[4].geometry)
        self.assertNotEqual(world_fingerprint(shared)["sha256"], world_fingerprint(separated)["sha256"])
        shared.engine.shapes.append(shared.engine.shapes[0])
        separated = scene()
        separated.engine.shapes.append(contacts.StaticShape(separated.engine.shapes[0].geometry,
                                                           separated.engine.shapes[0].frame, 0))
        self.assertNotEqual(world_fingerprint(shared)["sha256"], world_fingerprint(separated)["sha256"])

    def test_float_bits_and_scalar_types_are_preserved(self):
        a, b = scene(), scene()
        a.lowest, b.lowest = 0.0, -0.0
        self.assertNotEqual(world_fingerprint(a)["sha256"], world_fingerprint(b)["sha256"])
        a.engine.shapes[0].order, b.engine.shapes[0].order = True, 1
        self.assertNotEqual(world_fingerprint(a)["sha256"], world_fingerprint(b)["sha256"])

    def test_unknown_types_fields_nonfinite_data_and_foreign_references_are_rejected(self):
        class ExtendedWorld(collision.World):
            pass

        cases = [
            lambda w: setattr(w, "unknown_asset_field", 1),
            lambda w: setattr(w.engine.shapes[0].geometry, "CELL", 5.0),
            lambda w: setattr(w.engine, "HUGE", 100),
            lambda w: w.data.__setitem__(0, math.nan),
            lambda w: setattr(w.engine.shapes[2].geometry, "radius", math.inf),
            lambda w: w.engine.huge.append(contacts.StaticShape(contacts.Sphere((0.0, 0.0, 0.0), 1.0))),
        ]
        for mutate in cases:
            world = scene()
            mutate(world)
            with self.assertRaises(CollisionFingerprintError):
                world_fingerprint(world)
        with self.assertRaises(CollisionFingerprintError):
            world_fingerprint(ExtendedWorld([]))

    def test_bad_lazy_caches_are_rejected_instead_of_hidden_by_the_asset_hash(self):
        def bad_column(world):
            world.columns[(0, 0)][0] = (0.0,) * 22

        def bad_triangle(world):
            world.engine.shapes[0].geometry.cache[0].normal = (1.0, 0.0, 0.0)

        def bad_sides(world):
            world.engine.shapes[0].geometry.cache[0].sides = ((1.0, 0.0, 0.0),) * 3

        def bad_box(world):
            world.engine.shapes[0].geometry.boxes[0] = ()

        for corrupt in (bad_column, bad_triangle, bad_sides, bad_box):
            with self.subTest(cache=corrupt.__name__):
                world = scene()
                warm(world)
                corrupt(world)
                with self.assertRaises(CollisionFingerprintError):
                    world_fingerprint(world)

    def test_an_already_held_world_lock_is_rejected_without_waiting(self):
        world = scene()
        with world.lock, self.assertRaises(CollisionFingerprintError):
            world_fingerprint(world)

    def test_lazy_cache_validation_compares_types_and_the_sign_of_zero(self):
        for index, replacement in ((collision.MINY, -0.0), (collision.SURFACE, False)):
            with self.subTest(index=index):
                world = scene()
                warm(world)
                column = world.columns[0, 0]
                triangle = list(column[0])
                self.assertEqual(triangle[index], replacement)
                triangle[index] = replacement
                column[0] = tuple(triangle)
                with self.assertRaisesRegex(CollisionFingerprintError, "column cache disagrees"):
                    world_fingerprint(world)

    def test_model_source_identity_is_separate_from_the_geometry_identity(self):
        world = scene()
        original = world_fingerprint(world)
        with mock.patch.object(Path, "read_bytes", return_value=b"a different model source"):
            changed = world_fingerprint(world)
        self.assertEqual(changed["sha256"], original["sha256"])
        self.assertNotEqual(changed["model_sha256"], original["model_sha256"])


class RegistryTests(unittest.TestCase):
    def test_resolution_returns_only_an_explicitly_registered_world(self):
        registry = WorldRegistry()
        world = scene()
        descriptor = registry.register(world)
        self.assertIs(registry.resolve(descriptor), world)
        with self.assertRaises(CollisionFingerprintError):
            WorldRegistry().resolve(descriptor)
        flat_registry = WorldRegistry()
        flat_registry.register(collision.flat_world(0.0))
        with self.assertRaises(CollisionFingerprintError):
            flat_registry.resolve(descriptor)

    def test_resolution_revalidates_the_registered_asset(self):
        registry = WorldRegistry()
        world = scene()
        descriptor = registry.register(world)
        world.engine.shapes[2].geometry.radius = 10.0
        with self.assertRaises(CollisionFingerprintError):
            registry.resolve(descriptor)

    def test_descriptors_are_strict_and_do_not_accept_bool_as_a_version(self):
        registry = WorldRegistry()
        descriptor = registry.register(scene())
        cases = [dict(descriptor, extra=1), dict(descriptor, cache_version=True),
                 dict(descriptor, sha256="A" * 64), dict(descriptor, model_sha256="bad")]
        for value in cases:
            with self.assertRaises(CollisionFingerprintError):
                registry.resolve(value)


if __name__ == "__main__":
    unittest.main()
