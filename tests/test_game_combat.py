"""Shots, damage, death and respawn (combat.py), and the commands' view time (commands.py)."""

import math
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_game_graph_analysis import make_graph, node, output

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import bots, combat, observers, stats, world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.collision import StaticCollisionProvider, flat_world
from ow174.game.combat import Combat, HitDef, Target, Vitals
from ow174.game.commands import FIRE, SUBFRAME, Command, read_commands
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.match import Match
from ow174.game.script import expr, graph, runtime
from ow174.game.script.bot import BotScript
from ow174.game.script.decode import read_chunk
from ow174.game.script.driver import BodyStream
from ow174.game.statescript import chunk
from ow174.game.world import ARMOUR_POOL, HEALTH_POOL, SHIELDS_POOL

KANEZAKA_DM = PRACTICE_RANGE.__class__(**{**PRACTICE_RANGE.__dict__, "mode_guid": 0x023000000000001E,
                                         "free_for_all": True, "team_sizes": (8,)})  # fmt: skip


class FakeClient:
    def __init__(self, player=None) -> None:
        self.player = player
        self.reliable: list = []
        self.entities: list = []

    def queue_reliable(self, msg_id: int, value: dict) -> None:
        self.reliable.append((msg_id, value))

    def bot_create_acked(self, entity: int) -> bool:
        return True  # this model acknowledges every create immediately

    def queue_bot_live(self, entity: int, snapshot) -> None:
        pass  # the combat model leaves NPC body decoding to the link tests

    def queue_combat_notice(self, entity: int, msg_id: int, value: dict) -> None:
        self.queue_reliable(msg_id, value)

    def queue_bot_health(self, entity: int, components: dict) -> None:
        self.queue_entities([
            world.EntityUpdate(entity, world.OP_UPDATE, lambda origin: world.update(origin, components))
        ])

    def queue_entities(self, updates: list) -> None:
        self.entities.extend(updates)
        if self.player is not None:
            self.player.match.sent_updates(self.player, updates)  # the model delivers every queued record
        for item in updates:  # every datagram arrives
            if item.stream is not None:
                item.stream.arrived(item.chunk_last)

    def messages(self, msg_id: int) -> list:
        return [value for number, value in self.reliable if number == msg_id]


def in_world(match: Match, player) -> None:
    player.client = FakeClient(player)
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
        self.match = Match(game_map, collision_provider=StaticCollisionProvider(flat_world(
            game_map.spawns[0].position[1]
        )))
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
        match = Match(PRACTICE_RANGE, collision_provider=StaticCollisionProvider(flat_world(1.0)))
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

    def test_missing_held_fire_uses_each_reconstructed_pose_and_aim(self):
        def fight_after_gap(lost):
            run = Run(KANEZAKA_DM, ("Shooter", "Target"))
            run.match.bots.clear()
            run.match._pending_bot_scripts.clear()
            shooter, victim = run.players
            x, y, z = shooter.mover.position
            victim.mover.position = [x, y, z + 6.0]
            for _ in range(20):
                run.step({})
            fight = run.match.combat
            target = next(target for target in fight._targets() if target.owner is victim)
            vitals = fight.vitals_of(target)
            first = run.frame + 1
            # The click aimed away. Held shots use the command's ordinary aim instead.
            press = Command(
                first, forward=127, pitch=1220, buttons=FIRE | SUBFRAME, subframe=128,
                click_yaw=-32768, click_pitch=0,
            )
            held = [
                Command(frame, forward=127, pitch=1220, buttons=FIRE)
                for frame in range(first + 1, first + 30)
            ]
            turn = Command(first + 30, yaw=-32768)
            shooter.take_commands([press])
            self.assertEqual(vitals.total, 200.0)
            shooter.take_commands([turn] if lost else [*held, turn])
            rifle = next(instance for instance in shooter.body_script.component.instances.values()
                         if instance.graph.index == 0x0254)

            def result():
                hits = shooter.client.messages(combat.HIT)
                return vitals.total, rifle.vars[53].value(), [hit["+0x84"] - first for hit in hits]

            outcome = result()
            shooter.take_commands([press, held[-1], turn])
            self.assertEqual(result(), outcome)
            return outcome

        control = fight_after_gap(False)
        self.assertLess(control[0], 200.0)
        self.assertTrue(all(0 < offset < 30 for offset in control[2]))
        self.assertEqual(fight_after_gap(True), control)


