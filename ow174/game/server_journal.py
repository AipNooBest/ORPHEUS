"""Explicit in-memory controlled GameServer roots and raw-wire replay.

This harness never starts the network loop or binds a socket. Both directions use
the same production GameServer/Client/Match/Combat implementation. It verifies
roots, external reads, wire attempts and callback order, NOT full internal state.
Private tapes contain handoff keys, account data and encrypted datagrams.
"""

import dataclasses
import hashlib
import json

from ow174.game import replay_values
from ow174.game.collision import CollisionProvider, LoadState
from ow174.game.collision_fingerprint import WorldRegistry
from ow174.game.collision_tape import RecordingCollisionProvider, ReplayCollisionProvider
from ow174.game.content import GameMap, Spawn, heroes
from ow174.game.environment import ExecutionEnvironment
from ow174.game.environment_tape import RecordingEnvironment, ReplayEnvironment
from ow174.game.match import RETAIL_QUANTA, no_skin
from ow174.game.replay_manifest import assert_compatible, capture_manifest, runtime_configuration
from ow174.game.server import GameServer, Handoff
from ow174.paths import DATA_DIR, ROOT

SCHEMA = "ow174.controlled-server-journal.v1"
BUILD = "1.74.0.0.104319"
SCOPE = "controlled-roots-and-wire-only"
OPERATIONS = {
    "create_match": {"game_map", "players", "card"},
    "join_match": {"match_id", "players"},
    "datagram": {"data", "address", "now"},
    "tick": {"now", "missed"},
    "switch_hero": {"account", "hero"},
    "skin_changed": {"account", "hero"},
    "send_home": {"account"},
    "take_out": {"player", "reason"},
    "report_callbacks": set(),
}


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


class _MemorySocket:
    def __init__(self, session):
        self.session = session

    def sendto(self, data, address):
        return self.session._send(data, address)


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
            collision_provider_factory=lambda: self._collision_reads,
        )
        self.server.sock = _MemorySocket(self)

    @staticmethod
    def _player_key(player):
        return (player.match.id, player.slot)

    def _callback(self, kind, player):
        self._observe({"kind": kind, "player": self._player_key(player)})

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
            match = self.server.matches[-1]
            self._matches[match.id] = match
            return result
        if operation == "join_match":
            return self.server.join_match(self._match(args["match_id"]), args["players"])
        if operation == "datagram":
            if type(args["data"]) is not bytes:
                self._fail("datagram must contain raw bytes")
            _address(args["address"])
            return self.server._datagram(args["data"], args["address"], args["now"])
        if operation == "tick":
            # Exactly the scheduler's skipped-tick adjustment, followed by the real _tick.
            # Recorded now is explicit; this controlled harness does not reproduce _run clocks.
            self.server.tick += _integer(args["missed"], "missed ticks")
            return self.server._tick(args["now"])
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
        return self.server._report_leaves()

    def _manifest(self):
        configuration = runtime_configuration(quanta_us=RETAIL_QUANTA, run_options={"scope": SCOPE})
        return capture_manifest(root=ROOT, data_dir=DATA_DIR, configuration=configuration)

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
            # Callback delivery drains under the lock but invokes outside it in production.
            # Do not add an outer lock around _report_leaves and change that contract.
            if operation == "report_callbacks":
                before = self.server.tick, self._indices()
                result = self._dispatch(operation, args)
                after = self.server.tick, self._indices()
            else:
                with self.server.lock:
                    before = self.server.tick, self._indices()
                    result = self._dispatch(operation, args)
                    after = self.server.tick, self._indices()
            self._healthy()
            normalized = _handoffs(result) if operation in ("create_match", "join_match") else result
            row = {
                "operation": operation, "arguments": replay_values.encode(args),
                "before": replay_values.encode(before), "after": replay_values.encode(after),
                "result": replay_values.encode(normalized), "effects": replay_values.clone(self._effects),
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


class RecordingServerSession(_Session):
    """Drive a fresh isolated production server and record explicit roots in memory.

    sendto is an optional in-memory outcome source, not a live socket. Without it,
    all sends succeed with len(data). on_join/on_leave are observed stubs; external
    lobby effects and callback reentrancy are outside this controlled contract.
    """

    _recording = True

    def __init__(self, *, environment: ExecutionEnvironment, collision_provider: CollisionProvider,
                 registry=None, skin_of=no_skin, sendto=None):
        super().__init__()
        self.environment = RecordingEnvironment(environment)
        self.registry = WorldRegistry() if registry is None else registry
        self.collision = RecordingCollisionProvider(collision_provider, self.registry)
        self._skin_source, self._send_source = skin_of, sendto
        self._roots = []
        self._initial_manifest = self._manifest()
        self._initialize_server()
        self._initial = replay_values.encode((self.server.tick, self._indices()))

    def _observe(self, effect):
        if not self._active:
            self._fail("external effect outside a controlled root")
        self._effects.append(replay_values.encode((self._indices(), effect)))

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
            result = {
                "schema": SCHEMA, "build": BUILD, "scope": SCOPE,
                "manifest": self._initial_manifest, "initial": self._initial,
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
                           "collision", "roots", "end"}, "journal")
        if (document["schema"], document["build"], document["scope"]) != (SCHEMA, BUILD, SCOPE):
            self._fail("unsupported controlled journal version or scope")
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
        self.document = document
        self._expected_effects = []
        self._effect_index = 0

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
            _fields(expected, {"seq", "operation", "arguments", "before", "after", "result", "effects"},
                    "root")
            if type(expected["seq"]) is not int or expected["seq"] != seq:
                self._fail("root sequence differs")
            if type(expected["effects"]) is not list:
                self._fail("root effects must be a list")
            self._expected_effects, self._effect_index = expected["effects"], 0
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
            "schema": SCHEMA, "scope": SCOPE, "status": "matched_controlled_roots_and_wire",
            "roots": len(self.document["roots"]), "send_attempts": self._sends,
            "complete_battle_replay": False, "full_state_verified": False,
            "evidence_hash": hashlib.sha256(json.dumps(
                replay_values.encode(self.document), ensure_ascii=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest(),
        }


def replay_server_session(document, *, resolver):
    """Replay private controlled roots without starting GameServer._run or opening a socket."""
    return _ReplaySession(document, resolver).run()
