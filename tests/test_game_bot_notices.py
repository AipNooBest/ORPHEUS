"""Combat notice and NPC health ordering over the encrypted game link, without a listening socket."""

import struct
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_game_packet_budget import connection, decoded, large_messages
from test_game_repeated_lifecycles import wire_frame

from ow174.game import bodytrace, bots, combat, heroselect, movelog, world
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.match import BOT_RECORD_DELAY, COUNTDOWN
from ow174.game.script import decode, graph
from ow174.game.server import SILENCE_SECONDS, TICK_SECONDS, Client


class BotNoticeTests(unittest.TestCase):
    def setUp(self):
        self.monotonic_override = None
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)

    def connection_time(self, *, callsite):
        if self.monotonic_override is not None:
            return self.monotonic_override
        return self.match.tick * TICK_SECONDS

    def connect(self):
        self.server, self.client, self.player, self.peer = connection()
        self.match = self.player.match
        clock = patch.object(self.server.environment, "monotonic", side_effect=self.connection_time)
        clock.start()
        self.addCleanup(clock.stop)

    @staticmethod
    def assume_mode_ready(client):
        # Other tests isolate body or transport prerequisites from mode startup.
        client._mode_create_acked = True
        stream = client.player.mode_script
        stream.last = stream.data_last = 1
        stream.arrived(1)

    def prepare(self, *, last=False, single=False, mode_ready=True):
        self.connect()
        self.match.bots = bots.make(PRACTICE_RANGE.map_guid)
        self.match.combat.next_bot = bots.BOT_ENTITY + len(self.match.bots)
        self.match.tick = 100
        self.player.spawned = self.player.has_body = True
        enemies = [target for target in self.match.combat._targets() if target.team != self.player.team]
        self.target = enemies[-1] if last else enemies[0]
        self.entity = self.target.entity
        updates = self.match.spawn_updates(self.player)
        if single:
            updates = [record for record in updates if record.entity == self.entity]
        self.client.queue_entities(updates)
        if mode_ready:
            self.assume_mode_ready(self.client)
        self.window = ReceiveWindow()
        self.seq = 0

    def prepare_player_target(self, *, mode_ready=True, queue_create=True):
        self.connect()
        self.match.tick = 100
        self.player.spawned = self.player.has_body = True
        self.victim = self.match.add_player(2, "Target", SOLDIER, 1, False)
        key_in, key_out = b"t" * 32, b"u" * 32
        cipher = LinkCipher(key_in, key_out)
        victim_client = Client(self.server, ("memory.test", 40002), 2, cipher, self.victim)
        self.server.clients[2] = self.victim.client = victim_client
        self.victim.spawned = self.victim.has_body = victim_client.open = True
        self.target = next(target for target in self.match.combat._targets() if target.owner is self.victim)
        self.entity = self.target.entity
        if queue_create:
            self.client.queue_entities([self.match.remote_body_create(self.player, self.victim)])
        # These tests isolate body and mode delivery from the name association gate.
        self.client._confirmed_possessions[self.victim.entity] = self.victim.body
        if mode_ready:
            self.assume_mode_ready(self.client)
            self.assume_mode_ready(victim_client)
        self.window = ReceiveWindow()
        self.seq = 0
        return LinkCipher(key_out, key_in)

    def send(self, tick, *, deliver=True):
        self.match.tick = tick
        self.client.send_frame(tick, 10.0)
        packet, wire_tick, reliable, _, _, count = decoded(self, self.peer, self.server.sock.sent[-1][0])
        ids, records = wire_frame(packet)
        self.assertEqual((wire_tick, ids, len(records)), (tick, [number for _, number, _ in reliable], count))
        self.health = self.bot_health(records)
        self.mode_chunks = [
            (record.chunk.getvalue(), record.chunk.count)
            for record, _ in records if record.entity == world.GAME_MODE_ENTITY and record.chunk is not None
        ]
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

    def queue_mode_create(self):
        self.client.queue_entities([
            world.EntityUpdate(world.GAME_MODE_ENTITY, world.OP_CREATE, self.match._game_mode_create),
        ])

    def assert_mode_baseline(self):
        chunk, = self.mode_chunks
        frame = decode.read_chunk(*chunk)
        self.assertTrue(frame.full)
        self.assertEqual(frame.instances[1].graph, combat.MODE_UI)
        return chunk

    def test_mode_baseline_waits_for_mode_create_ack(self):
        self.prepare(single=True, mode_ready=False)
        self.queue_mode_create()
        mode_create = (world.GAME_MODE_ENTITY, world.OP_CREATE)
        self.assertIn(mode_create, self.send(100, deliver=False)[1])
        self.match.combat.spawned(self.player)

        self.assertNotIn(mode_create, self.send(103)[1])
        self.assertEqual(self.mode_chunks, [])
        self.ack()  # Report the missing create packet.
        self.assertIn(mode_create, self.send(106)[1])
        self.assertEqual(self.mode_chunks, [])
        self.ack()
        self.send(109)
        self.assert_mode_baseline()

    def test_elimination_waits_for_mode_baseline_but_hit_does_not(self):
        self.prepare(single=True, mode_ready=False)
        self.queue_mode_create()
        self.send(100)
        self.ack()
        self.match.combat.spawned(self.player)
        self.send(103, deliver=False)
        baseline = self.assert_mode_baseline()

        self.damage_bot(self.target.owner.health, 104)
        hit = self.notices(self.send(106)[0])
        self.assertEqual(hit, [
            (combat.HIT, {"+0x78": {"+0x0": {"+0x0": self.entity}, "+0x4": int(self.target.owner.health),
                                      "+0x8": combat.KILLING}, "+0x84": 104}),
        ])
        self.assertEqual(self.mode_chunks, [])
        self.ack()  # Report the lost baseline and the successfully delivered hit.
        self.assertEqual(self.notices(self.send(109)[0]), [])
        self.assertEqual(self.mode_chunks, [baseline])
        self.ack()
        self.ack()  # An unchanged ACK window must not issue the notice twice.
        self.assertEqual(self.notices(self.send(112)[0]), [
            (combat.KILL_NOTICE, {"+0x78": 0, "+0x80": {"+0x0": self.player.body},
                                  "+0x84": {"+0x0": self.entity}, "+0x88": 0, "+0x89": False}),
        ])
        self.ack()
        self.assertEqual(self.notices(self.send(115)[0]), [])

    def test_self_death_notice_waits_for_mode_baseline_ack(self):
        self.prepare(single=True, mode_ready=False)
        self.queue_mode_create()
        self.send(100)
        self.ack()
        self.match.combat.spawned(self.player)
        self.match.combat._died(self.player, self.player, False, None)

        self.assertEqual(self.send(103)[0], [])
        self.assert_mode_baseline()
        self.ack()
        self.assertEqual(self.send(106)[0], [
            (0, combat.DEATH_NOTICE, {"+0x78": 0, "+0x7C": {"+0x0": self.player.body}}),
        ])

    def test_victim_death_notice_waits_for_the_victims_mode_baseline(self):
        victim_peer = self.prepare_player_target(mode_ready=False)
        attacker, attacker_peer = self.client, self.peer
        self.queue_mode_create()
        self.send(100)
        self.ack()
        self.match.combat.spawned(self.player)
        self.send(103)
        self.assert_mode_baseline()
        self.ack()

        self.client, self.peer = self.victim.client, victim_peer
        self.window, self.seq = ReceiveWindow(), 0
        source = self.player.body
        self.client.queue_entities([self.match.remote_body_create(self.victim, self.player)])
        self.client._confirmed_possessions[self.player.entity] = source
        self.queue_mode_create()
        self.assertIn((source, world.OP_CREATE), self.send(104)[1])
        self.ack()
        self.match.combat.spawned(self.victim)
        self.match.combat._damage(self.player, self.target, 200, False, None, 105)
        self.assertEqual(self.send(106)[0], [])
        self.assert_mode_baseline()

        attacker.send_frame(106, 10.0)
        _, _, reliable, _, _, _ = decoded(self, attacker_peer, self.server.sock.sent[-1][0])
        self.assertEqual([number for _, number, _ in reliable], [combat.HIT, combat.KILL_NOTICE])

        self.ack()
        self.assertEqual(self.send(109)[0], [
            (0, combat.DEATH_NOTICE, {"+0x78": combat.KILLED_BY, "+0x7C": {"+0x0": source}}),
        ])

    def test_mode_notice_holds_retired_target_until_the_notice_is_acknowledged(self):
        self.prepare_player_target(mode_ready=False)
        self.queue_mode_create()
        self.send(100)
        self.ack()
        self.match.combat._damage(self.player, self.target, 200, False, None, 101)
        self.assertEqual([number for _, number, _ in self.send(101)[0]], [combat.HIT])
        self.ack()  # The hit is no longer an independent reason to hold the destroy.
        self.assertTrue(self.match.switch_hero(self.victim, heroes()[SOLDIER]))

        self.match.combat.spawned(self.player)
        old_destroy = (self.entity, world.OP_DESTROY)
        reliable, records = self.send(104)
        self.assertEqual(self.notices(reliable), [])
        self.assertNotIn(old_destroy, records)
        self.assert_mode_baseline()
        self.ack()
        reliable, records = self.send(107)
        self.assertEqual([(number, value["+0x84"]) for _, number, value in reliable], [
            (combat.KILL_NOTICE, {"+0x0": self.entity}),
        ])
        self.assertNotIn(old_destroy, records)
        self.ack()
        self.assertIn(old_destroy, self.send(110)[1])

    def test_expired_target_mode_notice_is_discarded_before_the_baseline_ack(self):
        self.prepare(single=True, mode_ready=False)
        self.queue_mode_create()
        self.send(100)
        self.ack()
        self.kill_bot()
        self.assertEqual([number for _, number, _ in self.send(101)[0]], [combat.HIT])
        self.ack()  # The hit and terminal health are already delivered.
        self.match.combat.spawned(self.player)
        self.send(104, deliver=False)
        baseline = self.assert_mode_baseline()

        self.respawn_target(289)
        old_destroy = (self.entity, world.OP_DESTROY)
        expiry = 289 + round(SILENCE_SECONDS / TICK_SECONDS)
        self.assertNotIn(old_destroy, self.send(expiry - 4)[1])
        self.ack()  # Report the missing mode baseline packet.
        self.assertNotIn(old_destroy, self.send(expiry - 1)[1])
        self.assertEqual(self.mode_chunks, [baseline])
        self.match.tick = expiry
        self.ack()  # The retirement deadline passed before the baseline ACK was processed.
        self.assertEqual(self.player.mode_script.delivered, 1)

        reliable, records = self.send(expiry)
        self.assertIn(old_destroy, records)
        self.assertEqual(self.notices(reliable), [])
        self.ack()
        self.assertEqual(self.notices(self.send(expiry + 1)[0]), [])

    def test_latest_countdown_replaces_unsent_mode_state_and_later_countdown_keeps_notices_ready(self):
        self.prepare(single=True, mode_ready=False)
        self.queue_mode_create()
        self.match.assemble_ends = 20.0
        self.match._send_countdown(self.player, 10.0)
        self.match._send_countdown(self.player, 15.0)

        self.send(100)
        self.assertEqual(self.mode_chunks, [])
        self.ack()
        self.send(103)
        baseline = self.assert_mode_baseline()
        self.assertEqual(decode.read_chunk(*baseline).entity_vars[COUNTDOWN], (5.0, ()))
        self.ack()

        self.match._send_countdown(self.player, 20.0)
        self.send(106, deliver=False)
        countdown = self.assert_mode_baseline()
        self.assertEqual(decode.read_chunk(*countdown).entity_vars[COUNTDOWN], (0.0, ()))
        self.damage_bot(self.target.owner.health, 107)
        self.assertEqual([number for _, number, _ in self.send(109)[0]], [combat.HIT, combat.KILL_NOTICE])

    def test_assemble_end_keeps_only_zero_countdown_while_mode_create_is_lost(self):
        self.prepare(single=True, mode_ready=False)
        self.match.game_map = replace(self.match.game_map, mode_guid=0x0230000000000002, mode_name="Control")
        self.match.controller = heroselect.controller_of(self.match.game_map.mode_guid)
        self.match.assemble_ends = 20.0
        self.player.steps_done = 4
        self.player.select_open = True
        self.queue_mode_create()
        self.match._send_countdown(self.player, 10.0)
        mode_create = (world.GAME_MODE_ENTITY, world.OP_CREATE)
        self.assertIn(mode_create, self.send(100, deliver=False)[1])
        self.assertEqual(self.mode_chunks, [])

        self.match._end_assemble(20.0)
        self.assertTrue(self.match.assembled)
        self.assertFalse(self.player.select_open)
        self.assertNotIn(mode_create, self.send(103)[1])
        self.assertEqual(self.mode_chunks, [])
        self.ack()
        self.assertIn(mode_create, self.send(106)[1])
        self.assertEqual(self.mode_chunks, [])
        self.ack()

        self.send(109)
        frame = decode.read_chunk(*self.assert_mode_baseline())
        self.assertEqual((frame.last, frame.entity_vars[COUNTDOWN]), (1, (0.0, ())))
        self.ack()
        self.send(112)
        self.assertEqual(self.mode_chunks, [])

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

    def test_player_hit_and_elimination_wait_for_a_lost_target_create(self):
        self.prepare_player_target()
        self.assertIn((self.entity, world.OP_CREATE), self.send(100, deliver=False)[1])
        self.match.combat._damage(self.player, self.target, 200, False, None, 101)
        self.assertEqual(self.notices(self.send(104)[0]), [])
        self.ack()
        reliable, records = self.send(107)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_CREATE), records)
        self.ack()
        self.ack()
        self.assertEqual(self.notices(self.send(110)[0]), [
            (combat.HIT, {"+0x78": {"+0x0": {"+0x0": self.entity}, "+0x4": 200, "+0x8": combat.KILLING},
                          "+0x84": 101}),
            (combat.KILL_NOTICE, {"+0x78": 0, "+0x80": {"+0x0": self.player.body},
                                  "+0x84": {"+0x0": self.entity}, "+0x88": 0, "+0x89": False}),
        ])
        self.ack()
        self.assertEqual(self.notices(self.send(113)[0]), [])

    def test_player_victim_notice_waits_for_a_lost_attacker_create(self):
        peer = self.prepare_player_target()
        self.client, self.peer = self.victim.client, peer
        source = self.player.body
        self.client.queue_entities([self.match.remote_body_create(self.victim, self.player)])
        self.client._confirmed_possessions[self.player.entity] = source
        self.assertIn((source, world.OP_CREATE), self.send(100, deliver=False)[1])
        self.match.combat._damage(self.player, self.target, 200, False, None, 101)
        self.assertEqual(self.send(104)[0], [])
        self.ack()
        reliable, records = self.send(107)
        self.assertEqual(reliable, [])
        self.assertIn((source, world.OP_CREATE), records)
        self.ack()
        self.assertEqual(self.send(110)[0], [
            (0, combat.DEATH_NOTICE, {"+0x78": combat.KILLED_BY, "+0x7C": {"+0x0": source}}),
        ])

    def test_hit_after_a_body_switch_waits_for_the_new_body_create(self):
        self.prepare_player_target()
        self.send(100)
        self.ack()
        self.match.switch_hero(self.victim, heroes()[SOLDIER])
        self.target = next(target for target in self.match.combat._targets() if target.owner is self.victim)
        self.entity = self.target.entity
        self.assertNotIn(self.entity, self.player.seen)
        self.match.combat._damage(self.player, self.target, 20, False, None, 101)
        reliable, records = self.send(101)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_CREATE), records)
        self.ack()
        self.assertEqual(self.notices(self.send(104)[0]), [
            (combat.HIT, {"+0x78": {"+0x0": {"+0x0": self.entity}, "+0x4": 20, "+0x8": 0}, "+0x84": 101}),
        ])

    def test_old_player_create_ack_cannot_release_notices_for_a_reused_body(self):
        self.prepare_player_target()
        self.send(100)
        fight = self.match.combat
        fight._tell_hit(self.player, self.target, 20, 0, 101)
        self.client.queue_entities([
            world.EntityUpdate(self.entity, world.OP_DESTROY),
            self.match.remote_body_create(self.player, self.victim),
        ])
        fight._tell_hit(self.player, self.target, 40, 0, 105)
        self.ack()  # acknowledge only the original lifetime
        reliable, records = self.send(107)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_CREATE), records)
        self.ack()
        notices = self.notices(self.send(110)[0])
        self.assertEqual([(number, value["+0x84"]) for number, value in notices], [(combat.HIT, 105)])

    def test_promoted_player_notices_hold_the_old_destroy_across_a_body_switch(self):
        self.prepare_player_target()
        self.send(100)
        self.ack()
        self.match.combat._damage(self.player, self.target, 200, False, None, 101)
        lost = self.notices(self.send(101, deliver=False)[0])
        self.assertEqual([number for number, _ in lost], [combat.HIT, combat.KILL_NOTICE])
        self.match.switch_hero(self.victim, heroes()[SOLDIER])
        fresh = self.victim.body
        self.client.queue_entities([self.match.remote_body_create(self.player, self.victim)])
        _, records = self.send(104)
        self.assertIn((fresh, world.OP_CREATE), records)
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.ack()
        retry, records = self.send(107)
        self.assertEqual(self.notices(retry), lost)
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.ack()
        self.assertIn((self.entity, world.OP_DESTROY), self.send(110)[1])

    def retire_player_with_lost_hit(self):
        self.prepare_player_target()
        self.send(100)
        self.ack()
        self.match.combat._tell_hit(self.player, self.target, 20, 0, 101)
        lost = self.notices(self.send(101, deliver=False)[0])
        self.assertEqual(lost, [
            (combat.HIT, {"+0x78": {"+0x0": {"+0x0": self.entity}, "+0x4": 20, "+0x8": 0}, "+0x84": 101}),
        ])
        self.match.switch_hero(self.victim, self.victim.hero)
        self.assertNotEqual(self.victim.body, self.entity)
        return lost

    def switch_at_body_id_wrap(self):
        # Exercise the real wrap selection without creating 253 unrelated bodies.
        self.victim.body = self.victim.entity | 0xFF
        self.match.switch_hero(self.victim, self.victim.hero)
        self.match.ensure_remote_body(self.player, self.victim)
        return self.victim.body

    def test_wrap_skips_the_body_with_a_lost_promoted_hit(self):
        lost = self.retire_player_with_lost_hit()
        fresh = self.switch_at_body_id_wrap()
        self.assertNotEqual(fresh, self.entity)
        reliable, records = self.send(104)
        self.assertIn((fresh, world.OP_CREATE), records)
        self.assertNotIn((self.entity, world.OP_CREATE), records)
        self.assertEqual(self.notices(reliable), [])
        self.ack()
        retry, records = self.send(107)
        self.assertEqual(self.notices(retry), lost)
        self.assertNotIn((self.entity, world.OP_CREATE), records)

    def test_timeout_destroy_and_tombstone_do_not_clear_the_reliable_tail(self):
        lost = self.retire_player_with_lost_hit()
        timeout = 101 + round(SILENCE_SECONDS / TICK_SECONDS)
        reliable, records = self.send(timeout)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_DESTROY), records)
        self.ack()  # the destroy arrived, and the earlier hit is now queued for retry
        after_tombstone = timeout + round(30 / TICK_SECONDS) + 1
        self.match.tick = after_tombstone
        fresh = self.switch_at_body_id_wrap()
        self.assertNotEqual(fresh, self.entity)
        reliable, records = self.send(after_tombstone)
        self.assertEqual(self.notices(reliable), lost)
        self.assertIn((fresh, world.OP_CREATE), records)
        self.assertNotIn((self.entity, world.OP_CREATE), records)
        self.ack()

        self.assertEqual(self.switch_at_body_id_wrap(), self.entity)
        reliable, records = self.send(after_tombstone + 3)
        self.assertEqual(self.notices(reliable), [])
        self.assertIn((self.entity, world.OP_CREATE), records)

    def test_body_tombstone_starts_at_the_retried_destroy_ack(self):
        self.prepare_player_target()
        self.send(100)
        self.ack()
        self.match.switch_hero(self.victim, self.victim.hero)
        self.assertIn((self.entity, world.OP_DESTROY), self.send(101, deliver=False)[1])

        late = 101 + round(30 / TICK_SECONDS) + 1
        self.match.tick = late
        self.assertNotEqual(self.switch_at_body_id_wrap(), self.entity)
        self.assertNotIn((self.entity, world.OP_CREATE), self.send(late)[1])
        self.ack()  # only now does the old lost destroy enter the retry queue
        self.assertIn((self.entity, world.OP_DESTROY), self.send(late + 3)[1])

        # The retry is acknowledged before the scheduler catches up after a pause.
        self.monotonic_override = (late + 3) * TICK_SECONDS + 31.0
        self.ack()
        self.monotonic_override += TICK_SECONDS
        self.match.tick = late + 3 + round(31 / TICK_SECONDS)
        self.assertNotEqual(self.switch_at_body_id_wrap(), self.entity)
        self.assertNotIn((self.entity, world.OP_CREATE), self.send(self.match.tick)[1])

        self.monotonic_override += 30.0
        self.match.tick += round(30 / TICK_SECONDS)
        self.assertEqual(self.switch_at_body_id_wrap(), self.entity)
        self.assertIn((self.entity, world.OP_CREATE), self.send(self.match.tick)[1])

    def test_body_id_exhaustion_leaves_the_current_body_and_vitals_intact(self):
        self.prepare_player_target()
        self.send(100)
        self.ack()
        with patch("ow174.game.match.BODY_SCRIPT_HEROES", set()):
            seen = {self.victim.body}
            for _ in range(254):
                self.match.switch_hero(self.victim, self.victim.hero)
                self.assertNotIn(self.victim.body, seen)
                seen.add(self.victim.body)
            body, hero, mover, script = (
                self.victim.body, self.victim.hero, self.victim.mover, self.victim.body_script,
            )
            target = next(target for target in self.match.combat._targets() if target.owner is self.victim)
            vitals = self.match.combat.vitals_of(target)
            vitals.damage(20)
            self.assertFalse(self.match.switch_hero(self.victim, self.player.hero))
        self.assertEqual((self.victim.body, self.victim.hero), (body, hero))
        self.assertIs(self.victim.mover, mover)
        self.assertIs(self.victim.body_script, script)
        self.assertIs(self.match.combat.vitals[self.victim.body], vitals)

        fight = self.match.combat
        fight.now = self.match.tick * TICK_SECONDS
        fight._damage(self.player, target, vitals.total, False, None, self.match.tick)
        [(due, victim)] = fight.respawns
        self.assertIs(victim, self.victim)
        self.match.tick = int(due / TICK_SECONDS) + 1
        fight.update(due, self.match.tick, frames=False)
        self.assertEqual(fight.respawns, [(due + combat.RESPAWN_RETRY, self.victim)])
        self.assertEqual(self.victim.body, body)
        self.assertTrue(fight.is_dead(self.victim))

        # Only the old ID on the observer's link gets a destroy ACK. The owner's
        # queued destroys for all other candidates remain unacknowledged.
        ack_tick = self.match.tick + 1
        self.assertIn((self.entity, world.OP_DESTROY), self.send(ack_tick)[1])
        self.ack()
        self.match.tick = ack_tick + round(30 / TICK_SECONDS) + 1
        fight.update(self.match.tick * TICK_SECONDS, self.match.tick, frames=False)
        self.assertEqual(self.victim.body, self.entity)
        self.assertFalse(fight.is_dead(self.victim))
        self.assertFalse(fight.respawns)

    def assert_movement_waits_for_create_ack(self, bot, tick):
        bot.moved_tick = tick  # make the production movement record due on each send tick
        _, lost = self.send(tick, deliver=False)
        self.assertIn((bot.entity, world.OP_CREATE), lost)
        due = tick + BOT_RECORD_DELAY + 1
        _, waiting = self.send(due)
        self.assertFalse(self.client.bot_create_acked(bot.entity))
        self.assertNotIn((bot.entity, 0), waiting)
        self.ack()  # the later frame arrived with the create missing
        _, retry = self.send(due + 3)
        self.assertIn((bot.entity, world.OP_CREATE), retry)
        self.assertNotIn((bot.entity, 0), retry)
        self.ack()
        self.assertTrue(self.client.bot_create_acked(bot.entity))
        _, active = self.send(due + 6)
        self.assertIn((bot.entity, 0), active)

    def test_movement_waits_for_the_lost_initial_bot_create(self):
        self.prepare(single=True)
        self.assert_movement_waits_for_create_ack(self.target.owner, 100)

    def test_respawned_bot_movement_waits_for_its_own_create(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.kill_bot()
        self.send(101)
        self.ack()
        old = self.target.owner
        index = self.match.bots.index(old)
        # Isolate movement from optional graph chunks so op 0 refers to ch3 only.
        with patch.object(graph, "entity_body", return_value=None):
            self.respawn_target(104)
        fresh = self.match.bots[index]
        self.assertNotEqual(fresh.entity, old.entity)
        self.assert_movement_waits_for_create_ack(fresh, 104)

    def test_skipped_respawn_deadline_still_sends_terminal_health(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.match.update(10.0, 101)
        fight = self.match.combat
        old = self.target.owner
        index = self.match.bots.index(old)
        fight._damage(self.player, self.target, old.health, False, None, 101)
        self.assertTrue(fight.vitals[self.entity].dead)

        # No intermediate combat update can flush the dirty pool before the respawn deadline.
        skipped = 200
        tick = 101 + skipped
        with patch.object(graph, "entity_body", return_value=None):
            self.match.update(10.0 + skipped * combat.TICK, tick)
        fresh = self.match.bots[index]
        self.assertNotEqual(fresh.entity, self.entity)

        reliable, records = self.send(tick)
        self.assert_kill_notices(reliable)
        self.assertIn((world.OP_UPDATE, 0.0), self.health)
        self.assertIn((fresh.entity, world.OP_CREATE), records)
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.ack()
        _, retired = self.send(tick + 3)
        self.assertIn((self.entity, world.OP_DESTROY), retired)

    def test_reused_entity_id_ignores_ack_for_previous_lifetime(self):
        self.prepare(single=True)
        build = self.client.entities[0].build
        self.send(100)
        self.damage_bot(20, 101)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_DESTROY)])
        self.send(104, deliver=False)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_CREATE, build)])
        self.match.combat._tell_hit(self.player, self.target, 30, 0, 105)
        self.client.queue_body_health(self.entity, world.health(170, 200))
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
        self.assertFalse(self.client._combat_notices)
        self.assertFalse(self.client._body_health)
        self.assertFalse(self.client.reliable)

    def respawn_target(self, tick):
        self.match.tick = tick
        self.match.combat._respawn_bot(self.target.owner)

    def test_lost_kill_notices_delay_only_the_old_destroy(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.kill_bot()
        lost, _ = self.send(101, deliver=False)
        self.assert_kill_notices(lost)
        self.assertEqual(self.health, [(world.OP_UPDATE, 0)])

        fresh = self.match.combat.next_bot
        self.respawn_target(289)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_DESTROY)])
        reliable, records = self.send(289)
        self.assertEqual(self.notices(reliable), [])
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.assertIn((fresh, world.OP_CREATE), records)
        self.ack()  # the death message and terminal health datagram was lost
        reliable, records = self.send(292)
        self.assert_kill_notices(reliable)
        self.assertEqual(self.health, [(world.OP_UPDATE, 0)])
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.ack()
        self.assertEqual(self.send(295)[1].count((self.entity, world.OP_DESTROY)), 1)

    def test_unscripted_bystander_retries_terminal_health_before_destroy(self):
        self.prepare(single=True)
        viewer = self.match.add_player(2, "Bystander", SOLDIER, 0, False)
        key_in, key_out = b"v" * 32, b"w" * 32
        observer = Client(self.server, ("memory.test", 40002), 2, LinkCipher(key_in, key_out), viewer)
        self.server.clients[observer.conn] = observer
        viewer.client, viewer.spawned, observer.open = observer, True, True
        self.match.bots_shown[viewer.slot] = self.match.tick
        observer.queue_entities([self.match._bot_create_update(self.target.owner)])
        peer, window = LinkCipher(key_out, key_in), ReceiveWindow()
        sequence = 0

        def send(tick, *, lose=False):
            self.match.tick = tick
            observer.send_frame(tick, 10.0)
            packet, _, reliable, _, _, _ = decoded(self, peer, self.server.sock.sent[-1][0])
            self.assertEqual(self.notices(reliable), [])
            if not lose:
                self.assertTrue(window.accept(packet.seq))
            return wire_frame(packet)[1]

        def ack():
            nonlocal sequence
            packet = Packet(observer.conn, KIND_CONTROL, sequence, window.expected, window.bits, ACK)
            sequence += 1
            self.server._datagram(peer.seal(packet), observer.address, 10.0)

        self.assertEqual(self.bot_health(send(100)), [(world.OP_CREATE, self.target.owner.health)])
        ack()
        self.assertTrue(observer.bot_create_acked(self.entity))
        self.assertNotIn(self.entity, observer._bot_scripts)
        self.kill_bot()
        self.assertEqual(self.bot_health(send(101, lose=True)), [(world.OP_UPDATE, 0.0)])
        self.assertNotIn(self.entity, observer._combat_notice_tail)

        fresh = self.match.combat.next_bot
        with patch.object(graph, "entity_body", return_value=None):
            self.respawn_target(289)
        records = [(item.entity, item.op) for item, _ in send(289)]
        self.assertIn((fresh, world.OP_CREATE), records)
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        ack()  # the replacement arrived, the old terminal pool did not
        retry = send(292)
        self.assertEqual(self.bot_health(retry), [(world.OP_UPDATE, 0.0)])
        self.assertNotIn((self.entity, world.OP_DESTROY), [(item.entity, item.op) for item, _ in retry])
        ack()
        self.assertIn((self.entity, world.OP_DESTROY), [(item.entity, item.op) for item, _ in send(295)])

    def test_acknowledged_kill_notices_wait_for_earlier_reliable_gap(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        large_messages(self.client, self.player, 1)
        self.send(101, deliver=False)  # reliable sequence 0
        self.kill_bot()
        parked, _ = self.send(104)
        self.assert_kill_notices(parked)
        self.assertEqual([seq for seq, _, _ in parked], [1, 2])
        self.ack()  # the peer got both notices but parks them behind sequence 0
        self.send(107, deliver=False)

        self.respawn_target(289)
        self.assertNotIn((self.entity, world.OP_DESTROY), self.send(289)[1])
        self.ack()
        reliable, records = self.send(292)
        self.assertEqual([(seq, identifier) for seq, identifier, _ in reliable], [(0, 20308)])
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.ack()
        self.assertIn((self.entity, world.OP_DESTROY), self.send(295)[1])

    def test_acknowledged_kill_notices_wait_for_budget_delayed_terminal_health(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.kill_bot()
        notices_only = self.client._payload(101, self.client.reliable, (), ())
        with patch("ow174.game.server.MAX_PAYLOAD", len(notices_only)):
            reliable, _ = self.send(101)
        self.assert_kill_notices(reliable)
        self.assertEqual(self.health, [])
        self.ack()
        self.assertIn(self.entity, self.client._body_health)

        self.respawn_target(289)
        reliable, records = self.send(289)
        self.assertEqual(self.notices(reliable), [])
        self.assertNotIn((self.entity, world.OP_DESTROY), records)
        self.assertEqual(self.health, [(world.OP_UPDATE, 0)])
        self.assertNotIn((self.entity, world.OP_DESTROY), self.send(292)[1])
        self.ack()
        self.assertIn((self.entity, world.OP_DESTROY), self.send(295)[1])

    def test_retirement_expires_without_ack_progress(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.kill_bot()
        self.send(101, deliver=False)
        self.respawn_target(289)
        expiry = 289 + round(SILENCE_SECONDS / TICK_SECONDS)
        self.assertNotIn((self.entity, world.OP_DESTROY), self.send(expiry - 1)[1])
        self.assertIn((self.entity, world.OP_DESTROY), self.send(expiry)[1])
        self.ack()
        reliable, _ = self.send(expiry + 1)
        self.assert_kill_notices(reliable)  # retirement cannot break the reliable sequence

    def test_reused_entity_id_cancels_held_destroy_and_stale_health(self):
        self.prepare(single=True)
        build = self.client.entities[0].build
        self.send(100)
        self.ack()
        self.kill_bot()
        self.send(101, deliver=False)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_DESTROY)])
        self.assertIn(self.entity, self.client._body_destroy_wait)
        self.client.queue_entities([world.EntityUpdate(self.entity, world.OP_CREATE, build)])
        self.assertNotIn(self.entity, self.client._body_destroy_wait)
        self.assertIn((self.entity, world.OP_CREATE), self.send(104)[1])
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.ack()
        self.assertNotIn((self.entity, world.OP_DESTROY), self.send(107)[1])
        self.assertEqual(self.health, [])

    def test_client_drop_releases_held_destroy_and_notice_ordinals(self):
        self.prepare(single=True)
        self.send(100)
        self.ack()
        self.kill_bot()
        self.respawn_target(289)
        self.assertIn(self.entity, self.client._body_destroy_wait)
        self.server.drop(self.client)
        self.assertFalse(self.client._body_destroy_wait)
        self.assertFalse(self.client._combat_notice_tail)
        self.assertFalse(self.client._body_health)

    def test_long_wait_keeps_only_bounded_latest_notices(self):
        with patch("ow174.game.server.MAX_PENDING_COMBAT_NOTICES", 2):
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

    def test_owner_health_retry_cannot_restore_a_superseded_pool(self):
        self.connect()
        self.match.tick = 100
        self.player.spawned = self.player.has_body = True
        self.target = next(target for target in self.match.combat._targets() if target.owner is self.player)
        self.entity = self.target.entity
        self.client.queue_entities(self.match.own_body_updates(self.player))
        self.window = ReceiveWindow()
        self.seq = 0
        self.send(100)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.ack()

        attacker = bots.make(PRACTICE_RANGE.map_guid)[0]
        fight = self.match.combat
        fight._damage(attacker, self.target, 20, False, None, 103)
        fight._send_health()
        self.send(103, deliver=False)
        self.assertEqual(self.health, [(world.OP_UPDATE, 180)])
        fight._damage(attacker, self.target, 10, False, None, 106)
        fight._send_health()
        self.send(106)
        self.assertEqual(self.health, [(world.OP_UPDATE, 170)])
        self.ack()
        self.send(109)
        self.assertEqual(self.health, [])
        self.assertEqual(fight.vitals_of(self.target).total, 170)

    def test_remote_player_health_changed_after_create_queued_waits_for_ack(self):
        self.prepare_player_target()
        self.damage_bot(20, 100)  # Create has a frozen 200 pool and has not gone out yet.
        self.send(100, deliver=False)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.send(103)
        self.assertEqual(self.health, [])
        self.ack()
        self.send(106)
        self.assertEqual(self.health, [(world.OP_CREATE, 200)])
        self.ack()
        self.send(109)
        self.assertEqual(self.health, [(world.OP_UPDATE, 180)])

    def test_late_observer_create_freezes_current_player_health(self):
        self.prepare_player_target(queue_create=False)
        fight = self.match.combat
        attacker = bots.make(PRACTICE_RANGE.map_guid)[0]
        fight._damage(attacker, self.target, 20, False, None, 100)
        fight._send_health()
        vitals = fight.vitals_of(self.target)
        self.assertEqual(vitals.total, 180)
        self.assertFalse(vitals.changed)

        self.match.ensure_remote_body(self.player, self.victim)
        self.send(103, deliver=False)
        self.assertEqual(self.health, [(world.OP_CREATE, 180)])
        fight._damage(attacker, self.target, 10, False, None, 104)
        fight._send_health()
        self.send(106)
        self.assertEqual(self.health, [])
        self.ack()
        self.send(109)
        self.assertEqual(self.health, [(world.OP_CREATE, 180)])
        self.ack()
        self.send(112)
        self.assertEqual(self.health, [(world.OP_UPDATE, 170)])

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
