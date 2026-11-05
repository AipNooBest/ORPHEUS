"""Explicit in-memory recording/replay of CollisionProvider.poll, with exact World content checks.

No default recording, file output, real asset loading or replacement geometry is supplied here.
This boundary is independent of environment reads and of complete match/transport state replay.
"""

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from ow174.game import collision, props
from ow174.game.collision import CollisionProvider, LoadState, World
from ow174.game.collision_fingerprint import SCHEMA as WORLD_SCHEMA
from ow174.game.collision_fingerprint import WorldRegistry, world_fingerprint

SCHEMA_V1 = "ow174.collision-read-tape.v1"
SCHEMA = "ow174.collision-read-tape.v2"
BUILD = "1.74.0.0.104319"
SCOPE = "collision-provider-reads-only"


class CollisionTapeError(RuntimeError):
    """The first invalid, failed, missing or misordered collision read is permanent."""


def _keys(value, expected: set[str], where: str) -> None:
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != expected:
        raise CollisionTapeError(f"invalid {where} fields")


def _guid(value) -> bool:
    return type(value) is int and 0 <= value < 1 << 64


@dataclass(frozen=True)
class _Key:
    map_guid: int
    name: str
    mode: int | None
    identifiers: tuple[int, ...]

    def exported(self) -> dict:
        return {"map_guid": self.map_guid, "name": self.name, "mode": self.mode,
                "mode_identifiers": list(self.identifiers)}


def _parse_key(value) -> _Key:
    _keys(value, {"map_guid", "name", "mode", "mode_identifiers"}, "poll key")
    if not _guid(value["map_guid"]) or type(value["name"]) is not str:
        raise CollisionTapeError("invalid map or name in collision key")
    if value["mode"] is not None and not _guid(value["mode"]):
        raise CollisionTapeError("invalid mode in collision key")
    ids = value["mode_identifiers"]
    if type(ids) is not list or any(not _guid(item) for item in ids) or ids != sorted(set(ids)):
        raise CollisionTapeError("collision identifiers must be sorted unique GUIDs")
    return _Key(value["map_guid"], value["name"], value["mode"], tuple(ids))


def _key(map_guid, name, mode) -> _Key:
    if not _guid(map_guid) or type(name) is not str or (mode is not None and not _guid(mode)):
        raise CollisionTapeError("invalid collision poll arguments")
    identifiers = props.mode_identifiers(mode)
    if type(identifiers) is not frozenset or any(not _guid(item) for item in identifiers):
        raise CollisionTapeError("invalid collision mode identifiers")
    return _Key(map_guid, name, mode, tuple(sorted(identifiers)))


@dataclass(frozen=True)
class _Asset:
    schema: str
    sha256: str
    model_sha256: str
    cache_version: int

    def exported(self) -> dict:
        return {"schema": self.schema, "sha256": self.sha256,
                "model_sha256": self.model_sha256, "cache_version": self.cache_version}


def _asset(value) -> _Asset:
    _keys(value, {"schema", "sha256", "model_sha256", "cache_version"}, "World fingerprint")
    if type(value["schema"]) is not str or value["schema"] != WORLD_SCHEMA:
        raise CollisionTapeError("unsupported World fingerprint schema")
    if type(value["cache_version"]) is not int or value["cache_version"] != collision.VERSION:
        raise CollisionTapeError("unsupported World fingerprint cache version")
    for field in ("sha256", "model_sha256"):
        text = value[field]
        if type(text) is not str or len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
            raise CollisionTapeError("invalid World fingerprint digest")
    return _Asset(value["schema"], value["sha256"], value["model_sha256"], value["cache_version"])


@dataclass(frozen=True)
class _Result:
    status: str
    error: str
    asset: _Asset | None

    def exported(self) -> dict:
        return {"status": self.status, "error": self.error,
                "asset": self.asset.exported() if self.asset is not None else None}


@dataclass(frozen=True)
class _Read:
    seq: int
    key: _Key
    result: _Result
    state_ref: int | None

    def exported(self) -> dict:
        return {"seq": self.seq, "operation": "poll", "key": self.key.exported(),
                "result": self.result.exported(), "state_ref": self.state_ref}


