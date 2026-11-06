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
from ow174.game.commands import FIRE, Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.match import sends
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
    def test_replacing_the_shooter_retires_the_projectiles_damage(self):
        for replace_body, expected_health in ((False, 80.0), (True, 200.0)):
            with self.subTest(replace_body=replace_body):
                run = Run(KANEZAKA_DM, ("Shooter", "Target"))
                shooter, victim = run.players
                match = run.match
                fight = match.combat
                match.bots.clear()
                y = match.game_map.spawns[0].position[1]
                shooter.mover.position = (0.0, y, 0.0)
                victim.mover.position = (0.0, y, 4.0)
                target = next(target for target in fight._targets() if target.owner is victim)
                vitals = fight.vitals_of(target)

                frame = shooter.body_script.frame + 1
                shooter.body_script.command(frame, SECONDARY)
                fight.command(shooter, Command(frame, buttons=SECONDARY))
                fight.update(20.0, frame, frames=False)
                self.assertEqual(vitals.total, 200.0)
                self.assertEqual(len(fight.rockets), 1)
                if replace_body:
                    old = shooter.body
                    self.assertTrue(match.switch_hero(shooter, shooter.hero))
                    self.assertNotEqual(shooter.body, old)
                    fight.update(20.0 + combat.TICK, frame + 1, frames=False)
                    self.assertFalse(fight.projectiles.shots)

                for step in range(2, 11):
                    fight.update(20.0 + step * combat.TICK, frame + step, frames=False)
                self.assertFalse(fight.rockets)
                self.assertEqual(vitals.total, expected_health)

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


