"""A strict offline research interpreter for the extracted 1.74 graphs.

This is an offline execution/diagnostic model, not a claim of retail simulation fidelity.
The default startup policy runs the first ordinary Entry of each definition; the bundle
does not establish when the other entries fire. Callers can select entries explicitly.
Local instance ids and weapon-script startup still need
real-client validation before their snapshots are treated as authoritative wire state.

Only supported paths execute. Unknown nodes, missing variables, expression instructions,
and state flags stop that path and produce a structured diagnostic, never a guessed zero.
No wall clock, weapon simulation, collision, or network encoder lives in this module.
"""

from collections import deque
from dataclasses import dataclass, field
from math import isfinite

from ow174.game.script.graph import ENTITY, INSTANCE, Graph, Node, bodies, graph, guid


class Unsupported(ValueError):
    """A value or node whose actual semantics have not been implemented."""


@dataclass(frozen=True)
class RuntimeValue:
    kind: str
    value: bool | int | float

    def __post_init__(self) -> None:
        if self.kind == "bool":
            valid = isinstance(self.value, bool)
        elif self.kind == "int":
            valid = type(self.value) is int and -(1 << 31) <= self.value < (1 << 31)
        elif self.kind == "float":
            valid = type(self.value) in (int, float) and abs(self.value) <= 3.4028234663852886e38
        elif self.kind in ("asset", "entity"):
            limit = 0xFFFFFFFFFFFFFFFF if self.kind == "asset" else 0xFFFFFFFF
            valid = type(self.value) is int and 0 <= self.value <= limit
        else:
            valid = False
        if not valid:
            raise ValueError(f"invalid {self.kind} value {self.value!r}")

    def snapshot(self) -> dict:
        return {"kind": self.kind, "value": self.value}


def value_of(value) -> RuntimeValue:
    """Turn an explicit caller value into a scalar, without inventing an asset type."""
    if isinstance(value, RuntimeValue):
        return value
    if isinstance(value, bool):
        return RuntimeValue("bool", value)
    if type(value) is int:
        return RuntimeValue("int", value)
    if type(value) is float and isfinite(value):
        return RuntimeValue("float", value)
    raise Unsupported(f"not a finite scalar: {value!r}")


class Variables:
    """Base values and reversible Stack layers, isolated by entity/instance scope."""

    def __init__(self, initial=None) -> None:
        self.base = {int(key): value_of(value) for key, value in (initial or {}).items()}
        self.layers: dict[int, list[tuple[tuple[int, int], RuntimeValue]]] = {}

    def read(self, identifier: int) -> RuntimeValue:
        layers = self.layers.get(identifier)
        if layers:
            return layers[-1][1]
        if identifier not in self.base:
            raise Unsupported(f"variable {identifier} is unset")
        return self.base[identifier]

    def write(self, identifier: int, value: RuntimeValue) -> None:
        self.base[identifier] = value

    def push(self, identifier: int, owner: tuple[int, int], value: RuntimeValue) -> None:
        layers = self.layers.setdefault(identifier, [])
        for index, (key, _item) in enumerate(layers):
            if key == owner:
                layers[index] = (owner, value)
                return
        layers.append((owner, value))

    def pop(self, owner: tuple[int, int]) -> None:
        for identifier in list(self.layers):
            layers = self.layers[identifier]
            layers[:] = [(key, item) for key, item in layers if key != owner]
            if not layers:
                del self.layers[identifier]

    def values(self) -> dict[int, RuntimeValue]:
        identifiers = sorted(self.base.keys() | self.layers.keys())
        return {identifier: self.read(identifier) for identifier in identifiers}


@dataclass
class RuntimeInstance:
    index: int
    graph: Graph
    role: str
    parent: tuple[int, int] | None = None  # parent instance, raw parent state index
    store: Variables = field(default_factory=Variables)
    active: set[int] = field(default_factory=set)  # raw m_states indexes, NOT wire bit positions
    children: dict[int, int] = field(default_factory=dict)  # raw state -> local child instance id
    visited: set[tuple[int, str]] = field(default_factory=set)
    pending_entries: list[int] = field(default_factory=list)

    @property
    def variables(self) -> dict[int, RuntimeValue]:
        return self.store.values()


