"""Predicted projectiles (projectiles.py): the lease, confirmations, the explosion release, the shot records
the other clients get, and the Biotic Field's entity, frame and heal; the other bodies' statescript
(observers.py)."""

import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_game_combat import KANEZAKA_DM, Run
from test_game_packet_budget import MemorySocket, decoded
from test_game_world import read_movement

from ow174.game import bodytrace, bots, combat, movelog, observers, projectiles, world
from ow174.game.bits import BitReader
from ow174.game.commands import Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.script.decode import ClientBody, read_chunk
from ow174.game.script.driver import Snapshot
from ow174.game.server import Client, GameServer
from ow174.game.world import HEALTH_POOL, OP_CREATE, OP_DESTROY

HELIX, HELIX_VISOR, CANISTER = 0x040000000000072C, 0x0400000000001C86, 0x040000000000072D
SECONDARY, ABILITY_2 = 0x02, 0x10


def bits_of(writer) -> BitReader:
    return BitReader(writer.getvalue())


def read_shot(r: BitReader) -> dict:
    """A shot record as the client's reader 0x7FF789C91F80 reads it (form 2 only)."""

    def counter():
        if not r.bit():
            return r.bits(4)
        value = r.bits(8)
        return r.bits(32) if value == 0xFF else value

    def w16():
        if not r.bit():
            return r.bits(4)
        return r.bits(16) if r.bit() else r.bits(8)

    def entity():
        kind = r.bits(2)
        if not r.bit():
            value = r.bits(8)
        else:
            value = r.bits(16)
            if value == 0xFFFF:
                value = r.bits(32)
        return 0x80000000 | kind << 29 | value

    out = {"shot": counter()}
    if r.bit():
        out["spread"] = r.bits(32)
    assert not r.bit(), "form 1"
    out.update(instance=w16(), state=w16(), activation=r.bits(6))
    out["volleys"] = 1 if r.bit() else w16()
    out["extra"] = w16()
    out["origin"] = tuple(struct.unpack("<f", struct.pack("<I", r.bits(32)))[0] for _ in range(3))
    out["yaw16"], out["pitch16"] = r.bits(16), r.bits(16)
    ids = []
    if r.bit():
        count = w16()
        ids.append(entity())
        for _ in range(count - 1):
            if r.bit():
                ids.append(ids[-1] + 1)
            else:
                negative = r.bit()
                step = counter()
                ids.append(ids[-1] - step if negative else ids[-1] + step)
    out["ids"] = ids
    return out


class LeaseTests(unittest.TestCase):
    def test_soldiers_predicted_definitions_and_ids(self):
        run = Run()
        player = run.players[0]
        fights = run.match.combat.projectiles
        self.assertEqual(projectiles.definitions(player.body_script), [CANISTER, HELIX_VISOR, HELIX])
        blocks = fights.lease(player)
        self.assertEqual(len(blocks), 6)  # two windows of each
        guid, start, stop, base, per = blocks[4]
        self.assertEqual((guid, stop - start, per), (HELIX, projectiles.LEASE_SPAN, 1))
        frame = start + 1234
        # the allocator 0x7FF789AE59D0: 0xA0000000 | base + (f - start) * perFrame
        self.assertEqual(fights.lease_id(player, HELIX, frame), 0xA0000000 | (base + 1234))
        self.assertIsNone(
            fights.lease_id(player, 0x04000000000003D4, frame)
        )  # the rifle: sync NONE, no lease

    def test_the_lease_rides_in_the_owners_body_create_only(self):
        run = Run(KANEZAKA_DM, ("A", "B"))
        a = run.players[0]
        blocks = run.match.combat.projectiles.lease(a)
        record = world.update(0, {90: [blocks]})
        data = record.getvalue()
        # 8 bits count, 8 bits index 90, pad, u8 mask length 1, mask 1, aligned u32 count, 21 bytes a block
        self.assertEqual(data[:4], bytes([1, 90, 1, 1]))
        self.assertEqual(struct.unpack_from("<I", data, 4)[0], len(blocks))
        guid, start, stop, base, per = struct.unpack_from("<QIIIB", data, 8)
        self.assertEqual((guid, start, stop, base, per), blocks[0])
        self.assertEqual(len(data), 8 + 21 * len(blocks))
        own = run.match._body_create(0, a, None, own=True)
        other = run.match._body_create(0, a, None)
        self.assertEqual(own.count - other.count, 8 * (len(data) - 1))  # the same create, plus component 90


class HelixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = Run(KANEZAKA_DM, ("A", "B"))
        self.a, self.b = self.run.players
        for _ in range(20):
            self.run.step({})
        self.a.client.entities.clear()
        self.b.client.entities.clear()
        self.frame = self.run.frame + 1
        self.run.step({self.a: (0, 0, SECONDARY)})
        for _ in range(150):
            self.run.step({})

    def test_the_owners_rocket_is_confirmed_then_released(self):
        lease = self.run.match.combat.projectiles.lease_id(self.a, HELIX, self.frame)
        creates = [item for item in self.a.client.entities if item.op == OP_CREATE and item.entity == lease]
        self.assertEqual(len(creates), 1)
        destroys = [item for item in self.a.client.entities if item.op == OP_DESTROY and item.entity == lease]
        self.assertEqual(len(destroys), 1)
        r = bits_of(destroys[0].ch2)
        self.assertEqual((r.bit(), r.bit()), (0, 1))  # no events, a projectile state
        self.assertEqual(r.var_b(), projectiles.NO_BASELINE)
        r.var_b()
        self.assertEqual(r.bit(), 0)  # alive: the client plays its own held explosion (+4 = 0)

    def test_the_other_client_gets_a_server_rocket_and_its_shot_record(self):
        creates = [item for item in self.b.client.entities if item.op == OP_CREATE]
        self.assertEqual([item.entity for item in creates], [projectiles.SEEN_FIRST])
        events = [
            item for item in self.b.client.entities if item.entity == self.a.body and item.ch2 is not None
        ]
        self.assertEqual(len(events), 1)
        r = bits_of(events[0].ch2)
        self.assertEqual((r.bit(), r.bit()), (1, 1))  # events, one event
        self.assertEqual(r.var_b(), self.frame)
        shot = read_shot(r)
        self.assertEqual((shot["instance"], shot["state"], shot["ids"]), (11, 16, [projectiles.SEEN_FIRST]))
        self.assertEqual((r.bit(), r.bit()), (0, 0))  # the end of the events, no projectile state
        destroy = [item for item in self.b.client.entities if item.op == OP_DESTROY]
        self.assertEqual([item.entity for item in destroy], [projectiles.SEEN_FIRST])
        self.assertIsNotNone(destroy[0].ch2)

    def test_the_direction_reads_back(self):
        for direction in ((0.0, 0.0, 1.0), (0.6, 0.0, 0.8), (0.0, -1.0, 0.0), (-0.48, 0.6, -0.64)):
            yaw16, pitch16 = projectiles.direction16(direction)
            import math

            a = (yaw16 - 32768) * math.pi / 32768
            b = (pitch16 - 32768) * math.pi / 65536
            back = (math.sin(a) * math.cos(b), math.sin(b), math.cos(a) * math.cos(b))
            for x, y in zip(direction, back, strict=True):
                self.assertAlmostEqual(x, y, delta=2e-4)


