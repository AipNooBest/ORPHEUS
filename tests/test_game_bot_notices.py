"""NPC notice and health ordering through the encrypted game link, without a listening socket."""

import struct
import unittest
from unittest.mock import patch

from test_game_packet_budget import connection, decoded, large_messages
from test_game_repeated_lifecycles import wire_frame

from ow174.game import bodytrace, bots, combat, movelog, world
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.server import Client


class BotNoticeTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)

    def prepare(self, *, last=False, single=False):
        self.server, self.client, self.player, self.peer = connection()
        self.match = self.player.match
        self.match.bots = bots.make(PRACTICE_RANGE.map_guid)
        self.match.tick = 100
        self.player.spawned = self.player.has_body = True
        enemies = [target for target in self.match.combat._targets() if target.team != self.player.team]
        self.target = enemies[-1] if last else enemies[0]
        self.entity = self.target.entity
        updates = self.match.spawn_updates(self.player)
        if single:
            updates = [record for record in updates if record.entity == self.entity]
        self.client.queue_entities(updates)
        self.window = ReceiveWindow()
        self.seq = 0

    def send(self, tick, *, deliver=True):
        self.match.tick = tick
        self.client.send_frame(tick, 10.0)
        packet, wire_tick, reliable, _, _, count = decoded(self, self.peer, self.server.sock.sent[-1][0])
        ids, records = wire_frame(packet)
        self.assertEqual((wire_tick, ids, len(records)), (tick, [number for _, number, _ in reliable], count))
        self.health = self.bot_health(records)
        if deliver:
            self.assertTrue(self.window.accept(packet.seq))
        return reliable, [(record.entity, record.op) for record, _ in records]

    def bot_health(self, records):
        return [
            (record.op, struct.unpack_from("<Iff", fields[51][0])[2])
            for record, fields in records if record.entity == self.entity and 51 in fields
        ]

    def ack(self):
        packet = Packet(self.client.conn, KIND_CONTROL, self.seq, self.window.expected, self.window.bits, ACK)
        self.seq += 1
        self.server._datagram(self.peer.seal(packet), self.client.address, 10.0)

    def damage_bot(self, amount, tick):
        self.match.tick = tick
        self.match.combat._damage(self.player, self.target, amount, False, None, tick)
        self.match.combat._send_health()

    def kill_bot(self):
        self.damage_bot(self.target.owner.health, 101)

    @staticmethod
    def notices(reliable):
        notice_ids = (combat.HIT, combat.KILL_NOTICE)
        return [(number, value) for _, number, value in reliable if number in notice_ids]

    def assert_kill_notices(self, reliable):
        self.assertEqual(self.notices(reliable), [
            (combat.HIT, {"+0x78": {"+0x0": {"+0x0": self.entity}, "+0x4": int(self.target.owner.health),
                                     "+0x8": combat.KILLING}, "+0x84": 101}),
            (combat.KILL_NOTICE, {"+0x78": 0, "+0x80": {"+0x0": self.player.body},
                                  "+0x84": {"+0x0": self.entity}, "+0x88": 0, "+0x89": False}),
        ])

    def test_budget_delayed_create_and_duplicate_ack(self):
        self.prepare(last=True)
        large_messages(self.client, self.player, 2)
        self.kill_bot()
        for tick in (101, 104, 107, 110, 113):
            reliable, records = self.send(tick)
            self.assertEqual(self.notices(reliable), [])
            if (self.entity, world.OP_CREATE) in records:
                break
        else:
            self.fail("bot create stayed queued")
        self.assertFalse(self.client.bot_create_acked(self.entity))
        self.ack()
        self.ack()  # same ACK window in a fresh authenticated inbound packet
        self.assertTrue(self.client.bot_create_acked(self.entity))
        reliable, _ = self.send(116)
        self.assert_kill_notices(reliable)
        self.ack()
        reliable, _ = self.send(119)
        self.assertEqual(self.notices(reliable), [])

    def test_lost_create_and_failed_resend_wait_for_positive_ack(self):
        self.prepare(single=True)
        _, records = self.send(100, deliver=False)
        self.assertEqual(records, [(self.entity, world.OP_CREATE)])
        self.kill_bot()
        reliable, records = self.send(104)
        self.assertEqual(self.notices(reliable), [])
        self.assertNotIn((self.entity, world.OP_CREATE), records)
        self.ack()  # ACK the following frame with the create's sequence missing
        self.assertFalse(self.client.bot_create_acked(self.entity))
        sent_count = len(self.server.sock.sent)
        self.server.sock.fail_next = True
        self.client.send_frame(105, 10.0)
        self.assertEqual(len(self.server.sock.sent), sent_count)
        self.assertFalse(self.client.bot_create_acked(self.entity))
        reliable, records = self.send(107)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_CREATE), records)
        self.ack()
        reliable, _ = self.send(110)
        self.assert_kill_notices(reliable)

    def test_reused_entity_id_ignores_ack_for_previous_lifetime(self):
        self.prepare(single=True)
        build = self.client.entities[0].build
        self.send(100)
        self.damage_bot(20, 101)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_DESTROY)])
        self.send(104, deliver=False)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_CREATE, build)])
        self.match.combat._tell_hit(self.player, self.target, 30, 0, 105)
        self.client.queue_bot_health(self.entity, world.health(170, 200))
        self.ack()  # only the old create
        self.assertFalse(self.client.bot_create_acked(self.entity))
        reliable, records = self.send(107)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_CREATE), records)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.ack()
        self.match.combat._tell_hit(self.player, self.target, 40, 0, 108)
        reliable, _ = self.send(110)
        self.assertEqual([(number, value["+0x84"]) for number, value in self.notices(reliable)],
                         [(combat.HIT, 105), (combat.HIT, 108)])
        self.assertEqual(self.health, [(world.OP_UPDATE, 170)])

    def test_respawn_and_drop_discard_pending_notices(self):
        self.prepare(single=True)
        self.send(100)
        self.kill_bot()
        self.match.combat._respawn_bot(self.target.owner)
        self.ack()
        self.assertFalse(self.client.bot_create_acked(self.entity))
        reliable, records = self.send(104)
        self.assertEqual(self.notices(reliable), [])
        fresh = next(entity for entity, op in records if op == world.OP_CREATE)
        self.assertNotEqual(fresh, self.entity)
        target = next(target for target in self.match.combat._targets() if target.entity == fresh)
        self.match.combat._damage(self.player, target, 20, False, None, 105)
        self.match.combat._send_health()
        self.server.drop(self.client)
        self.ack()
        self.assertFalse(self.client.bot_create_acked(fresh))
        self.assertFalse(self.client._bot_notices)
        self.assertFalse(self.client._bot_health)
        self.assertFalse(self.client.reliable)

    def test_long_wait_keeps_only_bounded_latest_notices(self):
        with patch("ow174.game.server.MAX_PENDING_BOT_NOTICES", 2):
            self.prepare(single=True)
        self.send(100)
        for frame in (101, 102, 103):
            self.match.combat._tell_hit(self.player, self.target, 20, 0, frame)
        self.ack()
        reliable, _ = self.send(104)
        self.assertEqual([value["+0x84"] for _, value in self.notices(reliable)], [102, 103])

    def test_same_tick_damage_and_loss_catch_up_only_after_create_ack(self):
        self.prepare(single=True)
        self.damage_bot(20, 100)  # damage after the create was queued in the same tick
        self.send(100, deliver=False)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.send(104)
        self.assertEqual(self.health, [])
        self.ack()  # the create was lost
        self.damage_bot(20, 105)
        self.send(107)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])  # retransmit the frozen create
        self.ack()
        self.ack()  # duplicate positive ACK must not schedule another snapshot
        self.send(110)
        self.assertEqual(self.health, [(world.OP_UPDATE, 160)])
        self.ack()
        self.send(113)
        self.assertEqual(self.health, [])

    def test_same_tick_damage_is_flushed_even_with_no_later_damage(self):
        self.prepare(single=True)
        self.damage_bot(20, 100)
        self.send(100)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.ack()
        self.send(104)
        self.assertEqual(self.health, [(world.OP_UPDATE, 180)])

    def test_superseded_health_is_not_resent_but_latest_still_retries(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.damage_bot(20, 101)
        self.send(101, deliver=False)
        self.assertEqual(self.health, [(world.OP_UPDATE, 180)])
        self.damage_bot(20, 102)  # queued 160 never goes on the wire
        self.damage_bot(20, 103)
        self.send(104)
        self.assertEqual(self.health, [(world.OP_UPDATE, 140)])
        self.ack()
        self.send(107)
        self.assertEqual(self.health, [])  # 180 was lost but would roll 140 back
        self.damage_bot(20, 108)
        self.send(110, deliver=False)
        self.assertEqual(self.health, [(world.OP_UPDATE, 120)])
        self.send(113)
        self.assertEqual(self.health, [])
        self.ack()
        self.send(116)
        self.assertEqual(self.health, [(world.OP_UPDATE, 120)])  # no replacement, retry
        self.ack()
        self.send(119)
        self.assertEqual(self.health, [])

    def test_health_waits_for_each_viewers_own_create_ack(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        viewer = self.match.add_player(2, "Beta", SOLDIER, 0, False)
        key_in, key_out = b"v" * 32, b"w" * 32
        observer = Client(self.server, ("memory.test", 40002), 2, LinkCipher(key_in, key_out), viewer)
        self.server.clients[observer.conn] = observer
        viewer.client, viewer.spawned, observer.open = observer, True, True
        observer.queue_entities([self.match._bot_create_update(self.target.owner)])
        peer = LinkCipher(key_out, key_in)
        window = ReceiveWindow()

        def observer_health(tick):
            observer.send_frame(tick, 10.0)
            packet = peer.open(self.server.sock.sent[-1][0])
            self.assertTrue(window.accept(packet.seq))
            _, records = wire_frame(packet)
            return self.bot_health(records)

        self.damage_bot(20, 101)
        self.send(101)
        self.assertEqual(self.health, [(world.OP_UPDATE, 180)])
        self.assertEqual(observer_health(101), [(world.OP_CREATE, 200)])
        self.damage_bot(20, 104)
        self.send(104)
        self.assertEqual(self.health, [(world.OP_UPDATE, 160)])
        self.assertEqual(observer_health(104), [])
        ack = Packet(observer.conn, KIND_CONTROL, 0, window.expected, window.bits, ACK)
        self.server._datagram(peer.seal(ack), observer.address, 10.0)
        self.assertEqual(observer_health(107), [(world.OP_UPDATE, 160)])
