"""Strict collision poll order, actual World resolution and permanent faults, without game I/O."""

import copy
import dataclasses
import sys
import unittest
from contextlib import suppress
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import props
from ow174.game.collision import LoadState, StaticCollisionProvider, World, flat_world
from ow174.game.collision_fingerprint import WorldRegistry
from ow174.game.collision_tape import (
    SCHEMA,
    SCHEMA_V1,
    SCOPE,
    CollisionTapeError,
    RecordingCollisionProvider,
    ReplayCollisionProvider,
)
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.environment import SystemEnvironment
from ow174.game.match import Match
from ow174.game.server_journal import RecordingServerSession, _ReplaySession


def wall_world():
    return World([
        (-10, 0, -10, -10, 0, 10, 10, 0, 10),
        (-10, 0, -10, 10, 0, 10, 10, 0, -10),
        (-5, 0, 2, 5, 0, 2, 5, 4, 2),
        (-5, 0, 2, 5, 4, 2, -5, 4, 2),
    ], name="real fixture geometry, including a wall")


class SequenceSource:
    def __init__(self, *states):
        self.states = list(states)
        self.calls = []

    def poll(self, map_guid, name="", mode=None):
        self.calls.append((map_guid, name, mode))
        result = self.states.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def recorded(*states, keys=None):
    registry = WorldRegistry()
    source = SequenceSource(*states)
    recording = RecordingCollisionProvider(source, registry)
    for key in keys or [(1, "", None)] * len(states):
        recording.poll(*key)
    return recording.finish(), registry