class ProjectileRangeTests(unittest.TestCase):
    def test_helix_sweeps_to_its_range_boundary(self):
        for z, hit in ((100.05, True), (100.7, False)):
            with self.subTest(target_feet_z=z):
                run = Run(KANEZAKA_DM, ("A", "B"))
                shooter, victim = run.players
                fight = run.match.combat
                run.match.bots.clear()
                y = run.match.game_map.spawns[0].position[1]
                shooter.mover.position = (0.0, y, 0.0)
                victim.mover.position = (0.0, y, z)
                frame = shooter.body_script.frame + 1
                shooter.body_script.command(frame, SECONDARY)
                fight.command(shooter, Command(frame, buttons=SECONDARY))
                [rocket] = fight.rockets
                reach = rocket.speed * rocket.left
                self.assertEqual(reach, 100.0)
                target = next(target for target in fight._targets() if target.owner is victim)
                contact = combat.ray_capsule(
                    rocket.position, rocket.direction, *target.segment(), target.radius,
                )
                self.assertGreater(contact, reach - rocket.speed * combat.TICK)
                self.assertEqual(contact < reach, hit)
                vitals = fight.vitals_of(target)
                before = vitals.total
                for tick in range(126):
                    fight.update(20.0 + tick * combat.TICK, frame + tick, frames=False)
                self.assertFalse(fight.rockets)
                self.assertEqual(vitals.total < before, hit)
                self.assertAlmostEqual(rocket.flown * rocket.speed, min(contact, reach))


class ObserverProjectileDeliveryTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)
        run = Run(KANEZAKA_DM, ("Shooter", "Viewer"))
        self.match = run.match
        self.match.bots.clear()
        for _ in range(20):
            run.step({})
        self.shooter, self.viewer = run.players
        self.tick = run.frame
        self.fight = self.match.combat
        watch = self.fight.observers.watches[(self.viewer.slot, self.shooter.body)]
        self.known = {index: item[0] for index, item in watch.stream.base.instances.items()}

        self.server = GameServer()
        self.server.sock = MemorySocket()
        key_in, key_out = b"i" * 32, b"o" * 32
        self.client = Client(
            self.server, ("memory.test", 40002), 2, LinkCipher(key_in, key_out), self.viewer,
        )
        self.server.clients[2] = self.client
        self.client.open = True
        self.viewer.client = self.client
        self.cipher = LinkCipher(key_out, key_in)
        self.window = ReceiveWindow()
        self.seq = 0

    def fire(self, *, nearby=False):
        x, y, z = self.shooter.mover.position
        self.viewer.mover.position = [x if nearby else x + 10.0, y, z + 2.0]
        self.shot_frame = self.tick + 1
        self.shooter.take_commands([Command(self.shot_frame, buttons=SECONDARY)])
        self.assertEqual(len(self.fight.rockets), 1)

    def advance(self, count):
        for _ in range(count):
            self.tick += 1
            self.match.update(self.tick * combat.TICK, self.tick)

    def send(self, *, lose=False):
        self.client.send_frame(self.tick, self.tick * combat.TICK)
        packet, _, _, _, _, _ = decoded(self, self.cipher, self.server.sock.sent[-1][0])
        if not lose:
            self.assertTrue(self.window.accept(packet.seq))
        return self.client.in_flight[packet.seq][1]  # reliable records in the authenticated send

    def ack(self):
        packet = Packet(self.client.conn, KIND_CONTROL, self.seq, self.window.expected, self.window.bits, ACK)
        self.seq += 1
        self.server._datagram(self.cipher.seal(packet), self.client.address, self.tick * combat.TICK)

    def launches(self, records):
        return [item for item in records if item.entity == self.shooter.body and item.ch2 is not None]

    def projectile_ops(self, records):
        return [item.op for item in records if item.entity == projectiles.SEEN_FIRST]

    def assert_launch(self, records):
        events = self.launches(records)
        self.assertEqual(len(events), 1)
        record = bits_of(events[0].ch2)
        self.assertEqual((record.bit(), record.bit(), record.var_b()), (1, 1, self.shot_frame))
        shot = read_shot(record)
        self.assertEqual(
            (shot["instance"], shot["state"], shot["activation"], shot["ids"]),
            (11, 16, 1, [projectiles.SEEN_FIRST]),
        )
        self.assertEqual((record.bit(), record.bit()), (0, 0))
        return events[0].ch2.getvalue()

    def assert_retired(self, records, *, explode):
        self.assertEqual(self.launches(records), [])
        self.assertEqual(self.projectile_ops(records), [OP_DESTROY])
        (release,) = [item for item in records if item.entity == projectiles.SEEN_FIRST]
        if explode:
            record = bits_of(release.ch2)
            self.assertEqual((record.bit(), record.bit()), (0, 1))
        else:
            self.assertIsNone(release.ch2)

    def test_create_volley_and_launch_ack_gate_observer_helix_retirement(self):
        # The packet prefix ends exactly at the create on the old path.
        self.client.queue_entities([world.EntityUpdate(0x80001000 + index) for index in range(126)])
        self.fire()
        self.assertEqual(self.projectile_ops(self.send(lose=True)), [OP_CREATE])
        self.assertEqual(self.launches(self.send()), [])
        self.ack()
        retry = self.send()
        self.assertEqual(self.projectile_ops(retry), [OP_CREATE])
        self.assertEqual(self.launches(retry), [])
        self.ack()
        self.assertEqual(self.launches(self.send()), [])  # create alone does not install the volley
        self.ack()

        self.advance(3)
        volley = self.send()
        self.assertEqual(self.launches(volley), [])
        frames = [
            read_chunk(item.chunk.getvalue(), item.chunk.count, self.known)
            for item in volley if item.entity == self.shooter.body and item.chunk is not None
        ]
        self.assertTrue(any(
            not frame.owner and frame.instances[11].states.get(16) == (
                False, {"offset": 0, "counter": 1, "volleys": 1},
            )
            for frame in frames if 11 in frame.instances
        ))
        self.ack()
        self.advance(3)
        original = self.assert_launch(self.send(lose=True))

        self.advance(140)
        self.assertFalse(self.fight.rockets)
        waiting = self.send()
        self.assertEqual(self.projectile_ops(waiting), [])
        self.assertEqual(self.launches(waiting), [])
        self.ack()  # report that the later frame arrived without the launch
        retried = self.send()
        self.assertEqual(self.assert_launch(retried), original)
        self.assertEqual(self.projectile_ops(retried), [])
        self.ack()
        self.advance(3)
        self.assert_retired(self.send(), explode=True)

    def send_finished_short_shot(self, *, lose=False):
        self.fire(nearby=True)
        self.advance(60)  # the real combat sweep hits the nearby enemy and ends the short flight
        self.assertFalse(self.fight.rockets)
        self.assertFalse(self.fight.projectiles.shots)
        created = self.send()
        self.assertEqual(self.projectile_ops(created), [OP_CREATE])
        self.assertEqual(self.launches(created), [])
        self.ack()
        self.advance(3)
        launch = self.send(lose=lose)
        self.assert_launch(launch)
        self.assertEqual(self.projectile_ops(launch), [])

    def test_ended_before_first_send_catches_up_in_create_launch_release_order(self):
        self.send_finished_short_shot()
        self.ack()
        self.advance(3)
        self.assert_retired(self.send(), explode=True)

    def test_unacknowledged_launch_expires_without_resending_or_exploding(self):
        self.send_finished_short_shot(lose=True)
        self.advance(70)  # beyond the 64-tick grace after the short flight ended
        self.assert_retired(self.send(), explode=False)
        self.ack()  # a loss report for the canceled launch must not restore it
        self.assertEqual(self.launches(self.send()), [])

    def test_replacing_the_shooter_body_retires_an_in_flight_launch(self):
        self.send_finished_short_shot(lose=True)
        old_body = self.shooter.body
        self.match.switch_hero(self.shooter, heroes()[SOLDIER])
        self.assertNotEqual(self.shooter.body, old_body)
        self.advance(3)
        self.assert_retired(self.send(), explode=False)
        self.ack()
        self.assertFalse(any(item.entity == old_body and item.ch2 is not None for item in self.send()))

    def npc_source(self):
        # Use a known volley layout so this delivery test needs no external NPC graph bundle.
        instance = self.shooter.body_script.component.instances[11]
        state = instance.state(16)
        state.counter = state.volleys = 1
        baseline = Snapshot(self.tick, instances={instance.id: (instance.graph.index, None)})
        script = SimpleNamespace(graphs=lambda: {instance.id: instance.graph})
        script.initial = observers.full_frame(script, baseline), baseline
        bot = bots.make(PRACTICE_RANGE.map_guid)[0]
        bot.body_script = script
        self.match.bots = [bot]
        self.shooter.client = None
        self.client.queue_entities([self.match._bot_create_update(bot)])
        return bot, state

    def fire_npc(self, bot, state):
        self.shot_frame = self.tick + 1
        return self.fight.projectiles.fired(
            bot, state, self.shot_frame, HELIX, 2.0, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
        )

    def npc_volley(self, bot, state):
        instance = state.instance
        snapshot = Snapshot(
            self.tick, instances={instance.id: (instance.graph.index, None)},
            states={(instance.id, state.index): (state.node.cls, False, state.payload())},
        )
        self.client.queue_bot_live(bot.entity, snapshot)

    def advance_delivery(self, count=1):
        self.tick += count
        self.match.tick = self.tick
        self.fight.projectiles.update(self.tick)

    def test_npc_shot_survives_lost_source_create_and_waits_for_matching_volley_ack(self):
        bot, state = self.npc_source()
        lost_create = self.send(lose=True)
        self.assertTrue(any(item.entity == bot.entity and item.op == OP_CREATE for item in lost_create))
        self.assertIsNotNone(self.fire_npc(bot, state))
        self.npc_volley(bot, state)
        self.advance_delivery()
        self.assertEqual(self.projectile_ops(self.send()), [])
        self.ack()
        create = self.send()
        self.assertTrue(any(item.entity == bot.entity and item.op == OP_CREATE for item in create))
        self.assertEqual(self.projectile_ops(create), [])
        self.ack()

        full = self.send()
        self.assertTrue(any(item.entity == bot.entity and item.chunk is not None for item in full))
        self.advance_delivery()
        self.assertEqual(self.projectile_ops(self.send()), [])  # source full has not been acknowledged
        self.ack()
        lost_volley = self.send(lose=True)
        [chunk] = [item.chunk for item in lost_volley if item.entity == bot.entity and item.chunk is not None]

        self.advance_delivery()
        projectile = self.send()
        self.assertEqual(self.projectile_ops(projectile), [OP_CREATE])
        self.assertFalse(any(item.ch2 is not None for item in projectile))
        self.ack()
        self.advance_delivery()
        retry = self.send()
        [resent] = [item.chunk for item in retry if item.entity == bot.entity and item.chunk is not None]
        self.assertEqual((resent.count, resent.getvalue()), (chunk.count, chunk.getvalue()))
        self.assertFalse(any(item.ch2 is not None for item in retry))
        self.ack()

        self.advance_delivery()
        [launch] = [item for item in self.send() if item.ch2 is not None]
        self.assertEqual(launch.entity, bot.entity)
        bits = bits_of(launch.ch2)
        self.assertEqual((bits.bit(), bits.bit(), bits.var_b()), (1, 1, self.shot_frame))
        record = read_shot(bits)
        self.assertEqual(
            (record["instance"], record["state"], record["activation"], record["ids"]),
            (state.instance.id, state.index, 1, [projectiles.SEEN_FIRST]),
        )

    def test_retired_npc_source_cannot_launch_against_a_reused_entity(self):
        bot, state = self.npc_source()
        self.assertIsNotNone(self.fire_npc(bot, state))
        replacement = bots.respawned(bot, bot.entity, self.tick)
        replacement.body_script = bot.body_script
        self.match.bots = [replacement]
        self.client.queue_entities([self.match._bot_create_update(replacement)])
        self.advance_delivery()
        self.assertEqual(self.projectile_ops(self.send()), [])
        self.ack()
        self.npc_volley(replacement, state)
        for _ in range(3):
            self.advance_delivery()
            records = self.send()
            self.assertEqual(self.projectile_ops(records), [])
            self.assertFalse(any(item.ch2 is not None for item in records))
            self.ack()
        self.assertFalse(self.fight.projectiles.observed)

    def test_npc_shot_expires_before_source_ack_without_creating_a_projectile(self):
        bot, state = self.npc_source()
        shot = self.fire_npc(bot, state)
        self.assertIsNotNone(shot)
        self.fight.projectiles.flown(shot, 0.08)
        self.advance_delivery(projectiles.OBSERVER_GRACE_TICKS)
        for _ in range(3):
            records = self.send()
            self.assertEqual(self.projectile_ops(records), [])
            self.assertFalse(any(item.ch2 is not None for item in records))
            self.ack()
            self.advance_delivery()
        self.assertFalse(self.fight.projectiles.observed)


