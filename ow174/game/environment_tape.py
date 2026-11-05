"""Explicit, in-memory recording and replay of the six ExecutionEnvironment reads.

This is an external-read baseline, not a network, match-state or combat replay.
Nothing records by default, writes a file, logs return values or supplies system
fallbacks. A finished tape may contain handoff keys: keep it private in memory.
"""

import math
import struct
from dataclasses import dataclass
from threading import RLock
from typing import cast

from ow174.game.environment import ExecutionEnvironment

SCHEMA = "ow174.environment-read-tape.v1"
BUILD = "1.74.0.0.104319"
SCOPE = "environment-reads-only"
_OPERATIONS = frozenset(("wall_time", "wall_time_ns", "monotonic", "randbits", "randbelow", "token_bytes"))
_CLOCKS = frozenset(("wall_time", "wall_time_ns", "monotonic"))
_FLOATS = frozenset(("wall_time", "monotonic"))
ReadValue = float | int | bytes


class EnvironmentTapeError(RuntimeError):
    """The first malformed, failed, missing or misordered external read is fatal."""


def _keys(value, expected: set[str], where: str) -> None:
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != expected:
        raise EnvironmentTapeError(f"invalid {where} fields")


def _validate_call(operation: str, callsite: str, argument: int | None) -> None:
    if type(operation) is not str or operation not in _OPERATIONS:
        raise EnvironmentTapeError("unknown external read operation")
    if type(callsite) is not str or not callsite:
        raise EnvironmentTapeError("external read callsite must be a nonempty string")
    if operation in _CLOCKS:
        if argument is not None:
            raise EnvironmentTapeError("clock read must have no argument")
    elif type(argument) is not int:
        raise EnvironmentTapeError("entropy argument must be an integer")
    elif argument < (1 if operation == "randbelow" else 0):
        raise EnvironmentTapeError("entropy argument is out of range")


def _validate_result(operation: str, argument: int | None, value: ReadValue) -> None:
    if operation in _FLOATS:
        if type(value) is not float or not math.isfinite(value):
            raise EnvironmentTapeError("clock result must be a finite float")
    elif operation == "token_bytes":
        if type(value) is not bytes or len(value) != argument:
            raise EnvironmentTapeError("token result must be bytes of the requested length")
    elif type(value) is not int:
        raise EnvironmentTapeError("external read result must be an integer")
    elif operation == "randbits" and (value < 0 or value.bit_length() > argument):
        raise EnvironmentTapeError("randbits result is out of range")
    elif operation == "randbelow" and not 0 <= value < argument:
        raise EnvironmentTapeError("randbelow result is out of range")


def _encode(value: ReadValue) -> dict[str, str]:
    if type(value) is float:
        return {"type": "float", "value": struct.pack(">d", value).hex()}
    if type(value) is int:
        return {"type": "int", "value": hex(value)}
    return {"type": "bytes", "value": value.hex()}


def _decode(value) -> ReadValue:
    _keys(value, {"type", "value"}, "typed result")
    tag, text = value["type"], value["value"]
    if type(tag) is not str or type(text) is not str:
        raise EnvironmentTapeError("invalid typed result encoding")
    try:
        if tag == "float" and len(text) == 16:
            result = struct.unpack(">d", bytes.fromhex(text))[0]
            if not math.isfinite(result):
                raise ValueError
        elif tag == "int":
            result = int(text, 16)
        elif tag == "bytes":
            result = bytes.fromhex(text)
        else:
            raise ValueError
        if _encode(result) != value:
            raise ValueError
        return result
    except (ValueError, struct.error):
        raise EnvironmentTapeError("invalid or noncanonical typed result encoding") from None


@dataclass(frozen=True)
class _Read:
    seq: int
    operation: str
    callsite: str
    argument: int | None
    result: ReadValue

    def exported(self) -> dict:
        return {
            "seq": self.seq, "operation": self.operation, "callsite": self.callsite,
            "argument": self.argument, "result": _encode(self.result),
        }


def _parse(tape) -> tuple[_Read, ...]:
    _keys(tape, {"schema", "build", "scope", "reads", "end"}, "tape")
    for key, expected in (("schema", SCHEMA), ("build", BUILD), ("scope", SCOPE)):
        if type(tape[key]) is not str or tape[key] != expected:
            raise EnvironmentTapeError(f"unsupported tape {key}")
    rows = tape["reads"]
    if type(rows) is not list:
        raise EnvironmentTapeError("tape reads must be a list")
    _keys(tape["end"], {"seq", "status"}, "end marker")
    end = tape["end"]
    if type(end["seq"]) is not int or end["seq"] != len(rows) or end["status"] != "complete":
        raise EnvironmentTapeError("invalid or incomplete tape end marker")
    if type(end["status"]) is not str:
        raise EnvironmentTapeError("invalid tape end status")
    reads = []
    for seq, row in enumerate(rows):
        _keys(row, {"seq", "operation", "callsite", "argument", "result"}, f"read {seq}")
        if type(row["seq"]) is not int or row["seq"] != seq:
            raise EnvironmentTapeError(f"invalid sequence at read {seq}")
        operation, callsite, argument = row["operation"], row["callsite"], row["argument"]
        _validate_call(operation, callsite, argument)
        result = _decode(row["result"])
        _validate_result(operation, argument, result)
        reads.append(_Read(seq, operation, callsite, argument, result))
    return tuple(reads)


