"""Read-only, complete stored mover/contact snapshots on explicit artificial physics scenes."""

import dataclasses
import json
import math
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_mover import FLOOR, wall

from ow174.game import collision, contacts, replay_values
from ow174.game.collision import MOVER, World
from ow174.game.collision_fingerprint import world_fingerprint
from ow174.game.commands import JUMP, Command
from ow174.game.mover import ADD, SPEED, Mod, Mover
from ow174.game.physics_snapshot import (
    BODY_FIELDS,
    MOVER_FIELDS,
    PhysicsSnapshotError,
    _Graph,
    _StaticReferences,
    snapshot_mover,
    snapshot_mover_with_identities,
)


def scene(walk=True):
    world = World(FLOOR + wall(-4, 2, 4, 2), name="explicit physics snapshot scene")
    mover = Mover((0.0, 1.0, 0.0), world=world)
    if walk:
        for frame in range(1, 30):
            mover.step(Command(frame, forward=127), .016, frame)
    return mover


def state(document):
    return replay_values.decode(document["state"])


def class_nodes(document, name):
    return [node for node in state(document)["nodes"] if node["class"] == name]


class PhysicsSnapshotTests(unittest.TestCase):
    def test_cold_mover_keeps_all_fields_and_does_not_create_its_body(self):
        mover = scene(False)
        document = snapshot_mover(mover)
        json.dumps(document, allow_nan=False)
        self.assertIsNone(mover.body)
        self.assertEqual(document["scope"], "stored-mover-physics-only")
        self.assertEqual(document["world_validation"], "fingerprinted")
        stored = dict(class_nodes(document, "Mover")[0]["fields"])
        self.assertEqual(set(stored), set(vars(mover)))
        self.assertEqual(tuple(stored), MOVER_FIELDS)
        self.assertEqual(stored["world"], {"static": "world"})
        self.assertIsNone(stored["body"])
        self.assertEqual(stored["data"], stored["values"])

    def test_real_warm_contact_state_is_stable_and_full_body_fields_are_present(self):
        first, second = scene(), scene()
        self.assertTrue(first.body.contacts)
        self.assertTrue(any(len(entry) == 5 for c in first.body.contacts for entry in c.candidates))
        document = snapshot_mover(first)
        self.assertEqual(document, snapshot_mover(first))
        self.assertEqual(document, snapshot_mover(second))
        body = dict(class_nodes(document, "CharacterBody")[0]["fields"])
        self.assertEqual(tuple(body), BODY_FIELDS)
        self.assertEqual(set(body), set(vars(first.body)))
        self.assertEqual(body["world"], {"static": "engine"})
        self.assertTrue(class_nodes(document, "Manifold"))
        self.assertTrue(class_nodes(document, "Point"))
        contact = class_nodes(document, "MeshContact")[0]
        self.assertEqual(contact["class_values"], {"sign": 1.0})
        self.assertEqual([reference["index"] for reference in contact["candidate_triangles"]],
                         [entry[0] for entry in first.body.contacts[0].candidates])

    def test_explicit_descriptor_skips_large_world_hash_and_never_calls_physics_methods(self):
        mover = scene()
        descriptor = world_fingerprint(mover.world)
        before = snapshot_mover(mover, world_descriptor=descriptor)
        mesh = mover.body.contacts[0].mesh
        cache_sizes = (len(mover.world.columns), len(mesh.cache), len(mesh.boxes))
        with (
            mock.patch("ow174.game.physics_snapshot.world_fingerprint", side_effect=AssertionError("hash")),
            mock.patch.object(Mover, "snapshot", side_effect=AssertionError("no public snapshot")),
            mock.patch.object(Mover, "step", side_effect=AssertionError("no step")),
            mock.patch.object(contacts.CharacterBody, "move", side_effect=AssertionError("no move")),
            mock.patch.object(contacts, "scene_sweep", side_effect=AssertionError("no sweep")),
            mock.patch.object(World, "column", side_effect=AssertionError("no warming")),
            mock.patch.object(contacts.MeshShape, "triangle", side_effect=AssertionError("triangle")),
        ):
            self.assertEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
        self.assertEqual(cache_sizes, (len(mover.world.columns), len(mesh.cache), len(mesh.boxes)))

    def test_memo_gjk_and_body_budget_changes_are_visible_without_pose_changes(self):
        mover = scene()
        descriptor = world_fingerprint(mover.world)
        before = snapshot_mover(mover, world_descriptor=descriptor)
        pose = (tuple(mover.position), mover.velocity)
        body = mover.body
        changes = [(body, "budget", body.budget + 1), (body, "moved", not body.moved),
                   (body.contacts[0], "box", ((-9.0, -9.0, -9.0), (9.0, 9.0, 9.0)))]
        for value, attribute, replacement in changes:
            original = getattr(value, attribute)
            setattr(value, attribute, replacement)
            self.assertNotEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
            setattr(value, attribute, original)
            self.assertEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
        entry = body.contacts[0].candidates[0]
        old_cache = entry[1]
        entry[1] = (old_cache[0] + .25, old_cache[1], old_cache[2])
        self.assertNotEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
        entry[1] = old_cache
        manifold = entry[4]
        old_gap = manifold.gap
        manifold.gap = .125 if old_gap is None else old_gap + .125
        self.assertNotEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
        manifold.gap = old_gap
        self.assertEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
        self.assertEqual(pose, (tuple(mover.position), mover.velocity))

    def test_convex_cache_first_sign_memo_and_shared_manifold_alias_are_retained(self):
        world = World(FLOOR)
        sphere = contacts.StaticShape(contacts.Sphere((0.0, 1.5, 0.0), .5, MOVER))
        world.engine = contacts.StaticWorld([sphere])
        mover = Mover((0.0, 1.0, 0.0), world=world)
        body = contacts.CharacterBody(.5, 1.5, world=world.engine)
        contact = contacts.ConvexContact(sphere)
        point = contacts.Point((0.0, 1.5, 0.0), -.02, (0, 1, 2, 3, 4))
        manifold = contacts.Manifold((0.0, 1.0, 0.0), [point, point])
        contact.cache = (.25, (0, 255, 255, 255), (0, 0, 0, 0))
        contact.memo = ((body.position, body.radius, body.top), contacts.NO_CACHE,
                        [manifold, manifold], .25)
        body.contacts = [contact, contact]
        mover.body = body
        descriptor = world_fingerprint(world)
        before = snapshot_mover(mover, world_descriptor=descriptor)
        nodes = state(before)["nodes"]
        self.assertEqual(len(class_nodes(before, "ConvexContact")), 1)
        self.assertEqual(len(class_nodes(before, "Manifold")), 1)
        self.assertEqual(len(class_nodes(before, "Point")), 1)
        fields = dict(class_nodes(before, "ConvexContact")[0]["fields"])
        self.assertFalse(fields["first"])
        self.assertEqual(fields["sign"], -1.0)
        self.assertIsNotNone(fields["memo"])
        self.assertTrue(any(node.get("items") and len(node["items"]) == 2
                            and node["items"][0] == node["items"][1] for node in nodes))
        contact.first = True
        self.assertNotEqual(before, snapshot_mover(mover, world_descriptor=descriptor))

    def test_history_mods_last_command_and_data_values_identity_are_not_collapsed(self):
        mover = scene()
        descriptor = world_fingerprint(mover.world)
        mover.mods[(3, 4, 0)] = Mod(SPEED, ADD, .5, 20, None, 2.0, (3, 4, 0))
        mover.mods[(3, 5, 0)] = Mod(SPEED, ADD, .25, 21, 35, 1.0, (3, 5, 0))
        before = snapshot_mover(mover, world_descriptor=descriptor)
        self.assertEqual(class_nodes(before, "deque")[0]["maxlen"], mover.history.maxlen)
        self.assertEqual(len(class_nodes(before, "Snapshot")), len(mover.history))
        self.assertEqual(len(class_nodes(before, "Mod")), 2)
        self.assertEqual(len(class_nodes(before, "Command")), 1)
        mover.values = dataclasses.replace(mover.data)
        separate = snapshot_mover(mover, world_descriptor=descriptor)
        fields = dict(class_nodes(separate, "Mover")[0]["fields"])
        self.assertNotEqual(fields["data"], fields["values"])
        self.assertNotEqual(before, separate)
        mover.values = mover.data
        mover.mods = dict(reversed(list(mover.mods.items())))
        self.assertNotEqual(before, snapshot_mover(mover, world_descriptor=descriptor))
        mover.mods = dict(reversed(list(mover.mods.items())))
        mover.history = deque(mover.history, maxlen=32)
        self.assertNotEqual(before, snapshot_mover(mover, world_descriptor=descriptor))

    def test_static_shape_geometry_and_cached_triangle_references_match_asset_numbering(self):
        source = World(FLOOR)
        mesh = source.engine.shapes[0].geometry
        first = contacts.StaticShape(mesh, order=0)
        second = contacts.StaticShape(mesh, order=1)
        source.engine = contacts.StaticWorld([first, first, second])
        mover = Mover((0.0, 1.0, 0.0), world=source)
        mover.body = contacts.CharacterBody(.5, 1.5, world=source.engine,
                                            contacts=[contacts.MeshContact(first, MOVER),
                                                      contacts.MeshContact(second, MOVER)])
        mesh.triangle(0)
        descriptor = world_fingerprint(source)
        document = snapshot_mover(mover, world_descriptor=descriptor)
        records = class_nodes(document, "MeshContact")
        self.assertEqual([dict(record["fields"])["shape"] for record in records],
                         [{"static": "shape", "index": 0}, {"static": "shape", "index": 1}])
        self.assertEqual([dict(record["fields"])["mesh"] for record in records],
                         [{"static": "geometry", "index": 0}] * 2)
        refs = _StaticReferences(source)
        self.assertEqual(refs.reference(mesh.cache[0]), {"static": "triangle", "geometry": 0, "index": 0})

    def test_foreign_world_shape_mesh_and_invalid_candidate_references_are_rejected(self):
        for change in ("world", "shape", "mesh", "triangle"):
            mover = scene()
            descriptor = world_fingerprint(mover.world)
            if change == "world":
                mover.body.world = contacts.StaticWorld([])
            elif change == "shape":
                mover.body.contacts[0].shape = contacts.StaticShape(mover.body.contacts[0].mesh)
            elif change == "mesh":
                mover.body.contacts[0].mesh = World(FLOOR).engine.shapes[0].geometry
            else:
                mover.body.contacts[0].candidates[0][0] = len(mover.body.contacts[0].mesh.masks)
            with self.subTest(change=change), self.assertRaises(PhysicsSnapshotError):
                snapshot_mover(mover, world_descriptor=descriptor)

    def test_unknown_fields_types_subclasses_and_nonfinite_data_fail_without_user_hooks(self):
        class Unknown:
            def __repr__(self):
                raise AssertionError("snapshot must not call user repr")

        mover = scene(False)
        descriptor = world_fingerprint(mover.world)
        mover.future = 1
        with self.assertRaises(PhysicsSnapshotError):
            snapshot_mover(mover, world_descriptor=descriptor)
        del mover.future
        mover.mods[(1, 2, 3)] = Unknown()
        with self.assertRaises(PhysicsSnapshotError):
            snapshot_mover(mover, world_descriptor=descriptor)
        mover.mods.clear()
        for bad in (math.inf, -math.inf, math.nan):
            mover.spring = bad
            with self.assertRaises(PhysicsSnapshotError):
                snapshot_mover(mover, world_descriptor=descriptor)

    def test_signed_zero_integer_float_and_detached_output_remain_distinct(self):
        mover = scene(False)
        descriptor = world_fingerprint(mover.world)
        mover.spring = -0.0
        negative = snapshot_mover(mover, world_descriptor=descriptor)
        mover.spring = 0.0
        self.assertNotEqual(negative, snapshot_mover(mover, world_descriptor=descriptor))
        mover.spring = 0
        self.assertNotEqual(negative, snapshot_mover(mover, world_descriptor=descriptor))
        before = json.dumps(negative)
        mover.position[0] = 3.0
        descriptor["sha256"] = "0" * 64
        self.assertEqual(json.dumps(negative), before)

    def test_known_local_physics_records_and_positive_infinite_gjk_distance_are_explicit(self):
        mover = scene(False)
        distance = contacts.gjk([(0.0, 0.0, 0.0)], [(100.0, 0.0, 0.0)], reach=1.0)
        self.assertEqual(distance.distance, math.inf)
        ground = collision.Ground.at((0.0, 1.0, 0.0), (0.0, 1.0, 0.0))
        constraint = contacts.Constraint((0.0, 1.0, 0.0), 1.0, contacts.WORLD_CAP, False)
        graph = _Graph(_StaticReferences(mover.world))
        graph.value([distance, ground, constraint])
        decoded = replay_values.decode(replay_values.encode(graph.nodes))
        row = next(node for node in decoded if node["class"] == "Distance")
        self.assertEqual(dict(row["fields"])["distance"],
                         {"ieee754_sentinel": "gjk.distance.beyond_reach",
                          "float64_bits": "7ff0000000000000"})
        self.assertEqual({node["class"] for node in decoded} & {"Distance", "Ground", "Constraint"},
                         {"Distance", "Ground", "Constraint"})
        for bad in (-math.inf, math.nan):
            distance.distance = bad
            graph = _Graph(_StaticReferences(mover.world))
            graph.value(distance)
            with self.assertRaises(replay_values.ReplayValueError):
                replay_values.encode(graph.nodes)

    def test_standing_jump_and_both_candidate_layouts_export_without_simulation(self):
        mover = scene(False)
        descriptor = world_fingerprint(mover.world)
        for frame in range(1, 45):
            mover.step(Command(frame, buttons=JUMP if frame == 1 else 0), .016, frame)
            if frame in (1, 16, 44):
                snapshot_mover(mover, world_descriptor=descriptor)
        mover = scene()
        descriptor = world_fingerprint(mover.world)
        contact = mover.body.contacts[0]
        contact.candidates[0][:] = contact.candidates[0][:2]
        snapshot_mover(mover, world_descriptor=descriptor)

    def test_descriptor_structure_is_checked_even_when_asset_hash_is_delegated(self):
        mover = scene(False)
        descriptor = world_fingerprint(mover.world)
        for name, value in (("cache_version", True), ("schema", "other"), ("sha256", "A" * 64),
                            ("model_sha256", "bad"), ("extra", 1)):
            malformed = {**descriptor, name: value}
            with self.subTest(name=name), self.assertRaises(PhysicsSnapshotError):
                snapshot_mover(mover, world_descriptor=malformed)

    def test_default_asset_validation_failure_uses_the_physics_snapshot_error_boundary(self):
        mover = scene(False)
        mover.world.data[0] = math.nan
        with self.assertRaises(PhysicsSnapshotError):
            snapshot_mover(mover)

    def test_identity_capture_matches_the_document_and_references_existing_nodes_once(self):
        mover = scene()
        expected = snapshot_mover(mover)
        target = "ow174.game.physics_snapshot.world_fingerprint"
        with mock.patch(target, wraps=world_fingerprint) as fingerprint:
            document, identities = snapshot_mover_with_identities(mover)
        fingerprint.assert_called_once_with(mover.world)
        self.assertEqual(document, expected)
        self.assertIs(type(identities), tuple)
        self.assertEqual([number for number, _ in identities], list(range(len(state(document)["nodes"]))))
        self.assertIs(identities[0][1], mover)
        self.assertEqual(len({id(obj) for _, obj in identities}), len(identities))
        referenced = {id(obj): obj for _, obj in identities}
        self.assertIs(referenced[id(mover.body)], mover.body)
        self.assertIs(referenced[id(mover.body.contacts)], mover.body.contacts)
        self.assertIs(referenced[id(mover.body.contacts[0])], mover.body.contacts[0])
        self.assertNotIn(id(mover.world), referenced)
        self.assertNotIn(id(mover.world.engine), referenced)

    def test_identity_capture_with_a_descriptor_does_not_allocate_or_advance_a_body(self):
        mover = scene(False)
        descriptor = world_fingerprint(mover.world)
        expected = snapshot_mover(mover, world_descriptor=descriptor)
        with (
            mock.patch("ow174.game.physics_snapshot.world_fingerprint", side_effect=AssertionError("hash")),
            mock.patch.object(Mover, "_engine_body", side_effect=AssertionError("body allocation")),
            mock.patch.object(Mover, "step", side_effect=AssertionError("step")),
        ):
            document, identities = snapshot_mover_with_identities(mover, world_descriptor=descriptor)
        self.assertEqual(document, expected)
        self.assertIsNone(mover.body)
        self.assertFalse(any(type(obj) is contacts.CharacterBody for _, obj in identities))
        self.assertEqual(expected, snapshot_mover(mover, world_descriptor=descriptor))


if __name__ == "__main__":
    unittest.main()
