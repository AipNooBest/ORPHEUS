"""Explicit in-memory controlled GameServer roots and raw-wire replay.

This harness runs the production GameServer/Client/Match/Combat implementation
in memory. It verifies controlled roots, external reads, wire attempts and callback
order. The scheduler root supplies finite receive outcomes without a live socket.
Private tapes contain handoff keys, account data and encrypted datagrams.
"""

import dataclasses
import hashlib
import json
from collections import deque
from contextlib import nullcontext

from ow174.game import replay_values
from ow174.game.collision import CollisionProvider, LoadState
from ow174.game.collision_fingerprint import WorldRegistry
from ow174.game.collision_tape import RecordingCollisionProvider, ReplayCollisionProvider
from ow174.game.content import GameMap, Spawn, heroes
from ow174.game.environment import ExecutionEnvironment
from ow174.game.environment_tape import RecordingEnvironment, ReplayEnvironment
from ow174.game.match import RETAIL_QUANTA, no_skin
from ow174.game.replay_manifest import assert_compatible, capture_manifest, runtime_configuration
from ow174.game.server import RECEIVE_BYTES, GameServer, Handoff
from ow174.paths import DATA_DIR, ROOT

SCHEMA = "ow174.controlled-server-journal.v2"
BUILD = "1.74.0.0.104319"
SCOPE = "controlled-roots-and-wire-only"
STORED_SCOPE = "controlled-roots-wire-and-registered-state"
OPERATIONS = {
    "create_match": {"game_map", "players", "card"},
    "join_match": {"match_id", "players"},
    "handoff_pending": {"conn"},
    "expire_handoff": {"conn"},
    "datagram": {"data", "address", "now"},
    "tick": {"now", "missed"},
    "switch_hero": {"account", "hero"},
    "skin_changed": {"account", "hero"},
    "send_home": {"account"},
    "take_out": {"player", "reason"},
    "report_callbacks": set(),
    "run_loop": {"receives", "callback_actions"},
}
CALLBACK_OPERATIONS = {
    "joined": {"take_out", "expire_handoff"},
    "left": {"take_out", "expire_handoff"},
    "clock_ready": {"create_match"},
}
RECEIVE_OPERATIONS = {"expire_handoff"}


class ServerJournalError(RuntimeError):
    pass


def _fields(value, names, where):
    if type(value) is not dict or set(value) != names:
        raise ServerJournalError(f"invalid {where} fields")


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ServerJournalError(f"invalid {label}")
    return value


def _address(value):
    if (type(value) is not tuple or len(value) != 2 or type(value[0]) is not str
            or type(value[1]) is not int or not 0 <= value[1] <= 65535):
        raise ServerJournalError("invalid IPv4 socket address")
    return value


def _game_map(value):
    _fields(value, {f.name for f in dataclasses.fields(GameMap)}, "GameMap")
    if type(value["spawns"]) is not tuple:
        raise ServerJournalError("GameMap spawns must be a tuple")
    spawns = []
    for spawn in value["spawns"]:
        _fields(spawn, {f.name for f in dataclasses.fields(Spawn)}, "Spawn")
        if type(spawn["position"]) is not tuple or len(spawn["position"]) != 3:
            raise ServerJournalError("invalid Spawn position")
        spawns.append(Spawn(**spawn))
    return GameMap(**{**value, "spawns": tuple(spawns)})


def _handoffs(value):
    if value is None:
        return None
    if not all(isinstance(item, Handoff) for item in value):
        raise ServerJournalError("unexpected handoff result")
    return [dataclasses.asdict(item) for item in value]


def _operation_result(operation, result):
    return _handoffs(result) if operation in ("create_match", "join_match") else result


class _MemorySocket:
    def __init__(self, session):
        self.session = session

    def sendto(self, data, address):
        return self.session._send(data, address)