class _EnvironmentReads:
    """Shared interface; one lock serializes each complete read and its tape mutation."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._fault: EnvironmentTapeError | None = None

    @property
    def fault(self) -> EnvironmentTapeError | None:
        with self._lock:
            return self._fault

    def _healthy(self) -> None:
        if self._fault is not None:
            raise self._fault from None

    def _fail(self, message: str) -> None:
        if self._fault is None:
            self._fault = EnvironmentTapeError(message)
        raise self._fault from None

    def assert_healthy(self) -> None:
        with self._lock:
            self._healthy()

    def _read(self, operation: str, callsite: str, argument: int | None = None) -> ReadValue:
        raise NotImplementedError

    def wall_time(self, *, callsite: str) -> float:
        return cast(float, self._read("wall_time", callsite))

    def wall_time_ns(self, *, callsite: str) -> int:
        return cast(int, self._read("wall_time_ns", callsite))

    def monotonic(self, *, callsite: str) -> float:
        return cast(float, self._read("monotonic", callsite))

    def randbits(self, bits: int, *, callsite: str) -> int:
        return cast(int, self._read("randbits", callsite, bits))

    def randbelow(self, upper: int, *, callsite: str) -> int:
        return cast(int, self._read("randbelow", callsite, upper))

    def token_bytes(self, length: int, *, callsite: str) -> bytes:
        return cast(bytes, self._read("token_bytes", callsite, length))


class RecordingEnvironment(_EnvironmentReads):
    """Record an explicitly supplied source; finish() returns a detached private tape.

    A source exception or invalid call/result makes the recorder permanently faulty.
    Reentrant source reads fail instead of creating an ambiguous sequence. No new
    read is accepted after finish(), even if a caller catches the resulting error.
    """

    def __init__(self, source: ExecutionEnvironment) -> None:
        super().__init__()
        if source is None:
            raise TypeError("an explicit environment source is required")
        self._source = source
        self._reads: list[_Read] = []
        self._finished = False
        self._reading = False

    @property
    def read_count(self) -> int:
        with self._lock:
            return len(self._reads)

    def _read(self, operation: str, callsite: str, argument: int | None = None) -> ReadValue:
        with self._lock:
            self._healthy()
            if self._finished:
                self._fail("external read after recording end")
            if self._reading:
                self._fail("reentrant external environment read")
            try:
                _validate_call(operation, callsite, argument)
            except EnvironmentTapeError as error:
                self._fail(str(error))
            self._reading = True
            try:
                try:
                    source = getattr(self._source, operation)
                    value = (
                        source(callsite=callsite) if argument is None else source(argument, callsite=callsite)
                    )
                except BaseException as error:
                    # The source's message can contain private data; never copy it to our diagnostic.
                    if self._fault is None:
                        self._fault = EnvironmentTapeError(
                            f"external source failed at read {len(self._reads)}"
                        )
                    if not isinstance(error, Exception):
                        raise
                    self._healthy()
                # A source might itself swallow an error from a reentrant call.
                self._healthy()
                try:
                    _validate_result(operation, argument, value)
                except EnvironmentTapeError as error:
                    self._fail(str(error))
                self._reads.append(_Read(len(self._reads), operation, callsite, argument, value))
                return value
            finally:
                self._reading = False

    def finish(self) -> dict:
        """End recording explicitly; repeated exports return independent data."""
        with self._lock:
            self._healthy()
            if self._reading:
                self._fail("recording cannot end during an external read")
            self._finished = True
            return {
                "schema": SCHEMA, "build": BUILD, "scope": SCOPE,
                "reads": [read.exported() for read in self._reads],
                "end": {"seq": len(self._reads), "status": "complete"},
            }

    def assert_complete(self) -> None:
        """A caught source/read error cannot become a successful finished recording."""
        with self._lock:
            self._healthy()
            if not self._finished:
                self._fail("recording has no end marker; call finish first")


class ReplayEnvironment(_EnvironmentReads):
    """Strictly consume a validated private tape, with no system fallback.

    Constructor validation copies rows into immutable internal values. Failed
    reads never advance position. Every later read and completion assertion raises
    the first fault, including when production code caught the original exception.
    """

    def __init__(self, tape) -> None:
        super().__init__()
        self._reads = _parse(tape)
        self._position = 0

    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._reads) - self._position

    def _read(self, operation: str, callsite: str, argument: int | None = None) -> ReadValue:
        with self._lock:
            self._healthy()
            try:
                _validate_call(operation, callsite, argument)
            except EnvironmentTapeError as error:
                self._fail(str(error))
            if self._position == len(self._reads):
                self._fail(f"unexpected external read after tape end at sequence {self._position}")
            read = self._reads[self._position]
            if (operation, callsite, argument) != (read.operation, read.callsite, read.argument):
                self._fail(f"external read does not match tape at sequence {self._position}")
            self._position += 1
            return read.result

    def assert_complete(self) -> None:
        """Require a healthy replay and consumption through its validated end marker."""
        with self._lock:
            self._healthy()
            if self._position != len(self._reads):
                self._fail(f"unconsumed external reads at sequence {self._position}")