class OwnerProjectileDeliveryTests(unittest.TestCase):
    def setUp(self):
        run = Run()
        self.match = run.match
        self.match.bots.clear()
        for _ in range(24):
            run.step({})
        self.player, = run.players
        self.tick = run.frame
        self.server = GameServer()
        self.server.sock = MemorySocket()
        key_in, key_out = b"i" * 32, b"o" * 32
        self.client = Client(
            self.server, ("memory.test", 40001), 1, LinkCipher(key_in, key_out), self.player,
        )
        self.server.clients[1] = self.client
        self.client.open = True
        self.player.client = self.client
        self.client._ack_entity_create(self.player.entity)  # the earlier model already bound the controller
        self.cipher = LinkCipher(key_out, key_in)
        self.window = ReceiveWindow()
        self.seq = 0
        self.projectiles = self.match.combat.projectiles
        frame = self.tick + 1
        # A near-floor impact becomes due while the owner volley ACK is delayed.
        self.player.take_commands([Command(frame, pitch=16384, buttons=SECONDARY)])
        self.shot, = self.projectiles.shots
        self.lease = self.projectiles.lease_id(self.player, HELIX, frame)

    def advance(self):
        self.tick += 1
        self.match.update(self.tick * combat.TICK, self.tick)

    def send(self, *, lose=False):
        self.client.send_frame(self.tick, self.tick * combat.TICK)
        packet, _, _, _, _, _ = decoded(self, self.cipher, self.server.sock.sent[-1][0])
        if not lose:
            self.assertTrue(self.window.accept(packet.seq))
        return [(item.op, item.ch2 is not None) for item in self.client.in_flight[packet.seq][1]
                if item.entity == self.lease]

    def ack(self):
        packet = Packet(self.client.conn, KIND_CONTROL, self.seq, self.window.expected, self.window.bits, ACK)
        self.seq += 1
        self.server._datagram(self.cipher.seal(packet), self.client.address, self.tick * combat.TICK)

    def late_volley_ack(self):
        for _ in range(13):
            self.advance()
            self.assertEqual(self.send(), [])
        self.assertLess(self.shot.release_at, self.tick)
        self.assertTrue(self.shot.reports)
        self.assertFalse(self.shot.created)
        self.ack()
        self.advance()

    def test_owner_volley_report_waits_for_a_successfully_sent_chunk(self):
        from test_game_repeated_lifecycles import wire_frame

        script = self.player.body_script
        before = script.report_count
        self.advance()
        self.advance()
        owner, = [item for item in self.client.entities if item.stream is script.stream]
        self.client.queue_reliable(
            20302, {"+0x78": {"+0x0": self.player.entity}, "+0x7C": False, "+0x80": 0},
        )
        budget = len(self.client._payload(self.tick, self.client.reliable, [], []))
        with patch("ow174.game.server.MAX_PAYLOAD", budget):
            self.assertEqual(self.send(), [])
        self.ack()
        self.assertIn(owner, self.client.entities)
        self.assertEqual(script.report_count, before)

        self.server.sock.fail_next = True
        with self.assertLogs("ow174.game", level="WARNING"):
            self.client.send_frame(self.tick, self.tick * combat.TICK)
        self.assertIn(owner, self.client.entities)
        self.assertEqual(script.report_count, before)
        self.player.take_commands([Command(self.tick + 1)])
        self.advance()
        self.assertEqual(self.shot.reports, [])

        self.assertEqual(self.send(), [])
        packet, _, _, _, _, _ = decoded(self, self.cipher, self.server.sock.sent[-1][0])
        _, records = wire_frame(packet)
        chunk, = [item.chunk for item, _ in records if item.entity == self.player.body and item.chunk]
        known = {number: value[0] for number, value in script.stream.base.instances.items()}
        frame = read_chunk(chunk.getvalue(), chunk.count, known)
        self.assertTrue(frame.owner)
        self.assertLess(frame.cmfd, script.frame)
        self.assertIn(self.shot.state, frame.instances[self.shot.instance].states)
        report = script.reports[-1]
        self.assertEqual((report.number, report.cmfd, report.last), (before + 1, frame.cmfd, frame.last))
        self.assertEqual(report.volleys[(self.shot.instance, self.shot.state)], self.shot.activation)
        self.assertIsNone(report.received)
        self.assertFalse(self.shot.created)
        self.ack()
        self.advance()
        shot_report, = self.shot.reports
        self.assertIs(shot_report, report)
        self.assertIs(report.received, True)
        self.assertEqual(self.send(), [(OP_CREATE, False)])

    def send_owner_report(self, steps, *, lose):
        from test_game_repeated_lifecycles import wire_frame

        script = self.player.body_script
        known = {number: value[0] for number, value in script.stream.base.instances.items()}
        self.player.take_commands([Command(self.tick + steps)])
        for _ in range(steps):
            self.advance()
        self.assertEqual(self.send(lose=lose), [])
        packet, _, _, _, _, _ = decoded(self, self.cipher, self.server.sock.sent[-1][0])
        _, records = wire_frame(packet)
        chunk, = [item.chunk for item, _ in records if item.entity == self.player.body and item.chunk]
        frame = read_chunk(chunk.getvalue(), chunk.count, known)
        self.assertTrue(frame.owner)
        self.assertIn(self.shot.state, frame.instances[self.shot.instance].states)
        return packet, frame

    def confirm_owner_reports(self):
        from test_game_repeated_lifecycles import wire_frame

        self.advance()
        self.assertEqual(self.send(), [(OP_CREATE, False)])
        packet, _, _, _, _, _ = decoded(self, self.cipher, self.server.sock.sent[-1][0])
        _, records = wire_frame(packet)
        return {item.entity for item, _ in records if item.op == OP_CREATE}

    def test_cumulative_owner_ack_confirms_only_the_delivered_report_frame(self):
        lost_packet, lost = self.send_owner_report(2, lose=True)
        arrived_packet, arrived = self.send_owner_report(3, lose=False)
        self.assertLess(self.shot.frame, lost.cmfd)
        self.assertLess(lost.cmfd, arrived.cmfd)
        self.assertEqual((arrived.first, arrived.last), (lost.last, lost.last + 1))
        self.assertEqual(arrived_packet.seq, lost_packet.seq + 1)
        self.assertEqual(self.window.bits & 0b11, 0b01)
        self.ack()
        script = self.player.body_script
        self.assertEqual(script.stream.base_last, arrived.last)

        lost_lease = self.projectiles.lease_id(self.player, HELIX, lost.cmfd)
        arrived_lease = self.projectiles.lease_id(self.player, HELIX, arrived.cmfd)
        self.assertEqual(len({self.lease, lost_lease, arrived_lease}), 3)
        self.assertEqual(self.confirm_owner_reports(), {self.lease, arrived_lease})

    def test_one_ack_confirms_both_positively_delivered_owner_reports(self):
        _, earlier = self.send_owner_report(2, lose=False)
        _, later = self.send_owner_report(3, lose=False)
        self.assertEqual(self.window.bits & 0b11, 0b11)
        self.ack()

        earlier_lease = self.projectiles.lease_id(self.player, HELIX, earlier.cmfd)
        later_lease = self.projectiles.lease_id(self.player, HELIX, later.cmfd)
        self.assertEqual(len({self.lease, earlier_lease, later_lease}), 3)
        self.assertEqual(self.confirm_owner_reports(), {self.lease, earlier_lease, later_lease})

    def test_deferred_owner_report_keeps_its_encode_baseline(self):
        _, earlier = self.send_owner_report(2, lose=False)
        script = self.player.body_script
        self.player.take_commands([Command(self.tick + 3)])
        for _ in range(3):
            self.advance()
        later, = [item for item in self.client.entities if item.stream is script.stream]
        later_frame = script.stream.cmfd

        # The earlier ACK moves the stream base before this projection reaches
        # the wire. Both encoded reports introduced the activation to their base.
        self.ack()
        self.assertEqual(script.stream.base_last, earlier.last)
        self.assertEqual(self.send(), [])
        self.assertEqual(script.reports[-1].last, later.chunk_last)
        self.ack()
        earlier_lease = self.projectiles.lease_id(self.player, HELIX, earlier.cmfd)
        later_lease = self.projectiles.lease_id(self.player, HELIX, later_frame)
        self.assertEqual(len({self.lease, earlier_lease, later_lease}), 3)
        self.assertEqual(self.confirm_owner_reports(), {self.lease, earlier_lease, later_lease})

    def test_all_lost_owner_reports_fall_back_to_the_shot_frame(self):
        packet, lost = self.send_owner_report(2, lose=True)
        for _ in range(3):
            self.advance()
        self.assertEqual(self.send(), [])  # A newer datagram with no new command frame.
        self.ack()
        self.assertNotIn(packet.seq, self.client.in_flight)
        self.assertNotIn(lost.last, self.player.body_script.stream.pending)

        deadline = self.shot.frame + projectiles.UNREPORTED_TICKS
        while self.tick < deadline - 1:
            self.advance()
        self.assertFalse(self.shot.created)
        self.assertEqual(self.confirm_owner_reports(), {self.lease})
        self.assertEqual(self.tick, deadline)

    def test_late_volley_ack_still_creates_before_releasing_the_owner_rocket(self):
        self.late_volley_ack()
        self.assertEqual(self.send(), [(OP_CREATE, False)])
        self.ack()
        self.advance()
        self.assertEqual(self.send(), [(OP_DESTROY, True)])
        self.assertFalse(self.projectiles.shots)

    def test_lost_confirmation_retries_before_releasing_the_owner_rocket(self):
        self.late_volley_ack()
        self.assertEqual(self.send(lose=True), [(OP_CREATE, False)])
        self.advance()
        self.assertEqual(self.send(), [])
        self.ack()  # the next packet exposes the missing create
        self.advance()
        self.assertEqual(self.send(), [(OP_CREATE, False)])
        self.ack()
        self.advance()
        self.assertEqual(self.send(), [(OP_DESTROY, True)])

    def test_unacknowledged_confirmation_expires_without_releasing_an_explosion(self):
        self.late_volley_ack()
        self.assertEqual(self.send(lose=True), [(OP_CREATE, False)])
        deadline = self.shot.release_at + projectiles.DELIVERY_GRACE_TICKS
        while self.tick < deadline:
            self.advance()
        self.assertFalse(self.projectiles.shots)
        self.assertEqual(self.send(), [(OP_DESTROY, False)])
        self.ack()
        self.assertEqual(self.send(), [])

    def test_body_replacement_retires_a_pending_confirmation(self):
        self.late_volley_ack()
        self.assertEqual(self.send(), [(OP_CREATE, False)])
        self.match.switch_hero(self.player, self.player.hero)
        self.advance()
        self.assertFalse(self.projectiles.shots)
        self.assertEqual(self.send(), [(OP_DESTROY, False)])
        self.ack()
        self.assertEqual(self.send(), [])

    def test_old_command_frame_does_not_consume_grace_before_impact(self):
        # The scheduler reached a later tick before the accepted shot could fly.
        self.tick += 90
        for _ in range(4):
            self.advance()
            if self.shot.release_at is not None:
                break
        self.assertEqual(self.shot.release_at, self.tick)
        self.assertEqual(self.send(), [(OP_CREATE, False)])
        self.ack()
        self.advance()
        self.assertEqual(self.send(), [(OP_DESTROY, True)])