class BioticFieldTests(unittest.TestCase):
    def test_canister_confirmation_uses_the_cast_timer_frame_after_a_gap(self):
        run = Run()
        player = run.players[0]
        fight = run.match.combat.projectiles
        cast = run.frame + 1
        player.take_commands([Command(cast, buttons=ABILITY_2)])

        # The 400 ms cast timer expires inside the missed command window.
        received = cast + 39
        player.take_commands([Command(received)])
        [shot] = fight.shots
        launched = cast + 25
        self.assertEqual((shot.definition, shot.frame), (CANISTER, launched))

        # No owner report has arrived, so the timeout confirms the scheduled lease.
        fight.update(received + projectiles.UNREPORTED_TICKS)
        expected = fight.lease_id(player, CANISTER, launched)
        late = fight.lease_id(player, CANISTER, received)
        creates = {item.entity for item in player.client.entities if item.op == OP_CREATE}
        self.assertIn(expected, creates)
        self.assertNotIn(late, creates)

    def test_the_default_field_create_keeps_its_wire_bytes(self):
        # These pre-theme records cover both paths, with the same non-zero observer position.
        for parent, size, expected in (
            (
                0xA0100042, 547,
                "728f030000000000000000000000000000000000000000000000000000e00f0000e00f0000"
                "e00f0000e04f0c7000800400004800008004000000a010000428000000000000",
            ),
            (
                None, 350,
                "720f030000e80f00000830000010100000000000000000000000000000e00f0000e00f0000"
                "e00f0000e00f00",
            ),
        ):
            with self.subTest(parent=parent):
                record = projectiles.field_create(parent, (1.25, -2.5, 3.0))(0)
                self.assertEqual((record.count, record.getvalue().hex()), (size, expected))

    def test_the_skin_theme_reaches_the_owner_and_observer_field(self):
        run = Run(KANEZAKA_DM, ("Owner", "Observer"))
        owner, observer = run.players
        grillmaster = 0x0A50000000001BC8  # replaces the field's 0B06.00D ring
        run.match.skin_of = lambda account, _hero: (
            (grillmaster, True) if account == owner.account_lo else (0, False)
        )
        run.match.switch_hero(owner, heroes()[SOLDIER])
        self.assertEqual(owner.skin, (grillmaster, True))
        for _ in range(20):
            run.step({})
        run.step({owner: (0, 0, ABILITY_2)})
        for _ in range(60):
            run.step({})
        [shot] = run.match.combat.projectiles.shots
        self.assertTrue(shot.created)
        for viewer, parents in ((owner, shot.created), (observer, [None])):
            with self.subTest(viewer=viewer.name):
                creates = [
                    item for item in viewer.client.entities
                    if item.op == OP_CREATE and item.entity >> 16 == 0xA008
                ]
                self.assertEqual(len(creates), len(parents))
                for item, parent in zip(creates, parents, strict=True):
                    record = item.build(0)
                    reader = bits_of(record)
                    self.assertEqual((reader.bit(), reader.bit(), reader.bits(12)), (0, 1, 0x03DC))
                    flags = world.WITH_TRANSFORM | world.WITH_SCALE | world.WITH_SKIN | world.SKIN_THEME
                    if parent is not None:
                        flags |= world.WITH_MOVEMENT
                    self.assertEqual(reader.bits(8), flags)
                    self.assertEqual((reader.bits(32), reader.bit()), (0x1BC8, 0))
                    position = struct.unpack("<3f", reader.bits(96).to_bytes(12, "little"))
                    reader.bits(7 * 32)  # rotation and scale
                    if parent is None:
                        self.assertEqual(position, shot.where)
                    else:
                        self.assertEqual(position, (0.0, 0.0, 0.0))
                        movement = read_movement(reader, world.CREATE_BASE, parent=parent)
                        self.assertEqual(movement["flags"], projectiles.KEEP_ROTATION)
                    self.assertEqual(reader.bits(8), 0)  # no components
                    self.assertEqual(reader.pos, record.count)

    def test_the_canister_the_field_entity_its_frame_and_the_heal(self):
        run = Run(KANEZAKA_DM, ("A", "B"))
        a, b = run.players
        for _ in range(20):
            run.step({})
        vitals = run.match.combat.vitals_of(next(t for t in run.match.combat._targets() if t.owner is a))
        vitals.damage(100.0)
        a.client.entities.clear()
        b.client.entities.clear()
        run.step({a: (0, 0, ABILITY_2)})
        for _ in range(60):
            run.step({})
        fights = run.match.combat.projectiles
        field_creates = [
            item for item in a.client.entities if item.op == OP_CREATE and item.entity >> 16 == 0xA008
        ]
        self.assertTrue(field_creates)
        record = field_creates[0].build(0)
        self.assertEqual(record.count > 0, True)
        frames = [
            item for item in a.client.entities if item.chunk is not None and item.entity >> 16 == 0xA008
        ]
        self.assertTrue(frames)
        decoded = read_chunk(frames[0].chunk.getvalue(), frames[0].chunk.count, {1: projectiles.FIELD_GRAPH})
        self.assertEqual(decoded.instances[1].graph, projectiles.FIELD_GRAPH)
        on = {index for index, (active, _) in decoded.instances[1].states.items() if active}
        self.assertEqual(on, {2, 3, 5})
        self.assertTrue(
            [item for item in b.client.entities if item.op == OP_CREATE and item.entity >> 16 == 0xA008]
        )
        self.assertGreater(vitals.now[HEALTH_POOL], 100.0)  # 35 a second on the owner standing in it
        for _ in range(int(projectiles.FIELD_SECONDS / 0.016) + 5):
            run.step({})
        self.assertFalse(fights.shots)
        destroyed = {item.entity for item in a.client.entities if item.op == OP_DESTROY}
        self.assertTrue({item.entity for item in field_creates} <= destroyed)


class ObserverTests(unittest.TestCase):
    def test_the_other_client_sees_the_weapon_and_the_shots(self):
        run = Run(KANEZAKA_DM, ("A", "B"))
        a, b = run.players
        for _ in range(12):
            run.step({})
        model = ClientBody.for_hero(SOLDIER)
        for item in b.client.entities:
            if item.chunk is not None and item.entity == a.body:
                model.apply(item)
        on = model.on()
        self.assertIn((11, 11), on)  # 0254 st11: the weapon in his hands (CosmeticEntity)
        self.assertNotIn((11, 70), on)  # no HUD presenter of his
        b.client.entities.clear()
        for _ in range(10):
            run.step({a: (0, 0, 0x01)})
        for item in b.client.entities:
            if item.chunk is not None and item.entity == a.body:
                model.apply(item)
        self.assertIn((11, 10), model.on())  # his rifle volley: B's client shoots its tracers itself


if __name__ == "__main__":
    unittest.main()
