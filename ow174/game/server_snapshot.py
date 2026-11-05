"""Read-only stored domain graphs for controlled GameServer replay.

External clocks, collision providers, sockets and lobby callbacks are named I/O
boundaries, verified by the journal's tapes/effects. This is not a restorable
checkpoint, live thread snapshot or a claim of original server mechanics.
"""

import dataclasses
import hashlib
import json
import math
import random
import struct
import types
import weakref
from collections import deque
from pathlib import Path

from ow174.game import (
    bots,
    combat,
    correction,
    heroselect,
    notices,
    observers,
    projectiles,
    replay_values,
    statescript,
    stats,
    voicelines,
    world,
)
from ow174.game.bits import BitWriter
from ow174.game.collision import LoadState, World
from ow174.game.collision_fingerprint import WorldRegistry
from ow174.game.commands import CommandQueue
from ow174.game.content import GameMap, Hero, Spawn
from ow174.game.link import LinkCipher, ReceiveWindow
from ow174.game.match import Match, Player, ShownController
from ow174.game.mover import Mover, MoverData, loaded_mover_table
from ow174.game.script import driver, expr, runtime
from ow174.game.script.bot import BotScript, BotWorld
from ow174.game.server import Client, GameServer, Handoff, _BotScriptDelivery
from ow174.paths import ROOT

SCHEMA = "ow174.controlled-server-stored-state.v1"
SCOPE = "registered-domain-fields-with-explicit-io-boundaries"
MAX_OBJECTS = 200_000
MAX_VALUES = 4_000_000


class ServerSnapshotError(RuntimeError):
    pass