def _result(value) -> _Result:
    _keys(value, {"status", "error", "asset"}, "poll result")
    status, error, asset = value["status"], value["error"], value["asset"]
    if type(status) is not str or status not in ("pending", "ready", "unavailable"):
        raise CollisionTapeError("invalid collision result state")
    if type(error) is not str or (status == "unavailable") != bool(error):
        raise CollisionTapeError("only unavailable collision results need an error")
    if (status == "ready") != (asset is not None):
        raise CollisionTapeError("only ready collision results must name a World asset")
    return _Result(status, error, _asset(asset) if asset is not None else None)


def _parse(tape) -> tuple[_Read, ...]:
    _keys(tape, {"schema", "build", "scope", "reads", "end"}, "collision tape")
    schema = tape["schema"]
    if type(schema) is not str or schema not in (SCHEMA_V1, SCHEMA):
        raise CollisionTapeError("unsupported collision tape schema")
    for field, expected in (("build", BUILD), ("scope", SCOPE)):
        if type(tape[field]) is not str or tape[field] != expected:
            raise CollisionTapeError(f"unsupported collision tape {field}")
    rows = tape["reads"]
    if type(rows) is not list:
        raise CollisionTapeError("collision tape reads must be a list")
    _keys(tape["end"], {"seq", "status"}, "collision tape end")
    end = tape["end"]
    if type(end["seq"]) is not int or end["seq"] != len(rows):
        raise CollisionTapeError("invalid collision tape end sequence")
    if type(end["status"]) is not str or end["status"] != "complete":
        raise CollisionTapeError("collision tape has no complete end marker")
    reads = []
    fields = {"seq", "operation", "key", "result"}
    if schema == SCHEMA:
        fields.add("state_ref")
    first_results: list[_Result] = []
    for seq, row in enumerate(rows):
        _keys(row, fields, f"collision read {seq}")
        if type(row["seq"]) is not int or row["seq"] != seq:
            raise CollisionTapeError(f"invalid collision read sequence at {seq}")
        if type(row["operation"]) is not str or row["operation"] != "poll":
            raise CollisionTapeError("unsupported collision operation")
        key = _parse_key(row["key"])
        result = _result(row["result"])
        state_ref = None
        if schema == SCHEMA:
            state_ref = row["state_ref"]
            if type(state_ref) is not int or not 0 <= state_ref <= len(first_results):
                raise CollisionTapeError(f"invalid collision state reference at sequence {seq}")
            if state_ref == len(first_results):
                first_results.append(result)
            elif first_results[state_ref] != result:
                raise CollisionTapeError(f"collision state reference changed result at sequence {seq}")
        reads.append(_Read(seq, key, result, state_ref))
    return tuple(reads)


class _CollisionReads:
    def __init__(self) -> None:
        self._lock = RLock()
        self._fault: CollisionTapeError | None = None
        self._reading = False

    @property
    def fault(self) -> CollisionTapeError | None:
        with self._lock:
            return self._fault

    def _healthy(self) -> None:
        if self._fault is not None:
            raise self._fault from None

    def _fail(self, message: str) -> None:
        if self._fault is None:
            self._fault = CollisionTapeError(message)
        raise self._fault from None

    def assert_healthy(self) -> None:
        with self._lock:
            self._healthy()

    def _interrupted(self, error: BaseException, phase: str) -> None:
        if self._fault is None:
            self._fault = CollisionTapeError(f"collision {phase} failed")
        if not isinstance(error, Exception):
            raise error
        self._healthy()


