"""Ordered external reads through the production server, client and match paths."""

import sys
import unittest
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import environment as environment_module
from ow174.game import match as match_module
from ow174.game import messages
from ow174.game.bits import BitReader, BitWriter
from ow174.game.collision import StaticCollisionProvider, flat_world
from ow174.game.content import PRACTICE_RANGE
from ow174.game.environment import SystemEnvironment
from ow174.game.link import ACK, SYN, LinkCipher, Packet
from ow174.game.match import Match, pong
from ow174.game.server import FIRST_TICK, HANDOFF_SECONDS, PING, Client, GameServer


@dataclass(frozen=True)
class Read:
    operation: str
    callsite: str
    result: object
    argument: int | None = None


class TapeError(AssertionError):
    pass


class ReadTape:
    """Explicit test input, including errors; no runtime fallback or seeded RNG."""

    def __init__(self, *reads: Read) -> None:
        self.reads = deque(reads)
        self.consumed = []
        self.failures = []

    def __bool__(self):
        return False  # An explicitly supplied environment must survive a false truth value.

    def take(self, operation, callsite, argument=None):
        actual = (operation, callsite, argument)
        if not self.reads:
            self.fail(TapeError(f"unexpected external read: {actual}"))
        expected = self.reads[0]
        wanted = (expected.operation, expected.callsite, expected.argument)
        if actual != wanted:
            self.fail(TapeError(f"external read order: expected {wanted}, got {actual}"))
        self.reads.popleft()
        self.consumed.append(actual)
        if isinstance(expected.result, Exception):
            self.fail(expected.result)
        return expected.result

    def fail(self, error):
        self.failures.append(error)
        raise error

    def assert_exhausted(self):
        if self.failures:
            raise TapeError(f"external read failed: {self.failures[0]}") from self.failures[0]
        if self.reads:
            raise TapeError(f"unconsumed external reads: {len(self.reads)}, next {self.reads[0].callsite}")

    def wall_time(self, *, callsite):
        return self.take("wall_time", callsite)

    def wall_time_ns(self, *, callsite):
        return self.take("wall_time_ns", callsite)

    def monotonic(self, *, callsite):
        return self.take("monotonic", callsite)

    def randbits(self, bits, *, callsite):
        return self.take("randbits", callsite, bits)

    def randbelow(self, upper, *, callsite):
        return self.take("randbelow", callsite, upper)

    def token_bytes(self, length, *, callsite):
        return self.take("token_bytes", callsite, length)


class MemorySocket:
    def __init__(self):
        self.sent = []

    def sendto(self, data, address):
        self.sent.append((data, address))
        return len(data)


class OneReceiveSocket(MemorySocket):
    """One synchronous _run iteration, without binding or creating a thread."""

    def __init__(self, server, data):
        super().__init__()
        self.server = server
        self.data = data
        self.timeouts = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recvfrom(self, maximum):
        self.server.running = False
        if self.data is None:
            raise TimeoutError
        return self.data, ("127.0.0.1", 4567)

    def close(self):
        self.closed = True


def initial_reads():
    return (
        Read("randbelow", "server.next_conn", 31, 0x100000),
        Read("randbits", "match.id.low", 11, 63),
        Read("randbits", "match.id.high", 22, 63),
        Read("wall_time", "match.started", 100.0),
        Read("token_bytes", "server.handoff.key_in", b"a" * 32, 32),
        Read("token_bytes", "server.handoff.key_out", b"b" * 32, 32),
        Read("wall_time", "server.handoff.deadline", 101.0),
    )


