"""Collision readiness gates authoritative simulation without blocking another match."""

import dataclasses
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_combat import FakeClient

from ow174.game import bots, collision, messages, props
from ow174.game.bits import BitWriter
from ow174.game.collision import AsyncCollisionProvider, LoadState, StaticCollisionProvider, World, flat_world
from ow174.game.commands import FIRE, Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER, Spawn, heroes
from ow174.game.match import Match
from ow174.game.server import Client

MAP = dataclasses.replace(
    PRACTICE_RANGE,
    name="readiness fixture",
    map_guid=0x0800000000FFCC00,
    spawns=(Spawn(0, (0.0, 0.0, 0.0), 0.0), Spawn(1, (0.0, 0.0, 5.0), 180.0)),
    team_sizes=(1, 1),
)


def wall_world():
    return World([
        (-20, 0, -20, -20, 0, 20, 20, 0, 20),
        (-20, 0, -20, 20, 0, 20, 20, 0, -20),
        (-10, 0, 2, 10, 0, 2, 10, 5, 2),
        (-10, 0, 2, 10, 5, 2, -10, 5, 2),
        (-10, 0, 2, 10, 5, 2, 10, 0, 2),
        (-10, 0, 2, -10, 5, 2, 10, 5, 2),
    ], name="explicit floor and wall")


class ControlledProvider:
    def __init__(self, state=None):
        self.state = LoadState("pending") if state is None else state
        self.calls = []

    def poll(self, map_guid, name="", mode=None):
        self.calls.append((map_guid, name, mode))
        return self.state


class HomeClient(FakeClient):
    queue_ownership = Client.queue_ownership

    def __init__(self, player):
        super().__init__(player)
        self.home = []

    def go_home(self, now):
        self.home.append(now)

    def ownership_delivered(self):
        return True  # this match-only fixture delivers reliable messages immediately


def players_in(match):
    players = [match.add_player(i + 1, name, SOLDIER, i, False) for i, name in enumerate(("A", "B"))]
    for player in players:
        player.client = HomeClient(player)
        match.map_loaded(player, 10.0)
        match.switch_hero(player, heroes()[SOLDIER])
    return players


def fire_packet(player, frame=1001, count=20):
    body, payload = BitWriter(), BitWriter()
    body.bits(0, 24)  # latency and key flags
    body.bits(count, 8)
    body.bits(frame, 32)
    body.bit(0)  # no throttles
    body.signed(0, 16)
    body.signed(0, 16)
    body.bits(FIRE, 24)
    body.bits(0, 8)
    body.bits(0, 16)
    for _ in range(count - 1):
        body.bit(0)
        body.bit(0)
    messages.write_channel(payload)
    payload.bit(1)
    payload.bits(body.count, 32)
    payload.append(body)
    client = Client.__new__(Client)
    client.player = player
    client.log = lambda *args: None
    client._payload_in(payload.getvalue(), 10.8)


def health(match, player):
    return next(match.combat.vitals_of(target).total for target in match.combat._targets()
                if target.owner is player)


