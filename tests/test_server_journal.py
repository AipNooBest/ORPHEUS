"""Controlled roots replay the actual encrypted server and combat paths, without sockets."""

import copy
import dataclasses
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import messages, replay_values
from ow174.game.bits import BitWriter
from ow174.game.collision import StaticCollisionProvider, World, flat_world
from ow174.game.content import PRACTICE_RANGE, SOLDIER, Spawn
from ow174.game.environment import SystemEnvironment
from ow174.game.environment_tape import EnvironmentTapeError
from ow174.game.link import ACK, FIN, KIND_FRAME, SYN, LinkCipher, Packet, ReceiveWindow, peek_connection
from ow174.game.match import SEND_EVERY
from ow174.game.server import FIRST_TICK, HANDOFF_SECONDS, GameServer
from ow174.game.server_journal import (
    RecordingServerSession,
    ServerJournalError,
    _ReplaySession,
    replay_server_session,
)

REAPER = 0x02E0000000000002
MAP = dataclasses.replace(
    PRACTICE_RANGE, name="controlled two-team scene", map_guid=0x0800000000FFF901,
    placeables=(), spawns=(Spawn(0, (0.0, 0.0, 0.0), 0.0), Spawn(1, (0.0, 0.0, 5.0), 180.0)),
    team_sizes=(1, 1),
)


class FixedSource:
    def __init__(self):
        self.counter = 0

    def wall_time(self, *, callsite):
        return 100.0

    def wall_time_ns(self, *, callsite):
        self.counter += 1
        return 100_000_000_000 + self.counter

    def monotonic(self, *, callsite):
        return 100.0

    def randbits(self, bits, *, callsite):
        self.counter += 1
        return self.counter & ((1 << bits) - 1)

    def randbelow(self, upper, *, callsite):
        self.counter += 1
        return self.counter % upper

    def token_bytes(self, length, *, callsite):
        self.counter += 1
        return bytes([self.counter % 256]) * length


class SchedulerSource(FixedSource):
    def __init__(self, *reads):
        super().__init__()
        self.reads = iter(reads)

    def scheduler_read(self, callsite):
        expected, value = next(self.reads)
        if callsite != "server.run." + expected:
            raise AssertionError(f"expected {expected}, got {callsite}")
        return value

    def wall_time(self, *, callsite):
        if callsite.startswith("server.run."):
            return self.scheduler_read(callsite)
        return super().wall_time(callsite=callsite)

    def monotonic(self, *, callsite):
        return self.scheduler_read(callsite)


def wall_world():
    return World([
        (-20, 0, -20, -20, 0, 20, 20, 0, 20), (-20, 0, -20, 20, 0, 20, 20, 0, -20),
        (-10, 0, 2, 10, 0, 2, 10, 5, 2), (-10, 0, 2, 10, 5, 2, -10, 5, 2),
    ], name="controlled floor and wall")


class Peer:
    def __init__(self, session, handoff, sent, address):
        self.session, self.handoff, self.sent, self.address = session, handoff, sent, address
        self.cipher = LinkCipher(handoff.key_out, handoff.key_in)
        self.window = ReceiveWindow()
        self.seq = self.reliable = self.cursor = 0
        self.last_raw = None

    def drain(self, *, lose=False):
        for data, address in self.sent[self.cursor:]:
            if peek_connection(data) != self.handoff.conn:
                continue
            if not lose:
                packet = self.cipher.open(data)
                assert packet is not None and address == self.address, (len(data), address, self.address)
                self.window.accept(packet.seq)
        self.cursor = len(self.sent)

    def send(self, *, flags=ACK, payload=b"", now=100.0, address=None):
        packet = Packet(self.handoff.conn, KIND_FRAME if payload else 0, self.seq,
                        self.window.expected, self.window.bits, flags, payload)
        self.seq += 1
        self.last_raw = self.cipher.seal(packet)
        self.session.datagram(self.last_raw, self.address if address is None else address, now)
        self.drain()

    def message(self, identifier, value, now=100.0):
        payload = BitWriter()
        messages.write_channel(payload, [(self.reliable, messages.encode(identifier, value))])
        self.reliable += 1
        self.send(payload=payload.getvalue(), now=now)

    def commands(self, first, count, buttons, *, forward=0, now=100.8):
        if count > 32:
            self.commands(first, 32, buttons, forward=forward, now=now)
            return self.commands(first + 32, count - 32, buttons, forward=forward, now=now)
        body, payload = BitWriter(), BitWriter()
        body.bits(0, 24)
        body.bits(count, 8)
        body.bits(first, 32)
        body.bit(bool(forward))
        if forward:
            body.bit(0)  # arbitrary throttles
            body.bit(0)  # unchanged right
            body.bit(1)  # forward follows
            body.signed(forward, 8)
        body.bits(0, 32)
        body.bits(buttons, 24)
        body.bits(0, 24)
        for _ in range(count - 1):
            body.bit(0)
            body.bit(0)
        messages.write_channel(payload)
        payload.bit(1)
        payload.bits(body.count, 32)
        payload.append(body)
        self.send(payload=payload.getvalue(), now=now)