class RecordingCollisionProvider(_CollisionReads):
    """Record an explicit source into memory; the registry retains the exact observed Worlds.

    One instance can be shared by multiple matches. Its single sequence follows actual poll order.
    Fingerprinting is intentionally strict and may be expensive; this is an opt-in offline boundary.
    """

    def __init__(self, source: CollisionProvider, registry: WorldRegistry) -> None:
        super().__init__()
        if source is None or registry is None:
            raise TypeError("an explicit collision source and World registry are required")
        self._source, self._registry = source, registry
        self._reads: list[_Read] = []
        # Retain the observed objects so their identities cannot be reused during recording.
        self._states: dict[int, tuple[LoadState, int, _Result]] = {}
        self._finished = False

    @property
    def read_count(self) -> int:
        with self._lock:
            return len(self._reads)

    def poll(self, map_guid: int, name: str = "", mode: int | None = None) -> LoadState:
        with self._lock:
            self._healthy()
            if self._finished:
                self._fail("collision poll after recording end")
            if self._reading:
                self._fail("reentrant collision poll")
            self._reading = True
            try:
                try:
                    key = _key(map_guid, name, mode)
                except BaseException as error:  # noqa: BLE001 - retain interruptions as a permanent fault
                    self._interrupted(error, "key read")
                try:
                    state = self._source.poll(map_guid, name, mode)
                except BaseException as error:  # noqa: BLE001 - retain interruptions as a permanent fault
                    self._interrupted(error, "source")
                self._healthy()
                try:
                    if type(state) is not LoadState:
                        raise CollisionTapeError("collision source did not return a LoadState")
                    _keys(vars(state), {"status", "world", "error"}, "source LoadState")
                    asset = self._registry.register(state.world) if state.status == "ready" else None
                    result = _result({"status": state.status, "error": state.error, "asset": asset})
                    if result.status != "ready" and state.world is not None:
                        raise CollisionTapeError("non-ready collision state contained a World")
                    seen = self._states.get(id(state))
                    if seen is not None and seen[2] != result:
                        raise CollisionTapeError("previously observed LoadState changed result")
                    if _key(map_guid, name, mode) != key:
                        raise CollisionTapeError("collision identifiers changed during poll")
                except BaseException as error:  # noqa: BLE001 - retain interruptions as a permanent fault
                    self._interrupted(error, "result validation")
                self._healthy()
                if seen is None:
                    state_ref = len(self._states)
                    self._states[id(state)] = (state, state_ref, result)
                else:
                    state_ref = seen[1]
                self._reads.append(_Read(len(self._reads), key, result, state_ref))
                return state
            finally:
                self._reading = False

    def finish(self) -> dict:
        with self._lock:
            self._healthy()
            if self._reading:
                self._fail("collision recording cannot end during a poll")
            self._finished = True
            return {"schema": SCHEMA, "build": BUILD, "scope": SCOPE,
                    "reads": [read.exported() for read in self._reads],
                    "end": {"seq": len(self._reads), "status": "complete"}}

    def assert_complete(self) -> None:
        with self._lock:
            self._healthy()
            if not self._finished:
                self._fail("collision recording has no end marker; call finish first")


class ReplayCollisionProvider(_CollisionReads):
    """Replay strict polls using an explicit resolver whose World content is checked each time."""

    def __init__(self, tape, *, resolver: Callable[[dict], World]) -> None:
        super().__init__()
        if not callable(resolver):
            raise TypeError("an explicit World resolver is required")
        self._reads = _parse(tape)
        self._resolver = resolver
        self._states: list[LoadState] = []
        self._position = 0

    @property
    def position(self) -> int:
        with self._lock:
            return self._position

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._reads) - self._position

    def poll(self, map_guid: int, name: str = "", mode: int | None = None) -> LoadState:
        with self._lock:
            self._healthy()
            if self._reading:
                self._fail("reentrant collision replay poll")
            self._reading = True
            try:
                try:
                    key = _key(map_guid, name, mode)
                except BaseException as error:  # noqa: BLE001 - retain interruptions as a permanent fault
                    self._interrupted(error, "key read")
                if self._position == len(self._reads):
                    self._fail(f"unexpected collision poll after tape end at sequence {self._position}")
                read = self._reads[self._position]
                if key != read.key:
                    self._fail(f"collision poll does not match tape at sequence {self._position}")
                state = None
                if read.state_ref is not None and read.state_ref < len(self._states):
                    state = self._states[read.state_ref]
                result = read.result
                world = None
                if result.asset is not None:
                    try:
                        world = self._resolver(result.asset.exported())
                        if type(world) is not World or _asset(world_fingerprint(world)) != result.asset:
                            raise CollisionTapeError("resolved collision asset does not match recorded World")
                        if (state is not None and world is not state.world
                                and _asset(world_fingerprint(state.world)) != result.asset):
                            raise CollisionTapeError("retained state no longer matches recorded World")
                    except BaseException as error:  # noqa: BLE001 - retain interruptions as a permanent fault
                        self._interrupted(error, "World resolution")
                self._healthy()
                if state is None:
                    state = LoadState(result.status, world, result.error)
                    if read.state_ref is not None:
                        self._states.append(state)
                self._position += 1
                return state
            finally:
                self._reading = False

    def assert_complete(self) -> None:
        with self._lock:
            self._healthy()
            if self._reading or self._position != len(self._reads):
                self._fail(f"unconsumed collision reads at sequence {self._position}")
