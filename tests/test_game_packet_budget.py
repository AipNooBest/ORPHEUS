"""Real encrypted Client frames stay within the budget and commit only actual sends; no UDP socket."""

import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import bodytrace, correction, messages, movelog, projectiles, world
from ow174.game import server as server_module
from ow174.game.bits import BitReader
from ow174.game.collision import StaticCollisionProvider, flat_world
from ow174.game.commands import FIRE, Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.link import ACK, KIND_CONTROL, KIND_FRAME, MAX_DATAGRAM, LinkCipher, Packet, ReceiveWindow
from ow174.game.match import Match, card_message
from ow174.game.script import decode
from ow174.game.script.driver import SETTLE_FRAMES
from ow174.game.server import MAX_PAYLOAD, Client, GameServer
from ow174.game.world import OP_CREATE, OP_DESTROY, OP_UPDATE, EntityUpdate, Movement

MAP = dataclasses.replace(PRACTICE_RANGE, map_guid=0x0800000000FFA911)
KEY_IN, KEY_OUT = b"i" * 32, b"o" * 32


class MemorySocket:
    def __init__(self):
        self.sent = []
        self.attempted = []
        self.fail_next = False

    def sendto(self, data, address):
        self.attempted.append((bytes(data), address))
        if self.fail_next:
            self.fail_next = False
            raise OSError("in-memory send failure")
        self.sent.append((bytes(data), address))
        return len(data)


class Arrivals:
    def __init__(self):
        self.values = []

    def arrived(self, last):
        self.values.append(last)


def connection(server=None, number=1):
    if server is None:
        server = GameServer()
        server.sock = MemorySocket()
    match = Match(MAP, collision_provider=StaticCollisionProvider(flat_world(1.0)))
    player = match.add_player(number, "Alpha", SOLDIER, 0, False)
    client = Client(server, ("memory.test", 40000 + number), number,
                    LinkCipher(KEY_IN, KEY_OUT), player)
    player.client = client
    client.open = True
    server.clients[number] = client
    server.matches.append(match)
    peer = LinkCipher(KEY_OUT, KEY_IN)
    return server, client, player, peer


def decoded(test, peer, data):
    test.assertLessEqual(len(data), MAX_DATAGRAM)
    packet = peer.open(data)
    test.assertIsNotNone(packet, "the production LinkCipher must accept the emitted native datagram")
    test.assertEqual(packet.kind, KIND_FRAME)
    bits = BitReader(packet.payload)
    tick = bits.var_a()
    reliable, unreliable = messages.read_channel(bits)
    ack = bits.var_a()
    bits.bits(4 + 1 + 1 + 1)
    count = bits.bits(7)
    return packet, tick, reliable, unreliable, ack, count


def large_messages(client, player, count=2):
    value = card_message(player)
    value["+0x80"] = {**value["+0x80"], "+0x40": "A" * 600}
    for _ in range(count):
        client.queue_reliable(20308, value)


def placeable(entity=0x80000077):
    record = EntityUpdate(entity, OP_CREATE, lambda origin: world.create_placeable(origin, 0x77))
    record.stream = Arrivals()
    record.chunk_last = 9
    return record


def prepare_corrections(player):
    player.has_body = True
    player.match.tick = 100
    player.match.start_body_script(player, EntityUpdate(player.body, OP_CREATE))
    player.body_script.create_arrived = True
    player.take_commands([Command(frame, forward=127) for frame in range(101, 111)])
    player.mover.corrected = 100


class PacketBudgetTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            patch = mock.patch.object(module, "ENABLED", False)
            patch.start()
            self.addCleanup(patch.stop)

    def test_trimmed_reliable_batch_refills_with_the_owner_rifle_delta(self):
        # The wire reader imports projectile fixtures that also use this module.
        from test_game_repeated_lifecycles import wire_frame

        server, client, player, peer = connection()
        prepare_corrections(player)
        script = player.body_script
        tree, = script.frames()
        initial = decode.read_chunk(tree.chunk.getvalue(), tree.chunk.count)
        known = {number: item.graph for number, item in initial.instances.items()}
        script.stream.arrived(tree.chunk_last)
        player.take_commands([Command(111, buttons=FIRE)])
        owner, = script.frames()
        client.queue_entities([owner])
        large_messages(client, player)
        reliable = list(client.reliable)
        tick = 110
        self.assertGreater(len(client._payload(tick, reliable, [], [])), MAX_PAYLOAD)
        self.assertLessEqual(len(client._payload(tick, reliable[:1], [], [owner])), MAX_PAYLOAD)

        client.send_frame(tick, 10.0)

        packet, _, rows, _, _, _ = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual([(seq, msg_id) for seq, msg_id, _ in rows], [(0, 20308)])
        _, records = wire_frame(packet)
        chunks = [record.chunk for record, _ in records if record.entity == player.body and record.chunk]
        self.assertEqual(len(chunks), 1)
        chunk, = chunks
        frame = decode.read_chunk(chunk.getvalue(), chunk.count, known)
        self.assertTrue(frame.owner)
        self.assertEqual((frame.cmfd, frame.last), (111, owner.chunk_last))
        self.assertTrue(frame.instances[11].states[10][0])
        self.assertNotIn(owner, client.entities)
        self.assertEqual(client.reliable, reliable[1:])
        self.assertIn(owner, client.in_flight[packet.seq][1])
        client.acked(packet.seq + 1, 1)
        self.assertEqual(script.stream.base_last, owner.chunk_last)

    def test_budget_delayed_owner_frames_coalesce_and_keep_initial_mode(self):
        from test_game_repeated_lifecycles import wire_frame

        server, client, player, peer = connection()
        prepare_corrections(player)
        script = player.body_script
        stream = script.stream
        model = decode.ClientBody.for_hero(SOLDIER)
        received = ReceiveWindow()
        sequence = 0

        def send(tick, *, defer_entities=False):
            nonlocal sequence
            player.match.tick = tick
            now = tick * 0.016
            if defer_entities:
                client.queue_reliable(
                    20302, {"+0x78": {"+0x0": player.entity}, "+0x7C": False, "+0x80": 0},
                )
                budget = len(client._payload(tick, client.reliable, [], []))
                with mock.patch.object(server_module, "MAX_PAYLOAD", budget):
                    client.send_frame(tick, now)
            else:
                client.send_frame(tick, now)
            packet, _, _, _, _, count = decoded(self, peer, server.sock.sent[-1][0])
            if defer_entities:
                self.assertEqual(count, 0)
            _, records = wire_frame(packet)
            frames = [
                model.apply(item)
                for item, _ in records
                if item.entity == player.body and item.chunk is not None
            ]
            self.assertTrue(received.accept(packet.seq))
            ack = Packet(client.conn, KIND_CONTROL, sequence, received.expected, received.bits, ACK)
            sequence += 1
            server._datagram(peer.seal(ack), client.address, now)
            return frames

        player.match._body_frames(player, client)
        full, = send(110)
        self.assertTrue(full.full)
        self.assertEqual(stream.base_last, full.last)

        first_frame = 111
        player.take_commands([Command(first_frame)])
        player.match._body_frames(player, client)
        first, = client.entities
        self.assertEqual(send(first_frame, defer_entities=True), [])
        after_first = first_frame + SETTLE_FRAMES + 2
        player.take_commands([Command(after_first)])
        player.match._body_frames(player, client)
        self.assertEqual(send(after_first, defer_entities=True), [])
        latest, = client.entities
        self.assertIsNot(latest, first)
        self.assertEqual(tuple(stream.pending), (latest.chunk_last,))
        self.assertEqual(script.report_count, 0)

        initial, = send(after_first)
        self.assertTrue(initial.owner)
        self.assertFalse(initial.correction)
        self.assertEqual((initial.cmfd, initial.last), (after_first, latest.chunk_last))
        player.take_commands([Command(after_first + SETTLE_FRAMES - 1)])
        player.match._body_frames(player, client)
        self.assertEqual(client.entities, [])

        before, after = placeable(0x80000077), placeable(0x80000078)
        client.queue_entities([before])
        start = after_first + SETTLE_FRAMES + 1
        release = start + 15
        for frame in (start, start + 7, start + 14, release):
            player.take_commands([Command(frame, buttons=FIRE if frame < release else 0)])
            player.match._body_frames(player, client)
            if frame == start:
                client.queue_entities([after])
            self.assertEqual(send(frame, defer_entities=True), [])
            left, pending, right = client.entities
            self.assertIs(left, before)
            self.assertIs(pending.stream, stream)
            self.assertIs(right, after)
            self.assertEqual(tuple(stream.pending), (pending.chunk_last,))
            self.assertEqual(stream.pending[pending.chunk_last][0].k, frame)
            self.assertEqual(script.report_count, 1)

        current, = send(release)
        self.assertTrue(current.owner)
        self.assertTrue(current.correction)
        self.assertEqual(current.cmfd, release)
        self.assertNotIn((11, 10), model.on())
        self.assertEqual(script.component.instances[11].vars[53].value(), 27)
        self.assertEqual(model.vars[(11, 53)], 27)
        self.assertEqual(stream.base_last, current.last)
        self.assertEqual(tuple(stream.pending), ())

    def test_legal_messages_defer_create_then_ack_original_record_once(self):
        server, client, player, peer = connection()
        large_messages(client, player)
        reliable = list(client.reliable)
        create = placeable()
        client.queue_entities([create])
        budget = len(client._payload(1002, reliable[:1], [], []))
        with mock.patch.object(server_module, "MAX_PAYLOAD", budget):
            client.send_frame(1002, 10.0)
        first, _, rows, _, _, count = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual([seq for seq, _, _ in rows], [0])
        self.assertEqual(count, 0)
        self.assertEqual(client.reliable, reliable[1:])
        self.assertEqual(client.entities, [create])
        self.assertIs(client.entities[0], create)
        self.assertEqual(client.in_flight[first.seq], (reliable[:1], []))
        self.assertEqual(create.stream.values, [])
        client.acked(first.seq + 1, 1)

        client.send_frame(1005, 10.1)
        second, _, rows, _, _, count = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual([seq for seq, _, _ in rows], [1])
        self.assertEqual(count, 1)
        self.assertEqual(client.entities, [])
        self.assertIs(client.in_flight[second.seq][1][0], create)
        client.acked(second.seq + 1, 1)
        client.acked(second.seq + 1, 1)
        self.assertEqual(create.stream.values, [9])
        self.assertEqual(client.reliable, [])
        self.assertEqual(client.in_flight, {})

    def test_excluded_correction_does_not_advance_ack_or_log_but_selected_send_commits(self):
        server, client, player, peer = connection()
        prepare_corrections(player)
        tick = 107
        player.match.tick = tick
        client.queue_reliable(20302, {"+0x78": {"+0x0": player.entity}, "+0x7C": False, "+0x80": 0})
        old_ack = correction.command_ack(player, tick)
        self.assertEqual(old_ack, 102)
        budget = len(client._payload(tick, client.reliable, [], []))
        with mock.patch.object(movelog, "ENABLED", True), mock.patch.object(movelog, "sent") as sent:
            with mock.patch.object(server_module, "MAX_PAYLOAD", budget):
                client.send_frame(tick, 10.0)
            first = decoded(self, peer, server.sock.sent[-1][0])
            self.assertEqual((first[4], first[5]), (102, 0))
            self.assertEqual(player.mover.corrected, 100)
            self.assertEqual(correction.command_ack(player, tick), 102)
            sent.assert_not_called()

            client.send_frame(tick, 10.1)
            second = decoded(self, peer, server.sock.sent[-1][0])
            self.assertEqual((second[4], second[5]), (109, 1))
            self.assertEqual(player.mover.corrected, 107)
            sent.assert_called_once_with(player, tick, 107, 109)

    def test_budget_delayed_first_correction_keeps_commands_for_the_later_replay(self):
        server, client, player, peer = connection()
        prepare_corrections(player)
        player.mover.corrected = None  # the first movement correction has not reached the wire
        player.take_commands([Command(frame, forward=127) for frame in range(111, 124)])
        player.match.tick = 107
        client.queue_reliable(20302, {"+0x78": {"+0x0": player.entity}, "+0x7C": False, "+0x80": 0})
        budget = len(client._payload(107, client.reliable, [], []))
        with mock.patch.object(server_module, "MAX_PAYLOAD", budget):
            client.send_frame(107, 10.0)
        first = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual((first[4], first[5]), (109, 0))
        self.assertIsNone(player.mover.corrected)

        player.match.tick = 110
        client.send_frame(110, 10.048)
        second = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual((second[4], second[5], player.mover.corrected), (112, 1, 110))

    def test_ack_is_bounded_even_while_the_first_body_create_is_unconfirmed(self):
        _, _, player, _ = connection()
        player.has_body = True
        player.match.tick = 100
        player.match.start_body_script(player, EntityUpdate(player.body, OP_CREATE))
        player.take_commands([Command(113), Command(179)])  # mover skips this long gap
        self.assertFalse(player.body_script.create_arrived)
        self.assertIsNone(correction.own_record(player, 140))
        with self.subTest(history="all future"):
            self.assertIsNone(correction.snapshot_for(player.mover, 107))
            self.assertEqual(correction.command_ack(player, 107), 109)
        with self.subTest(history="eligible snapshot older than tick"):
            self.assertEqual(correction.snapshot_for(player.mover, 140).frame, 113)
            self.assertEqual(correction.command_ack(player, 140), 115)
        self.assertIsNone(player.mover.corrected)

    def test_old_mover_or_body_correction_tokens_cannot_commit(self):
        for retire in ("mover", "body"):
            with self.subTest(retire=retire):
                _, _, player, _ = connection()
                prepare_corrections(player)
                update = correction.own_record(player, 107)
                old_mover = player.mover
                if retire == "mover":
                    player.new_body(player.body + 1)
                else:
                    player.body += 1
                before = player.mover.corrected
                with mock.patch.object(movelog, "ENABLED", True), mock.patch.object(movelog, "sent") as sent:
                    correction.sent(player, update)
                    sent.assert_not_called()
                self.assertEqual(player.mover.corrected, before)
                self.assertEqual(old_mover.corrected, 100)
                self.assertEqual(correction.command_ack(player, 107, [update]),
                                 correction.command_ack(player, 107))

    def test_oserror_preserves_queues_and_inflight_but_spends_nonce_sequence(self):
        server, client, player, peer = connection()
        client.queue_reliable(20302, {"+0x78": {"+0x0": player.entity}, "+0x7C": False, "+0x80": 0})
        create = placeable()
        client.queue_entities([create])
        pending = list(client.reliable)
        server.sock.fail_next = True
        with self.assertLogs("ow174.game", level="WARNING"):
            client.send_frame(1002, 10.0)
        self.assertEqual(server.sock.sent, [])
        self.assertEqual(client.seq, 1)
        self.assertEqual(client.reliable, pending)
        self.assertEqual(client.entities, [create])
        self.assertEqual(client.in_flight, {})
        self.assertEqual(create.stream.values, [])
        self.assertEqual(peer.open(server.sock.attempted[0][0]).seq, 0)

        client.send_frame(1005, 10.1)
        packet = decoded(self, peer, server.sock.sent[-1][0])[0]
        self.assertEqual(packet.seq, 1)
        self.assertEqual(client.seq, 2)
        self.assertEqual(client.reliable, [])
        self.assertEqual(client.entities, [])
        self.assertEqual(list(client.in_flight), [1])
        client.acked(2, 1)
        self.assertEqual(create.stream.values, [9])

    def test_deferred_create_keeps_seen_false_until_it_reaches_the_wire(self):
        server, client, viewer, peer = connection()
        other = viewer.match.add_player(2, "Beta", SOLDIER, 1, False)
        other.has_body = True
        create = viewer.match.remote_body_create(viewer, other)
        client.queue_entities([create])
        large_messages(client, viewer)
        budget = len(client._payload(1002, client.reliable[:1], [], []))
        with mock.patch.object(server_module, "MAX_PAYLOAD", budget):
            client.send_frame(1002, 10.0)
        self.assertEqual(decoded(self, peer, server.sock.sent[-1][0])[5], 0)
        self.assertFalse(viewer.seen[other.body])
        self.assertEqual([update for update in viewer.match.remote_movements(viewer)
                          if update.entity == other.body], [])
        self.assertIs(client.entities[0], create)
        client.send_frame(1005, 10.1)
        self.assertEqual(decoded(self, peer, server.sock.sent[-1][0])[5], 1)
        self.assertTrue(viewer.seen[other.body])
        movements = [update for update in viewer.match.remote_movements(viewer)
                     if update.entity == other.body]
        self.assertEqual(len(movements), 1)
        self.assertIsNotNone(movements[0].movement)

    def test_ch2_and_queued_movement_are_consumed_and_only_ch2_resends_on_loss(self):
        for resend in (True, False):
            with self.subTest(resend=resend):
                server, client, _, peer = connection()
                event = EntityUpdate(0xA0000201)
                event.ch2 = projectiles.release(1005)
                event.resend = resend
                moving = EntityUpdate(0xA0000301, movement=Movement((1.0, 1.0, 1.0)))
                client.queue_entities([event, moving])
                client.send_frame(1002, 10.0)
                packet = decoded(self, peer, server.sock.sent[-1][0])[0]
                self.assertEqual(client.entities, [])
                self.assertEqual(client.in_flight[packet.seq][1], [event])
                client.acked(packet.seq + 1, 0)
                self.assertEqual(client.entities, [event] if resend else [])
                if resend:
                    client.send_frame(1005, 10.1)
                    client.acked(client.seq, 1)
                    self.assertEqual(client.entities, [])

    def test_more_than_127_candidates_send_a_fifo_prefix_without_losing_tail(self):
        server, client, _, peer = connection()
        records = [EntityUpdate(0x80000001 + index, OP_DESTROY) for index in range(130)]
        client.queue_entities(records)
        client.send_frame(1002, 10.0)
        first = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual(first[5], 127)
        self.assertEqual(client.in_flight[first[0].seq][1], records[:127])
        self.assertEqual(client.entities, records[127:])
        client.send_frame(1005, 10.1)
        second = decoded(self, peer, server.sock.sent[-1][0])
        self.assertEqual(second[5], 3)
        self.assertEqual(client.in_flight[second[0].seq][1], records[127:])
        self.assertEqual(client.entities, [])

    def test_unfit_single_record_or_message_rejects_without_consuming(self):
        for kind in ("entity", "message", "entity_length"):
            with self.subTest(kind=kind):
                server, client, player, _ = connection()
                if kind == "message":
                    value = card_message(player)
                    value["+0x80"] = {**value["+0x80"], "+0x40": "A" * (MAX_PAYLOAD + 100)}
                    client.queue_reliable(20308, value)
                else:
                    length = 1300 if kind == "entity" else 2500
                    record = EntityUpdate(0xA0000901, OP_CREATE,
                                          lambda origin, length=length, body=player.hero.body: world.create(
                                              origin, body, {29: [None, "X" * length]}))
                    client.queue_entities([record])
                pending = (list(client.reliable), list(client.entities))
                with self.assertLogs("ow174.game", level="ERROR") as logs:
                    client.send_frame(1002, 10.0)
                self.assertIn("frame rejected", logs.output[0])
                self.assertEqual(server.sock.attempted, [])
                self.assertEqual(client.seq, 0)
                self.assertEqual((client.reliable, client.entities), pending)
                self.assertEqual(client.in_flight, {})

    def test_encoding_failure_does_not_abort_later_clients_in_the_tick(self):
        server, bad, _, _ = connection()
        _, good, good_player, peer = connection(server, number=2)
        # The native s8 field encoder raises struct.error for a bad queued component value.
        broken = EntityUpdate(0xA0000901, OP_UPDATE,
                              lambda origin: world.update(origin, {74: [None, None, 999]}))
        bad.queue_entities([broken])
        good.queue_reliable(20302, {"+0x78": {"+0x0": good_player.entity}, "+0x7C": False, "+0x80": 0})
        server.tick = 1001
        with self.assertLogs("ow174.game", level="ERROR"):
            server._tick(max(bad.heard, good.heard))
        self.assertEqual(bad.entities, [broken])
        self.assertEqual(bad.seq, 0)
        self.assertEqual(bad.in_flight, {})
        self.assertEqual(len(server.sock.sent), 1)
        packet = decoded(self, peer, server.sock.sent[0][0])[0]
        self.assertEqual(packet.conn, good.conn)
        self.assertEqual(good.reliable, [])


if __name__ == "__main__":
    unittest.main()