def pick(entity, hero):
    return {"+0x78": {"+0x0": entity},
            "+0x80": bytes.fromhex("0000000001c15d04") + hero.to_bytes(8, "little") + bytes(8),
            "+0xA8": 0x0240000000001361, "+0xB0": 3}


def acknowledge_ownership(session, peers):
    for index in range(SEND_EVERY):
        now = 100.5 + index * 0.016
        session.tick(now)
        for peer in peers:
            peer.drain()
            peer.send(now=now)


class ServerJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)

    def make(self, world=None, *, source=None, skin_of=None, sendto=None, verify_stored_state=False):
        sent = []

        def send(data, address):
            sent.append((data, address))
            return len(data)

        args = {"environment": FixedSource() if source is None else source,
                "collision_provider": StaticCollisionProvider(flat_world(0.0) if world is None else world),
                "sendto": send if sendto is None else sendto,
                "verify_stored_state": verify_stored_state}
        if skin_of is not None:
            args["skin_of"] = skin_of
        return RecordingServerSession(**args), sent

    def connected(self, session, sent, *, two=True):
        entries = [(1, "Alpha", SOLDIER, 0, False), (2, "Beta", SOLDIER, 1, False)]
        handoffs = session.create_match(MAP, entries if two else entries[:1])
        peers = [Peer(session, h, sent, ("127.0.0.1", 42000 + i)) for i, h in enumerate(handoffs)]
        for peer in peers:
            peer.send(flags=SYN)
            peer.send()
            peer.message(21601, {})
        session.report_callbacks()
        acknowledge_ownership(session, peers)
        session.tick(100.8)
        players = session.server.matches[0].players
        self.assertTrue(all(player.spawned for player in players))
        for peer, player in zip(peers, players, strict=True):
            peer.message(21616, pick(player.entity, SOLDIER), now=100.8)
            self.assertIsNotNone(player.body_script)
        # Actual create/full ACK processing goes through the raw datagram router.
        for tick in range(8):
            session.tick(100.816 + tick * 0.016)
            for peer in peers:
                peer.drain()
                peer.send(now=100.816 + tick * 0.016)
        return peers, players

    def verify(self, session):
        document = session.finish()
        replay = _ReplaySession(document, session.registry.resolve)
        result = replay.run()
        self.assertEqual(result["status"], "matched_controlled_roots_and_wire")
        self.assertFalse(result["complete_battle_replay"])
        self.assertFalse(result["full_state_verified"])
        return document, replay, result

    def test_factory_is_per_match_and_defaults_remain_optional(self):
        provider = StaticCollisionProvider(flat_world(0.0))
        factory = mock.Mock(return_value=provider)
        server = GameServer(environment=FixedSource(), collision_provider_factory=factory)
        server.create_match(MAP, [])
        server.create_match(MAP, [])
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(all(match.collision_world is provider.state.world for match in server.matches))

    def test_encrypted_input_ack_loss_duplicate_and_wall_replay_without_socket_or_game(self):
        with mock.patch("socket.socket", side_effect=AssertionError("must not create sockets")):
            session, sent = self.make(wall_world())
            peers, players = self.connected(session, sent)
            shooter, victim = players
            peers[0].commands(shooter.body_script.frame + 1, 50, 1, forward=127)
            self.assertLess(shooter.mover.position[2], 2.0)
            health = session.server.matches[0].combat.vitals[victim.body].total
            self.assertEqual(health, 200.0)
            newest = shooter.commands.last_frame
            session.datagram(peers[0].last_raw, peers[0].address, 101.0)  # duplicate wire seq
            self.assertEqual(shooter.commands.last_frame, newest)
            for index in range(6):
                session.tick(101.016 + index * 0.016)
                peers[0].drain(lose=True)
                peers[1].drain()
            peers[0].send(now=101.2)  # loss ACK triggers ordinary reliable retransmission
            session.tick(101.216, missed=3)
            document, replay, result = self.verify(session)
        self.assertGreater(result["send_attempts"], 10)
        copied = replay.server.matches[0].players
        self.assertEqual(copied[0].mover.position, shooter.mover.position)
        self.assertEqual(replay.server.matches[0].combat.vitals[copied[1].body].total, health)
        self.assertEqual(replay.server.tick, session.server.tick)
        self.assertEqual(document["scope"], "controlled-roots-and-wire-only")

    def test_death_respawn_hero_skin_and_same_address_reentry_use_production_paths(self):
        skins = {}
        session, sent = self.make(skin_of=lambda account, hero: skins.get((account, hero), (0, False)))
        peers, players = self.connected(session, sent)
        shooter, victim = players
        peers[0].commands(shooter.body_script.frame + 1, 60, 1)
        fight = victim.match.combat
        self.assertTrue(fight.is_dead(victim))
        old_body = victim.body
        session.tick(105.0, missed=200)
        self.assertNotEqual(victim.body, old_body)
        self.assertFalse(fight.is_dead(victim))
        self.assertTrue(session.apply("switch_hero", {"account": 1, "hero": REAPER}))
        skins[(1, REAPER)] = (0x123, True)
        self.assertTrue(session.apply("skin_changed", {"account": 1, "hero": REAPER}))
        old = peers[0]
        old.send(flags=FIN, now=105.01)
        session.report_callbacks()
        (handoff,) = session.create_match(MAP, [(1, "Alpha", SOLDIER, 0, False)])
        new = Peer(session, handoff, sent, old.address)
        new.send(flags=SYN, now=105.02)
        new.send(now=105.02)
        # A late, authenticated old packet cannot affect the new connection on that address.
        session.datagram(old.last_raw, old.address, 105.03)
        session.report_callbacks()
        document, replay, result = self.verify(session)
        self.assertIn(handoff.conn, replay.server.clients)
        self.assertNotIn(old.handoff.conn, replay.server.clients)
        self.assertTrue(any(replay_values.decode(effect)[1]["kind"] == "left"
                            for root in document["roots"] for effect in root["effects"]))
        self.assertGreater(result["roots"], 30)

    def test_expired_handoff_leaves_on_the_next_tick_and_replays_the_callback(self):
        session, sent = self.make()
        entries = [(1, "Alpha", SOLDIER, 0, False), (2, "Beta", SOLDIER, 1, False)]
        first, second = session.create_match(MAP, entries)
        alpha, beta = session.server.matches[0].players
        second_deadline = session.server.handoffs[second.conn][2]

        self.assertTrue(session.apply("expire_handoff", {"conn": first.conn}))
        self.assertEqual(session.server.handoffs[first.conn], (first, alpha, 0.0))
        self.assertEqual(session.server.handoffs[second.conn][2], second_deadline)
        self.assertFalse(alpha.gone)
        cancelled = Peer(session, first, sent, ("127.0.0.1", 43000))
        cancelled.send(flags=SYN)
        self.assertNotIn(first.conn, session.server.clients)
        self.assertIn(first.conn, session.server.handoffs)
        self.assertEqual(sent, [])
        session.report_callbacks()

        session.tick(100.0)
        self.assertTrue(alpha.gone)
        self.assertFalse(beta.gone)
        self.assertNotIn(first.conn, session.server.handoffs)
        self.assertFalse(session.apply("expire_handoff", {"conn": first.conn}))
        session.report_callbacks()

        peer = Peer(session, second, sent, ("127.0.0.1", 43000))
        peer.send(flags=SYN)
        self.assertFalse(session.apply("expire_handoff", {"conn": second.conn}))

        document, replay, _ = self.verify(session)
        self.assertIn(second.conn, replay.server.clients)
        self.assertNotIn(first.conn, replay.server.handoffs)
        callback_rows = [row for row in document["roots"] if row["operation"] == "report_callbacks"]
        self.assertEqual(
            [[replay_values.decode(effect)[1] for effect in row["effects"]] for row in callback_rows],
            [[], [{"kind": "left", "player": (first.match_id, 0)}]],
        )

    def test_bounded_loop_replays_syn_before_the_due_tick_and_early_receives(self):
        deadline = 100.0 + HANDOFF_SECONDS
        source = SchedulerSource(
            ("start", 10.0), ("timeout", 10.0), ("datagram", deadline - 0.001),
            ("due", 10.030), ("next_tick", 10.034), ("tick", deadline),
            ("timeout", 10.040), ("due", 10.045), ("timeout", 10.046), ("due", 10.047),
        )
        with (
            mock.patch("socket.socket", side_effect=AssertionError("must not create sockets")),
            self.assertLogs("ow174.game", level="WARNING") as logged,
        ):
            session, sent = self.make(source=source)
            (handoff,) = session.create_match(MAP, [(1, "Alpha", SOLDIER, 0, False)])
            peer = Peer(session, handoff, sent, ("127.0.0.1", 43000))
            syn = peer.cipher.seal(Packet(handoff.conn, flags=SYN))
            socket_before = session.server.sock
            receives = [(syn, peer.address), ("error", (10054, "controlled receive failure")), None]
            session.run_loop(receives)
            document, replay, result = self.verify(session)
        self.assertIs(session.server.sock, socket_before)
        self.assertFalse(session.server.running)
        self.assertEqual(result["send_attempts"], 1)
        [(reply, address)] = sent
        self.assertEqual(address, peer.address)
        self.assertEqual(peer.cipher.open(reply).flags, SYN | ACK)
        self.assertEqual(sum("controlled receive failure" in line for line in logged.output), 2)
        for server in (session.server, replay.server):
            self.assertIn(handoff.conn, server.clients)
            self.assertNotIn(handoff.conn, server.handoffs)
            self.assertEqual(server.tick, FIRST_TICK + 3)
            self.assertEqual(server.clients[handoff.conn].heard, deadline - 0.001)
        self.assertEqual([row["operation"] for row in document["roots"]], ["create_match", "run_loop"])
        self.assertEqual(
            [row["callsite"] for row in document["environment"]["reads"]
             if row["callsite"].startswith("server.run.")],
            ["server.run." + name for name in (
                "start", "timeout", "datagram", "due", "next_tick", "tick",
                "timeout", "due", "timeout", "due",
            )],
        )
        effects = [replay_values.decode(effect)[1] for effect in document["roots"][-1]["effects"]]
        socket_effects = [effect for effect in effects if effect["kind"].startswith("socket_")]
        self.assertEqual(
            [effect["kind"] for effect in socket_effects],
            ["socket_timeout", "socket_receive"] * 3 + ["socket_close"],
        )
        self.assertEqual(socket_effects[0]["timeout"], 0.0001)
        self.assertAlmostEqual(socket_effects[2]["timeout"], 0.008)
        self.assertAlmostEqual(socket_effects[4]["timeout"], 0.002)
        self.assertEqual([effect["size"] for effect in socket_effects[1:6:2]], [4096] * 3)
        for replacement in (receives[:-1], [*receives, None]):
            with self.subTest(receive_count=len(replacement)):
                changed = copy.deepcopy(document)
                args = replay_values.decode(changed["roots"][-1]["arguments"])
                args["receives"] = replacement
                changed["roots"][-1]["arguments"] = replay_values.encode(args)
                with (
                    self.assertLogs("ow174.game", level="WARNING"),
                    self.assertRaises((EnvironmentTapeError, ServerJournalError)),
                ):
                    replay_server_session(changed, resolver=session.registry.resolve)

    def test_bounded_loop_replays_callback_reentry_after_releasing_the_server_lock(self):
        source = SchedulerSource(
            ("start", 10.0), ("timeout", 10.0), ("due", 10.0), ("next_tick", 10.0), ("tick", 100.1),
            ("timeout", 10.005), ("due", 10.010),
        )
        session, sent = self.make(source=source, verify_stored_state=True)
        entries = [(1, "Alpha", SOLDIER, 0, False), (2, "Beta", SOLDIER, 1, False)]
        first, second = session.create_match(MAP, entries)
        self.assertTrue(session.apply("expire_handoff", {"conn": first.conn}))
        alpha = (first.match_id, 0)
        beta = (second.match_id, 1)
        args = {"player": beta, "reason": "callback removed the second player"}
        take_out = GameServer.take_out

        def checked_take_out(server, player, reason):
            self.assertFalse(server.lock._is_owned(), "callback reentry must start outside the game lock")
            return take_out(server, player, reason)

        with (
            mock.patch("socket.socket", side_effect=AssertionError("must not create sockets")),
            mock.patch.object(GameServer, "take_out", checked_take_out),
        ):
            session.run_loop([None, None], callback_actions=[("left", alpha, "take_out", args)])
            document, replay, result = self.verify(session)
        self.assertTrue(result["stored_state_verified"])
        self.assertEqual(result["send_attempts"], 0)
        self.assertEqual(sent, [])
        for server in (session.server, replay.server):
            self.assertEqual(server.tick, FIRST_TICK + 1)
            self.assertIsNone(server.player_of(1))
            self.assertIsNone(server.player_of(2))
        effects = [replay_values.decode(effect)[1] for effect in document["roots"][-1]["effects"]]
        kinds = ["left", "callback_action", "callback_result", "left"]
        lifecycle = [effect for effect in effects if effect["kind"] in kinds]
        self.assertEqual([effect["kind"] for effect in lifecycle], kinds)
        self.assertEqual([lifecycle[0]["player"], lifecycle[-1]["player"]], [alpha, beta])
        self.assertEqual(lifecycle[1]["callback"], "left")
        self.assertEqual(lifecycle[1]["player"], alpha)
        self.assertEqual(lifecycle[1]["operation"], "take_out")
        self.assertEqual(lifecycle[1]["arguments"], args)
        self.assertEqual(lifecycle[2]["result"], "left before its game connected")

    def test_bounded_loop_rejects_malformed_receives_before_running(self):
        for receives in ([], [b"not an outcome"], [("error", (True, "not a valid errno"))]):
            with self.subTest(receives=receives):
                session, _ = self.make()
                with mock.patch.object(GameServer, "_run") as run, self.assertRaises(ServerJournalError):
                    session.run_loop(receives)
                run.assert_not_called()
                self.assertEqual(session.server.tick, FIRST_TICK)
                self.assertFalse(session.server.running)

    def test_bounded_loop_rejects_unmatched_callback_actions(self):
        session, sent = self.make()
        (handoff,) = session.create_match(MAP, [(1, "Alpha", SOLDIER, 0, False)])
        waiting = session.server.handoffs[handoff.conn]
        actions = [("left", (handoff.match_id, 0), "expire_handoff", {"conn": handoff.conn})]
        with self.assertRaisesRegex(ServerJournalError, "unconsumed callback actions") as caught:
            session.run_loop([None], callback_actions=actions)
        self.assertEqual(session.server.handoffs[handoff.conn], waiting)
        self.assertFalse(session.server.running)
        self.assertEqual(sent, [])
        with self.assertRaises(ServerJournalError) as finish:
            session.finish()
        self.assertIs(finish.exception, caught.exception)

    def test_bounded_loop_faults_if_the_production_tick_raises(self):
        session, _ = self.make()
        socket_before = session.server.sock
        with (
            mock.patch.object(GameServer, "_tick", side_effect=RuntimeError("controlled tick failure")),
            self.assertRaises(ServerJournalError) as caught,
        ):
            session.run_loop([None])
        self.assertIs(session.fault, caught.exception)
        self.assertIs(session.server.sock, socket_before)
        self.assertFalse(session.server.running)
        with self.assertRaises(ServerJournalError) as finish:
            session.finish()
        self.assertIs(finish.exception, caught.exception)

    def test_bounded_loop_fault_survives_a_callback_error_caught_by_the_server(self):
        session, _ = self.make()
        entries = [(1, "Alpha", SOLDIER, 0, False), (2, "Beta", SOLDIER, 1, False)]
        first, second = session.create_match(MAP, entries)
        self.assertTrue(session.apply("expire_handoff", {"conn": first.conn}))
        socket_before = session.server.sock
        action_args = {"player": (second.match_id, 1), "reason": "callback failed"}
        actions = [("left", (first.match_id, 0), "take_out", action_args)]
        with (
            mock.patch.object(GameServer, "take_out", side_effect=RuntimeError("callback failure")),
            self.assertLogs("ow174.game", level="ERROR") as logged,
            self.assertRaisesRegex(ServerJournalError, "callback action failed") as caught,
        ):
            session.run_loop([None], callback_actions=actions)
        self.assertIn("join or leave handler failed", "\n".join(logged.output))
        self.assertIsNotNone(session.server.player_of(2))
        self.assertIs(session.server.sock, socket_before)
        self.assertFalse(session.server.running)
        with self.assertRaises(ServerJournalError) as finish:
            session.finish()
        self.assertIs(finish.exception, caught.exception)

    def test_os_send_failure_is_replayed_as_an_attempt_not_as_packet_delivery(self):
        def refused(data, address):
            raise OSError(10054, "controlled send failure")

        session, sent = self.make(sendto=refused)
        (handoff,) = session.create_match(MAP, [(1, "Alpha", SOLDIER, 0, False)])
        peer = Peer(session, handoff, sent, ("127.0.0.1", 43000))
        with self.assertLogs("ow174.game", level="WARNING"):
            peer.send(flags=SYN)
        with self.assertLogs("ow174.game", level="WARNING"):
            _, _, result = self.verify(session)
        self.assertEqual(result["send_attempts"], 1)
        self.assertFalse(session.server.clients[handoff.conn].open)

    def test_wire_result_root_sequence_read_positions_and_closed_tail_tampering_fail(self):
        session, sent = self.make()
        self.connected(session, sent, two=False)
        document = session.finish()
        variants = []
        changed = copy.deepcopy(document)
        changed["roots"][0]["seq"] = 2
        variants.append(changed)
        changed = copy.deepcopy(document)
        changed["roots"][0]["after"] = replay_values.encode((999, (0, 0)))
        variants.append(changed)
        changed = copy.deepcopy(document)
        changed["roots"][0]["result"] = replay_values.encode([])
        variants.append(changed)
        changed = copy.deepcopy(document)
        send_root = next(row for row in changed["roots"] if any(
            replay_values.decode(effect)[1]["kind"] == "send" for effect in row["effects"]
        ))
        index = next(i for i, effect in enumerate(send_root["effects"])
                     if replay_values.decode(effect)[1]["kind"] == "send")
        position, effect = replay_values.decode(send_root["effects"][index])
        effect["data"] = b"tampered wire"
        send_root["effects"][index] = replay_values.encode((position, effect))
        variants.append(changed)
        changed = copy.deepcopy(document)
        del changed["end"]
        variants.append(changed)
        for changed in variants:
            with self.subTest(case=len(changed)), self.assertRaises(ServerJournalError):
                replay_server_session(changed, resolver=session.registry.resolve)

    def test_unsupported_or_failed_roots_permanently_prevent_successful_finish(self):
        for operation, args in (("start", {}), ("datagram", {"data": b"", "address": [], "now": 1.0})):
            session, _ = self.make()
            with self.assertRaises(ServerJournalError):
                session.apply(operation, args)
            first = session.fault
            self.assertIsInstance(first, ServerJournalError)
            with self.assertRaises(ServerJournalError) as caught:
                session.finish()
            self.assertIs(caught.exception, first)

    def test_real_secure_environment_is_opt_in_and_tape_is_private_in_memory(self):
        session, _ = self.make(source=SystemEnvironment())
        session.create_match(MAP, [])
        document, _, result = self.verify(session)
        self.assertIn("environment", document)
        self.assertEqual(result["send_attempts"], 0)
        document["roots"].clear()
        self.assertEqual(len(session.finish()["roots"]), 1)


if __name__ == "__main__":
    unittest.main()
