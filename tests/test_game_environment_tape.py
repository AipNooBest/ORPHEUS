"""Typed external-read tapes, including failures swallowed by production callers."""

import copy
import math
import struct
import sys
import traceback
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import match as match_module
from ow174.game.collision import StaticCollisionProvider, flat_world
from ow174.game.content import PRACTICE_RANGE
from ow174.game.environment_tape import EnvironmentTapeError, RecordingEnvironment, ReplayEnvironment
from ow174.game.server import FIRST_TICK, GameServer


class Source:
    """Ordered source results, with no clocks, entropy, files or sockets."""

    def __init__(self, *values):
        self.values = deque(values)
        self.calls = []

    def __bool__(self):
        return False

    def take(self, operation, callsite, argument=None):
        self.calls.append((operation, callsite, argument))
        value = self.values.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

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


def invoke(environment, operation, callsite="probe", argument=None):
    method = getattr(environment, operation)
    return method(callsite=callsite) if argument is None else method(argument, callsite=callsite)


def tape(operation="wall_time", result=1.0, argument=None):
    recording = RecordingEnvironment(Source(result))
    invoke(recording, operation, argument=argument)
    return recording.finish()


class OneTimeoutSocket:
    def __init__(self, server):
        self.server = server
        self.closed = False

    def settimeout(self, timeout):
        pass

    def recvfrom(self, maximum):
        self.server.running = False
        raise TimeoutError

    def close(self):
        self.closed = True