class OwnerVolleyDeliveryTests(unittest.TestCase):
    def play(self, hero, commands, *, weapon_switch_frame=None):
        from test_game_repeated_lifecycles import wire_frame

        for diagnostics in (bodytrace, movelog):
            self.enterContext(patch.object(diagnostics, "ENABLED", False))
        run = Run()
        player, = run.players
        match = run.match
        match.bots.clear()
        match._pending_bot_scripts.clear()
        match.switch_hero(player, heroes()[hero])
        while run.frame < 1016:
            if run.frame + 1 == weapon_switch_frame:
                run.frame += 1
                match.update(run.frame * combat.TICK, run.frame)
                player.take_commands([Command(run.frame, action=2)])
            else:
                run.step({})

        server = GameServer()
        server.sock = MemorySocket()
        key_in, key_out = b"i" * 32, b"o" * 32
        client = Client(server, ("memory.test", 40001), 1, LinkCipher(key_in, key_out), player)
        server.clients[client.conn] = client
        client.open = True
        player.client = client
        client._ack_entity_create(player.entity)  # the earlier model already bound the controller
        cipher = LinkCipher(key_out, key_in)
        window = ReceiveWindow()
        sequence = 0
        script = player.body_script
        reports = {}
        created = []

        for command in commands:
            frame = command.frame
            now = frame * combat.TICK
            match.update(now, frame)
            if sends(frame):
                client.send_frame(frame, now)
                packet = decoded(self, cipher, server.sock.sent[-1][0])[0]
                known = {index: record[0] for index, record in script.stream.base.instances.items()}
                for item, _ in wire_frame(packet)[1]:
                    if item.op == OP_CREATE:
                        created.append(item.entity)
                    if item.entity == player.body and item.chunk is not None:
                        owner = read_chunk(item.chunk.getvalue(), item.chunk.count, known)
                        self.assertTrue(owner.owner)
                        reports[owner.cmfd] = owner
                self.assertTrue(window.accept(packet.seq))
                ack = Packet(client.conn, KIND_CONTROL, sequence, window.expected, window.bits, ACK)
                sequence += 1
                server._datagram(cipher.seal(ack), client.address, now)
            player.take_commands([command])
        return player, reports, created

    def test_rapid_arrows_use_only_their_own_delivered_volley_activation(self):
        releases = (1022, 1058)
        commands = [
            Command(frame, buttons=FIRE if 1017 <= frame < 1022 or 1053 <= frame < 1058 else 0)
            for frame in range(1017, 1064)
        ]
        player, reports, _ = self.play(0x02E0000000000005, commands)  # Hanzo

        delivery = player.match.combat.projectiles
        arrows = {shot.frame: shot for shot in delivery.shots}
        self.assertEqual(set(arrows), set(releases))
        for activation, frame in enumerate(releases, 1):
            with self.subTest(frame=frame):
                shot = arrows[frame]
                self.assertEqual((shot.definition, shot.instance, shot.state), (0x040000000000020B, 11, 4))
                active, payload = reports[frame].instances[11].states[4]
                self.assertFalse(active)
                self.assertEqual((payload["counter"], payload["start"]), (activation, frame * 16))
                lease = delivery.lease_id(player, shot.definition, frame)
                self.assertIsNotNone(lease)
                self.assertEqual(set(shot.created), {lease})

    def test_mercy_stop_of_an_acknowledged_volley_adds_no_alternate_lease(self):
        commands = [
            Command(frame, buttons=FIRE if 1022 <= frame < 1090 else 0)
            for frame in range(1017, 1115)
        ]
        player, reports, created = self.play(0x02E0000000000004, commands, weapon_switch_frame=1011)  # Mercy
        start, stop = (reports[frame].instances[11].states[6] for frame in (1043, 1091))
        self.assertTrue(start[0])
        self.assertFalse(stop[0])
        self.assertEqual(start[1], stop[1])

        delivery = player.match.combat.projectiles
        shots = delivery.shots
        self.assertEqual([shot.frame for shot in shots], [1042, 1054, 1067, 1079])
        self.assertEqual({shot.activation for shot in shots}, {(1, 16676)})
        definition = 0x0400000000000158
        leases = [delivery.lease_id(player, definition, shot.frame) for shot in shots]
        start_lease = delivery.lease_id(player, definition, 1043)
        stop_lease = delivery.lease_id(player, definition, 1091)
        self.assertNotIn(stop_lease, created)
        expected_leases = {*leases, start_lease}
        self.assertCountEqual([item for item in created if item in expected_leases], expected_leases)
        self.assertEqual([set(shot.created) for shot in shots], [
            {leases[0], start_lease}, {leases[1]}, {leases[2]}, {leases[3]},
        ])


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
        script = SimpleNamespace(graphs=lambda: {instance.id: instance.graph}, latest_live=baseline)
        script.initial = observers.full_frame(script, baseline), baseline
        bot = bots.make(PRACTICE_RANGE.map_guid)[0]
        bot.body_script = script
        self.match.bots = [bot]
        self.shooter.client = None
        self.client.queue_entities([self.match._bot_create_update(bot)])
        return bot, state

    def fire_npc(self, bot, state, *, snapshot=None):
        self.shot_frame = self.tick + 1
        return self.fight.projectiles.fired(
            bot, state, self.shot_frame, HELIX, 2.0, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0),
            activation=(state.counter, state.start), snapshot=snapshot,
        )

    def npc_snapshot(self, state, value=None):
        instance = state.instance
        return Snapshot(
            self.tick, instances={instance.id: (instance.graph.index, None)},
            vars={(instance.id, 32): (value, ())} if value is not None else {},
            states={(instance.id, state.index): (state.node.cls, False, state.payload())},
        )

    def npc_volley(self, bot, state):
        self.client.queue_bot_live(bot.entity, self.npc_snapshot(state))

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
        snapshot = self.npc_snapshot(state)
        self.assertIsNotNone(self.fire_npc(bot, state, snapshot=snapshot))
        self.client.queue_bot_live(bot.entity, snapshot)
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
        shot = self.fire_npc(bot, state, snapshot=self.npc_snapshot(state))
        self.assertIsNotNone(shot)
        self.fight.projectiles.flown(shot, 0.08)
        self.advance_delivery(projectiles.DELIVERY_GRACE_TICKS)
        for _ in range(3):
            records = self.send()
            self.assertEqual(self.projectile_ops(records), [])
            self.assertFalse(any(item.ch2 is not None for item in records))
            self.ack()
            self.advance_delivery()
        self.assertFalse(self.fight.projectiles.observed)

    def test_expired_npc_launch_unpins_the_latest_state_and_never_retries(self):
        bot, state = self.npc_source()
        for _ in range(2):
            self.send()  # source create, then source full
            self.ack()
        snapshot = self.npc_snapshot(state)
        shot = self.fire_npc(bot, state, snapshot=snapshot)
        self.client.queue_bot_live(bot.entity, snapshot)
        self.assertEqual(self.projectile_ops(self.send()), [OP_CREATE])
        self.ack()
        self.advance_delivery()
        self.assertTrue(any(item.ch2 is not None for item in self.send(lose=True)))

        self.fight.projectiles.flown(shot, 0.08)
        state.counter = 2
        self.client.queue_bot_live(bot.entity, self.npc_snapshot(state))
        self.advance_delivery(projectiles.DELIVERY_GRACE_TICKS)
        retired = self.send()
        self.assertEqual(self.projectile_ops(retired), [OP_DESTROY])
        self.assertFalse(any(item.ch2 is not None for item in retired))
        [chunk] = [item.chunk for item in retired if item.entity == bot.entity and item.chunk is not None]
        delta = read_chunk(chunk.getvalue(), chunk.count, self.known)
        self.assertEqual(delta.instances[state.instance.id].states[state.index][1]["counter"], 2)
        self.ack()  # reports loss of the canceled launch
        self.assertFalse(any(item.ch2 is not None for item in self.send()))

    def test_npc_launch_acks_order_volley_activations_through_backlog(self):
        bot, state = self.npc_source()
        self.send()  # NPC create
        self.ack()
        self.send()  # full frame remains unacknowledged while three shots are fired
        frames = []
        for counter in (1, 2, 3):
            self.advance_delivery(19)
            state.counter = counter
            snapshot = self.npc_snapshot(state, float(counter))
            self.assertIsNotNone(self.fire_npc(bot, state, snapshot=snapshot))
            self.client.queue_bot_live(bot.entity, snapshot)
            frames.append(self.shot_frame)
        self.client.queue_bot_live(bot.entity, self.npc_snapshot(state, 4.0))  # ordinary state coalesces

        def volley(records):
            [chunk] = [item.chunk for item in records if item.entity == bot.entity and item.chunk is not None]
            frame = read_chunk(chunk.getvalue(), chunk.count, self.known)
            return frame.instances[state.instance.id]

        def launch(records, counter):
            [item] = [item for item in records if item.ch2 is not None]
            self.assertEqual(item.entity, bot.entity)
            bits = bits_of(item.ch2)
            self.assertEqual((bits.bit(), bits.bit(), bits.var_b()), (1, 1, frames[counter - 1]))
            record = read_shot(bits)
            self.assertEqual(
                (record["activation"], record["ids"]), (counter, [projectiles.SEEN_FIRST + counter - 1]),
            )
            return item.ch2.getvalue()

        self.ack()
        self.advance_delivery()
        first = self.send()
        self.assertEqual(volley(first).states[state.index][1]["counter"], 1)
        self.assertEqual(volley(first).vars, {32: (4.0, ())})
        self.assertFalse(any(item.ch2 is not None for item in first))
        self.ack()
        self.advance_delivery()
        lost_launch = launch(self.send(lose=True), 1)
        self.advance_delivery()
        self.assertFalse(any(item.chunk is not None or item.ch2 is not None for item in self.send()))
        self.ack()  # loss report does not unlock activation 2
        retried = self.send()
        self.assertFalse(any(item.chunk is not None for item in retried))
        self.assertEqual(launch(retried, 1), lost_launch)
        self.ack()

        for counter in (2, 3):
            self.advance_delivery()
            installed = self.send()
            self.assertEqual(volley(installed).states[state.index][1]["counter"], counter)
            self.assertFalse(any(item.ch2 is not None for item in installed))
            self.ack()
            self.advance_delivery()
            launch(self.send(), counter)
            self.ack()