class _LoopSocket(_MemorySocket):
    """Finite receive outcomes for one synchronous production scheduler run."""

    def __init__(self, session, receives):
        super().__init__(session)
        self.receives = deque(receives)
        self.closed = False

    def settimeout(self, timeout):
        self.session._healthy()
        self.session._observe({"kind": "socket_timeout", "timeout": timeout})

    def recvfrom(self, size):
        self.session._healthy()
        if self.closed or not self.receives:
            self.session._fail("scheduler consumed too many receive outcomes")
        self.session._observe({"kind": "socket_receive", "size": size})
        received = self.receives.popleft()
        if type(received) is dict:
            self.session._receive_actions(received["actions"])
            received = received["outcome"]
        if not self.receives:
            self.session.server.running = False
        if received is None:
            raise TimeoutError()
        data, address = received
        if data == "error":
            raise OSError(*address)
        if len(data) > size:
            self.session._fail("datagram exceeds the scheduler receive buffer")
        return data, address

    def close(self):
        self.session._healthy()
        if self.closed or self.receives:
            self.session._fail("scheduler closed with an invalid receive boundary")
        self.closed = True
        self.session._observe({"kind": "socket_close"})


class _CollisionReads:
    def __init__(self, session):
        self.session = session

    def poll(self, map_guid: int, name: str = "", mode: int | None = None) -> LoadState:
        state = self.session.collision.poll(map_guid, name, mode)
        self.session._observe({"kind": "collision_poll", "map": map_guid, "name": name,
                               "mode": mode, "status": state.status, "error": state.error})
        return state


