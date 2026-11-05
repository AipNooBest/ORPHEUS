"""Run two authored fixed-target MoveTo states through the player's command and collision path."""

import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_game_mover import FLOOR, wall
from test_game_notices import into_world

from ow174.game import bodytrace, movelog
from ow174.game.collision import StaticCollisionProvider, World, flat_world
from ow174.game.commands import Command
from ow174.game.content import PRACTICE_RANGE, Spawn, heroes
from ow174.game.match import Match
from ow174.game.mover import STEP
from ow174.game.script import runtime
from ow174.game.script.expr import INSTANCE, Entity, Vec3

START = 100
DESTINATION = Vec3(0.0, 1.0, 3.0)
SCENE = replace(PRACTICE_RANGE, spawns=(Spawn(None, (0.0, 1.0, 0.0), 0.0),))
AUTHORED_STATES = (
    (0x02E0000000000029, 0x01DB, 1, 8041, 50.0),  # Genji Swift Strike
    (0x02E0000000000004, 0x006E, 0, 431, 17.0),  # Mercy Guardian Angel's default speed branch
)


class FixedTargetMoveToTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(bodytrace, "ENABLED", False))
        self.enterContext(patch.object(movelog, "ENABLED", False))

    def spawn(self, hero_guid, graph_id, geometry):
        hero = heroes()[hero_guid]
        match = Match(SCENE, collision_provider=StaticCollisionProvider(geometry))
        player = match.add_player(1, hero.name, hero.guid, 0, False)
        into_world(match, player, START)
        self.assertTrue(match.switch_hero(player, hero))
        instances = player.body_script.component.instances.values()
        instance = next(instance for instance in instances if instance.graph.index == graph_id)
        return player, instance

    def start(self, case, geometry, destination=DESTINATION):
        hero_guid, graph_id, state_index, target_var, speed = case
        player, instance = self.spawn(hero_guid, graph_id, geometry)
        state = instance.state(state_index)
        self.assertEqual(state.node.cls, "STUStatescriptStateMoveTo")
        self.assertIsNone(state.node.fields["m_destinationEntity"])
        self.assertIsNone(state.node.fields["m_destinationBone"])
        # The ray and ally selection use independent engine queries. Supply only their resolved endpoint.
        instance.write((INSTANCE, target_var), destination)
        self.assertEqual(state.evaluate(state.node.fields["m_865F3D01"]), destination)
        self.assertEqual(state.evaluate(state.node.fields["m_speed"]), speed)
        ended = self.enterContext(patch.object(state, "on_end", wraps=state.on_end))
        state.request(runtime.BEGIN)
        instance.component.run_now()
        self.assertTrue(state.active)
        self.assertEqual(player.mover.position, [0.0, 1.0, 0.0])
        return player, state, ended

    @staticmethod
    def commands(player, frames):
        for frame in frames:
            player.match.tick = frame
            player.take_commands([Command(frame)])
        return list(player.mover.history)

    def test_genji_first_shift_reads_unit_scaled_cast_length(self):
        hero_guid, graph_id, state_index, _, speed = AUTHORED_STATES[0]
        player, instance = self.spawn(hero_guid, graph_id, flat_world(1.0))
        self.assertEqual(instance.vars[1648].value(), 15.0)
        cast_lengths = []

        def observe_ray(caller, node, _state):
            if caller is instance:
                cast_lengths.append(caller.evaluate(node.field("m_castLength")))
            caller.follow(node, "m_outPlug", None)
            return True

        with patch.dict(runtime.HANDLERS, {"STU_08D6BA6E": observe_ray}):
            player.match.tick = START + 1
            player.take_commands([Command(START + 1, buttons=0x08)])

        self.assertEqual(cast_lengths, [15.0])
        state = instance.state(state_index)
        self.assertTrue(state.active)
        self.assertEqual(state.evaluate(state.node.field("m_speed")), speed)
        self.assertIsNone(state.move_request())

    def test_genji_authored_miss_uses_the_world_body_position(self):
        hero_guid, graph_id, state_index, _, speed = AUTHORED_STATES[0]
        origin = Vec3(0.0, 2.2, 0.0)
        direction = Vec3(0.0, 0.0, 1.0)
        hit_point = Vec3(0.0, 1.0, 3.0)
        for point, expected in ((None, Vec3(0.0, 1.0, 15.0)), (hit_point, hit_point)):
            with self.subTest(hit=point is not None):
                player, instance = self.spawn(hero_guid, graph_id, flat_world(1.0))
                world_class = type(instance.component.world)
                native = world_class.native

                def resolve_direction(world, cfg, reader, *, source=instance, original=native):
                    if reader is source:
                        if cfg.get("$") == "STU_21A13145":
                            return origin
                        if cfg.get("$") == "STU_2200A66D":
                            return direction
                    return original(world, cfg, reader)

                def complete_ray(world, request, *, point=point):
                    self.assertEqual(
                        (request.origin, request.direction, request.length), (origin, direction, 15.0),
                    )
                    self.assertEqual(request.ignored_entities, (world.body,))
                    return runtime.RaycastResult(point is not None, None, point, None)

                with (
                    patch.object(world_class, "native", resolve_direction),
                    patch.object(world_class, "raycast", complete_ray),
                ):
                    player.match.tick = START + 1
                    player.take_commands([Command(START + 1, buttons=0x08)])
                self.assertEqual(instance.state(state_index).move_request(), (expected, speed))

    def test_entity_position_tracks_the_current_body_and_clears_on_retirement(self):
        hero_guid, graph_id, *_ = AUTHORED_STATES[0]
        player, instance = self.spawn(hero_guid, graph_id, flat_world(1.0))
        match = player.match
        script_world = instance.component.world
        other = match.add_player(2, "Other", hero_guid, 0, False)
        into_world(match, other, START)
        self.assertTrue(match.switch_hero(other, heroes()[hero_guid]))
        bot = match.bots[0]

        self.assertEqual(script_world.position(player.body), Vec3(0.0, 1.0, 0.0))
        self.assertEqual(script_world.position(bot.entity), Vec3(*bot.position))
        other.mover.position[:] = (3.0, 1.0, 8.0)
        self.assertEqual(script_world.position(other.body), Vec3(3.0, 1.0, 8.0))
        other.spawned = False
        self.assertEqual(script_world.position(other.body), Vec3(0.0, 0.0, 0.0))
        self.assertEqual(other.body_script.world.position(other.body), Vec3(3.0, 1.0, 8.0))
        other.spawned = True

        old_body = other.body
        self.assertTrue(match.switch_hero(other, heroes()[hero_guid]))
        self.assertEqual(script_world.position(old_body), Vec3(0.0, 0.0, 0.0))
        self.assertEqual(script_world.position(other.body), Vec3(*other.spawn[0]))
        match.stop_body_script(player)
        self.assertEqual(script_world.position(player.body), Vec3(0.0, 0.0, 0.0))

    def test_entity_scale_requires_own_body_and_unit_override(self):
        hero_guid, graph_id, *_ = AUTHORED_STATES[0]
        _, instance = self.spawn(hero_guid, graph_id, flat_world(1.0))
        owner_scale = {"$": "STU_FDBB0B67", "m_entity": {"$": "STU_B5A0CAF0"}}
        dynamic_scale = {
            "$": "STU_FDBB0B67",
            "m_entity": {"$": "STUConfigVarDynamic", "m_identifier": 38, "m_60DB8F99": INSTANCE},
        }
        self.assertEqual(instance.evaluate(owner_scale), 1.0)
        self.assertIsNone(instance.evaluate(dynamic_scale))
        instance.write((INSTANCE, 38), Entity(instance.component.entity + 1))
        self.assertIsNone(instance.evaluate(dynamic_scale))
        instance.write((INSTANCE, 38), Entity(instance.component.entity))
        self.assertEqual(instance.evaluate(dynamic_scale), 1.0)

        for override, result in ((0.5, None), (True, None), (1.0, 1.0), (None, 1.0)):
            with self.subTest(override=override):
                instance.component.set(31210, override)
                self.assertEqual(instance.evaluate(owner_scale), result)

    def test_both_authored_states_reach_and_hold_the_resolved_endpoint(self):
        for case in AUTHORED_STATES:
            with self.subTest(graph=f"{case[1]:04X}"):
                player, state, ended = self.start(case, flat_world(1.0))
                path = self.commands(player, range(START + 1, START + 26))
                self.assertGreater(path[0].position[2], 0.0)
                maximum = case[-1] * player.quanta / 1_000_000 + STEP
                last = 0.0
                for pose in path:
                    x, y, z = pose.position
                    self.assertEqual((x, y), (0.0, 1.0))
                    self.assertLessEqual(z, DESTINATION.z)
                    self.assertGreaterEqual(z - last, 0.0)
                    self.assertLessEqual(z - last, maximum)
                    last = z
                self.assertEqual([pose.position for pose in path[-4:]], [(0.0, 1.0, 3.0)] * 4)
                self.assertEqual(path[-1].velocity, (0.0, 0.0, 0.0))
                self.assertFalse(state.active)
                ended.assert_called_once_with(True)

    def test_both_authored_states_stop_at_the_same_wall(self):
        geometry = World(FLOOR + wall(-5.0, 2.0, 5.0, 2.0))
        for case in AUTHORED_STATES:
            with self.subTest(graph=f"{case[1]:04X}"):
                player, state, ended = self.start(case, geometry)
                path = self.commands(player, range(START + 1, START + 26))
                stop = path[-1].position
                self.assertGreater(stop[2], 0.5)
                self.assertLessEqual(stop[2] + player.mover.data.stand_radius, 2.0 + STEP)
                self.assertEqual([pose.position for pose in path[-4:]], [stop] * 4)
                self.assertEqual(path[-1].velocity, (0.0, 0.0, 0.0))
                self.assertFalse(state.active)
                ended.assert_called_once_with(False)

    def test_world_target_can_be_above_the_ground(self):
        destination = Vec3(0.0, 4.0, 0.0)
        for case in AUTHORED_STATES:
            with self.subTest(graph=f"{case[1]:04X}"):
                player, state, ended = self.start(case, flat_world(1.0), destination)
                finish = START + math.ceil(3.0 / (case[-1] * player.quanta / 1_000_000))
                path = self.commands(player, range(START + 1, finish + 1))
                self.assertEqual(path[-1].position, tuple(destination))
                self.assertFalse(state.active)
                ended.assert_called_once_with(True)
                path = self.commands(player, (finish + 1,))
                self.assertLess(path[-1].position[1], destination.y)

    def test_unresolved_or_out_of_range_target_does_not_drive_a_pose(self):
        case = AUTHORED_STATES[0]
        for destination in (0.0, Vec3(0.0, 1.0, 65536.0)):
            with self.subTest(destination=destination):
                player, state, ended = self.start(case, flat_world(1.0), destination)
                path = self.commands(player, range(START + 1, START + 5))
                self.assertTrue(all(pose.position == (0.0, 1.0, 0.0) for pose in path))
                self.assertTrue(state.active)
                ended.assert_not_called()

    def test_downward_target_cannot_put_feet_below_walkable_ground(self):
        case = AUTHORED_STATES[0]
        player, state, ended = self.start(case, flat_world(1.0), Vec3(0.0, -2.0, 0.0))
        path = self.commands(player, range(START + 1, START + 5))
        self.assertTrue(all(pose.position == (0.0, 1.0, 0.0) for pose in path))
        self.assertFalse(state.active)
        ended.assert_called_once_with(False)

    def test_bounded_missing_commands_reproduce_the_same_path(self):
        geometry = flat_world(1.0)
        for case in AUTHORED_STATES:
            with self.subTest(graph=f"{case[1]:04X}"):
                contiguous, state, ended = self.start(case, geometry)
                sparse, sparse_state, sparse_ended = self.start(case, geometry)
                expected = self.commands(contiguous, range(START + 1, START + 19))
                actual = self.commands(sparse, (START + 1, START + 8, START + 18))
                self.assertEqual(actual, expected)
                self.assertEqual(actual[-1].position, (0.0, 1.0, 3.0))
                self.assertFalse(state.active or sparse_state.active)
                ended.assert_called_once_with(True)
                sparse_ended.assert_called_once_with(True)

    def test_abort_stops_before_the_next_command_pose(self):
        geometry = flat_world(1.0)
        for case in AUTHORED_STATES:
            with self.subTest(graph=f"{case[1]:04X}"):
                player, state, ended = self.start(case, geometry)
                path = self.commands(player, (START + 1, START + 2))
                stopped = path[-1].position
                self.assertGreater(stopped[2], 0.0)
                self.assertLess(stopped[2], DESTINATION.z)
                state.request(runtime.ABORT)
                state.instance.component.run_now()
                path = self.commands(player, range(START + 3, START + 9))
                self.assertEqual([pose.position for pose in path[2:]], [stopped] * 6)
                self.assertEqual(path[-1].velocity, (0.0, 0.0, 0.0))
                self.assertFalse(state.active)
                ended.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