class BioticFieldTests(unittest.TestCase):
    def test_active_field_heals_elapsed_ticks_once_and_stops_at_expiry(self):
        run = Run(KANEZAKA_DM)
        player, = run.players
        fight = run.match.combat
        target = next(target for target in fight._targets() if target.owner is player)
        vitals = fight.vitals_of(target)
        vitals.damage(199.0)
        run.step({player: (0, 0, ABILITY_2)})
        for _ in range(80):
            run.step({})
            if fight.projectiles.shots and fight.projectiles.shots[0].field_tick is not None:
                break
        field, = fight.projectiles.shots
        self.assertEqual(field.field_tick, run.frame)
        started = run.frame
        healing = projectiles.HEAL_PER_SECOND * combat.TICK

        def update(offset):
            tick = started + offset
            run.match.update(tick * combat.TICK, tick)

        update(5)
        self.assertAlmostEqual(vitals.total, 1.0 + 5 * healing)
        update(5)
        self.assertAlmostEqual(vitals.total, 1.0 + 5 * healing)
        for offset in range(6, 310):
            update(offset)
        self.assertAlmostEqual(vitals.total, 1.0 + 309 * healing)
        update(315)
        self.assertEqual(fight.projectiles.shots, [])
        self.assertAlmostEqual(vitals.total, 1.0 + 312 * healing)
        update(316)
        self.assertAlmostEqual(vitals.total, 1.0 + 312 * healing)

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