class _Session:
    def __init__(self):
        self._fault = None
        self._active = False
        self._finished = False
        self._matches = {}
        self._effects = []
        self._sends = 0
        self._state_profile = "wire-only"
        self._callback_actions = deque()

    @property
    def fault(self):
        return self._fault

    def _fail(self, message):
        if self._fault is None:
            self._fault = ServerJournalError(message)
        raise self._fault from None

    def _healthy(self):
        if self._fault is not None:
            raise self._fault from None
        for tape in (self.environment, self.collision):
            if tape.fault is not None:
                self._fail("an external read tape failed")

    def _indices(self):
        if self._recording:
            return (self.environment.read_count, self.collision.read_count)
        return (self.environment.position, self.collision.position)

    def _initialize_server(self):
        self._collision_reads = _CollisionReads(self)
        self.server = GameServer(
            environment=self.environment,
            skin_of=self._skin,
            on_join=lambda player: self._callback("joined", player),
            on_leave=lambda player: self._callback("left", player),
            on_clock_ready=lambda: self._callback("clock_ready", None),
            collision_provider_factory=lambda: self._collision_reads,
        )
        self.server.sock = _MemorySocket(self)

    @staticmethod
    def _player_key(player):
        return (player.match.id, player.slot)

    def _callback(self, kind, player):
        key = self._player_key(player) if player is not None else None
        self._observe({"kind": kind} if key is None else {"kind": kind, "player": key})
        while self._callback_actions and self._callback_actions[0][:2] == (kind, key):
            _, _, operation, arguments = self._callback_actions.popleft()
            self._observe({"kind": "callback_action", "operation": operation, "arguments": arguments,
                           "player": key, "callback": kind})
            try:
                result = _operation_result(operation, self._dispatch(operation, arguments))
            except Exception as error:  # noqa: BLE001 - production catches callback exceptions
                self._fail(f"callback action failed: {type(error).__name__}")
            self._observe({"kind": "callback_result", "result": result})

    def _receive_actions(self, actions):
        for operation, arguments in actions:
            self._observe({"kind": "receive_action", "operation": operation, "arguments": arguments})
            try:
                result = self._dispatch(operation, arguments)
            except Exception as error:  # noqa: BLE001 - action errors are not socket errors
                self._fail(f"receive action failed: {type(error).__name__}")
            self._observe({"kind": "receive_result", "result": result})

    def _match(self, identifier):
        if type(identifier) is not tuple or len(identifier) != 2:
            self._fail("invalid match reference")
        found = self._matches.get(identifier)
        if found is None:
            self._fail("unknown match reference")
        return found

    def _player(self, key):
        if type(key) is not tuple or len(key) != 2:
            self._fail("invalid player reference")
        match, slot = self._match(key[0]), _integer(key[1], "player slot")
        return next((p for p in match.players if p.slot == slot), None) or self._fail("unknown player slot")

    def _dispatch(self, operation, args):
        if operation == "create_match":
            result = self.server.create_match(_game_map(args["game_map"]), args["players"], args["card"])
            if result is not None:
                match = self.server.matches[-1]
                self._matches[match.id] = match
            return result
        if operation == "join_match":
            return self.server.join_match(self._match(args["match_id"]), args["players"])
        if operation == "handoff_pending":
            return self.server.handoff_pending(_integer(args["conn"], "handoff connection", minimum=1))
        if operation == "expire_handoff":
            return self.server.expire_handoff(_integer(args["conn"], "handoff connection", minimum=1))
        if operation == "datagram":
            if type(args["data"]) is not bytes:
                self._fail("datagram must contain raw bytes")
            _address(args["address"])
            return self.server._datagram(args["data"], args["address"], args["now"])
        if operation == "tick":
            return self.server._tick(args["now"], missed=_integer(args["missed"], "missed ticks"))
        if operation == "switch_hero":
            hero = heroes().get(_integer(args["hero"], "hero"))
            if hero is None:
                self._fail("unknown hero")
            return self.server.switch_hero(args["account"], hero)
        if operation == "skin_changed":
            return self.server.skin_changed(args["account"], args["hero"])
        if operation == "send_home":
            return self.server.send_home(args["account"])
        if operation == "take_out":
            return self.server.take_out(self._player(args["player"]), args["reason"])
        if operation == "run_loop":
            return self._run_loop(args["receives"], args["callback_actions"])
        return self.server._report_leaves()

    def _run_loop(self, receives, callback_actions):
        if self.server.running:
            self._fail("controlled scheduler requires a stopped server")
        if type(receives) is not list or not receives:
            self._fail("scheduler receives must be a nonempty list")
        for received in receives:
            if type(received) is dict:
                _fields(received, {"outcome", "actions"}, "scheduler receive")
                actions = received["actions"]
                if type(actions) not in (tuple, list) or not actions:
                    self._fail("receive actions must be a nonempty ordered sequence")
                for action in actions:
                    if type(action) is not tuple or len(action) != 2:
                        self._fail("invalid receive action")
                    operation, arguments = action
                    if type(operation) is not str or operation not in RECEIVE_OPERATIONS:
                        self._fail("unsupported receive action")
                    _fields(arguments, OPERATIONS[operation], "receive action arguments")
                    _integer(arguments["conn"], "handoff connection", minimum=1)
                received = received["outcome"]
            if received is None:
                continue
            if type(received) is not tuple or len(received) != 2:
                self._fail("invalid scheduler receive outcome")
            data, address = received
            if type(data) is bytes:
                _address(address)
                if len(data) > RECEIVE_BYTES:
                    self._fail("datagram exceeds the scheduler receive buffer")
            elif (data != "error" or type(address) is not tuple or len(address) != 2
                  or type(address[0]) is not int or type(address[1]) is not str):
                self._fail("invalid scheduler receive outcome")

        if type(callback_actions) not in (tuple, list):
            self._fail("callback actions must be an ordered sequence")
        for action in callback_actions:
            if type(action) is not tuple or len(action) != 4:
                self._fail("invalid callback action")
            kind, key, operation, arguments = action
            if (type(kind) is not str or type(operation) is not str
                    or operation not in CALLBACK_OPERATIONS.get(kind, ())):
                self._fail("unsupported callback action")
            if kind == "clock_ready":
                if key is not None:
                    self._fail("clock-ready callback has no player reference")
            else:
                self._player(key)
            _fields(arguments, OPERATIONS[operation], "callback action arguments")
            if operation == "take_out":
                self._player(arguments["player"])
                if type(arguments["reason"]) is not str:
                    self._fail("invalid callback leave reason")
            elif operation == "expire_handoff":
                _integer(arguments["conn"], "handoff connection", minimum=1)
            else:
                _game_map(arguments["game_map"])
                _integer(arguments["card"], "queue card")
                if type(arguments["players"]) not in (tuple, list):
                    self._fail("callback match players must be an ordered sequence")

        original_socket = self.server.sock
        loop_socket = _LoopSocket(self, receives)
        self._callback_actions.extend(callback_actions)
        self.server.sock = loop_socket
        self.server.running = True
        try:
            self.server._run(on_iteration_error=self._iteration_failed)
            self._healthy()
            if not loop_socket.closed:
                self._fail("scheduler did not close the controlled socket")
            if self._callback_actions:
                self._fail("unconsumed callback actions")
        finally:
            self.server.running = False
            self.server.sock = original_socket
            self._callback_actions.clear()

    def _iteration_failed(self, error):
        self._fail(f"scheduler iteration failed: {type(error).__name__}")

    def _manifest(self):
        configuration = runtime_configuration(
            quanta_us=RETAIL_QUANTA,
            run_options={"scope": self._scope(), "state_profile": self._state_profile},
        )
        return capture_manifest(root=ROOT, data_dir=DATA_DIR, configuration=configuration)

    def _scope(self):
        return SCOPE if self._state_profile == "wire-only" else STORED_SCOPE

    def _stored_hash(self):
        if self._state_profile == "wire-only":
            return None
        from ow174.game.server_snapshot import export_server_state, snapshot_digest

        return snapshot_digest(export_server_state(
            self.server, archives=self._matches.values(), collision_provider=self._collision_reads,
            assets=self._state_assets,
        ))

    def _check_boundary(self, before, state_before):
        raise NotImplementedError

    def _root(self, operation, arguments):
        self._healthy()
        if self._finished or self._active:
            self._fail("root after end or reentrant root")
        if type(operation) is not str or operation not in OPERATIONS:
            self._fail("unsupported controlled server root")
        _fields(arguments, OPERATIONS[operation], "root arguments")
        # Validate/detach before execution; no arbitrary objects, serializers or coercion.
        args = replay_values.clone(arguments)
        if operation in ("datagram", "tick") and type(args["now"]) is not float:
            self._fail("root now must be an exact finite float")
        self._active = True
        self._effects = []
        try:
            # The production loop and callback delivery acquire their own locks.
            lock = nullcontext() if operation in ("report_callbacks", "run_loop") else self.server.lock
            with lock:
                before = self.server.tick, self._indices()
                state_before = self._stored_hash()
                self._check_boundary(before, state_before)
                result = self._dispatch(operation, args)
                after = self.server.tick, self._indices()
                state_after = self._stored_hash()
            self._healthy()
            row = {
                "operation": operation, "arguments": replay_values.encode(args),
                "before": replay_values.encode(before), "after": replay_values.encode(after),
                "result": replay_values.encode(_operation_result(operation, result)),
                "effects": replay_values.clone(self._effects),
                "state_before": state_before, "state_after": state_after,
            }
            return row, result
        except BaseException:
            if self._fault is None:
                self._fault = ServerJournalError("controlled server root failed")
            raise
        finally:
            self._active = False

    def create_match(self, game_map, players, card=0):
        return self.apply("create_match", {"game_map": dataclasses.asdict(game_map),
                                           "players": players, "card": card})

    def datagram(self, data, address, now):
        return self.apply("datagram", {"data": data, "address": address, "now": now})

    def tick(self, now, missed=0):
        return self.apply("tick", {"now": now, "missed": missed})

    def report_callbacks(self):
        return self.apply("report_callbacks", {})

    def run_loop(self, receives, callback_actions=()):
        return self.apply("run_loop", {"receives": receives, "callback_actions": callback_actions})


