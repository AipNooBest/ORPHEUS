"""Read-only, fail-closed body VM storage export; transport has a separate owner.

No graph callbacks, variable resolution, frame snapshots or random draws run here.
Static Graph/Node storage is bound by exact typed definition and source-data hashes.
This is an observation format, not a restorer or a Python stack checkpoint.
"""

import hashlib
import json
import random

from ow174.game import replay_values
from ow174.game.script import expr, graph, nodes, runtime
from ow174.game.script.bot import BotWorld
from ow174.game.script.driver import BodyScript, BodyWorld

SCHEMA = "ow174.body-vm-storage.v1"
COMPONENT_SCHEMA = "ow174.component-vm-storage.v1"
SCOPE = "body-vm-storage-excludes-transport"
COMPONENT_SCOPE = "component-vm-storage"
TRANSPORT_FIELDS = frozenset(("stream", "create_arrived", "first_owner_frame", "reports", "report_count"))
_COMPONENT = frozenset((
    "entity", "frame_ms", "world", "rng", "frame", "now", "instances", "vars", "held", "button",
    "disabled", "seq", "next_id", "destroyed", "shots", "owner_dead", "fault", "_operation_depth",
    "_follow_depth", "_work_left",
))
_INSTANCE = frozenset((
    "component", "id", "graph", "parent", "parent_state", "vars", "states", "queue", "ticking",
    "deferred", "params", "stopped", "teardown", "initialized", "start",
))
_STATE = frozenset((
    "instance", "node", "index", "active", "ending", "counter", "start", "ended_at", "ticked_at",
    "watched", "flags_changed", "flags_check", "polled", "natives",
))
_SCRIPT = frozenset((
    "hero", "entity", "record", "frame_ms", "world", "component", "frame", "buttons", "stream",
    "create_arrived", "retired", "first_owner_frame", "server_only", "ult_seconds", "ultimates",
    "reports", "report_count",
))
_GRAPH = frozenset((
    "index", "header", "fields", "items", "containers", "nodes", "unknown_rids", "_by_pos", "_by_rid",
    "states", "entries", "remote_nodes", "sync_vars", "_owner", "_remote", "_presence",
))
_NODE = frozenset(("pos", "cls", "uid", "rid", "remote", "state", "client_only", "server_only", "fields"))
_VAR = ("base", "key", "links", "watchers")
_STATE_LAYOUTS = {
    runtime.State: ((), ()),
    nodes.BooleanSwitch: (("current", "pending"), ()),
    nodes.Stack: (("top", "under", "value", "var"), ()),
    nodes.LogicalButton: (("down", "pressed_at", "early_counter"), ()),
    nodes.AIBehavior: ((), ()),
    nodes.AIFireWeapon: (("button_id",), ()),
    nodes.DisableLogicalButton: ((), ("button_id",)),
    nodes.Wait: ((), ("ms",)), nodes.WaitForServerFrames: ((), ()),
    nodes.WaitFrames: (("target",), ()),
    nodes.ChaseVar: (("cur", "reached", "remaining", "last", "var", "interrupted"), ()),
    nodes.WeaponVolley: (
        ("offset", "volleys", "per_shot", "ammo0", "rate", "spread0", "links", "reported"),
        ("max_shots", "min_shots"),
    ),
    nodes.MoveTo: (("movement",), ()),
    nodes.Anim: ((), ()),
    nodes.Ability: (("flags", "presses", "cur", "cooldown_rate", "last", "var", "stack_var"), ()),
    nodes.Defer: ((), ()), nodes.Watch: ((), ("seen",)), nodes.Switch: (("case",), ()),
    nodes.SubScript: (("child",), ()), nodes.GameMessageEntry: ((), ("received",)),
    nodes.SendGameMessageState: ((), ()), nodes.CosmeticEntity: ((), ("var",)),
    nodes.GamePadVibration: ((), ()), nodes.TrackTargets: ((), ()), nodes.Effect: ((), ()),
    nodes.ClientOnlyPulser: (("pulses", "pulsed_at"), ()), nodes.ActiveQuery: ((), ()),
}
_VALUE_LAYOUTS = {
    expr.Asset: ("guid",), expr.Entity: ("id",), expr.Vec3: ("x", "y", "z"),
    expr.Handle: ("kind", "instance", "state", "extra"), expr.Map: ("items",),
    runtime.Link: ("state", "slot", "priority", "above", "stack"),
    runtime.Event: ("time", "seq", "kind", "state", "op", "param", "node", "button", "pressed",
                    "message", "transient"),
    graph.SyncVar: ("index", "var", "scope", "flag"),
}