class ProviderTests(unittest.TestCase):
    def test_ready_needs_an_explicit_world(self):
        with self.assertRaises(ValueError):
            LoadState("ready")
        with self.assertRaises(ValueError):
            LoadState("ready", object())
        with self.assertRaises(ValueError):
            LoadState("pending", flat_world(0.0))
        with self.assertRaises(ValueError):
            LoadState("unavailable")
        world = wall_world()
        self.assertIs(StaticCollisionProvider(world).poll(MAP.map_guid).world, world)

    def test_async_request_returns_pending_until_the_worker_publishes(self):
        started, release, published = threading.Event(), threading.Event(), threading.Event()
        world = wall_world()
        guid = MAP.map_guid + 1
        key = (guid, props.mode_identifiers(MAP.mode_guid), True)
        original_keep = collision._keep

        def load(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError("test loader was not released")
            return world

        def keep(*args):
            result = original_keep(*args)
            published.set()
            return result

        provider = AsyncCollisionProvider()
        with mock.patch.object(collision, "_read_cache", side_effect=load) as loader, \
             mock.patch.object(collision, "_keep", side_effect=keep):
            try:
                self.assertEqual(provider.poll(guid, MAP.name, MAP.mode_guid).status, "pending")
                self.assertTrue(started.wait(5))
                self.assertFalse(release.is_set())
                self.assertEqual(provider.poll(guid, MAP.name, MAP.mode_guid).status, "pending")
                self.assertEqual(loader.call_count, 1)
            finally:
                release.set()
                self.assertTrue(published.wait(5))
        result = provider.poll(guid, MAP.name, MAP.mode_guid)
        self.assertEqual(result.status, "ready")
        self.assertIs(result.world, world)
        self.assertNotIn(key, collision._pending)

    def test_unexpected_loader_failure_is_terminal_and_clears_pending(self):
        guid = MAP.map_guid + 2
        key = (guid, props.mode_identifiers(MAP.mode_guid), True)
        published = threading.Event()
        original_keep = collision._keep

        def keep(*args):
            result = original_keep(*args)
            published.set()
            return result

        provider = AsyncCollisionProvider()
        failure = RuntimeError("broken fixture cache")
        with mock.patch.object(collision, "_read_cache", side_effect=failure) as loader, \
             mock.patch.object(collision, "_keep", side_effect=keep), \
             self.assertLogs("ow174.game", level="WARNING"):
            self.assertIn(provider.poll(guid, MAP.name, MAP.mode_guid).status, ("pending", "unavailable"))
            self.assertTrue(published.wait(5))
            failed = provider.poll(guid, MAP.name, MAP.mode_guid)
            self.assertEqual(failed.status, "unavailable")
            self.assertIn("broken fixture cache", failed.error)
            self.assertIsNone(failed.world)
            self.assertIs(provider.poll(guid, MAP.name, MAP.mode_guid), failed)
            self.assertEqual(loader.call_count, 1)
        self.assertNotIn(key, collision._pending)

    def test_cache_only_missing_map_never_consults_client_and_does_not_poison_builder(self):
        guid = MAP.map_guid + 3
        published = threading.Event()
        original_keep = collision._keep
        deployed = AsyncCollisionProvider(build_missing=False)

        def keep(*args):
            result = original_keep(*args)
            published.set()
            return result

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(collision, "cache_path", return_value=Path(tmp) / "missing.col"),
            mock.patch.object(collision, "collision_keys", side_effect=AssertionError) as keys,
            mock.patch.object(collision, "installed_game", side_effect=AssertionError) as lookup,
            mock.patch.object(collision, "build", side_effect=AssertionError("cache build")) as build,
            mock.patch.object(collision, "_keep", side_effect=keep),
            self.assertLogs("ow174.game", level="WARNING"),
        ):
            deployed.poll(guid, MAP.name, MAP.mode_guid)
            self.assertTrue(published.wait(5))
            result = deployed.poll(guid, MAP.name, MAP.mode_guid)
            self.assertEqual(result.status, "unavailable")
            self.assertIn("no collision cache", result.error)
            keys.assert_not_called()
            lookup.assert_not_called()
            build.assert_not_called()

            published.clear()
            local_test = AsyncCollisionProvider(build_missing=True)
            world = wall_world()
            with mock.patch.object(collision, "_read_cache", return_value=world) as load:
                local_test.poll(guid, MAP.name, MAP.mode_guid)
                self.assertTrue(published.wait(5))
                self.assertIs(local_test.poll(guid, MAP.name, MAP.mode_guid).world, world)
                self.assertTrue(load.call_args.args[2])
            self.assertIs(deployed.poll(guid, MAP.name, MAP.mode_guid), result)

    def test_six_pending_requests_keep_their_completion_before_any_second_poll(self):
        count = collision.KEEP_WORLDS + 2
        guids = [MAP.map_guid + 100 + index for index in range(count)]
        world = wall_world()
        release, published = threading.Event(), threading.Event()
        started = threading.Barrier(count + 1)
        lock = threading.Lock()
        completions = []
        original_keep = collision._keep

        def load(*args):
            started.wait(5)
            if not release.wait(5):
                raise AssertionError("test loader was not released")
            return world

        def keep(*args):
            state = original_keep(*args)
            with lock:
                completions.append(args[0])
                if len(completions) == count:
                    published.set()
            return state

        providers = [AsyncCollisionProvider() for _ in guids]
        with mock.patch.object(collision, "_read_cache", side_effect=load) as loader, \
             mock.patch.object(collision, "_keep", side_effect=keep):
            try:
                matches = [Match(dataclasses.replace(MAP, map_guid=guid), collision_provider=provider)
                           for provider, guid in zip(providers, guids, strict=True)]
                initial = [match.collision_state.status for match in matches]
                self.assertEqual(initial, ["pending"] * count)
                started.wait(5)
            finally:
                release.set()
                self.assertTrue(published.wait(5))
            self.assertNotIn(completions[0], collision._worlds)
            for provider, guid in zip(providers, guids, strict=True):
                state = provider.poll(guid)
                self.assertEqual(state.status, "ready")
                self.assertIs(state.world, world)
            for match in matches:
                match.update(10.8, 1000)
                self.assertIs(match.collision_world, world)
                self.assertEqual(match.collision_state.status, "ready")
            self.assertEqual(loader.call_count, count)


