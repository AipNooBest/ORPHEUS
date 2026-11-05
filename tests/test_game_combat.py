"""Shots, damage, death and respawn (combat.py), and the commands' view time (commands.py)."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import bots, combat
from ow174.game.bits import BitReader, BitWriter
from ow174.game.combat import Combat, HitDef, Target, Vitals
from ow174.game.commands import FIRE, Command, read_commands
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.match import Match
from ow174.game.script.decode import read_chunk
from ow174.game.world import ARMOUR_POOL, HEALTH_POOL, SHIELDS_POOL

KANEZAKA_DM = PRACTICE_RANGE.__class__(**{**PRACTICE_RANGE.__dict__, "mode_guid": 0x023000000000001E,
                                         "free_for_all": True, "team_sizes": (8,)})  # fmt: skip


class FakeClient:
    def __init__(self) -> None:
        self.reliable: list = []
        self.entities: list = []

    def queue_reliable(self, msg_id: int, value: dict) -> None:
        self.reliable.append((msg_id, value))

    def queue_entities(self, updates: list) -> None:
        self.entities.extend(updates)
        for item in updates:  # every datagram arrives
            if item.stream is not None:
                item.stream.arrived(item.chunk_last)

    def messages(self, msg_id: int) -> list:
        return [value for number, value in self.reliable if number == msg_id]


def in_world(match: Match, player) -> None:
    player.client = FakeClient()
    player.spawned = True
    player.steps_done = 4
    match.bots_shown[player.slot] = 0


def aim(origin, target) -> tuple[int, int]:
    dx, dy, dz = (t - o for t, o in zip(target, origin, strict=True))
    yaw = round(math.atan2(dx, dz) * 65536 / (2 * math.pi))
    pitch = round(-math.atan2(dy, math.hypot(dx, dz)) * 65536 / (2 * math.pi))
    return (yaw + 32768) % 65536 - 32768, pitch


class Run:
    """A match ticked frame by frame, the players' commands taken as the server takes them."""

    def __init__(self, game_map=PRACTICE_RANGE, names=("A",)) -> None:
        self.match = Match(game_map)
        self.players = []
        for number, name in enumerate(names):
            player = self.match.add_player(number + 1, name, SOLDIER, number, False)
            in_world(self.match, player)
            self.players.append(player)
        self.frame = 1000
        self.match.tick = self.frame
        for player in self.players:
            self.match.switch_hero(player, heroes()[SOLDIER])

    def step(self, commands: dict) -> None:
        """One tick: every player's command for this frame (player -> (yaw, pitch, buttons))."""
        self.frame += 1
        self.match.update(self.frame * 0.016, self.frame)
        for player in self.players:  # what server.send_frame adds to every frame
            player.client.queue_entities(self.match.remote_movements(player))
        for player in self.players:
            yaw, pitch, buttons = commands.get(player, (0, 0, 0))
            player.take_commands([Command(self.frame, yaw=yaw, pitch=pitch, buttons=buttons, extra=0)])


class CommandTests(unittest.TestCase):
    def record(self) -> bytes:
        """A click in the middle of a frame (buttons & 0x10000: sub-frame 128, the aim then), then the
        same command with the view delay 5 ms longer."""
        body = BitWriter()
        body.bits(35, 16)  # latency
        body.bits(0, 8)  # key flags
        body.bits(2, 8)  # two commands
        body.bits(7000, 32)
        body.bit(0)  # no throttles
        body.signed(100, 16)  # yaw
        body.signed(-200, 16)  # pitch
        body.bits(0x10001, 24)  # FIRE, with sub-frame data
        body.bits(0, 8)  # action
        body.bits(128, 8)  # the sub-frame
        body.signed(90, 16)  # the aim at the click
        body.signed(-210, 16)
        body.bits(100, 16)  # extra: the view delay, ms
        body.bit(0)  # the next frame
        body.bit(1)  # it differs
        body.bit(0)  # the same throttles
        body.bit(0)  # the same pitch
        body.bit(0)  # the same yaw
        body.bit(1)  # extra changes by S(5): +5
        body.bit(1)
        body.signed(5, 5)
        body.bit(1)  # buttons: FIRE without sub-frame data
        body.bits(0x1, 24)
        body.bit(0)  # the same action
        out = BitWriter()
        out.bit(1)
        out.bits(body.count, 32)
        out.append(body)
        return out.getvalue()

    def test_the_view_delay_and_the_aim_at_the_click(self):
        first, second = read_commands(BitReader(self.record()))
        self.assertEqual(
            (first.extra, first.subframe, first.click_yaw, first.click_pitch), (100, 128, 90, -210)
        )
        self.assertEqual(first.aim(), (90, -210))
        # 0x7FF7894BEEC0: (extra + round((1 - sub / 255) * 16)) ms
        self.assertAlmostEqual(first.view_delay(16000), (100 + round((1 - 128 / 255) * 16)) * 0.001)
        self.assertEqual((second.extra, second.subframe, second.click_yaw), (105, 0xFF, None))
        self.assertEqual(second.aim(), (100, -200))
        self.assertAlmostEqual(second.view_delay(16000), 0.105)