@dataclass(frozen=True)
class Diagnostic:
    instance: int
    graph: int
    node: int | None
    feature: str
    message: str
    path: tuple[str, ...] = ()
    blocking: bool = True

    def snapshot(self) -> dict:
        return {
            "instance": self.instance,
            "graph": self.graph,
            "node": self.node,
            "feature": self.feature,
            "message": self.message,
            "path": list(self.path),
            "blocking": self.blocking,
        }


class BodyRuntime:
    """A deterministic body graph model; unsupported paths remain visible in snapshot().

    ``initialize(frame=None, entries=None)`` creates definition-order instances, followed
    by the weapon manager and nonempty weapon slots. ``entries`` maps graph indexes to
    explicit ordinary Entry node positions. Default first-Entry selection is experimental.
    ``advance(command)`` accepts an object with .frame/.buttons (e.g. Command), deduplicates
    uint32 frames, and advances integer microsecond time. The first command anchors time
    if initialize() did not receive a frame. A frame restart needs a fresh runtime.
    Missing command frames are blocking diagnostics; no absent inputs are synthesized.
    Numeric expression opcodes require an explicitly supplied verified evaluator.

    Wire consumers must map RuntimeInstance.active through Graph.owner_slot(), preserve
    typed variables, and encode SubScript payloads for children. They must also gate on
    complete/diagnostics: the mere existence of a snapshot does not mean a graph works.
    """

    def __init__(
        self,
        body_guid: int,
        *,
        quanta_us: int = 16000,
        entity_vars=None,
        graph_loader=graph,
        body_catalog=None,
        max_steps: int = 4096,
        generation: int = 0,
        expression_evaluator=None,
    ) -> None:
        if type(quanta_us) is not int or type(max_steps) is not int or quanta_us <= 0 or max_steps < 1:
            raise ValueError("quanta_us and max_steps must be positive integers")
        self.body_guid = body_guid
        self.quanta_us = quanta_us
        self.generation = generation
        self.max_steps = max_steps
        self.graph_loader = graph_loader
        # A caller may inject an independently verified 1.74 decoder. Numeric opcode
        # meanings are not borrowed from the incompatible beta-era Prometheus table.
        self.expression_evaluator = expression_evaluator
        catalog = bodies() if body_catalog is None else body_catalog
        if body_guid not in catalog:
            raise ValueError(f"unknown body {body_guid:016X}")
        self.definition = catalog[body_guid]
        self.entity_store = Variables(entity_vars)
        self.instances: list[RuntimeInstance] = []
        self.diagnostics: list[Diagnostic] = []
        self.frame: int | None = None
        self.elapsed_us = 0
        self.buttons = 0
        self.initialized = False
        self.events: list[dict] = []  # internal state transitions, not weapon/impact events
        self._queue = deque()
        self._active: dict[tuple[int, int], tuple[int, int] | None] = {}
        self._branches: dict[tuple[int, int], bool] = {}
        self._deadlines: dict[tuple[int, int], int] = {}
        self._diagnostic_keys: set[tuple] = set()
        self._entries: dict[int, list[int]] = {}
        self._steps = 0
        self._next_instance_id = 1
        self._by_index: dict[int, RuntimeInstance] = {}
        self._retired: set[int] = set()

    @property
    def entity_vars(self) -> dict[int, RuntimeValue]:
        return self.entity_store.values()

    @property
    def complete(self) -> bool:
        return self.initialized and not any(item.blocking for item in self.diagnostics)

    def initialize(self, frame: int | None = None, *, entries: dict[int, list[int]] | None = None) -> dict:
        if self.initialized:
            raise ValueError("initialize once; a new body lifecycle requires a new BodyRuntime")
        if frame is not None and (type(frame) is not int or not 0 <= frame <= 0xFFFFFFFF):
            raise ValueError("frame must be uint32")
        self.initialized = True
        self.frame = frame
        self._entries = entries or {}
        definitions = [(item, "initial") for item in self.definition.get("graphs", [])]
        manager = self.definition.get("manager")
        if manager is not None:
            definitions.append(({"graph": manager}, "weapon-manager"))
        definitions += [
            ({"graph": number}, f"weapon:{slot}")
            for slot, number in enumerate(self.definition.get("weapons", []))
            if number is not None
        ]
        # Register all root definitions before startup can allocate child instances.
        roots = [(self._instance(item["graph"], role), item) for item, role in definitions]
        for instance, definition in roots:
            if instance is not None:
                self._schema(instance, definition.get("m_1EB5A024"))
                self._start(instance)
                self._drain()
        self._stabilize()
        return self.snapshot()

    def advance(self, command) -> dict:
        if not self.initialized:
            raise ValueError("initialize before advance")
        newest = command.frame
        if type(newest) is not int or not 0 <= newest <= 0xFFFFFFFF:
            raise ValueError("command.frame must be uint32")
        if self.frame is not None:
            distance = (newest - self.frame) & 0xFFFFFFFF
            if distance == 0 or distance >= 0x80000000:
                return self.snapshot()
            if distance != 1:
                self._report(None, None, "command-gap", f"missing commands between {self.frame} and {newest}")
                return self.snapshot()
            self.elapsed_us += self.quanta_us
        self.frame = newest
        self.buttons = int(getattr(command, "buttons", 0))
        self.events = []
        self._steps = 0
        # Waits expire in stable instance/node order. No wall-clock reads occur here.
        for key, deadline in sorted(self._deadlines.items()):
            if deadline <= self.elapsed_us and key in self._active:
                instance, node = self._node(key)
                self._end(instance, node, finished=True)
        self._drain()
        self._stabilize()
        return self.snapshot()

    def set_variable(self, instance: int, identifier: int, value, *, scope: int = INSTANCE) -> None:
        self._store(self._get(instance), scope).write(identifier, value_of(value))

    def evaluate(self, value, instance: int) -> RuntimeValue:
        """Evaluate the supported value subset; unknown numeric opcodes raise Unsupported."""
        owner = self._get(instance)
        if isinstance(value, (bool, int, float, RuntimeValue)):
            return value_of(value)
        if not isinstance(value, dict):
            raise Unsupported(f"unsupported value {value!r}")
        cls = value.get("$")
        scalar = {"STUConfigVarBool": "bool", "STUConfigVarInt": "int", "STUConfigVarFloat": "float"}
        if cls in scalar:
            raw = value.get("m_value")
            if raw is None:
                raise Unsupported(f"{cls} has no value")
            if scalar[cls] == "bool":
                if raw not in (False, True, 0, 1):
                    raise Unsupported(f"invalid Bool {raw!r}")
                return RuntimeValue("bool", bool(raw))
            try:
                return RuntimeValue(scalar[cls], raw)
            except ValueError as error:
                raise Unsupported(str(error)) from error
        if cls == "STUConfigVarDynamic":
            identifier, scope = self._target(value)
            return self._store(owner, scope).read(identifier)
        if cls == "STU_91EFD5B1":  # explicit value-or-default wrapper in the extracted data
            try:
                return self.evaluate(value.get("m_value"), instance)
            except Unsupported as error:
                if "is unset" not in str(error):
                    raise
                return self.evaluate(value.get("m_default"), instance)
        if cls == "STU_919FD47C":
            return self._expression(value, owner)
        asset_fields = {
            "STUConfigVarIdentifier": "m_identifier",
            "STUConfigVarGameMessage": "m_gameMessage",
            "STUConfigVarContactSet": "m_contactSet",
            "STUConfigVarTexture": "m_texture",
            "STUConfigVarHardPoint": "m_hardPoint",
            "STUConfigVarLoadout": "m_loadout",
            "STU_8556841E": "m_entityDef",
        }
        if cls in asset_fields:
            number = guid(value.get(asset_fields[cls]))
            if number is not None:
                return RuntimeValue("asset", number)
        raise Unsupported(f"value class {cls!r}")

    def snapshot(self) -> dict:
        return {
            "body": self.body_guid,
            "generation": self.generation,
            "frame": self.frame,
            "elapsed_us": self.elapsed_us,
            "quanta_us": self.quanta_us,
            "complete": self.complete,
            "startup_policy": "explicit-entries-or-first-ordinary-entry (experimental)",
            "entity_vars": {key: value.snapshot() for key, value in self.entity_vars.items()},
            "instances": [
                {
                    "index": item.index,
                    "graph": item.graph.index,
                    "role": item.role,
                    "parent": item.parent,
                    "variables": {key: value.snapshot() for key, value in item.variables.items()},
                    "active": sorted(item.active),
                    "children": dict(sorted(item.children.items())),
                    "pending_entries": item.pending_entries[:],
                    "visited": sorted(item.visited),
                }
                for item in self.instances
            ],
            "diagnostics": [item.snapshot() for item in self.diagnostics],
            "events": [dict(event) for event in self.events],
        }

    def _expression(self, value: dict, owner: RuntimeInstance) -> RuntimeValue:
        expression = value.get("m_expression") or {}
        code = expression.get("m_opcodes") or []
        if self.expression_evaluator is None:
            raise Unsupported(f"unverified expression instructions {code}")
        return value_of(self.expression_evaluator(value, self, owner.index))

    def _get(self, index: int) -> RuntimeInstance:
        return self._by_index[index]

    def _node(self, key: tuple[int, int]) -> tuple[RuntimeInstance, Node]:
        instance = self._get(key[0])
        return instance, instance.graph.node(key[1])

    def _store(self, instance: RuntimeInstance, scope: int) -> Variables:
        if scope == INSTANCE:
            return instance.store
        if scope == ENTITY:
            return self.entity_store
        raise Unsupported(f"variable scope {scope}")

    @staticmethod
    def _target(value) -> tuple[int, int]:
        if not isinstance(value, dict) or value.get("$") != "STUConfigVarDynamic":
            raise Unsupported("assignment target is not a dynamic variable")
        identifier, scope = value.get("m_identifier"), value.get("m_60DB8F99")
        if type(identifier) is not int or type(scope) is not int or scope not in (INSTANCE, ENTITY):
            raise Unsupported(f"invalid variable target {identifier}/{scope}")
        return identifier, scope

    def _instance(self, number: int, role: str, parent=None) -> RuntimeInstance | None:
        metadata = self.graph_loader(number)
        if metadata is None:
            self._report(None, None, "missing-graph", f"graph {number:04X}")
            return None
        if self._next_instance_id > 0xFFFF:
            self._report(None, None, "instance-limit", "local instance id space exhausted")
            return None
        item = RuntimeInstance(self._next_instance_id, metadata, role, parent)
        self._next_instance_id += 1
        self.instances.append(item)
        self._by_index[item.index] = item
        return item

    def _schema(self, instance: RuntimeInstance, entries) -> None:
        for entry in entries or []:
            identifier = guid(entry.get("m_0D09D2D9"))
            try:
                if identifier is None:
                    raise Unsupported("schema override has no identifier")
                result = self.evaluate(entry.get("m_value"), instance.index)
                instance.store.write(identifier & 0xFFFFFFFF, result)
            except Unsupported as error:
                self._report(instance, None, "schema", str(error))

    def _start(self, instance: RuntimeInstance) -> None:
        ordinary = [node for node in instance.graph.entries if node and node.cls == "STUStatescriptEntry"]
        selected = self._entries.get(instance.graph.index)
        if selected is None:
            selected = [ordinary[0].pos] if ordinary else []
        instance.pending_entries = [
            node.pos for node in instance.graph.entries if node and node.pos not in selected
        ]
        for position in reversed(selected):
            node = instance.graph.node(position)
            if node is None or node.cls != "STUStatescriptEntry":
                self._report(instance, node, "entry", f"ordinary Entry {position} is unavailable")
            else:
                self._queue.appendleft((instance.index, node.pos, "entry", None, (f"entry:{position}",)))

    def _report(self, instance, node, feature: str, message: str, path=()) -> None:
        key = (instance.index if instance else 0, node.pos if node else None, feature, message)
        if key not in self._diagnostic_keys:
            self._diagnostic_keys.add(key)
            self.diagnostics.append(
                Diagnostic(
                    key[0], instance.graph.index if instance else 0, key[1], feature, message, tuple(path)
                )
            )

    def _plugs(self, instance, node, names, parent, path) -> None:
        work = []
        for name in names:
            for position, plug in node.links(name):
                if position is None:
                    self._report(instance, node, "missing-link", name, path)
                else:
                    work.append((instance.index, position, plug, parent, (*path, f"{node.pos}.{name}")))
        self._queue.extendleft(reversed(work))

    def _drain(self) -> None:
        while self._queue:
            index, position, plug, parent, path = self._queue.popleft()
            if index in self._retired:
                continue
            instance = self._get(index)
            node = instance.graph.node(position)
            self._steps += 1
            if self._steps > self.max_steps:
                self._report(instance, node, "step-budget", "possible graph cycle", path)
                self._queue.clear()
                break
            if node is None:
                self._report(instance, None, "missing-node", f"node {position}", path)
                continue
            if node.client_only:
                continue
            instance.visited.add((position, plug))
            try:
                self._dispatch(instance, node, plug, parent, path)
            except Unsupported as error:
                self._report(instance, node, node.cls, str(error), path)

    def _dispatch(self, instance, node, plug, parent, path) -> None:
        if plug == "m_F198FD3A" and node.is_state:
            self._end(instance, node)
            return
        if node.cls == "STUStatescriptEntry" and plug == "entry":
            self._plugs(instance, node, ["m_outPlug"], parent, path)
            return
        if node.cls == "STUStatescriptActionSetVar" and plug == "m_inPlug":
            if node.fields.get("m_index") is not None or node.fields.get("m_key") is not None:
                raise Unsupported("indexed SetVar")
            identifier, scope = self._target(node.fields.get("m_out_Var"))
            result = self.evaluate(node.fields.get("m_value"), instance.index)
            self._store(instance, scope).write(identifier, result)
            self._plugs(instance, node, ["m_outPlug"], parent, path)
            return
        if plug != "m_beginPlug" or not node.is_state:
            raise Unsupported(f"node/input {node.cls}.{plug}")
        if type(node.state) is not int:
            raise Unsupported("state has no m_states index")
        if (instance.index, node.pos) in self._active:
            return
        if any(node.fields.get(key) for key in ("m_stateGroup", "m_transitionPlug", "m_4F8F2F3F")):
            raise Unsupported("state group/transition semantics")
        names = ["m_onBeginPlug", "m_subgraphPlug"]
        if node.cls == "STUStatescriptStateStack":
            if node.fields.get("m_91FED9B3", 0):
                raise Unsupported("nondefault Stack mode")
            if node.links("m_topSubgraphPlug") or node.links("m_underSubgraphPlug"):
                raise Unsupported("Stack priority subgraphs")
            identifier, scope = self._target(node.fields.get("m_out_Var"))
            result = self.evaluate(node.fields.get("m_value"), instance.index)
            self._store(instance, scope).push(identifier, (instance.index, node.pos), result)
        elif node.cls == "STUStatescriptStateBooleanSwitch":
            branch = bool(self.evaluate(node.fields.get("m_condition"), instance.index).value)
            self._branches[(instance.index, node.pos)] = branch
            names += ["m_onTruePlug" if branch else "m_onFalsePlug"]
            names += ["m_trueSubgraphPlug" if branch else "m_falseSubgraphPlug"]
        elif node.cls == "STUStatescriptStateWait":
            if node.fields.get("m_0DBAFD7F", 0) or node.fields.get("m_BF1A93B0", 0):
                raise Unsupported("nondefault Wait flags")
            duration = self.evaluate(node.fields.get("m_timeout"), instance.index).value
            if isinstance(duration, bool) or duration < 0:
                raise Unsupported("invalid Wait duration")
            self._deadlines[(instance.index, node.pos)] = self.elapsed_us + round(duration * 1_000_000)
        elif node.cls == "STU_37D754C8":
            self._subscript(instance, node)
        elif node.cls != "STUStatescriptState":
            raise Unsupported(f"state class {node.cls}")
        self._active[(instance.index, node.pos)] = parent
        instance.active.add(node.state)
        self._event(instance, node, "state-begin")
        self._plugs(instance, node, names, (instance.index, node.pos), path)

    def _subscript(self, instance: RuntimeInstance, node: Node) -> None:
        if node.fields.get("m_D02E43DC", 0) or node.fields.get("m_24AB04BB", 0):
            raise Unsupported("nondefault SubScript flags")
        schema = node.field("m_36F7B64C.m_36F7B64C")
        if (
            not isinstance(schema, dict)
            or schema.get("m_17BB822A")
            or schema.get("m_CB2634D7")
            or schema.get("m_75408640", 0)
        ):
            raise Unsupported("dynamic SubScript/schema overrides")
        number = guid(schema.get("m_C974E369", {}).get("m_graph"))
        if number is None:
            raise Unsupported("SubScript has no static graph")
        child = self._instance(number & 0xFFFF, "subscript", (instance.index, node.state))
        if child is None:
            raise Unsupported("SubScript graph is missing")
        instance.children[node.state] = child.index
        self._start(child)

    def _end(self, instance: RuntimeInstance, node: Node, *, finished: bool = False) -> None:
        key = (instance.index, node.pos)
        if key not in self._active:
            return
        for child_key, parent in list(self._active.items()):
            if parent == key:
                child_instance, child_node = self._node(child_key)
                self._end(child_instance, child_node)
        child_id = instance.children.pop(node.state, None)
        if child_id is not None:
            child_instance = self._get(child_id)
            for child_node in child_instance.graph.nodes:
                self._end(child_instance, child_node)
            self._retired.add(child_id)
            self.instances.remove(child_instance)
        instance.store.pop(key)
        self.entity_store.pop(key)
        self._active.pop(key, None)
        self._branches.pop(key, None)
        self._deadlines.pop(key, None)
        instance.active.discard(node.state)
        self._event(instance, node, "state-end")
        names = ["m_onEndPlug"]
        if node.cls == "STUStatescriptStateWait":
            names.append("m_onFinishedPlug" if finished else "m_onAbortPlug")
        self._plugs(instance, node, names, None, (f"end:{node.pos}",))

    def _refresh_switches(self) -> None:
        for key, previous in sorted(self._branches.items()):
            if key not in self._active:
                continue
            instance, node = self._node(key)
            try:
                current = bool(self.evaluate(node.fields.get("m_condition"), instance.index).value)
            except Unsupported as error:
                self._report(instance, node, "switch-condition", str(error))
                continue
            if current != previous:
                # A branch change ends its scoped children before beginning the other branch.
                for child_key, parent in list(self._active.items()):
                    if parent == key:
                        child_instance, child_node = self._node(child_key)
                        self._end(child_instance, child_node)
                self._branches[key] = current
                names = ["m_onTruePlug" if current else "m_onFalsePlug"]
                names.append("m_trueSubgraphPlug" if current else "m_falseSubgraphPlug")
                self._plugs(instance, node, names, key, (f"switch:{node.pos}",))

    def _stabilize(self) -> None:
        """Propagate supported variable bindings and branch changes to a bounded fixed point."""
        for _pass in range(self.max_steps):
            before = self._state_key()
            for key in sorted(self._active):
                instance, node = self._node(key)
                if node.cls == "STUStatescriptStateStack":
                    try:
                        identifier, scope = self._target(node.fields.get("m_out_Var"))
                        result = self.evaluate(node.fields.get("m_value"), instance.index)
                        self._store(instance, scope).push(identifier, key, result)
                    except Unsupported as error:
                        self._report(instance, node, "stack-binding", str(error))
            self._refresh_switches()
            self._drain()
            if self._state_key() == before:
                return
            if self._steps > self.max_steps:
                return
        self._report(None, None, "stabilization-budget", "variable/branch cycle")

    def _state_key(self) -> tuple:
        return (
            tuple(self.entity_vars.items()),
            tuple(
                (item.index, tuple(item.variables.items()), tuple(sorted(item.active)))
                for item in self.instances
            ),
            tuple(sorted(self._branches.items())),
        )

    def _event(self, instance: RuntimeInstance, node: Node, kind: str) -> None:
        self.events.append(
            {
                "id": (self.generation, self.frame, instance.index, node.pos, len(self.events)),
                "kind": kind,
                "instance": instance.index,
                "node": node.pos,
                "elapsed_us": self.elapsed_us,
            }
        )