class BodyVMSnapshotError(ValueError):
    pass


def _name(cls) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _layout(obj, required, optional=()):
    fields = vars(obj)
    required, optional = set(required), set(optional)
    if required - fields.keys() or fields.keys() - required - optional:
        raise BodyVMSnapshotError(f"unknown or missing stored fields in {_name(type(obj))}")
    return fields


def _integer(value, where):
    if type(value) is not int:
        raise BodyVMSnapshotError(f"invalid integer identity at {where}")
    return value


def _containers(fields, names, kind, where):
    if any(type(fields[name]) is not kind for name in names):
        raise BodyVMSnapshotError(f"unknown stored container at {where}")


def _digest(value) -> str:
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Export:
    def __init__(self, component):
        if type(component) is not runtime.Component:
            raise BodyVMSnapshotError("unknown component class")
        fields = _layout(component, _COMPONENT)
        _containers(fields, ("instances", "vars", "disabled"), dict, "component")
        _containers(fields, ("held", "destroyed", "shots"), list, "component")
        _integer(fields["entity"], "component")
        self.component = component
        self.instances = {}
        self.states = {}
        self.variables = {}
        self.graphs = {}
        self.node_owners = {}
        self.node_refs = {}
        self.pending = []
        self.records = []
        self.ancestors = set()
        self.storage = []
        self.storage_ids = {}
        self.storage_scopes = []

    def identity(self, obj, scope="vm"):
        """Number only original stored objects; keep them alive for this capture's duration."""
        found = self.storage_ids.get(id(obj))
        if found is None:
            found = len(self.storage)
            self.storage_ids[id(obj)] = found
            self.storage.append(obj)
            self.storage_scopes.append(set())
        elif self.storage[found] is not obj:
            raise BodyVMSnapshotError("storage identity changed during capture")
        self.storage_scopes[found].add(scope)
        return ["storage", found]

    def identities(self):
        return tuple((number, obj, "+".join(tag for tag in ("vm", "model")
                                           if tag in self.storage_scopes[number]))
                     for number, obj in enumerate(self.storage))

    def _instance(self, obj):
        if type(obj) is not runtime.Instance:
            raise BodyVMSnapshotError("unknown instance class")
        fields = _layout(obj, _INSTANCE)
        _containers(fields, ("vars", "states"), dict, "instance")
        _containers(fields, ("queue", "ticking", "deferred", "params"), list, "instance")
        if fields["component"] is not self.component:
            raise BodyVMSnapshotError("foreign component instance reference")
        number = _integer(fields["id"], "instance")
        if number in self.instances and self.instances[number] is not obj:
            raise BodyVMSnapshotError("distinct instances reuse a local instance identity")
        if number not in self.instances:
            self.instances[number] = obj
            self.pending.append(("instance", obj))
        return ["instance", number]

    def _state(self, obj):
        kind = type(obj)
        if kind not in _STATE_LAYOUTS:
            raise BodyVMSnapshotError("unknown state class")
        required, optional = _STATE_LAYOUTS[kind]
        fields = _layout(obj, _STATE | set(required), optional)
        _containers(fields, ("watched",), dict, "state")
        _containers(fields, ("natives",), set, "state")
        owner = fields["instance"]
        self._instance(owner)
        self._graph(owner.graph)
        index = _integer(fields["index"], "state")
        if owner.states.get(index) is not obj:
            raise BodyVMSnapshotError("state is not registered under its instance identity")
        if (type(fields["node"]) is not graph.Node or not 0 <= index < len(owner.graph.states)
                or owner.graph.states[index] is not fields["node"]):
            raise BodyVMSnapshotError("state node does not match its graph slot")
        if kind is not runtime.STATE_CLASSES.get(fields["node"].cls, runtime.State):
            raise BodyVMSnapshotError("state class does not match its registered node model")
        key = (owner.id, index)
        if key not in self.states:
            self.states[key] = obj
            self.pending.append(("state", obj))
        elif self.states[key] is not obj:
            raise BodyVMSnapshotError("distinct states reuse a local state identity")
        return ["state", owner.id, index]

    def _var(self, obj):
        if type(obj) is not runtime.Var:
            raise BodyVMSnapshotError("unknown variable class")
        if runtime.Var.__slots__ != _VAR:
            raise BodyVMSnapshotError("unknown variable slot layout")
        try:
            data = {name: object.__getattribute__(obj, name) for name in _VAR}
        except AttributeError:
            raise BodyVMSnapshotError("missing variable storage field") from None
        _containers(data, ("links",), list, "variable")
        _containers(data, ("watchers",), dict, "variable")
        key = data["key"]
        if type(key) is not tuple or len(key) != 2 or any(type(value) is not int for value in key):
            raise BodyVMSnapshotError("invalid variable identity")
        if key not in self.variables:
            self.variables[key] = obj
            self.pending.append(("var", obj))
        elif self.variables[key] is not obj:
            raise BodyVMSnapshotError("distinct variables reuse a local variable identity")
        return ["var", *key]

    def _graph(self, obj):
        if type(obj) is not graph.Graph:
            raise BodyVMSnapshotError("unknown graph class")
        fields = _layout(obj, _GRAPH)
        _containers(fields, ("header", "fields", "containers", "_by_pos", "_by_rid"), dict, "graph")
        _containers(fields, ("nodes", "unknown_rids", "states", "entries", "remote_nodes", "sync_vars",
                            "_owner", "_remote", "_presence"), list, "graph")
        index = _integer(fields["index"], "graph")
        if index in self.graphs and self.graphs[index] is not obj:
            raise BodyVMSnapshotError("distinct graph objects reuse a graph identity")
        if index not in self.graphs:
            self.graphs[index] = obj
            self.pending.append(("graph", obj))
            for node in fields["nodes"]:
                if type(node) is not graph.Node:
                    raise BodyVMSnapshotError("unknown graph node class")
                data = _layout(node, _NODE)
                _containers(data, ("fields",), dict, "node")
                if type(data["cls"]) is not str:
                    raise BodyVMSnapshotError("unknown node model class identity")
                if any(type(data[name]) is not bool for name in ("client_only", "server_only")):
                    raise BodyVMSnapshotError("invalid graph node filter flags")
                for name in ("uid", "rid", "remote"):
                    _integer(data[name], f"node.{name}")
                if data["state"] is not None:
                    _integer(data["state"], "node.state")
                position = _integer(data["pos"], "node")
                key = (index, position)
                if key in self.node_owners:
                    raise BodyVMSnapshotError("duplicate graph node identity")
                if id(node) in self.node_refs:
                    raise BodyVMSnapshotError("node is shared by distinct graph identities")
                self.node_owners[key] = node
                self.node_refs[id(node)] = key
            for name, attribute in (("_by_pos", "pos"), ("_by_rid", "rid")):
                for key, node in fields[name].items():
                    if (type(key) is not int or type(node) is not graph.Node
                            or self.node_refs.get(id(node), (None,))[0] != index
                            or getattr(node, attribute) != key):
                        raise BodyVMSnapshotError("graph lookup does not match node identity")
            for name in ("states", "entries", "remote_nodes", "_owner", "_remote"):
                for node in fields[name]:
                    if node is not None and self.node_refs.get(id(node), (None,))[0] != index:
                        raise BodyVMSnapshotError("graph list refers to a foreign node")
            expected_owner = [node for node in fields["states"] if node is not None
                              and not node.client_only and not node.server_only]
            expected_remote = []
            for node in fields["remote_nodes"]:
                if node is None:
                    continue
                flag = node.fields.get("m_2BBEEAB8")
                if flag is not None and type(flag) not in (bool, int):
                    raise BodyVMSnapshotError("invalid graph node state flag")
                if flag and not node.client_only and not node.server_only:
                    expected_remote.append(node)
            expected_presence = []
            for item in fields["sync_vars"]:
                if type(item) is not graph.SyncVar:
                    raise BodyVMSnapshotError("unknown graph sync variable class")
                values = _layout(item, _VALUE_LAYOUTS[graph.SyncVar])
                for name in ("index", "scope", "flag"):
                    _integer(values[name], f"sync variable.{name}")
                if values["var"] is not None:
                    _integer(values["var"], "sync variable.var")
                if values["var"] is not None and values["scope"] == graph.INSTANCE:
                    expected_presence.append(item)
            for name, expected in (("_owner", expected_owner), ("_remote", expected_remote),
                                   ("_presence", expected_presence)):
                if (len(fields[name]) != len(expected)
                        or any(a is not b for a, b in zip(fields[name], expected, strict=True))):
                    raise BodyVMSnapshotError("graph derived cache differs from its source sequence")
        return ["graph", index]

    def _node(self, obj):
        found = self.node_refs.get(id(obj))
        if found is None or self.node_owners[found] is not obj:
            raise BodyVMSnapshotError("node is outside the component's graph definitions")
        _layout(obj, _NODE)
        return ["node", *found]

    def fields(self, obj, required, optional=(), exclude=()):
        data = _layout(obj, required, optional)
        self.identity(data)
        result = []
        for name, value in data.items():
            if name not in exclude:
                scope = "model" if type(obj) is BodyScript and name == "record" else "vm"
                result.append([name, self.value(value, scope=scope)])
        return result

    def _set_key(self, obj):
        """Sort unordered members before registering any identity or reference."""
        kind = type(obj)
        if obj is None or kind in (bool, int, float, str, bytes):
            return replay_values.encode(obj)
        if kind is runtime.Instance:
            return ["instance", _integer(obj.id, "set instance")]
        if kind in _STATE_LAYOUTS:
            if type(obj.instance) is not runtime.Instance:
                raise BodyVMSnapshotError("unknown set state owner")
            return ["state", _integer(obj.instance.id, "set instance"), _integer(obj.index, "set state")]
        if kind is runtime.Var:
            return ["var", replay_values.encode(obj.key)]
        if kind is graph.Node:
            return self._node(obj)
        if kind in (expr.Asset, expr.Entity, expr.Vec3, expr.Handle):
            data = _layout(obj, _VALUE_LAYOUTS[kind])
            return [_name(kind), [[key, self._set_key(value)] for key, value in data.items()]]
        if kind in (tuple, frozenset):
            values = [self._set_key(item) for item in obj]
            if kind is frozenset:
                values.sort(key=lambda value: json.dumps(value, sort_keys=True))
            return [kind.__name__, values]
        raise BodyVMSnapshotError("set member has no supported stable ordering key")

    def value(self, obj, *, scope="vm"):
        kind = type(obj)
        if obj is None or kind in (bool, int, float, str, bytes):
            return obj
        if obj is self.component:
            return {"ref": ["component"]}
        if kind is runtime.Instance:
            return {"ref": self._instance(obj)}
        if kind in _STATE_LAYOUTS:
            return {"ref": self._state(obj)}
        if kind is runtime.Var:
            return {"ref": self._var(obj)}
        if kind is graph.Graph:
            return {"ref": self._graph(obj)}
        if kind is graph.Node:
            return {"ref": self._node(obj)}
        if kind in (runtime.World, BodyWorld, BotWorld):
            if obj is not self.component.world:
                raise BodyVMSnapshotError("foreign VM world reference")
            fields = {"hero", "mode"}
            if kind is BodyWorld:
                fields.update(("body", "throttles", "match"))
            if kind is BotWorld:
                fields.update(("weapon_buttons", "owner_position"))
            # The match and its pending deliveries belong to the enclosing server snapshot.
            return {"class": _name(kind), "fields_identity": self.identity(vars(obj)),
                    "fields": self.fields(obj, fields, exclude={"match"})}
        if kind is random.Random:
            if obj is not self.component.rng:
                raise BodyVMSnapshotError("foreign RNG reference")
            data = _layout(obj, {"gauss_next"})
            return {
                "class": _name(kind), "identity": self.identity(obj),
                "fields_identity": self.identity(data), "fields": self.value(data),
                "state": self.value(random.Random.getstate(obj)),
            }
        if kind is runtime.EventSequence:
            if (runtime.EventSequence.__slots__ != ("_next_value",) or obj is not self.component.seq
                    or type(obj.next_value) is not int or obj.next_value < 0):
                raise BodyVMSnapshotError("invalid event sequence")
            return {"class": _name(kind), "identity": self.identity(obj), "next_value": obj.next_value}
        if kind is runtime.ScriptBudgetError:
            _layout(obj, set())
            return {"class": _name(kind), "identity": self.identity(obj),
                    "fields_identity": self.identity(vars(obj)), "args": self.value(obj.args)}
        if kind in _VALUE_LAYOUTS:
            fields = _layout(obj, _VALUE_LAYOUTS[kind])
            result = {"class": _name(kind), "fields_identity": self.identity(fields, scope)}
            identity = id(obj)
            if kind in (runtime.Link, runtime.Event):
                reference = self.identity(obj, scope)
                if identity in self.ancestors:
                    return {"ref": reference}
                result["identity"] = reference
            elif identity in self.ancestors:
                raise BodyVMSnapshotError("cyclic immutable value record")
            self.ancestors.add(identity)
            try:
                result["fields"] = [[name, self.value(value, scope=scope)] for name, value in fields.items()]
                return result
            finally:
                self.ancestors.remove(identity)
        if kind not in (list, tuple, dict, set, frozenset):
            raise BodyVMSnapshotError(f"unknown stored value class: {_name(kind)}")
        identity = id(obj)
        reference = self.identity(obj, scope) if kind in (list, dict, set) else None
        if reference is not None and identity in self.ancestors:
            return {"ref": reference}
        if reference is not None:
            self.ancestors.add(identity)
        try:
            if kind is dict:
                return {
                    "kind": "dict", "identity": reference,
                    "items": [[self.value(key, scope=scope), self.value(value, scope=scope)]
                              for key, value in obj.items()],
                }
            if kind in (set, frozenset):
                ordered = sorted(obj, key=lambda item: json.dumps(self._set_key(item), sort_keys=True))
            else:
                ordered = obj
            result = {"kind": kind.__name__, "items": [self.value(value, scope=scope) for value in ordered]}
            if reference is not None:
                result["identity"] = reference
            return result
        finally:
            if reference is not None:
                self.ancestors.remove(identity)

    def _graph_record(self, obj):
        data = _layout(obj, _GRAPH)
        identities, bindings, active = {}, [], set()

        def identity(value):
            number = identities.get(id(value))
            storage = self.identity(value, "model")
            if number is None:
                number = len(identities)
                identities[id(value)] = number
                bindings.append({"local": number, "storage": storage})
            return ["model_storage", number]

        def value(item):
            kind = type(item)
            if item is None or kind in (bool, int, float, str, bytes):
                return item
            if kind is graph.Node:
                return {"ref": self._node(item)}
            if kind is graph.SyncVar:
                fields = _layout(item, _VALUE_LAYOUTS[kind])
                return {"class": _name(kind), "fields_identity": identity(fields),
                        "fields": [[name, value(nested)] for name, nested in fields.items()]}
            if kind not in (dict, list, tuple):
                raise BodyVMSnapshotError("unknown graph definition value")
            reference = identity(item) if kind in (dict, list) else None
            if reference is not None and id(item) in active:
                return {"ref": reference}
            if reference is not None:
                active.add(id(item))
            try:
                if kind is dict:
                    if any(type(key) not in (str, int) for key in item):
                        raise BodyVMSnapshotError("unknown graph definition mapping key")
                    children = [[key, value(nested)] for key, nested in item.items()]
                else:
                    children = [value(nested) for nested in item]
                row = {"kind": kind.__name__, "items": children}
                if reference is not None:
                    row["identity"] = reference
                return row
            finally:
                if reference is not None:
                    active.remove(id(item))

        definition = {"fields_identity": identity(data),
                      "fields": [[name, value(nested)] for name, nested in data.items()], "nodes": []}
        for node in data["nodes"]:
            fields = _layout(node, _NODE)
            definition["nodes"].append({
                "ref": self._node(node), "fields_identity": identity(fields),
                "fields": [[name, value(nested)] for name, nested in fields.items()],
            })
        return {"ref": ["graph", obj.index], "class": _name(type(obj)),
                "fields_identity": self.identity(data, "model"), "storage_bindings": bindings,
                "definition_sha256": _digest(replay_values.encode(definition))}

    def capture(self, extra_roots, script=None):
        # Register graph ownership before a queue/state refers to one of its Nodes.
        for number, instance in self.component.instances.items():
            self._instance(instance)
            if type(number) is not int or number != instance.id:
                raise BodyVMSnapshotError("instance allocation mapping identity mismatch")
        for instance in (*self.component.instances.values(), *self.component.destroyed):
            self._instance(instance)
            self._graph(instance.graph)
        for root in extra_roots:
            if type(root) is runtime.Instance:
                self._instance(root)
                self._graph(root.graph)
            elif type(root) in _STATE_LAYOUTS:
                self._state(root)
                self._graph(root.instance.graph)
            elif type(root) is runtime.Var:
                self._var(root)
            else:
                raise BodyVMSnapshotError("extra root must be a known Instance, State or Var")
        result = {
            "component_fields_identity": self.identity(vars(self.component)),
            "component": self.fields(self.component, _COMPONENT), "objects": self.records,
            "extra_roots": [self.value(root) for root in extra_roots],
        }
        if script is not None:
            result["body_script_fields_identity"] = self.identity(vars(script))
            result["body_script"] = self.fields(script, _SCRIPT, exclude=TRANSPORT_FIELDS)
        cursor = 0
        while cursor < len(self.pending):
            kind, obj = self.pending[cursor]
            cursor += 1
            if kind == "instance":
                self._graph(obj.graph)
                for key, state in obj.states.items():
                    self._state(state)
                    if type(key) is not int or key != state.index:
                        raise BodyVMSnapshotError("state allocation mapping identity mismatch")
                record = {"ref": ["instance", obj.id], "class": _name(type(obj)),
                          "fields_identity": self.identity(vars(obj)),
                          "fields": self.fields(obj, _INSTANCE)}
            elif kind == "state":
                required, optional = _STATE_LAYOUTS[type(obj)]
                record = {"ref": ["state", obj.instance.id, obj.index], "class": _name(type(obj)),
                          "fields_identity": self.identity(vars(obj)),
                          "fields": self.fields(obj, _STATE | set(required), optional)}
            elif kind == "var":
                owner, number = obj.key
                table = self.component.vars if owner == 0 else self.instances.get(owner, None)
                table = table.vars if type(table) is runtime.Instance else table
                if type(table) is not dict or table.get(number) is not obj:
                    raise BodyVMSnapshotError("variable is outside its owner's storage")
                record = {"ref": ["var", *obj.key], "class": _name(type(obj)),
                          "fields": [[name, self.value(object.__getattribute__(obj, name))]
                                     for name in _VAR]}
            else:
                record = self._graph_record(obj)
            self.records.append(record)
        return result