class ProjectileTimingTests(unittest.TestCase):
    def match_with_projectile(self, lifetime=1.0):
        run = Run(KANEZAKA_DM, ("Shooter", "Target"))
        run.step({})
        shooter, target = run.players
        x, y, z = shooter.mover.position
        target.mover.position = [x, y, z + 5.0]
        fight = run.match.combat
        victim = next(body for body in fight._targets() if body.owner is target)
        rocket = combat.Rocket(
            shooter, shooter.team, fight._eye(shooter), (0.0, 0.0, 1.0), 35.0,
            lifetime, HitDef(19.0), None,
        )
        fight.rockets.append(rocket)
        return run, rocket, fight.vitals_of(victim)

    def test_skipped_world_ticks_match_the_contiguous_static_hit(self):
        results = []
        for advances in (range(1, 14), (13,)):
            run, rocket, vitals = self.match_with_projectile()
            rocket.last_tick = run.frame
            for advance in advances:
                frame = run.frame + advance
                run.match.update(frame * combat.TICK, frame)
            self.assertEqual(vitals.total, 181.0)
            self.assertFalse(run.match.combat.rockets)
            results.append(rocket.flown)
        self.assertAlmostEqual(*results)

    def test_skipped_world_ticks_do_not_extend_flight_past_its_bound(self):
        run, rocket, vitals = self.match_with_projectile(lifetime=0.05)
        rocket.last_tick = run.frame
        frame = run.frame + 13
        run.match.update(frame * combat.TICK, frame)
        self.assertEqual(vitals.total, 200.0)
        self.assertEqual(rocket.flown, 0.05)
        self.assertFalse(run.match.combat.rockets)

    def test_player_admission_moves_once_and_repeated_world_tick_moves_zero(self):
        run, rocket, vitals = self.match_with_projectile()
        frame = run.frame + 13
        run.match.update(frame * combat.TICK, frame)
        self.assertEqual(rocket.flown, combat.TICK)
        run.match.update(frame * combat.TICK, frame)
        self.assertEqual(rocket.flown, combat.TICK)
        frame += 3
        run.match.update(frame * combat.TICK, frame)
        self.assertEqual(rocket.flown, 4 * combat.TICK)
        self.assertEqual(vitals.total, 200.0)

    def test_skipped_npc_frames_fire_before_a_lethal_projectile_and_stop_after_it(self):
        def run_frames(advances, *, admitted):
            run = Run(KANEZAKA_DM)
            match = run.match
            match.bots.clear()
            match._pending_bot_scripts.clear()
            run.step({})
            fight = match.combat
            player, = run.players
            player.client = None
            x, y, z = player.mover.position
            bot = bots.Bot(bots.BOT_ENTITY, {
                "hero": f"0x{SOLDIER:X}", "team": player.team, "position": (x, y, z + 2.5),
                "rotation": (0, 0, 0, 1), "graphs": [f"0x{bots.FIRING:X}"],
            }, {"name": "Firing bot", "body": "0x0400000000001403", "health": 200}, {})
            definition = make_graph(0xF0FF, [
                node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
                node(1, "STUStatescriptStateWait", state=0, fields={
                    "m_timeout": {"$": "STUConfigVarFloat", "m_value": 0.032},
                    "m_onFinishedPlug": output([2, "m_beginPlug"]),
                }),
                node(2, "STUStatescriptStateWeaponVolley", state=1, fields={
                    "m_onFinishedPlug": output([1, "m_beginPlug"]),
                }),
            ], states=[1, 2], entries=[0])
            with (
                patch.object(graph, "entity_body", return_value={"graphs": [{"graph": definition.index}]}),
                patch.object(graph, "graph", return_value=definition),
            ):
                bot.body_script = BotScript(
                    bot.body, bot.hero, bot.entity, run.frame, match.game_map.mode_guid, 200,
                )
            match.bots.append(bot)
            projectile = combat.Rocket(
                player, player.team, fight._eye(player), (0.0, 0.0, 1.0), 50.0, 1.0, HitDef(250.0), None,
                last_tick=run.frame if admitted else None,
            )
            fight.rockets.append(projectile)
            with patch.object(fight, "_shoot") as shoot:
                for advance in advances:
                    frame = run.frame + advance
                    match.update(frame * combat.TICK, frame)
            fired = [call.args[4] // 16 - run.frame for call in shoot.call_args_list]
            return fired, bot.dead, bot.body_script.frame - run.frame

        continuous = run_frames(range(1, 9), admitted=True)
        self.assertEqual(continuous, ([2], True, 3))
        self.assertEqual(run_frames((8,), admitted=True), continuous)
        self.assertEqual(run_frames((8,), admitted=False), ([2, 4, 6, 8], False, 8))


class BotTests(unittest.TestCase):
    def test_bot_damage_and_death_use_the_attacker_entity(self):
        run = Run()
        (player,) = run.players
        fight = run.match.combat
        bot = next(bot for bot in run.match.bots if bot.team != player.team)
        target = next(target for target in fight._targets() if target.owner is player)
        vitals = fight.vitals_of(target)
        fight._damage(bot, target, 19.0, False, None, run.frame)
        self.assertEqual(vitals.total, 181.0)
        self.assertFalse(fight.is_dead(player))

        loadout = 0x09E000000000008D
        with (
            patch.object(runtime, "send_message", wraps=runtime.send_message) as sent,
            patch.object(
                player.client, "queue_combat_notice", wraps=player.client.queue_combat_notice
            ) as notices,
        ):
            fight._damage(bot, target, 200.0, True, loadout, run.frame)
        params = {
            combat.KILLER: expr.Entity(bot.entity),
            combat.CRIT: True,
            combat.LOADOUT: expr.Asset(loadout),
            combat.ASSISTS: (),
        }
        sent.assert_any_call(player.body_script.component, combat.DIED, params, bot.entity)
        self.assertEqual(vitals.killer, bot.entity)
        self.assertTrue(player.body_script.component.owner_dead)
        notice = {"+0x78": combat.KILLED_BY, "+0x7C": {"+0x0": bot.entity}}
        notices.assert_called_once_with(bot.entity, combat.DEATH_NOTICE, notice)
        self.assertEqual(player.client.messages(combat.DEATH_NOTICE), [notice])
        self.assertEqual(player.client.messages(combat.HIT), [])
        self.assertEqual(player.client.messages(combat.KILL_NOTICE), [])
        self.assertEqual(player.stats.value(stats.DEATHS), 1.0)
        self.assertEqual(player.stats.value(stats.ELIMINATIONS), 0.0)
        self.assertEqual(player.stats.value(stats.HERO_DAMAGE), 0.0)

    def test_bot_death_script_uses_the_opposing_bot_as_sender(self):
        run = Run()
        (viewer,) = run.players
        fight = run.match.combat
        source = next(bot for bot in run.match.bots if bot.team == viewer.team)
        enemies = fight._enemies(source)
        self.assertTrue(all(target.team != source.team and target.owner is not source for target in enemies))
        target = next(target for target in enemies if isinstance(target.owner, bots.Bot))
        victim = target.owner
        script = victim.body_script = Mock()
        script.die.return_value = None
        with patch.object(viewer.client, "queue_bot_death", create=True) as delivered:
            fight._damage(source, target, victim.health, False, None, run.frame)
        self.assertTrue(victim.dead)
        self.assertEqual(fight.vitals_of(target).killer, source.entity)
        script.die.assert_called_once_with(
            run.frame,
            {combat.KILLER: expr.Entity(source.entity), combat.CRIT: False,
             combat.LOADOUT: None, combat.ASSISTS: ()},
            source.entity,
        )
        delivered.assert_called_once_with(victim.entity, None)
        self.assertEqual(viewer.client.messages(combat.KILL_NOTICE), [])

    @patch.object(graph, "entity_body", return_value=None)
    def test_shooting_a_bot_until_it_dies_and_comes_back(self, _entity_body):
        # The combat fixture models health and movement. Script delivery uses the encrypted peer tests.
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

    def test_remote_kill_feed_pulse_carries_the_hero_death_attribution(self):
        for _ in range(40):  # let the graph settle before testing attribution
            self.run.step({})
        script = self.b.body_script
        stream = BodyStream(self.b.body)
        stream.base = observers.snapshot(script)
        known = {index: value[0] for index, value in stream.base.instances.items()}
        feed_id = next(index for index, value in known.items() if value == 0x01CF)

        fight = self.run.match.combat
        target = next(target for target in fight._targets() if target.owner is self.b)
        loadout = 0x09E000000000008D  # Soldier's rifle
        last_command = script.frame
        fight._damage(self.a, target, 200.0, True, loadout, self.run.frame)
        self.assertEqual(script.frame, last_command)

        delta, _ = observers.delta(script, stream, observers.snapshot(script))
        wire = chunk(delta, script.frame, 0)
        frame = read_chunk(wire.getvalue(), wire.count, known)
        self.assertFalse(frame.owner)
        feed = frame.instances[feed_id]
        self.assertEqual(feed.states[108], (True, {"fresh": True, "count": 1}))
        self.assertEqual(feed.vars[392], (expr.Entity(self.a.body), ()))  # 0765.025 attacker
        self.assertEqual(feed.vars[1821], (True, ()))  # 0765.025 critical
        self.assertEqual(feed.vars[9300], (expr.Asset(loadout), ()))
        self.assertEqual(feed.vars[9296], ((), ()))

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

    def test_replacement_body_waits_for_its_own_respawn_deadline(self):
        fight = self.run.match.combat
        delay = combat.respawn_seconds(self.run.match)
        fight.update(100.0, 1000, frames=False)

        def kill_current_body():
            target = next(target for target in fight._targets() if target.owner is self.b)
            fight._damage(self.a, target, 200.0, False, None, fight.tick)
            self.assertTrue(fight.is_dead(self.b))

        kill_current_body()
        old = self.b.body
        self.run.match.switch_hero(self.b, self.b.hero)
        current = self.b.body
        self.assertNotEqual(current, old)
        self.assertNotIn(old, fight.vitals)

        fight.update(101.0, 1062, frames=False)
        kill_current_body()
        fight.update(100.0 + delay, 1188, frames=False)
        self.assertEqual(self.b.body, current)
        self.assertTrue(fight.is_dead(self.b))

        fight.update(101.0 + delay, 1250, frames=False)
        self.assertNotEqual(self.b.body, current)
        self.assertFalse(fight.is_dead(self.b))


class ModeFrameTests(unittest.TestCase):
    def test_the_mode_entity_lists_its_ui_graph(self):
        frame = combat.mode_frame({1303: 12.5})
        decoded = read_chunk(_chunk(frame), _chunk_bits(frame), {})
        self.assertEqual({key: item.graph for key, item in decoded.instances.items()}, {1: combat.MODE_UI})
        self.assertEqual(decoded.entity_vars[1303][0], 12.5)

    def test_mode_ui_shared_variables_start_in_both_initial_and_countdown_frames(self):
        ui = graph.graph(combat.MODE_UI)
        component = runtime.Component(0xA0000100)
        instance = component.create(ui)
        component.run_frame(1)
        self.assertTrue(instance.states[21].active)

        for values in ({}, {1303: 12.5}):
            with self.subTest(values=values):
                frame = combat.mode_frame(values)
                decoded = read_chunk(_chunk(frame), _chunk_bits(frame), {})
                self.assertEqual(decoded.instances[1].states[21], (instance.states[21].active, {}))


def _chunk(frame: BitWriter) -> bytes:
    return chunk(frame, 0, 1).getvalue()


def _chunk_bits(frame: BitWriter) -> int:
    return chunk(frame, 0, 1).count


if __name__ == "__main__":
    unittest.main()