class RecordingServerSession(_Session):
    """Drive a fresh isolated production server and record explicit roots in memory.

    sendto is an optional in-memory outcome source, not a live socket. Without it,
    all sends succeed with len(data). A scheduler root can dispatch explicit server
    operations from its observed callbacks, including create_match after clock
    rollover. A scripted receive can expire a handoff while the game lock is
    released. A controlled lobby handler can record a reservation check through
    handoff_pending and a seat cancellation through expire_handoff. The
    surrounding lobby is external.
    """

    _recording = True

    def __init__(self, *, environment: ExecutionEnvironment, collision_provider: CollisionProvider,
                 registry=None, skin_of=no_skin, sendto=None, verify_stored_state=False):
        super().__init__()
        if type(verify_stored_state) is not bool:
            raise TypeError("verify_stored_state must be an explicit boolean")
        self._state_profile = "registered-domain-v1" if verify_stored_state else "wire-only"
        if verify_stored_state:
            # Explicit model preparation, before server construction/capture. The
            # exporter itself only peeks; it must not initialize a cache while observing.
            from ow174.game.mover import _movers

            _movers()
        self.environment = RecordingEnvironment(environment)
        self.registry = WorldRegistry() if registry is None else registry
        self._state_assets = WorldRegistry()
        self.collision = RecordingCollisionProvider(collision_provider, self.registry)
        self._skin_source, self._send_source = skin_of, sendto
        self._roots = []
        self._initial_manifest = self._manifest()
        self._initialize_server()
        self._initial = replay_values.encode((self.server.tick, self._indices()))
        self._initial_state = self._stored_hash()

    def handoff_pending(self, conn: int) -> bool:
        """Route the controlled lobby's reservation check through its game journal root."""
        return self.apply("handoff_pending", {"conn": conn})

    def expire_handoff(self, conn: int) -> bool:
        """Route a controlled lobby cancellation through its game journal root."""
        return self.apply("expire_handoff", {"conn": conn})

    def _observe(self, effect):
        if not self._active:
            self._fail("external effect outside a controlled root")
        self._effects.append(replay_values.encode((self._indices(), effect)))

    def _check_boundary(self, before, state_before):
        previous = self._roots[-1] if self._roots else None
        expected_boundary = self._initial if previous is None else previous["after"]
        expected_state = self._initial_state if previous is None else previous["state_after"]
        if replay_values.encode(before) != expected_boundary or state_before != expected_state:
            self._fail("unrecorded change between controlled roots")

    def _skin(self, account, hero):
        result = self._skin_source(account, hero)
        if (type(result) is not tuple or len(result) != 2 or type(result[0]) is not int
                or result[0] < 0 or type(result[1]) is not bool):
            self._fail("invalid equipped-skin result")
        self._observe({"kind": "skin", "account": account, "hero": hero, "result": result})
        return result

    def _send(self, data, address):
        if type(data) is not bytes:
            self._fail("send attempt must contain bytes")
        _address(address)
        effect = {"kind": "send", "data": data, "address": address}
        self._sends += 1
        try:
            result = len(data) if self._send_source is None else self._send_source(data, address)
        except OSError as error:
            effect["error"] = tuple(error.args)
            self._observe(effect)
            raise
        if type(result) is not int or not 0 <= result <= len(data):
            self._fail("invalid socket send result")
        effect["result"] = result
        self._observe(effect)
        return result

    def apply(self, operation, arguments):
        try:
            row, result = self._root(operation, arguments)
            row["seq"] = len(self._roots)
            self._roots.append(row)
            return result
        except BaseException:
            if self._fault is None:
                self._fault = ServerJournalError("recording root could not be captured")
            raise

    def finish(self):
        self._healthy()
        if self._active:
            self._fail("cannot end an active root")
        try:
            assert_compatible(self._initial_manifest, self._manifest())
            expected_state = self._roots[-1]["state_after"] if self._roots else self._initial_state
            expected_boundary = self._roots[-1]["after"] if self._roots else self._initial
            boundary = replay_values.encode((self.server.tick, self._indices()))
            if self._stored_hash() != expected_state or boundary != expected_boundary:
                self._fail("unrecorded stored domain change before recording end")
            result = {
                "schema": SCHEMA, "build": BUILD, "scope": self._scope(),
                "manifest": self._initial_manifest, "initial": self._initial,
                "state_profile": self._state_profile, "initial_state": self._initial_state,
                "environment": self.environment.finish(), "collision": self.collision.finish(),
                "roots": self._roots, "end": {"seq": len(self._roots), "status": "complete"},
            }
            self._finished = True
            return replay_values.clone(result)
        except BaseException:
            if self._fault is None:
                self._fault = ServerJournalError("recording could not be completed")
            raise


