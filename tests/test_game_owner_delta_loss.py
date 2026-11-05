"""The owner stream retains cumulative changes without keeping confirmed-lost snapshots."""

import unittest
from unittest.mock import patch

import test_server_journal as journal_tests
from test_game_packet_budget import connection, prepare_corrections
from test_game_repeated_lifecycles import wire_frame
from test_script_driver import ClientModel

from ow174.game import bodytrace, movelog
from ow174.game.bits import BitWriter
from ow174.game.commands import FIRE
from ow174.game.content import SOLDIER
from ow174.game.link import KIND_FRAME, SYN
from ow174.game.match import SEND_EVERY
from ow174.game.script.driver import SETTLE_FRAMES, BodyStream, Snapshot


class OwnerDeltaLossTests(unittest.TestCase):
    def setUp(self):
        for diagnostics in (bodytrace, movelog):
            self.enterContext(patch.object(diagnostics, "ENABLED", False))

    def test_first_owner_chunk_retries_exactly_before_corrections(self):
        session, sent = journal_tests.ServerJournalTests().make()
        handoff, = session.create_match(journal_tests.MAP, [(1, "Alpha", SOLDIER, 0, False)])
        peer = journal_tests.Peer(session, handoff, sent, ("memory.test", 42000))
        peer.send(flags=SYN)
        peer.send()
        peer.message(21601, {})
        session.report_callbacks()
        journal_tests.acknowledge_ownership(session, [peer])
        now = 100.8
        session.tick(now)
        player, = session.server.matches[0].players
        peer.message(21616, journal_tests.pick(player.entity, SOLDIER), now=now)
        script = player.body_script
        model = ClientModel()

        def next_world():
            nonlocal now
            cursor = len(sent)
            for _ in range(SEND_EVERY):
                now += 0.016
                session.tick(now)
                if len(sent) > cursor:
                    break
            self.assertEqual(len(sent), cursor + 1)
            packet = peer.cipher.open(sent[-1][0])
            self.assertEqual(packet.kind, KIND_FRAME)
            return [item for item, _ in wire_frame(packet)[1] if item.entity == player.body and item.chunk]

        def receive(*, lose=False):
            peer.drain(lose=lose)
            peer.send(now=now)

        self.assertEqual(next_world(), [])  # create precedes the full script
        receive()
        full, = next_world()
        self.assertTrue(model.apply(full).full)
        receive()
        self.assertNotIn((11, 70), model.on())  # 0254 ammo UXPresenter

        command = script.frame + 1
        peer.commands(command, 1, FIRE, now=now)
        self.assertEqual(script.component.instances[11].vars[53].value(), 29)
        first, = next_world()
        original = first.chunk.getvalue(), first.chunk.count
        report, = script.reports
        receive(lose=True)
        self.assertEqual(next_world(), [])
        receive()  # a later positive ACK reports the first frame missing

        retry, = next_world()  # no new command was needed to retry the initial state
        self.assertEqual((retry.chunk.getvalue(), retry.chunk.count), original)
        receive(lose=True)
        peer.commands(command + 1, SETTLE_FRAMES, 0, now=now)
        self.assertEqual(next_world(), [])  # even with fresh commands, no correction yet
        receive()
        retry, = next_world()
        self.assertEqual((retry.chunk.getvalue(), retry.chunk.count), original)
        initial = model.apply(retry)
        self.assertEqual((initial.owner, initial.correction, initial.cmfd), (True, False, command))
        self.assertTrue({(11, 11), (11, 70)} <= model.on())  # weapon and ammo presenter
        receive()
        self.assertEqual(script.reports, [report])
        self.assertIs(report.received, True)
        self.assertIsNone(script.stream.initial_owner)

        correction, = next_world()
        frame = model.apply(correction)
        self.assertEqual((frame.owner, frame.correction, frame.cmfd), (True, True, command + SETTLE_FRAMES))
        self.assertEqual(model.vars[(11, 53)], 29)
        receive()

    def test_stop_cancels_an_already_queued_first_owner_retry(self):
        server, client, player, peer = connection()
        prepare_corrections(player)
        script = player.body_script
        full, = script.frames()
        script.stream.arrived(full.chunk_last)
        initial, = script.frames()
        client.queue_entities([initial])
        client.send_frame(110, 10.0)
        packet = peer.open(server.sock.sent[-1][0])
        client.acked(packet.seq + 1, 0)
        self.assertIn(initial, client.entities)
        self.assertTrue(initial.resend)

        player.match.stop_body_script(player, failed=True)
        self.assertTrue(player.has_body and script.retired)
        self.assertEqual(player.body, initial.entity)
        self.assertNotIn(initial, client.entities)
        self.assertFalse(initial.resend)
        self.assertIsNone(script.stream.initial_owner)

    def test_encrypted_negative_acks_compact_owner_snapshots_and_preserve_rifle_ammo(self):
        fixture = journal_tests.ServerJournalTests()
        session, sent = fixture.make()
        (peer,), (player,) = fixture.connected(session, sent, two=False)
        client, script = player.client, player.body_script
        stream = script.stream
        model = ClientModel()
        for raw, _ in sent:
            packet = peer.cipher.open(raw)
            if packet.kind == KIND_FRAME:
                for update, _ in wire_frame(packet)[1]:
                    if update.entity == player.body and update.chunk is not None:
                        model.apply(update)
        now = 101.0

        def next_frame(*, receive):
            nonlocal now
            cursor = len(sent)
            for _ in range(SEND_EVERY * 2):
                now += 0.016
                session.tick(now)
                if len(sent) > cursor:
                    break
            self.assertEqual(len(sent), cursor + 1)
            packet = peer.cipher.open(sent[-1][0])
            self.assertEqual(packet.kind, KIND_FRAME)
            owner_chunks = [item for item in client.in_flight[packet.seq][1] if item.stream is stream]
            peer.drain(lose=not receive)
            if receive:
                for item in owner_chunks:
                    model.apply(item)
                peer.send(now=now)
            return packet.seq, owner_chunks

        self.assertIsNotNone(script.first_owner_frame)
        peer.commands(script.frame + 1, SETTLE_FRAMES, 0, now=now)
        _, (baseline,) = next_frame(receive=True)
        self.assertEqual(stream.base_last, baseline.chunk_last)

        for _ in range(120):
            peer.commands(script.frame + 1, 3, FIRE, now=now)
            dropped_seq, (dropped,) = next_frame(receive=False)
            _, idle_chunks = next_frame(receive=True)
            self.assertEqual(idle_chunks, [])
            self.assertNotIn(dropped_seq, client.in_flight)
            self.assertNotIn(dropped.chunk_last, tuple(stream.pending))
            self.assertEqual(stream.base_last, baseline.chunk_last)
        self.assertEqual(stream.pending, {})
        self.assertIn(("var", 11, 53), stream.touched())

        peer.commands(script.frame + 1, 1, 0, now=now)
        _, (recovered,) = next_frame(receive=True)
        self.assertEqual(stream.base_last, recovered.chunk_last)
        self.assertEqual(stream.touched(), set())
        self.assertNotIn((11, 10), model.on())
        self.assertEqual(script.component.instances[11].vars[53].value(), 22)
        self.assertEqual(model.vars[(11, 53)], 22)

    def test_older_positive_ack_preserves_keys_from_the_lost_suffix(self):
        stream = BodyStream(0xA0000101)
        full = stream.full(BitWriter(), Snapshot(100))
        stream.arrived(full.chunk_last)
        old_key, shared, new_key = ("var", 11, 53), ("state", 11, 10), ("state", 11, 16)
        old = stream.delta(BitWriter(), Snapshot(101), {old_key, shared}, 101)
        received = stream.delta(BitWriter(), Snapshot(102), {old_key, shared}, 102)
        new = stream.delta(BitWriter(), Snapshot(103), {shared, new_key}, 103)

        stream.lost(old.chunk_last)
        stream.lost(new.chunk_last)
        stream.arrived(received.chunk_last)
        stream.arrived(old.chunk_last)  # A late ACK for a retired transport record.
        self.assertEqual(stream.base_last, received.chunk_last)
        self.assertEqual(stream.pending, {})
        self.assertEqual(stream.touched(), {shared, new_key})

        recovered = stream.delta(BitWriter(), Snapshot(104), stream.touched(), 104)
        stream.arrived(recovered.chunk_last)
        stream.lost(new.chunk_last)
        self.assertEqual(stream.base.k, 104)
        self.assertEqual(stream.touched(), set())


if __name__ == "__main__":
    unittest.main()