class GameEnvironmentTests(unittest.TestCase):
    def setUp(self):
        # These tests cover external clocks/entropy, without starting async asset loading.
        collision = mock.patch.object(match_module, "default_collision_provider",
                                      return_value=StaticCollisionProvider(flat_world(1.0)))
        collision.start()
        self.addCleanup(collision.stop)

    def test_system_environment_uses_original_secure_sources_for_each_read(self):
        sources = mock.Mock()
        with (
            mock.patch.object(environment_module.time, "time", side_effect=[1.0, 2.0]) as wall,
            mock.patch.object(environment_module.time, "time_ns", side_effect=[3, 4]) as nanos,
            mock.patch.object(environment_module.time, "monotonic", return_value=5.0) as monotonic,
            mock.patch.object(environment_module.secrets, "randbits", return_value=6) as randbits,
            mock.patch.object(environment_module.secrets, "randbelow", return_value=7) as randbelow,
            mock.patch.object(environment_module.secrets, "token_bytes", return_value=b"key") as token,
        ):
            for name, source in (
                ("wall", wall),
                ("nanos", nanos),
                ("monotonic", monotonic),
                ("randbits", randbits),
                ("randbelow", randbelow),
                ("token", token),
            ):
                sources.attach_mock(source, name)
            environment = SystemEnvironment()
            self.assertEqual(environment.wall_time(callsite="first"), 1.0)
            self.assertEqual(environment.wall_time(callsite="second"), 2.0)
            self.assertEqual(environment.wall_time_ns(callsite="first"), 3)
            self.assertEqual(environment.wall_time_ns(callsite="second"), 4)
            self.assertEqual(environment.monotonic(callsite="deadline"), 5.0)
            self.assertEqual(environment.randbits(63, callsite="identity"), 6)
            self.assertEqual(environment.randbelow(100, callsite="connection"), 7)
            self.assertEqual(environment.token_bytes(32, callsite="key"), b"key")
        self.assertEqual(
            sources.mock_calls,
            [
                mock.call.wall(),
                mock.call.wall(),
                mock.call.nanos(),
                mock.call.nanos(),
                mock.call.monotonic(),
                mock.call.randbits(63),
                mock.call.randbelow(100),
                mock.call.token(32),
            ],
        )

    def test_default_constructors_and_standalone_pong_use_system_environment(self):
        server = GameServer()
        match = Match(PRACTICE_RANGE)
        self.assertIsInstance(server.environment, SystemEnvironment)
        self.assertIsInstance(match.environment, SystemEnvironment)
        with mock.patch.object(environment_module.time, "time_ns", return_value=30) as nanos:
            self.assertEqual(
                pong({"+0x78": 7, "+0x80": [10]}, 20),
                {
                    "+0x78": 7,
                    "+0x80": [10, 20, 30],
                },
            )
        nanos.assert_called_once_with()

    def test_create_join_and_client_constructor_share_the_explicit_environment(self):
        tape = ReadTape(
            *initial_reads(),
            Read("token_bytes", "server.handoff.key_in", b"c" * 32, 32),
            Read("token_bytes", "server.handoff.key_out", b"d" * 32, 32),
            Read("wall_time", "server.handoff.deadline", 102.0),
            Read("wall_time", "client.heard", 103.0),
        )
        server = GameServer(environment=tape)
        (first,) = server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        match = server.matches[0]
        (second,) = server.join_match(match, [(2, "Beta", 0, 1, False)])
        self.assertIs(server.environment, tape)
        self.assertIs(match.environment, tape)
        self.assertEqual((match.id, match.started), ((11, 22), 100.0))
        self.assertEqual((first.conn, second.conn), (32, 33))
        self.assertEqual((first.key_in, first.key_out), (b"a" * 32, b"b" * 32))
        self.assertEqual((second.key_in, second.key_out), (b"c" * 32, b"d" * 32))
        self.assertEqual(server.handoffs[first.conn][2], 101.0 + HANDOFF_SECONDS)
        self.assertEqual(server.handoffs[second.conn][2], 102.0 + HANDOFF_SECONDS)
        server.sock = MemorySocket()
        peer = LinkCipher(first.key_out, first.key_in)
        server._datagram(peer.seal(Packet(first.conn, flags=SYN)), ("127.0.0.1", 4567), 110.0)
        client = server.clients[first.conn]
        self.assertIs(client.server.environment, tape)
        self.assertEqual(client.heard, 110.0)  # received(now) follows the independent constructor read.
        self.assertEqual(peer.open(server.sock.sent[0][0]).flags, SYN | ACK)
        tape.assert_exhausted()

    def test_direct_match_preserves_a_falsey_explicit_environment(self):
        tape = ReadTape(
            Read("randbits", "match.id.low", 71, 63),
            Read("randbits", "match.id.high", 72, 63),
            Read("wall_time", "match.started", 73.0),
        )
        match = Match(PRACTICE_RANGE, environment=tape)
        self.assertIs(match.environment, tape)
        self.assertEqual((match.id, match.started), ((71, 72), 73.0))
        tape.assert_exhausted()

    def test_ping_keeps_its_two_independent_ns_reads_and_the_wire_values(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("wall_time", "client.heard", 10.0),
            Read("wall_time_ns", "client.ping.received_ns", 123),
            Read("wall_time_ns", "match.pong.sent_ns", 456),
        )
        server = GameServer(environment=tape)
        client = Client(
            server, ("127.0.0.1", 4567), 1, LinkCipher(bytes(32), bytes(32)), SimpleNamespace(name="Alpha")
        )
        client._message(PING, {"+0x78": 9, "+0x80": [11, 22]}, 999.0)
        channel = BitWriter()
        messages.write_channel(channel, client.reliable)
        reliable, unreliable = messages.read_channel(BitReader(channel.getvalue()))
        self.assertEqual(unreliable, [])
        self.assertEqual(reliable, [(0, 20306, {"+0x78": 9, "+0x80": [11, 22, 123, 456]})])
        tape.assert_exhausted()

    def test_send_home_and_take_out_keep_each_existing_wall_read(self):
        tape = ReadTape(
            *initial_reads(),
            Read("wall_time", "client.heard", 102.0),
            Read("wall_time", "server.send_home", 200.0),
            Read("wall_time", "server.send_home", 201.0),
            Read("wall_time", "server.take_out", 202.0),
        )
        server = GameServer(environment=tape)
        (handoff,) = server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        player = server.matches[0].players[0]
        client = Client(
            server, ("127.0.0.1", 4567), handoff.conn, LinkCipher(handoff.key_in, handoff.key_out), player
        )
        player.client = client
        server.clients[handoff.conn] = client
        self.assertEqual(server.send_home(999), 0)  # No matching client means no clock read.
        self.assertEqual(server.send_home(1), 1)
        self.assertEqual(server.send_home(1), 1)
        self.assertEqual(server.take_out(player, "test"), "sent to the menu")
        self.assertEqual(client.leaving_since, 200.0)
        self.assertEqual(len(client.reliable), 1)
        tape.assert_exhausted()

    def test_run_reads_datagram_time_before_due_and_keeps_both_monotonic_reads(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("monotonic", "server.run.start", 10.0),
            Read("monotonic", "server.run.timeout", 10.0),
            Read("wall_time", "server.run.datagram", 1000.0),
            Read("monotonic", "server.run.due", 10.0),
            Read("monotonic", "server.run.next_tick", 10.1),
            Read("wall_time", "server.run.tick", 1000.1),
        )
        server = GameServer(environment=tape)
        server.sock = OneReceiveSocket(server, b"invalid")
        server.running = True
        server._run()
        self.assertGreater(server.tick, FIRST_TICK + 1)  # The original skipped-tick path still ran.
        self.assertEqual(server.sock.timeouts, [0.0001])
        self.assertTrue(server.sock.closed)
        self.assertEqual(next(iter(server._dropped.values())), [0, 1000.0])
        tape.assert_exhausted()

    def test_run_cannot_accept_an_expired_handoff_before_its_due_tick(self):
        deadline = 101.0 + HANDOFF_SECONDS
        for received, cancelled, accepted in (
            (deadline - 0.001, False, True),
            (deadline, False, False),
            (102.0, True, False),
        ):
            with self.subTest(received=received, cancelled=cancelled):
                client_reads = (Read("wall_time", "client.heard", received),) if accepted else ()
                tape = ReadTape(
                    *initial_reads(),
                    Read("monotonic", "server.run.start", 10.0),
                    Read("monotonic", "server.run.timeout", 10.0),
                    Read("wall_time", "server.run.datagram", received),
                    *client_reads,
                    Read("monotonic", "server.run.due", 10.0),
                    Read("monotonic", "server.run.next_tick", 10.0),
                    Read("wall_time", "server.run.tick", max(deadline, received)),
                )
                left = []
                server = GameServer(environment=tape, on_leave=left.append)
                (handoff,) = server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
                _, player, _ = server.handoffs[handoff.conn]
                if cancelled:
                    self.assertTrue(server.expire_handoff(handoff.conn))
                peer = LinkCipher(handoff.key_out, handoff.key_in)
                server.sock = OneReceiveSocket(server, peer.seal(Packet(handoff.conn, flags=SYN)))
                server.running = True
                server._run()

                self.assertNotIn(handoff.conn, server.handoffs)
                self.assertEqual(handoff.conn in server.clients, accepted)
                self.assertEqual(player.gone, not accepted)
                self.assertEqual(left, [] if accepted else [player])
                flags = [peer.open(data).flags for data, _ in server.sock.sent]
                self.assertEqual(flags, [SYN | ACK] if accepted else [])
                tape.assert_exhausted()

    def test_receive_timeout_does_not_add_a_datagram_wall_read(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("monotonic", "server.run.start", 20.0),
            Read("monotonic", "server.run.timeout", 20.0),
            Read("monotonic", "server.run.due", 20.0),
            Read("monotonic", "server.run.next_tick", 20.0),
            Read("wall_time", "server.run.tick", 1000.0),
        )
        server = GameServer(environment=tape)
        server.sock = OneReceiveSocket(server, None)
        server.running = True
        server._run()
        self.assertEqual(server.tick, FIRST_TICK + 1)
        tape.assert_exhausted()

    def test_early_receive_keeps_the_tick_clock_reads_unconsumed(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("monotonic", "server.run.start", 30.0),
            Read("monotonic", "server.run.timeout", 29.0),
            Read("wall_time", "server.run.datagram", 1000.0),
            Read("monotonic", "server.run.due", 29.5),
        )
        server = GameServer(environment=tape)
        server.sock = OneReceiveSocket(server, b"invalid")
        server.running = True
        server._run()
        self.assertEqual(server.tick, FIRST_TICK)
        self.assertEqual(server.sock.timeouts, [1.0])
        tape.assert_exhausted()

    def test_entropy_failure_propagates_without_reading_later_values(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("randbits", "match.id.low", 1, 63),
            Read("randbits", "match.id.high", RuntimeError("entropy unavailable"), 63),
            Read("wall_time", "match.started", 100.0),
        )
        server = GameServer(environment=tape)
        with self.assertRaisesRegex(RuntimeError, "entropy unavailable"):
            server.create_match(PRACTICE_RANGE, [])
        self.assertEqual(len(tape.consumed), 3)
        self.assertEqual(len(tape.reads), 1)
        self.assertEqual(server.matches, [])

    def test_ping_clock_failure_does_not_queue_a_partial_pong(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("wall_time", "client.heard", 10.0),
            Read("wall_time_ns", "client.ping.received_ns", 123),
            Read("wall_time_ns", "match.pong.sent_ns", RuntimeError("clock unavailable")),
        )
        server = GameServer(environment=tape)
        client = Client(
            server, ("127.0.0.1", 4567), 1, LinkCipher(bytes(32), bytes(32)), SimpleNamespace(name="Alpha")
        )
        with self.assertRaisesRegex(RuntimeError, "clock unavailable"):
            client._message(PING, {}, 999.0)
        self.assertEqual(client.reliable, [])
        self.assertEqual(len(tape.reads), 0)
        with self.assertRaisesRegex(TapeError, "external read failed: clock unavailable"):
            tape.assert_exhausted()

    def test_run_keeps_original_exception_handling_but_a_failed_tape_cannot_pass(self):
        tape = ReadTape(
            Read("randbelow", "server.next_conn", 0, 0x100000),
            Read("monotonic", "server.run.start", 20.0),
            Read("monotonic", "server.run.timeout", 20.0),
            Read("monotonic", "server.run.due", 20.0),
            Read("monotonic", "server.run.next_tick", 20.0),
            Read("wall_time", "server.run.tick", RuntimeError("clock unavailable")),
        )
        server = GameServer(environment=tape)
        server.sock = OneReceiveSocket(server, None)
        server.running = True
        with self.assertLogs("ow174.game", level="ERROR") as logged:
            server._run()
        self.assertEqual(server.tick, FIRST_TICK)
        self.assertTrue(server.sock.closed)
        self.assertIn("clock unavailable", "\n".join(logged.output))
        self.assertEqual(len(tape.reads), 0)
        with self.assertRaisesRegex(TapeError, "external read failed: clock unavailable"):
            tape.assert_exhausted()

    def test_missing_or_misordered_input_is_explicit_and_never_falls_back(self):
        tape = ReadTape(Read("randbits", "match.id.low", 1, 63))
        with self.assertRaisesRegex(TapeError, "external read order"):
            GameServer(environment=tape)
        self.assertEqual(len(tape.reads), 1)
        with self.assertRaisesRegex(TapeError, "external read failed"):
            tape.assert_exhausted()
        with self.assertRaisesRegex(TapeError, "unconsumed external reads"):
            ReadTape(Read("wall_time", "match.started", 1.0)).assert_exhausted()
        with self.assertRaisesRegex(TapeError, "unexpected external read"):
            GameServer(environment=ReadTape())


if __name__ == "__main__":
    unittest.main()
