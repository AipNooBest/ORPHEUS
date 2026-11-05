"""The owner stream retains cumulative changes without keeping confirmed-lost snapshots."""

import unittest
from unittest.mock import patch

import test_server_journal as journal_tests
from test_script_driver import ClientModel

from ow174.game import bodytrace, movelog
from ow174.game.bits import BitWriter
from ow174.game.commands import FIRE
from ow174.game.link import KIND_FRAME
from ow174.game.match import SEND_EVERY
from ow174.game.script.driver import BodyStream, Snapshot


class OwnerDeltaLossTests(unittest.TestCase):
    def test_encrypted_negative_acks_compact_owner_snapshots_and_preserve_rifle_ammo(self):
        for diagnostics in (bodytrace, movelog):
            self.enterContext(patch.object(diagnostics, "ENABLED", False))
        fixture = journal_tests.ServerJournalTests()
        session, sent = fixture.make()
        (peer,), (player,) = fixture.connected(session, sent, two=False)
        client, script = player.client, player.body_script
        stream = script.stream
        model = ClientModel()
        model.apply(stream.full_update)
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

        for count in (1, 24):
            peer.commands(script.frame + 1, count, 0, now=now)
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