def _document(component, extra_roots, script=None):
    if type(extra_roots) not in (tuple, list):
        raise BodyVMSnapshotError("extra_roots must be a tuple or list")
    try:
        source = hashlib.sha256(graph.GRAPHS_PATH.read_bytes()).hexdigest()
        exporter = _Export(component)
        raw = exporter.capture(extra_roots, script)
        document = {
            "schema": SCHEMA if script is not None else COMPONENT_SCHEMA,
            "scope": SCOPE if script is not None else COMPONENT_SCOPE,
            "graph_data_sha256": source, "value": replay_values.encode(raw),
        }
        return document, exporter.identities()
    except (OSError, replay_values.ReplayValueError, RecursionError) as error:
        raise BodyVMSnapshotError(f"VM storage export failed: {type(error).__name__}") from None


def export_component_vm(component, *, extra_roots=()) -> dict:
    """Export one Component namespace, including retired objects retained by extra roots."""
    return _document(component, extra_roots)[0]


def export_component_vm_with_identities(component, *, extra_roots=()) -> tuple:
    """Return one capture plus ephemeral (storage id, original object, scope tag) bindings.

    Scope tags are vm, model or vm+model. The sidecar is not serializable replay
    data; an outer adapter can detect unsupported sharing across artifact scopes.
    No identity callback or gameplay function is invoked.
    """
    return _document(component, extra_roots)