class MatchReadinessTests(unittest.TestCase):
    def test_pending_spawn_and_commands_wait_while_another_match_runs(self):
        provider = ControlledProvider()
        waiting = Match(MAP, collision_provider=provider)
        a, b = players_in(waiting)
        waiting.bots = [mock.Mock()]
        waiting.update(10.8, 1000)
        self.assertFalse(a.spawned)
        self.assertEqual(a.steps_done, 2)  # 20302/20301 can go before the world
        self.assertEqual([number for number, _ in a.client.reliable], [20302, 20301])
        self.assertEqual(waiting.snapshot()["collision"]["state"], "pending")
        waiting.bots[0].step.assert_not_called()
        before = tuple(a.mover.position)
        a.take_commands([Command(1001, forward=127, buttons=FIRE)])
        fire_packet(a)
        self.assertEqual(tuple(a.mover.position), before)
        self.assertIsNone(a.commands.last_frame)
        self.assertIsNone(a.body_script)
        self.assertEqual(waiting.combat.vitals, {})

        other = Match(MAP, collision_provider=StaticCollisionProvider(flat_world(0.0)))
        shooter, victim = players_in(other)
        other.update(10.8, 1000)
        fire_packet(shooter)
        self.assertTrue(shooter.spawned)
        self.assertLess(health(other, victim), 200.0)
        self.assertFalse(a.spawned)

        ready = wall_world()
        provider.state = LoadState("ready", ready)
        waiting.bots = []
        waiting.update(10.9, 1001)
        self.assertTrue(a.spawned)
        self.assertTrue(b.spawned)
        self.assertIs(a.mover.world, ready)
        self.assertTrue(a.mover.on_map)
        fire_packet(a, frame=1002)
        self.assertEqual(health(waiting, b), 200.0)
        self.assertIs(waiting.combat._collision(), ready)

    def test_one_packet_keeps_the_same_world_if_the_provider_changes_mid_command(self):
        ready = wall_world()
        provider = ControlledProvider(LoadState("ready", ready))
        match = Match(MAP, collision_provider=provider)
        a, b = players_in(match)
        match.update(10.8, 1000)
        original_step = a.mover.step

        def step(*args, **kwargs):
            # An external resource transition must not become a new read during this packet.
            provider.state = LoadState("pending")
            return original_step(*args, **kwargs)

        with mock.patch.object(a.mover, "step", side_effect=step):
            fire_packet(a)
        self.assertEqual(a.commands.last_frame, 1020)
        self.assertEqual(health(match, b), 200.0)
        self.assertIs(match.collision_world, ready)
        self.assertIs(a.mover.world, ready)
        self.assertEqual(len(provider.calls), 1)

    def test_active_match_keeps_collision_after_global_lru_eviction(self):
        ready = wall_world()
        key = (MAP.map_guid, props.mode_identifiers(MAP.mode_guid), True)
        collision._keep(key, ready)
        match = Match(MAP)
        a, b = players_in(match)
        match.update(10.8, 1000)
        for index in range(collision.KEEP_WORLDS):
            collision._keep((MAP.map_guid + 10 + index, frozenset(), True), flat_world(0.0))
        self.assertNotIn(key, collision._worlds)
        failure = AssertionError("must stay pinned")
        with mock.patch.object(collision, "_read_cache", side_effect=failure) as loader:
            fire_packet(a)
            match.update(10.9, 1001)
            loader.assert_not_called()
        self.assertEqual(health(match, b), 200.0)
        self.assertIs(a.mover.world, ready)
        self.assertIs(match.combat._collision(), ready)
        bot = bots.make(PRACTICE_RANGE.map_guid)[0]
        match.bots = [bot]
        with mock.patch.object(bot, "step") as step:
            match.update(11.0, 1002)
        self.assertIs(step.call_args.args[2], ready)

    def test_ready_movement_cannot_cross_the_wall(self):
        match = Match(MAP, collision_provider=StaticCollisionProvider(wall_world()))
        a, _ = players_in(match)
        match.update(10.8, 1000)
        a.take_commands([Command(frame, forward=127) for frame in range(1001, 1065)])
        self.assertLess(a.mover.position[2], 2.0)
        self.assertGreater(a.mover.position[2], 1.0)

    def test_unavailable_resources_are_visible_and_return_players_home_once(self):
        provider = ControlledProvider(LoadState("unavailable", error="missing collision chunk"))
        left = []
        match = Match(MAP, on_leave=left.append, collision_provider=provider)
        players = players_in(match)
        clients = [player.client for player in players]
        with self.assertLogs("ow174.game", level="ERROR"):
            match.update(10.8, 1000)
        self.assertTrue(match.ended)
        self.assertTrue(all(player.gone and not player.spawned for player in players))
        self.assertEqual(left, players)
        self.assertEqual([client.home for client in clients], [[10.8], [10.8]])
        self.assertEqual(match.snapshot()["collision"],
                         {"state": "unavailable", "error": "missing collision chunk"})
        match.update(10.9, 1001)
        self.assertEqual(left, players)
        self.assertEqual([client.home for client in clients], [[10.8], [10.8]])


if __name__ == "__main__":
    unittest.main()