class CollisionTapeTests(unittest.TestCase):
    def test_three_states_and_multiple_maps_share_one_ordered_sequence(self):
        world = wall_world()
        source = SequenceSource(LoadState("pending"), LoadState("ready", world),
                                LoadState("unavailable", error="no chunk"), LoadState("ready", world))
        registry = WorldRegistry()
        recorder = RecordingCollisionProvider(source, registry)
        keys = [(1, "地图 A", 11), (2, "", None), (1, "地图 A", 11), (2, "B", 22)]
        identities = {None: frozenset(), 11: frozenset({0xFF, 7}), 22: frozenset({8})}
        with mock.patch.object(props, "mode_identifiers", side_effect=lambda mode: identities[mode]):
            original = [recorder.poll(*key) for key in keys]
            tape = recorder.finish()
            recorder.assert_complete()
            self.assertEqual(source.calls, keys)
            self.assertEqual(recorder.read_count, 4)
            self.assertEqual(tape["scope"], SCOPE)
            self.assertEqual(tape["reads"][0]["key"]["mode_identifiers"], [7, 0xFF])
            replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
            actual = [replay.poll(*key) for key in keys]
            replay.assert_complete()
        self.assertEqual([state.status for state in actual], [state.status for state in original])
        self.assertEqual(actual[2].error, "no chunk")
        self.assertIs(actual[1].world, world)
        self.assertIs(actual[3].world, world)
        self.assertEqual((replay.position, replay.remaining), (4, 0))

    def test_v2_replays_identity_for_repeated_states_without_interning_equal_states(self):
        pending = LoadState("pending")
        ready = LoadState("ready", wall_world())
        equal_ready = dataclasses.replace(ready)
        unavailable = LoadState("unavailable", error="no chunk")
        tape, _ = recorded(pending, ready, equal_ready, pending, ready, unavailable, unavailable)
        self.assertEqual(tape["schema"], SCHEMA)
        self.assertEqual([row["state_ref"] for row in tape["reads"]], [0, 1, 2, 0, 1, 3, 3])

        resolver = mock.Mock(side_effect=lambda asset: wall_world())
        replay = ReplayCollisionProvider(tape, resolver=resolver)
        states = [replay.poll(1) for _ in tape["reads"]]
        replay.assert_complete()
        self.assertIs(states[0], states[3])
        self.assertIs(states[1], states[4])
        self.assertIsNot(states[1], states[2])
        self.assertIs(states[5], states[6])
        self.assertEqual(resolver.call_count, 3)

    def test_v1_replays_repeated_results_as_separate_load_states(self):
        ready = LoadState("ready", wall_world())
        tape, registry = recorded(ready, ready)
        tape["schema"] = SCHEMA_V1
        for read in tape["reads"]:
            del read["state_ref"]
        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        first, second = replay.poll(1), replay.poll(1)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        replay.assert_complete()

    def test_repeated_state_verifies_original_world_even_if_resolver_returns_fresh_copy(self):
        ready = LoadState("ready", wall_world())
        tape, _ = recorded(ready, ready)
        replay = ReplayCollisionProvider(tape, resolver=lambda asset: wall_world())
        first = replay.poll(1)
        first.world.data[0] += 0.125
        with self.assertRaisesRegex(CollisionTapeError, "World resolution failed") as failure:
            replay.poll(1)
        self.assertEqual(replay.position, 1)
        with self.assertRaises(CollisionTapeError) as completion:
            replay.assert_complete()
        self.assertIs(failure.exception, completion.exception)

    def test_detached_exports_and_replay_rows_do_not_share_mutable_tape_values(self):
        tape, registry = recorded(LoadState("ready", wall_world()))
        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        tape["reads"][0]["key"]["name"] = "changed"
        tape["reads"][0]["result"]["asset"]["sha256"] = "0" * 64
        self.assertIsNotNone(replay.poll(1).world)
        replay.assert_complete()

        recorder = RecordingCollisionProvider(SequenceSource(LoadState("pending")), WorldRegistry())
        recorder.poll(1)
        first = recorder.finish()
        first["reads"][0]["key"]["mode_identifiers"].append(99)
        second = recorder.finish()
        self.assertEqual(second["reads"][0]["key"]["mode_identifiers"], [])

    def test_explicit_empty_recording_can_complete(self):
        recorder = RecordingCollisionProvider(SequenceSource(), WorldRegistry())
        tape = recorder.finish()
        recorder.assert_complete()
        replay = ReplayCollisionProvider(tape, resolver=WorldRegistry().resolve)
        replay.assert_complete()
        self.assertIsNone(replay.fault)

    def test_source_and_resolver_must_be_explicit(self):
        with self.assertRaises(TypeError):
            RecordingCollisionProvider(None, WorldRegistry())
        with self.assertRaises(TypeError):
            RecordingCollisionProvider(SequenceSource(), None)
        with self.assertRaises(TypeError):
            ReplayCollisionProvider({}, resolver=None)

    def test_each_argument_and_derived_identifiers_must_match_before_consumption(self):
        tape, registry = recorded(LoadState("pending"), keys=[(10, "name", 7)])
        for key in [(11, "name", 7), (10, "Name", 7), (10, "name", 8)]:
            replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
            with self.assertRaises(CollisionTapeError):
                replay.poll(*key)
            self.assertEqual(replay.position, 0)
            fault = replay.fault
            with self.assertRaises(CollisionTapeError) as after:
                replay.poll(10, "name", 7)
            self.assertIs(after.exception, fault)
            with self.assertRaises(CollisionTapeError):
                replay.assert_complete()
        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        with mock.patch.object(props, "mode_identifiers", return_value=frozenset({99})), \
             self.assertRaises(CollisionTapeError):
            replay.poll(10, "name", 7)
        self.assertEqual(replay.position, 0)

    def test_exhaustion_and_early_completion_are_sticky_even_if_swallowed(self):
        tape, registry = recorded(LoadState("pending"))
        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        replay.poll(1)
        with self.assertRaises(CollisionTapeError):
            replay.poll(1)
        self.assertEqual(replay.position, 1)
        with self.assertRaises(CollisionTapeError):
            replay.assert_complete()

        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        with self.assertRaises(CollisionTapeError):
            replay.assert_complete()
        fault = replay.fault
        with self.assertRaises(CollisionTapeError) as after:
            replay.poll(1)
        self.assertIs(after.exception, fault)
        self.assertEqual(replay.position, 0)

    def test_recording_source_failure_or_invalid_state_cannot_finish_later(self):
        extended = LoadState("pending")
        object.__setattr__(extended, "future_field", 1)
        for result in [RuntimeError("private source detail"), None, object(), extended]:
            recorder = RecordingCollisionProvider(SequenceSource(result), WorldRegistry())
            with self.assertRaises(CollisionTapeError) as first:
                recorder.poll(1)
            self.assertNotIn("private source detail", str(first.exception))
            with self.assertRaises(CollisionTapeError) as after:
                recorder.finish()
            self.assertIs(after.exception, first.exception)
            with self.assertRaises(CollisionTapeError):
                recorder.assert_complete()
            self.assertEqual(recorder.read_count, 0)

    def test_recording_interruption_is_raised_and_permanently_remembered(self):
        recorder = RecordingCollisionProvider(SequenceSource(KeyboardInterrupt()), WorldRegistry())
        with self.assertRaises(KeyboardInterrupt):
            recorder.poll(1)
        self.assertIsNotNone(recorder.fault)
        with self.assertRaises(CollisionTapeError):
            recorder.finish()

    def test_invalid_call_or_changed_mode_table_faults_recording(self):
        for key in [(True, "", None), (-1, "", None), (1 << 64, "", None),
                    (1, None, None), (1, "", False), (1, "", -1)]:
            source = SequenceSource(LoadState("pending"))
            recorder = RecordingCollisionProvider(source, WorldRegistry())
            with self.assertRaises(CollisionTapeError):
                recorder.poll(*key)
            self.assertEqual(source.calls, [])
            self.assertEqual(recorder.read_count, 0)
        recorder = RecordingCollisionProvider(SequenceSource(LoadState("pending")), WorldRegistry())
        with mock.patch.object(props, "mode_identifiers", side_effect=[frozenset({1}), frozenset({2})]), \
             self.assertRaises(CollisionTapeError):
            recorder.poll(1)
        self.assertEqual(recorder.read_count, 0)

    def test_recording_reentrant_read_or_finish_cannot_be_swallowed_by_source(self):
        for action in ("poll", "finish"):
            source = mock.Mock()
            recorder = RecordingCollisionProvider(source, WorldRegistry())

            def read(*args, recorder=recorder, action=action):
                with suppress(CollisionTapeError):
                    recorder.poll(1) if action == "poll" else recorder.finish()
                return LoadState("pending")

            source.poll.side_effect = read
            with self.assertRaises(CollisionTapeError):
                recorder.poll(1)
            self.assertEqual(recorder.read_count, 0)
            with self.assertRaises(CollisionTapeError):
                recorder.finish()

    def test_poll_after_finish_and_missing_recording_end_are_permanent_faults(self):
        recorder = RecordingCollisionProvider(SequenceSource(LoadState("pending")), WorldRegistry())
        recorder.finish()
        with self.assertRaises(CollisionTapeError):
            recorder.poll(1)
        with self.assertRaises(CollisionTapeError):
            recorder.assert_complete()

        recorder = RecordingCollisionProvider(SequenceSource(), WorldRegistry())
        with self.assertRaises(CollisionTapeError):
            recorder.assert_complete()
        with self.assertRaises(CollisionTapeError):
            recorder.finish()

    def test_unknown_assets_and_flat_substitutes_fail_without_consuming(self):
        tape, _ = recorded(LoadState("ready", wall_world()))
        resolvers = [WorldRegistry().resolve, lambda asset: flat_world(0.0), lambda asset: None]
        for resolver in resolvers:
            replay = ReplayCollisionProvider(tape, resolver=resolver)
            with self.assertRaises(CollisionTapeError):
                replay.poll(1)
            self.assertEqual(replay.position, 0)
            self.assertIsNotNone(replay.fault)
            with self.assertRaises(CollisionTapeError):
                replay.assert_complete()

    def test_resolver_failure_interruption_or_reentrancy_stays_faulty(self):
        world = wall_world()
        tape, registry = recorded(LoadState("ready", world))
        resolver = mock.Mock(side_effect=RuntimeError("do not repeat this"))
        replay = ReplayCollisionProvider(tape, resolver=resolver)
        with self.assertRaises(CollisionTapeError):
            replay.poll(1)
        resolver.side_effect = None
        resolver.return_value = world
        with self.assertRaises(CollisionTapeError):
            replay.poll(1)
        self.assertEqual(resolver.call_count, 1)

        replay = ReplayCollisionProvider(tape, resolver=mock.Mock(side_effect=KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            replay.poll(1)
        with self.assertRaises(CollisionTapeError):
            replay.assert_complete()

        def reentrant(asset):
            with suppress(CollisionTapeError):
                replay.poll(1)
            return registry.resolve(asset)

        replay = ReplayCollisionProvider(tape, resolver=reentrant)
        with self.assertRaises(CollisionTapeError):
            replay.poll(1)
        self.assertEqual(replay.position, 0)

    def test_mutated_registered_world_is_not_accepted_as_the_same_asset(self):
        world = wall_world()
        tape, registry = recorded(LoadState("ready", world))
        world.data[0] += 0.125
        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        with self.assertRaises(CollisionTapeError):
            replay.poll(1)
        self.assertEqual(replay.position, 0)

    def test_malformed_schema_rows_keys_states_assets_and_end_are_rejected(self):
        tape, registry = recorded(LoadState("ready", wall_world()))
        cases = []

        def change(path, value):
            item = copy.deepcopy(tape)
            target = item
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            cases.append(item)

        for field in ("schema", "build", "scope"):
            change([field], "other")
        change(["reads"], ())
        change(["end", "seq"], True)
        change(["end", "seq"], 2)
        change(["end", "status"], "incomplete")
        change(["end", "extra"], 1)
        change(["reads", 0, "seq"], False)
        change(["reads", 0, "operation"], "world_for")
        change(["reads", 0, "state_ref"], False)
        change(["reads", 0, "state_ref"], 1)
        change(["reads", 0, "state_ref"], -1)
        change(["reads", 0, "key", "map_guid"], True)
        change(["reads", 0, "key", "mode"], False)
        change(["reads", 0, "key", "name"], None)
        change(["reads", 0, "key", "mode_identifiers"], [1, 1])
        change(["reads", 0, "key", "mode_identifiers"], [2, 1])
        change(["reads", 0, "key", "mode_identifiers"], [True])
        change(["reads", 0, "result", "status"], "unknown")
        change(["reads", 0, "result", "error"], "unexpected ready error")
        change(["reads", 0, "result", "asset"], None)
        change(["reads", 0, "result", "asset", "sha256"], "A" * 64)
        change(["reads", 0, "result", "asset", "model_sha256"], "0")
        change(["reads", 0, "result", "asset", "cache_version"], True)
        change(["reads", 0, "result", "asset", "extra"], 1)
        item = copy.deepcopy(tape)
        del item["end"]
        cases.append(item)
        for malformed in cases:
            with self.subTest(malformed=malformed), self.assertRaises(CollisionTapeError):
                ReplayCollisionProvider(malformed, resolver=registry.resolve)

    def test_reused_state_reference_cannot_change_result(self):
        tape, registry = recorded(LoadState("unavailable", error="first"),
                                  LoadState("unavailable", error="second"))
        tape["reads"][1]["state_ref"] = tape["reads"][0]["state_ref"]
        with self.assertRaisesRegex(CollisionTapeError, "state reference changed result"):
            ReplayCollisionProvider(tape, resolver=registry.resolve)

    def test_shared_provider_is_usable_by_real_match_constructors_without_default_changes(self):
        world = wall_world()
        registry = WorldRegistry()
        recorder = RecordingCollisionProvider(StaticCollisionProvider(world), registry)
        game_maps = [dataclasses.replace(PRACTICE_RANGE, map_guid=0x0800000000FFEE00 + index)
                     for index in range(2)]
        original = [Match(game_map, collision_provider=recorder) for game_map in game_maps]
        tape = recorder.finish()
        self.assertEqual(recorder.read_count, 2)
        replay = ReplayCollisionProvider(tape, resolver=registry.resolve)
        repeated = [Match(game_map, collision_provider=replay) for game_map in game_maps]
        replay.assert_complete()
        for first, second in zip(original, repeated, strict=True):
            self.assertIs(first.collision_world, world)
            self.assertIs(second.collision_world, world)

    def test_shared_load_state_replays_two_registered_matches(self):
        world = wall_world()
        game_map = dataclasses.replace(
            PRACTICE_RANGE, name="controlled shared collision state", map_guid=0x0800000000FFEE00,
            placeables=(),
        )
        session = RecordingServerSession(
            environment=SystemEnvironment(), collision_provider=StaticCollisionProvider(world),
            verify_stored_state=True,
        )
        with mock.patch("socket.socket", side_effect=AssertionError("must not create a socket")):
            session.create_match(game_map, [(1, "First", SOLDIER, 0, False)])
            session.create_match(game_map, [(2, "Second", SOLDIER, 0, False)])
            first, second = session.server.matches
            self.assertIs(first.collision_state, second.collision_state)
            replay = _ReplaySession(session.finish(), session.registry.resolve)
            self.assertTrue(replay.run()["stored_state_verified"])
        first, second = replay.server.matches
        self.assertIs(first.collision_state, second.collision_state)
        self.assertIs(first.collision_world, world)


if __name__ == "__main__":
    unittest.main()