def export_body_vm(script) -> dict:
    if type(script) is not BodyScript:
        raise BodyVMSnapshotError("unknown body script class")
    data = _layout(script, _SCRIPT)
    if data["world"] is not data["component"].world or data["entity"] != data["component"].entity:
        raise BodyVMSnapshotError("body script component/world identity mismatch")
    return _document(data["component"], data["ultimates"], script)[0]


def snapshot_digest(document) -> str:
    """Validate the typed envelope and hash it; no live object is inspected."""
    if type(document) is not dict or set(document) != {"schema", "scope", "graph_data_sha256", "value"}:
        raise BodyVMSnapshotError("invalid VM snapshot fields")
    scopes = {SCHEMA: SCOPE, COMPONENT_SCHEMA: COMPONENT_SCOPE}
    if type(document["schema"]) is not str or document["schema"] not in scopes:
        raise BodyVMSnapshotError("unknown VM snapshot schema")
    if document["scope"] != scopes[document["schema"]]:
        raise BodyVMSnapshotError("invalid VM snapshot scope")
    source = document["graph_data_sha256"]
    if type(source) is not str or len(source) != 64 or any(c not in "0123456789abcdef" for c in source):
        raise BodyVMSnapshotError("invalid graph source signature")
    try:
        replay_values.decode(document["value"])
    except replay_values.ReplayValueError:
        raise BodyVMSnapshotError("invalid typed VM snapshot") from None
    return _digest(document)