class EnvironmentTapeTests(unittest.TestCase):
    def assert_sticky(self, environment, error, followup):
        self.assertIs(environment.fault, error)
        for operation in (environment.assert_healthy, environment.assert_complete, followup):
            with self.assertRaises(EnvironmentTapeError) as caught:
                operation()
            self.assertIs(caught.exception, error)

    def test_six_reads_share_one_sequence_and_preserve_exact_types_and_bits(self):
        values = [
            -0.0, -(1 << 90), b"\x00\xff\x01", math.nextafter(1.0, 2.0),
            (1 << 20000) - 1, 2, float.fromhex("0x0.0000000000001p-1022"), 0, b"",
        ]
        calls = [
            ("wall_time", "clock", None), ("wall_time_ns", "clock", None),
            ("token_bytes", "key", 3), ("monotonic", "clock", None),
            ("randbits", "identity", 20000), ("randbelow", "connection", 3),
            ("wall_time", "clock", None), ("randbits", "zero", 0), ("token_bytes", "empty", 0),
        ]
        source = Source(*values)
        recording = RecordingEnvironment(source)
        for value, (operation, callsite, argument) in zip(values, calls, strict=True):
            returned = invoke(recording, operation, callsite, argument)
            self.assertIs(type(returned), type(value))
            self.assertIs(returned, value)
        self.assertEqual(source.calls, calls)
        self.assertEqual(recording.read_count, len(values))
        recorded = recording.finish()
        recording.assert_complete()
        self.assertEqual(recorded["reads"][0]["result"], {"type": "float", "value": "8000000000000000"})
        self.assertEqual([row["seq"] for row in recorded["reads"]], list(range(len(values))))
        self.assertEqual(recorded["end"], {"seq": len(values), "status": "complete"})
        replay = ReplayEnvironment(recorded)
        for expected, (operation, callsite, argument) in zip(values, calls, strict=True):
            actual = invoke(replay, operation, callsite, argument)
            self.assertIs(type(actual), type(expected))
            if type(expected) is float:
                self.assertEqual(struct.pack(">d", actual), struct.pack(">d", expected))
            else:
                self.assertEqual(actual, expected)
        self.assertEqual((replay.position, replay.remaining), (len(values), 0))
        replay.assert_complete()
        self.assertEqual(source.calls, calls)  # Replay has no access to the recording source.

    def test_export_and_replay_own_their_data(self):
        recording = RecordingEnvironment(Source(b"private"))
        recording.token_bytes(7, callsite="key")
        first = recording.finish()
        second = recording.finish()
        replay = ReplayEnvironment(second)
        first["reads"][0]["result"]["value"] = "00"
        first["end"]["status"] = "broken"
        second["reads"][0]["callsite"] = "changed"
        second["reads"].clear()
        self.assertEqual(replay.token_bytes(7, callsite="key"), b"private")
        replay.assert_complete()
        self.assertEqual(recording.finish()["reads"][0]["result"]["value"], b"private".hex())
        recording.assert_complete()

    def test_empty_tape_is_complete_but_read_after_end_is_sticky(self):
        recording = RecordingEnvironment(Source())
        replay = ReplayEnvironment(recording.finish())
        recording.assert_complete()
        replay.assert_complete()
        for environment in (recording, replay):
            with self.assertRaises(EnvironmentTapeError) as caught:
                environment.wall_time(callsite="extra")
            self.assert_sticky(
                environment, caught.exception, lambda env=environment: env.wall_time(callsite="extra")
            )
        with self.assertRaises(EnvironmentTapeError):
            recording.finish()

    def test_completion_without_end_or_with_unconsumed_reads_is_sticky(self):
        recording = RecordingEnvironment(Source(1.0))
        with self.assertRaisesRegex(EnvironmentTapeError, "end marker") as caught:
            recording.assert_complete()
        self.assert_sticky(recording, caught.exception, recording.finish)
        self.assertEqual(recording.read_count, 0)
        replay = ReplayEnvironment(tape())
        with self.assertRaisesRegex(EnvironmentTapeError, "unconsumed") as caught:
            replay.assert_complete()
        self.assert_sticky(replay, caught.exception, lambda: replay.wall_time(callsite="probe"))
        self.assertEqual((replay.position, replay.remaining), (0, 1))

    def test_format_version_sequence_and_end_are_strict(self):
        original = tape()
        changes = [
            lambda t: t.pop("end"), lambda t: t.update(extra=True),
            lambda t: t.update(schema="ow174.environment-read-tape.v2"),
            lambda t: t.update(build="other"), lambda t: t.update(scope="full-combat"),
            lambda t: t.update(reads=tuple(t["reads"])), lambda t: t["reads"][0].update(extra=True),
            lambda t: t["reads"][0].pop("argument"), lambda t: t["reads"][0].update(seq=True),
            lambda t: t["reads"][0].update(seq=1), lambda t: t["reads"].append(copy.deepcopy(t["reads"][0])),
            lambda t: t["end"].update(seq=True), lambda t: t["end"].update(seq=2),
            lambda t: t["end"].update(status="faulted"), lambda t: t["end"].update(extra=True),
            lambda t: t["reads"][0].update(operation="unknown"),
            lambda t: t["reads"][0].update(argument=0),
            lambda t: t["reads"][0].update(callsite=""),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                damaged = copy.deepcopy(original)
                change(damaged)
                with self.assertRaises(EnvironmentTapeError):
                    ReplayEnvironment(damaged)
        for value in (None, [], (), "tape"):
            with self.subTest(value=value), self.assertRaises(EnvironmentTapeError):
                ReplayEnvironment(value)

    def test_typed_encoding_is_canonical_and_validated_before_any_consumption(self):
        cases = [
            ("wall_time", 1.0, None, {"type": "float", "value": "7ff0000000000000"}),
            ("wall_time", 1.0, None, {"type": "float", "value": "7ff8000000000001"}),
            ("wall_time", 1.0, None, {"type": "float", "value": "3FF0000000000000"}),
            ("wall_time", 1.0, None, {"type": "float", "value": "0000"}),
            ("wall_time", 1.0, None, {"type": "int", "value": "0x1"}),
            ("wall_time_ns", 1, None, {"type": "int", "value": "0X1"}),
            ("wall_time_ns", 1, None, {"type": "int", "value": "0x01"}),
            ("wall_time_ns", 1, None, {"type": "int", "value": "-0x0"}),
            ("wall_time_ns", 1, None, {"type": "int", "value": 1}),
            ("randbits", 1, 1, {"type": "int", "value": "0x2"}),
            ("randbelow", 1, 2, {"type": "int", "value": "-0x1"}),
            ("token_bytes", b"\xab", 1, {"type": "bytes", "value": "AB"}),
            ("token_bytes", b"\xab", 1, {"type": "bytes", "value": "a b"}),
            ("token_bytes", b"\xab", 1, {"type": "bytes", "value": "ab00"}),
            ("token_bytes", b"\xab", 1, {"type": "str", "value": "ab"}),
            ("token_bytes", b"\xab", 1, {"type": "bytes", "value": "ab", "extra": True}),
        ]
        for operation, result, argument, invalid in cases:
            with self.subTest(operation=operation, invalid=invalid):
                recorded = tape(operation, result, argument)
                recorded["reads"][0]["result"] = invalid
                with self.assertRaises(EnvironmentTapeError):
                    ReplayEnvironment(recorded)

    def test_invalid_call_arguments_never_reach_source_and_poison_the_wrapper(self):
        calls = [
            ("randbits", "probe", -1), ("randbits", "probe", True), ("randbits", "probe", 1.5),
            ("randbelow", "probe", 0), ("randbelow", "probe", False),
            ("token_bytes", "probe", -1), ("token_bytes", "probe", True),
            ("wall_time", "", None), ("wall_time", None, None), ("wall_time", True, None),
        ]
        for operation, callsite, argument in calls:
            with self.subTest(operation=operation, callsite=callsite, argument=argument):
                source = Source(1.0)
                recording = RecordingEnvironment(source)
                with self.assertRaises(EnvironmentTapeError) as caught:
                    invoke(recording, operation, callsite, argument)
                self.assert_sticky(recording, caught.exception, recording.finish)
                self.assertEqual((source.calls, recording.read_count), ([], 0))
                replay = ReplayEnvironment(tape())
                with self.assertRaises(EnvironmentTapeError) as caught:
                    invoke(replay, operation, callsite, argument)
                self.assert_sticky(
                    replay, caught.exception, lambda env=replay: env.wall_time(callsite="probe")
                )
                self.assertEqual(replay.position, 0)

    def test_invalid_source_results_are_fatal_without_exporting_them(self):
        cases = [
            ("wall_time", 1, None), ("wall_time", True, None),
            ("wall_time", math.nan, None), ("wall_time", math.inf, None),
            ("monotonic", -math.inf, None), ("wall_time_ns", 1.0, None),
            ("wall_time_ns", True, None), ("randbits", -1, 3), ("randbits", 8, 3),
            ("randbits", 1, 0), ("randbits", True, 1), ("randbelow", 3, 3),
            ("randbelow", -1, 3), ("token_bytes", bytearray(b"a"), 1),
            ("token_bytes", "a", 1), ("token_bytes", b"private-secret", 1),
        ]
        for operation, result, argument in cases:
            with self.subTest(operation=operation, result=result, argument=argument):
                source = Source(result, 1.0)
                recording = RecordingEnvironment(source)
                with self.assertRaises(EnvironmentTapeError) as caught:
                    invoke(recording, operation, argument=argument)
                self.assert_sticky(
                    recording, caught.exception, lambda env=recording: env.wall_time(callsite="later")
                )
                with self.assertRaises(EnvironmentTapeError):
                    recording.finish()
                self.assertEqual((recording.read_count, len(source.calls)), (0, 1))
                self.assertNotIn("private-secret", str(caught.exception))

    def test_source_failure_cannot_be_swallowed_or_expose_the_source_message(self):
        source = Source(1.0, RuntimeError("PRIVATE_HANDOFF_KEY"), 2.0)
        recording = RecordingEnvironment(source)
        recording.wall_time(callsite="first")
        with self.assertRaises(EnvironmentTapeError) as caught:
            recording.token_bytes(32, callsite="key")
        self.assert_sticky(recording, caught.exception, lambda: recording.wall_time(callsite="later"))
        with self.assertRaises(EnvironmentTapeError):
            recording.finish()
        self.assertEqual((recording.read_count, len(source.calls)), (1, 2))
        diagnostic = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("PRIVATE_HANDOFF_KEY", diagnostic)

    def test_misordered_operation_callsite_or_argument_cannot_be_retried(self):
        for operation, callsite, argument in (
            ("randbelow", "probe", 64), ("randbits", "other", 63), ("randbits", "probe", 64),
        ):
            with self.subTest(operation=operation, callsite=callsite, argument=argument):
                replay = ReplayEnvironment(tape("randbits", 1, 63))
                with self.assertRaises(EnvironmentTapeError) as caught:
                    invoke(replay, operation, callsite, argument)
                self.assert_sticky(
                    replay, caught.exception, lambda env=replay: env.randbits(63, callsite="probe")
                )
                self.assertEqual((replay.position, replay.remaining), (0, 1))

    def test_source_cannot_swallow_a_reentrant_read_or_finish_failure(self):
        for nested in ("read", "finish"):
            with self.subTest(nested=nested):
                class Reentrant:
                    action = nested
                    recording = None

                    def wall_time(self, *, callsite):
                        try:
                            if self.action == "read":
                                self.recording.monotonic(callsite="nested")
                            else:
                                self.recording.finish()
                        except EnvironmentTapeError:
                            pass
                        return 1.0

                source = Reentrant()
                recording = RecordingEnvironment(source)
                source.recording = recording
                with self.assertRaises(EnvironmentTapeError) as caught:
                    recording.wall_time(callsite="outer")
                self.assert_sticky(recording, caught.exception, recording.finish)
                self.assertEqual(recording.read_count, 0)

    def test_interrupt_marks_recording_faulty_even_if_a_caller_catches_it(self):
        recording = RecordingEnvironment(Source(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            recording.wall_time(callsite="interrupted")
        self.assertIsInstance(recording.fault, EnvironmentTapeError)
        with self.assertRaises(EnvironmentTapeError):
            recording.finish()

    def test_explicit_wrapper_replays_real_match_and_handoff_construction(self):
        source = Source(31, 11, 22, 100.0, b"a" * 32, b"b" * 32, 101.0)
        recording = RecordingEnvironment(source)
        with mock.patch.object(
            match_module, "default_collision_provider", return_value=StaticCollisionProvider(flat_world(0.0))
        ):
            first = GameServer(environment=recording)
            (handoff,) = first.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
            replay = ReplayEnvironment(recording.finish())
            second = GameServer(environment=replay)
            (copied,) = second.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        self.assertEqual((copied.conn, copied.key_in, copied.key_out),
                         (handoff.conn, handoff.key_in, handoff.key_out))
        self.assertEqual((second.matches[0].id, second.matches[0].started), ((11, 22), 100.0))
        self.assertEqual(second.handoffs[copied.conn][2], first.handoffs[handoff.conn][2])
        recording.assert_complete()
        replay.assert_complete()
        self.assertEqual(len(source.calls), 7)

    def test_server_catching_a_source_error_does_not_make_recording_successful(self):
        source = Source(0, 20.0, 20.0, 20.0, 20.0, RuntimeError("PRIVATE_HANDOFF_KEY"))
        recording = RecordingEnvironment(source)
        server = GameServer(environment=recording)
        server.sock = OneTimeoutSocket(server)
        server.running = True
        with self.assertLogs("ow174.game", level="ERROR") as logged:
            server._run()
        self.assertTrue(server.sock.closed)
        self.assertEqual(server.tick, FIRST_TICK)
        self.assertNotIn("PRIVATE_HANDOFF_KEY", "\n".join(logged.output))
        self.assertEqual((recording.read_count, len(source.calls)), (5, 6))
        with self.assertRaises(EnvironmentTapeError):
            recording.finish()
        with self.assertRaises(EnvironmentTapeError):
            recording.assert_complete()

    def test_server_catching_an_extra_read_cannot_pass_even_when_replay_is_exhausted(self):
        recording = RecordingEnvironment(Source(0, 20.0, 20.0, 20.0, 20.0))
        recording.randbelow(0x100000, callsite="server.next_conn")
        for callsite in ("server.run.start", "server.run.timeout", "server.run.due", "server.run.next_tick"):
            recording.monotonic(callsite=callsite)
        replay = ReplayEnvironment(recording.finish())
        server = GameServer(environment=replay)
        server.sock = OneTimeoutSocket(server)
        server.running = True
        with self.assertLogs("ow174.game", level="ERROR"):
            server._run()
        self.assertEqual((replay.position, replay.remaining), (5, 0))
        self.assertTrue(server.sock.closed)
        with self.assertRaisesRegex(EnvironmentTapeError, "after tape end"):
            replay.assert_complete()


if __name__ == "__main__":
    unittest.main()