def _fail(reason):
    raise ServerSnapshotError(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _names(text):
    return frozenset(text.split())


# Exact registered classes. Field names are explicit: a newly introduced runtime
# attribute must be reviewed, rather than silently omitted from a passing hash.
FIELDS = {
    GameServer: _names("environment collision_provider_factory host port on_leave on_join on_clock_ready "
                       "skin_of sock lock handoffs clients _dropped matches tick _next_conn _left_players "
                       "_joined_players _reporting_players _clock_ticks_left _clock_ready_pending "
                       "_sent_home running"),
    Client: _names("server address conn cipher player window seq open closed heard next_reliable reliable "
                   "unreliable entities in_flight _combat_notices _bot_health _combat_notice_tail "
                   "_body_destroy_wait _player_body_reuse_after _bot_scripts expected_in parked_in "
                   "leaving_since _mode_create_acked _pending_mode_frame _mode_notices _pending_possessions"),
    _BotScriptDelivery: _names("stream source live volleys death_frames death_snapshot"),
    Match: _names("environment id game_map card skin_of tick controller assemble_ends assembled players "
                  "_body_messages started on_leave ended bots _pending_bot_scripts bots_shown notices combat "
                  "collision_provider collision_state collision_world _collision_failed"),
    Player: _names("match slot account_lo name rank role card card_sent card_again hero team quanta entity "
                   "body has_body skin client gone loaded_at steps_done spawned spawn mover commands "
                   "body_script body_script_failed seen shown script mode_script next_countdown "
                   "select_open controller_hero "
                   "stats stats_sent next_stats"),
    ShownController: _names("script create_acked pick card_again possession"),
    CommandQueue: _names("last_frame epoch wraps"),
    combat.Combat: _names(
        "match vitals history rockets respawns next_bot now tick melee_seen "
        "projectiles observers voice"
    ),
    combat.Vitals: _names("entity most now ids changed died_at death_tick killer"),
    projectiles.Projectiles: _names("combat match shots observed next_field next_seen"),
    projectiles.FieldArrived: _names("shot viewer stream done"),
    observers.Observers: _names("match watches"),
    observers.Watch: _names("body arrived stream sent next_lazy"),
    observers._CreateArrived: _names("watch"),
    voicelines.VoiceLines: _names("match pending"),
    notices.Notices: _names("match entered waiting notice_chunk sent_at sending"),
    stats.PlayerStats: _names("values"),
    statescript.Stream: _names("entity last delivered data_last cmfd next_ack"),
    driver.BodyStream: _names("entity last cmfd base base_last pending full_update"),
    driver._CreateArrived: _names("script"),
    driver.BodyScript: _names(
        "hero entity record frame_ms world component frame buttons stream create_arrived "
        "retired first_owner_frame server_only ult_seconds ultimates reports report_count"
    ),
    driver.BodyWorld: _names("hero mode body throttles match"),
    BotScript: _names("component frame server_only initial death_frame _produced behavior"),
    BotWorld: _names("hero mode weapon_buttons owner_position"),
    runtime.World: _names("hero mode"),
    LinkCipher: _names("key_in key_out salt_in salt_out"),
    ReceiveWindow: _names("expected bits"),
    world.EntityUpdate: _names("entity op build movement chunk chunk_last ch2 stream resend correction"),
    BitWriter: _names("value count"),
    world.RecordWriter: _names("value count origin"),
    world.Movement: _names("position yaw pitch velocity flags throttles input_frame input_throttles "
                           "air_ticks gravity frame crouch_frame spring spring_speed fall"),
    bots.Bot: _names(
        "entity spawner hero_data hints dead body_script records_from hero name body team health speed accel "
        "brake pace direction position yaw velocity firing route index wait target start keys turning held "
        "last_held input_tick moved_tick last_tick"
    ),
}
OPTIONAL_FIELDS = {Client: _names("_entity_lifecycles _entity_record_generations _frame_budget_warning")}
DATACLASSES = (
    GameMap, Hero, Spawn, Handoff, LoadState, correction.SendToken, MoverData,
    heroselect.Controller, notices.Switch, combat.HitDef, combat.Target, combat.Rocket,
    projectiles.Shot, projectiles.ShotRecord, projectiles._ObservedProjectile, projectiles._ProjectileArrival,
    driver.Snapshot, statescript.Graph,
    statescript.Bool, statescript.Int, statescript.Float, statescript.Asset, statescript.Entity,
    statescript.Instance, statescript.Binding, statescript.StateWire, statescript.EventWire,
    statescript.InstanceWire, expr.Asset, expr.Entity, expr.Vec3, expr.Handle, expr.Map,
)


class _Graph:
    def __init__(self, boundaries, assets):
        self.boundaries = {}
        for name, obj in boundaries.items():
            if obj is None:
                continue
            existing = self.boundaries.get(id(obj))
            if existing is not None and existing[1] != name:
                _fail("one external object has conflicting boundary names")
            self.boundaries[id(obj)] = (obj, name)
        self.assets = assets
        self.asset_descriptors = {}
        self.asset_worlds = {}
        self.objects = []
        self.memo = {}
        self.nodes = []
        self.weak_nodes = []
        self.vm_nodes = []
        self.vm_extra = {}
        self.physics_owners = {}
        self.values = 0

    def value(self, obj):
        self.values += 1
        if self.values > MAX_VALUES:
            _fail("stored graph value budget exhausted")
        if obj is None or type(obj) in (bool, int, float, str, bytes):
            return replay_values.encode(obj)
        boundary = self.boundaries.get(id(obj))
        if boundary is not None and boundary[0] is obj:
            return ["boundary", boundary[1]]
        from ow174.game.body_vm_snapshot import _STATE_LAYOUTS

        if any(type(obj) is cls for cls in _STATE_LAYOUTS):
            component = obj.instance.component
            self.vm_extra.setdefault(id(component), []).append(obj)
            return ["vm_state", self.value(component), obj.instance.id, obj.index]
        if type(obj) is runtime.Instance:
            component = obj.component
            self.vm_extra.setdefault(id(component), []).append(obj)
            return ["vm_instance", self.value(component), obj.id]
        if type(obj) is tuple:
            return ["tuple", [self.value(item) for item in obj]]
        identity = id(obj)
        found = self.memo.get(identity)
        if found is not None:
            if self.objects[found] is not obj:
                _fail("stored graph identity conflict")
            return ["ref", found]
        if len(self.objects) >= MAX_OBJECTS:
            _fail("stored graph object budget exhausted")
        number = len(self.objects)
        self.memo[identity] = number
        self.objects.append(obj)
        self.nodes.append(None)
        return ["ref", number]

    def _asset(self, obj):
        existing = self.asset_worlds.get(id(obj))
        if existing is not None:
            return existing
        descriptor = self.assets.register(obj)
        key = _json(descriptor)
        self.asset_descriptors[key] = descriptor
        self.asset_worlds[id(obj)] = descriptor
        return descriptor

    def _fields(self, obj, required, optional=frozenset()):
        actual = object.__getattribute__(obj, "__dict__")
        names = set(actual)
        if not required <= names or names - required - optional:
            _fail(f"unregistered stored fields on {type(obj).__name__}")
        # Preserve attribute-dictionary identity and insertion order as well as the
        # reviewed layout. Two objects can otherwise share all future attribute writes.
        return {"fields": sorted(actual), "storage": self.value(actual)}

    def _code(self, code):
        path = Path(code.co_filename).resolve()
        try:
            relative = path.relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            _fail("builder function is outside the pinned source model")
        if not relative.startswith("ow174/game/"):
            _fail("unregistered builder code source")
        constants = []
        for value in code.co_consts:
            constants.append(self._constant(value))
        return {
            "source": relative, "name": code.co_name, "qualname": code.co_qualname,
            "first_line": code.co_firstlineno, "argcount": code.co_argcount,
            "posonlyargcount": code.co_posonlyargcount, "kwonlyargcount": code.co_kwonlyargcount,
            "nlocals": code.co_nlocals, "stacksize": code.co_stacksize, "flags": code.co_flags,
            "bytecode": code.co_code.hex(), "constants": constants, "names": list(code.co_names),
            "varnames": list(code.co_varnames), "freevars": list(code.co_freevars),
            "cellvars": list(code.co_cellvars), "linetable": code.co_linetable.hex(),
            "exceptiontable": code.co_exceptiontable.hex(),
        }

    def _constant(self, value):
        if type(value) is types.CodeType:
            return ["code", self._code(value)]
        if type(value) is tuple:
            return ["tuple", [self._constant(item) for item in value]]
        if type(value) is frozenset:
            return ["frozenset", sorted((self._constant(item) for item in value), key=_json)]
        if value is Ellipsis:
            return ["ellipsis"]
        if type(value) is float and not math.isfinite(value):
            # Python code may contain an overflowed literal. It is code metadata,
            # not permission for a nonfinite simulation field or typed root value.
            return ["code_float_bits", struct.pack(">d", value).hex()]
        return ["value", replay_values.encode(value)]

    def _function(self, obj):
        if not obj.__module__.startswith("ow174.game."):
            _fail("unregistered builder function module")
        if vars(obj):
            _fail("builder function has unregistered mutable attributes")
        free = []
        for name, cell in zip(obj.__code__.co_freevars, obj.__closure__ or (), strict=True):
            try:
                value = cell.cell_contents
            except ValueError:
                _fail("builder function has an empty closure cell")
            free.append([name, self.value(value)])
        return {
            "module": obj.__module__, "qualname": obj.__qualname__, "code": self._code(obj.__code__),
            "defaults": self.value(obj.__defaults__), "kwdefaults": self.value(obj.__kwdefaults__),
            "closure": free,
        }

    def build(self, number):
        obj, kind = self.objects[number], type(self.objects[number])
        label = f"{kind.__module__}.{kind.__qualname__}"
        if kind is list:
            data = [self.value(value) for value in obj]
        elif kind is dict:
            data = [[self.value(key), self.value(value)] for key, value in obj.items()]
        elif kind is deque:
            data = {"maxlen": obj.maxlen, "items": [self.value(value) for value in obj]}
        elif kind in (set, frozenset):
            # Registered game sets here are membership sets of immutable plain keys.
            # Causal VM watcher/state sets are handled by the component adapter.
            encoded = [replay_values.encode(value) for value in obj]
            data = sorted(encoded, key=_json)
        elif kind in (weakref.WeakKeyDictionary, weakref.WeakSet):
            values = list(obj.items()) if kind is weakref.WeakKeyDictionary else list(obj)
            self.weak_nodes.append((number, kind, values))
            data = []
        elif kind is types.MethodType:
            data = {"self": self.value(obj.__self__), "function": self.value(obj.__func__)}
        elif kind is types.FunctionType:
            data = self._function(obj)
        elif kind is World:
            data = {"asset": self._asset(obj)}
        elif kind is Mover:
            from ow174.game.commands import Command
            from ow174.game.mover import Snapshot
            from ow174.game.physics_snapshot import snapshot_mover_with_identities

            physics, identities = snapshot_mover_with_identities(
                obj, world_descriptor=self._asset(obj.world)
            )
            for _, owned in identities:
                if owned is obj or type(owned) in (tuple, MoverData, Command, Snapshot):
                    continue
                previous = self.physics_owners.get(id(owned))
                if previous is not None and previous[0] is owned and previous[1] is not obj:
                    _fail("mutable physics state shared across Mover artifacts is unsupported")
                self.physics_owners[id(owned)] = (owned, obj)

            data = {
                "world": self.value(obj.world), "data": self.value(obj.data),
                "values": self.value(obj.values), "physics": physics,
            }
        elif kind is runtime.Component:
            # Extra refs may be discovered later through callbacks/voice pending work.
            self.vm_nodes.append(number)
            data = {"world": self.value(obj.world), "rng": self.value(obj.rng),
                    "sequence": self.value(obj.seq), "component": None}
        elif kind is runtime.EventSequence:
            if tuple(kind.__slots__) != ("_next_value",):
                _fail("unregistered event sequence storage")
            data = {"next_value": replay_values.encode(object.__getattribute__(obj, "_next_value"))}
        elif kind is random.Random:
            data = {"fields": self._fields(obj, frozenset(("gauss_next",))),
                    "rng_state": replay_values.encode(random.Random.getstate(obj))}
        elif kind in FIELDS:
            data = self._fields(obj, FIELDS[kind], OPTIONAL_FIELDS.get(kind, frozenset()))
        elif kind in DATACLASSES:
            fields = frozenset(field.name for field in dataclasses.fields(kind))
            data = self._fields(obj, fields)
        else:
            _fail(f"unregistered stored domain type: {label}")
        self.nodes[number] = {"id": number, "type": label, "state": data}

    def finish(self):
        index = 0
        while index < len(self.objects):
            self.build(index)
            index += 1
        from ow174.game.body_vm_snapshot import export_component_vm_with_identities

        owners = {}
        for number in self.vm_nodes:
            component = self.objects[number]
            extras = self.vm_extra.get(id(component), ())
            document, identities = export_component_vm_with_identities(component, extra_roots=extras)
            bridges = []
            for storage_id, obj, scope in identities:
                core = self.memo.get(id(obj))
                if core is not None and self.objects[core] is obj:
                    bridges.append([storage_id, ["ref", core]])
                    continue
                old = owners.get(id(obj))
                if old is not None and old[0] is obj and old[1] != number:
                    if scope != "model" or old[2] != "model":
                        _fail("private mutable VM storage shared across Component artifacts is unsupported")
                else:
                    owners[id(obj)] = (obj, number, scope)
            self.nodes[number]["state"]["component"] = document
            self.nodes[number]["state"]["storage_bridges"] = bridges
        # Weak lookup entries without another strong domain reference cannot affect
        # later queue/ACK decisions. Do not retain them to manufacture a new root.
        for number, kind, values in self.weak_nodes:
            entries = []
            if kind is weakref.WeakKeyDictionary:
                for key, value in values:
                    target = self.memo.get(id(key))
                    if target is not None and self.objects[target] is key:
                        entries.append([target, replay_values.encode(value)])
                entries.sort(key=lambda row: row[0])
            else:
                for value in values:
                    target = self.memo.get(id(value))
                    if target is not None and self.objects[target] is value:
                        entries.append(target)
                entries.sort()
            self.nodes[number]["state"] = entries
        return self.nodes


def export_server_state(server, *, archives=(), collision_provider=None, assets=None):
    """Export all registered stored fields; unknown fields/types fail instead of being skipped.

    Only stopped controlled servers are accepted. Every match must use the same
    explicitly supplied external collision boundary. Asset descriptors are checked
    at registration; a journal must also revalidate the static assets at its end.
    The returned document includes private key material: keep it in memory.
    """
    if type(server) is not GameServer or server.running:
        _fail("stored exporter requires a stopped controlled GameServer")
    archives = tuple(archives)
    matches = list(server.matches) + list(archives)
    for match in matches:
        if type(match) is not Match:
            _fail("unregistered archived match type")
        if match.environment is not server.environment or match.skin_of is not server.skin_of:
            _fail("match external boundaries differ from its controlled server")
        if collision_provider is None or match.collision_provider is not collision_provider:
            _fail("match has an unregistered external collision provider")
    boundaries = {
        "environment": server.environment, "socket": server.sock, "domain_lock": server.lock,
        "skin": server.skin_of, "joined_callback": server.on_join, "left_callback": server.on_leave,
        "clock_ready_callback": server.on_clock_ready,
        "collision_factory": server.collision_provider_factory, "collision_provider": collision_provider,
    }
    graph = _Graph(boundaries, WorldRegistry() if assets is None else assets)
    with server.lock:
        roots = {"server": graph.value(server), "archived_matches": graph.value(archives),
                 "canonical_movers": graph.value(loaded_mover_table())}
        nodes = graph.finish()
    return {
        "schema": SCHEMA, "scope": SCOPE, "restorable": False,
        "roots": roots, "nodes": nodes,
        "assets": [graph.asset_descriptors[key] for key in sorted(graph.asset_descriptors)],
    }


def snapshot_digest(document):
    """Digest a detached stored graph without evaluating any gameplay object."""
    names = {"schema", "scope", "restorable", "roots", "nodes", "assets"}
    if (type(document) is not dict or set(document) != names or document["schema"] != SCHEMA
            or document["scope"] != SCOPE or document["restorable"] is not False):
        _fail("invalid stored server snapshot schema")
    if type(document["nodes"]) is not list:
        _fail("invalid stored node table")
    for index, node in enumerate(document["nodes"]):
        if (type(node) is not dict or set(node) != {"id", "type", "state"}
                or type(node["id"]) is not int or node["id"] != index or type(node["type"]) is not str):
            _fail("invalid stored node identity")
    return hashlib.sha256(_json(document).encode("utf-8")).hexdigest()