class GeometryTests(unittest.TestCase):
    def test_a_ray_meets_a_capsule(self):
        t = combat.ray_capsule((0.0, 1.0, -10.0), (0.0, 0.0, 1.0), (0.0, 0.5, 0.0), (0.0, 1.5, 0.0), 0.5)
        self.assertAlmostEqual(t, 9.5)
        self.assertIsNone(
            combat.ray_capsule((2.0, 1.0, -10.0), (0.0, 0.0, 1.0), (0, 0.5, 0), (0, 1.5, 0), 0.5)
        )

    def test_the_target_as_the_shooter_saw_it(self):
        match = Match(PRACTICE_RANGE)
        fight = Combat(match)
        target = Target(5, 1, (10.0, 0.0, 0.0), 1.8, 0.4, 1.55, None)
        fight.history[5] = __import__("collections").deque([(100, (0.0, 0.0, 0.0), 1.8, 1.55),
                                                            (101, (1.0, 0.0, 0.0), 1.8, 1.55)])  # fmt: skip
        self.assertEqual(fight.seen(target, 100.25).feet, (0.25, 0.0, 0.0))
        self.assertEqual(fight.seen(target, 90).feet, (0.0, 0.0, 0.0))  # older than kept: the oldest
        self.assertEqual(fight.seen(target, 120).feet, (1.0, 0.0, 0.0))  # newer: the newest


class DamageTests(unittest.TestCase):
    def test_falloff_and_explosion_rings(self):
        rifle = HitDef(20.0, None, 30.0, 50.0, 0.3, 2.0)
        self.assertEqual(rifle.scale(10), 1.0)
        self.assertAlmostEqual(rifle.scale(40), 0.65)
        self.assertEqual(rifle.scale(60), 0.3)
        helix = HitDef(80.0, rings=((1.0, 1.0), (3.0, 0.5)))
        self.assertEqual(helix.splash(0.5), 1.0)
        self.assertAlmostEqual(helix.splash(2.0), 0.75)
        self.assertEqual(helix.splash(3.5), 0.0)

    def test_the_data_of_soldiers_weapons(self):
        run = Run()
        script = run.players[0].body_script
        weapon = script.component.instances[11]
        rifle = combat.hit_def(weapon.state(10), weapon.state(10).node.field("m_6396149F"))
        self.assertEqual(
            (rifle.amount, rifle.falloff_start, rifle.falloff_end, rifle.critical), (20.0, 30.0, 50.0, 2.0)
        )
        self.assertAlmostEqual(rifle.falloff_floor, 0.3, places=6)
        helix = weapon.state(16)
        self.assertEqual(combat.hit_def(helix, helix.node.field("m_6396149F")).amount, 40.0)
        blast = combat.hit_def(helix, helix.node.field("m_D2D11CE9"))
        self.assertEqual((blast.amount, blast.rings), (80.0, ((1.0, 1.0), (3.0, 0.5))))

    def test_pools_take_damage_shields_armour_health(self):
        vitals = Vitals(1, 200.0, armour=50.0, shields=25.0)
        self.assertEqual(vitals.damage(30.0), 25.0 + 2.5)  # 25 from shields, the 5 left halved by armour
        self.assertEqual((vitals.now[SHIELDS_POOL], vitals.now[ARMOUR_POOL]), (0.0, 47.5))
        vitals.damage(100.0)
        self.assertEqual(vitals.now[ARMOUR_POOL], 0.0)
        self.assertEqual(vitals.now[HEALTH_POOL], 200.0 - (95.0 - 47.5))
        self.assertEqual(vitals.heal(1000.0), 47.5 + 50.0 + 25.0)


class BotTests(unittest.TestCase):
    def test_shooting_a_bot_until_it_dies_and_comes_back(self):
        run = Run()
        player = run.players[0]
        bot = min(run.match.bots, key=lambda b: math.dist(b.position, player.mover.position))
        dead_entity = bot.entity
        target = (bot.position[0], bot.position[1] + 1.0, bot.position[2])
        for _ in range(400):
            if bot.dead:
                break
            yaw, pitch = aim(combat.Combat._eye(run.match.combat, player), target)
            run.step({player: (yaw, pitch, FIRE)})
        self.assertTrue(bot.dead)
        hits = player.client.messages(combat.HIT)
        self.assertTrue(hits)
        self.assertEqual(hits[-1]["+0x78"]["+0x8"] & combat.KILLING, combat.KILLING)
        self.assertEqual(player.client.messages(combat.KILL_NOTICE)[-1]["+0x84"], {"+0x0": dead_entity})
        self.assertEqual(bot.movement(run.frame).flags, bots.DEAD)
        for _ in range(round(combat.BOT_RESPAWN / 0.016) + 2):
            run.step({})
        self.assertNotIn(bot, run.match.bots)
        self.assertNotIn(dead_entity, [b.entity for b in run.match.bots])
        self.assertEqual(len(run.match.bots), 15)


class DeathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = Run(KANEZAKA_DM, ("A", "B"))  # B stands on his spawn point, 1.5 m from A's
        self.a, self.b = self.run.players

    def kill_b(self) -> int:
        frames = 0
        while not self.run.match.combat.is_dead(self.b) and frames < 600:
            target = (self.b.mover.position[0], self.b.mover.position[1] + 1.0, self.b.mover.position[2])
            yaw, pitch = aim(combat.Combat._eye(self.run.match.combat, self.a), target)
            self.run.step({self.a: (yaw, pitch, FIRE)})
            frames += 1
        self.assertTrue(self.run.match.combat.is_dead(self.b))
        return frames

    def test_a_kill_tells_both_players_and_the_victims_body(self):
        self.kill_b()
        self.assertTrue(self.run.match.combat.is_dead(self.b))
        self.assertEqual(self.a.client.messages(combat.KILL_NOTICE)[-1]["+0x84"], {"+0x0": self.b.body})
        death = self.b.client.messages(combat.DEATH_NOTICE)[-1]
        self.assertEqual(death, {"+0x78": combat.KILLED_BY, "+0x7C": {"+0x0": self.a.body}})
        self.assertTrue(self.b.body_script.component.owner_dead)
        self.assertEqual(self.b.movement().flags & combat.DEAD, combat.DEAD)
        self.run.step({})  # the body script runs "died" (003E.025): the kill feed's pulser goes on
        self.run.step({})
        feed = self.b.body_script.component.instances[13].state(108)
        self.assertTrue(feed.payload()["count"] >= 1)

    def test_the_dead_cannot_shoot(self):
        """Reading (a) of the live report: a dead player's presses are dropped (the client's edge handler,
        0x7FF78AAAC570), so his volley never starts and no shot of his counts."""
        self.kill_b()
        volley = self.b.body_script.component.instances[11].state(10)
        ammo = self.b.body_script.component.instances[11].vars[53].value()
        for _ in range(30):
            self.run.step({self.b: (0, 0, FIRE)})
        self.assertFalse(volley.active)
        self.assertEqual(self.b.body_script.component.instances[11].vars[53].value(), ammo)

    def test_the_killer_stops_when_he_lets_go_and_spends_ammo(self):
        """Reading (b) and the ammo report: after the kill the killer's volley ends when FIRE goes up, and
        his ammo #53 goes down in the owner frames his client reads."""
        self.kill_b()
        weapon = self.a.body_script.component.instances[11]
        self.run.step({self.a: (0, 0, 0)})
        self.run.step({self.a: (0, 0, 0)})
        self.assertFalse(weapon.state(10).active)
        before = weapon.vars[53].value()
        self.a.client.entities.clear()
        for _ in range(20):
            self.run.step({self.a: (0, 0, FIRE)})
        for _ in range(3):
            self.run.step({self.a: (0, 0, 0)})
        self.assertFalse(weapon.state(10).active)
        after = weapon.vars[53].value()
        self.assertLess(after, before)
        graphs = {item.id: item.graph.index for item in self.a.body_script.component.instances.values()}
        seen = []
        for item in self.a.client.entities:
            if item.chunk is not None and item.entity == self.a.body:
                frame = read_chunk(item.chunk.getvalue(), item.chunk.count, graphs)
                if 11 in frame.instances and 53 in frame.instances[11].vars:
                    seen.append(frame.instances[11].vars[53][0])
        self.assertIn(after, seen)

    def test_quick_melee_hits_once_for_30(self):
        target = (self.b.mover.position[0], self.b.mover.position[1] + 1.0, self.b.mover.position[2])
        yaw, pitch = aim(combat.Combat._eye(self.run.match.combat, self.a), target)
        self.run.step({self.a: (yaw, pitch, 0x800)})  # logical button 31, 0043's ability
        for _ in range(40):
            self.run.step({self.a: (yaw, pitch, 0)})
        self.assertEqual(self.run.match.combat.vitals[self.b.body].now[HEALTH_POOL], 170.0)
        self.assertEqual([hit["+0x78"]["+0x4"] for hit in self.a.client.messages(combat.HIT)], [30])

    def test_respawn_after_the_modes_time(self):
        self.kill_b()
        old = self.b.body
        for _ in range(round(combat.RESPAWN / 0.016) + 2):
            self.run.step({})
        self.assertNotEqual(self.b.body, old)
        self.assertFalse(self.run.match.combat.is_dead(self.b))
        self.assertFalse(self.b.body_script.component.owner_dead)


class ModeFrameTests(unittest.TestCase):
    def test_the_mode_entity_lists_its_ui_graph(self):
        frame = combat.mode_frame({1303: 12.5})
        decoded = read_chunk(_chunk(frame), _chunk_bits(frame), {})
        self.assertEqual({key: item.graph for key, item in decoded.instances.items()}, {1: combat.MODE_UI})
        self.assertEqual(decoded.entity_vars[1303][0], 12.5)


def _chunk(frame: BitWriter) -> bytes:
    from ow174.game.statescript import chunk

    return chunk(frame, 0, 1).getvalue()


def _chunk_bits(frame: BitWriter) -> int:
    from ow174.game.statescript import chunk

    return chunk(frame, 0, 1).count


if __name__ == "__main__":
    unittest.main()