class _ReplaySession(_Session):
    _recording = False

    def __init__(self, document, resolver):
        super().__init__()
        document = replay_values.clone(document)
        _fields(document, {"schema", "build", "scope", "manifest", "initial", "environment",
                           "collision", "roots", "end", "state_profile", "initial_state"}, "journal")
        if document["schema"] != SCHEMA or document["build"] != BUILD:
            self._fail("unsupported controlled journal version or scope")
        profile = document["state_profile"]
        if type(profile) is not str or profile not in ("wire-only", "registered-domain-v1"):
            self._fail("unsupported stored-state profile")
        self._state_profile = profile
        if document["scope"] != self._scope():
            self._fail("journal scope differs from its state profile")
        if profile == "registered-domain-v1":
            from ow174.game.mover import _movers

            _movers()
        self._state_assets = WorldRegistry()
        if type(document["roots"]) is not list:
            self._fail("journal roots must be a list")
        _fields(document["end"], {"seq", "status"}, "journal end")
        if (type(document["end"]["seq"]) is not int
                or document["end"] != {"seq": len(document["roots"]), "status": "complete"}):
            self._fail("journal end is missing or incomplete")
        assert_compatible(document["manifest"], self._manifest())
        self.environment = ReplayEnvironment(document["environment"])
        self.collision = ReplayCollisionProvider(document["collision"], resolver=resolver)
        self._initialize_server()
        if replay_values.encode((self.server.tick, self._indices())) != document["initial"]:
            self._fail("initial server boundary differs")
        if self._stored_hash() != document["initial_state"]:
            self._fail("initial stored state differs")
        self.document = document
        self._expected_effects = []
        self._effect_index = 0
        self._expected_boundary = None

    def _check_boundary(self, before, state_before):
        expected = self._expected_boundary
        if (expected is None or replay_values.encode(before) != expected["before"]
                or state_before != expected["state_before"]):
            self._fail("stored state or boundary differs before root dispatch")

    def _next(self, kind):
        if not self._active or self._effect_index == len(self._expected_effects):
            self._fail("unexpected external effect")
        row = self._expected_effects[self._effect_index]
        try:
            indices, effect = replay_values.decode(row)
        except (ValueError, TypeError):
            self._fail("malformed external effect")
        if indices != self._indices() or type(effect) is not dict or effect.get("kind") != kind:
            self._fail("external effect order or read position differs")
        self._effect_index += 1
        self._effects.append(row)
        return effect

    def _observe(self, effect):
        expected = self._next(effect["kind"])
        if replay_values.encode(effect) != replay_values.encode(expected):
            self._fail("observed external effect differs")

    def _skin(self, account, hero):
        effect = self._next("skin")
        _fields(effect, {"kind", "account", "hero", "result"}, "skin effect")
        if (account, hero) != (effect["account"], effect["hero"]):
            self._fail("equipped-skin query differs")
        result = effect["result"]
        if (type(result) is not tuple or len(result) != 2 or type(result[0]) is not int
                or result[0] < 0 or type(result[1]) is not bool):
            self._fail("invalid recorded equipped-skin result")
        return result

    def _send(self, data, address):
        effect = self._next("send")
        outcome = "error" if "error" in effect else "result"
        _fields(effect, {"kind", "data", "address", outcome}, "send effect")
        if replay_values.encode((data, address)) != replay_values.encode((effect["data"], effect["address"])):
            self._fail("outbound datagram or address differs")
        self._sends += 1
        if outcome == "error":
            if type(effect["error"]) is not tuple:
                self._fail("invalid socket error arguments")
            raise OSError(*effect["error"])
        result = effect["result"]
        if type(result) is not int or not 0 <= result <= len(data):
            self._fail("invalid recorded socket result")
        return result

    def run(self):
        for seq, expected in enumerate(self.document["roots"]):
            _fields(expected, {"seq", "operation", "arguments", "before", "after", "result", "effects",
                               "state_before", "state_after"},
                    "root")
            if type(expected["seq"]) is not int or expected["seq"] != seq:
                self._fail("root sequence differs")
            if type(expected["effects"]) is not list:
                self._fail("root effects must be a list")
            self._expected_effects, self._effect_index = expected["effects"], 0
            self._expected_boundary = expected
            row, _ = self._root(expected["operation"], replay_values.decode(expected["arguments"]))
            row["seq"] = seq
            if self._effect_index != len(self._expected_effects):
                self._fail("unconsumed root effects")
            if row != expected:
                self._fail("root result or boundary differs")
        self._healthy()
        self.environment.assert_complete()
        self.collision.assert_complete()
        self._finished = True
        return {
            "schema": SCHEMA, "scope": self._scope(), "status": "matched_controlled_roots_and_wire",
            "roots": len(self.document["roots"]), "send_attempts": self._sends,
            "complete_battle_replay": False, "full_state_verified": False,
            "stored_state_verified": self._state_profile == "registered-domain-v1",
            "evidence_hash": hashlib.sha256(json.dumps(
                replay_values.encode(self.document), ensure_ascii=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
        }


def replay_server_session(document, *, resolver):
    """Replay private controlled roots using only in-memory network outcomes."""
    return _ReplaySession(document, resolver).run()
