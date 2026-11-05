"""Validate runtime snapshots before encoding owner statescript frames.

Graph state indexes, owner bit slots and variable presence slots are different
namespaces. This adapter uses graph metadata for each mapping. Native state
payloads still require an explicitly supplied codec result; an unknown active
state must not silently become a payload-free state on the wire.
"""

import math
import struct
from dataclasses import dataclass

from ow174.game.bits import BitWriter
from ow174.game.script.research_runtime import RuntimeValue
from ow174.game.statescript import (
    Asset,
    Bool,
    Entity,
    Float,
    Graph,
    Instance,
    Int,
    Value,
    owner_full_frame,
    subscript,
)

SUBSCRIPT_CLASSES = {"STUStatescriptStateSubScript", "STU_37D754C8"}
# 37D754C8 is the 1.74 extracted class of 13C1 state 43: the already-used
# controller -> 288A SubScript relationship (heroselect.py).


class ReplicationError(ValueError):
    """The runtime cannot yet produce a complete, unambiguous owner frame."""


def wire_value(value) -> Value:
    """Scalars retain their kind; assets/entities must use explicit Value wrappers."""
    if isinstance(value, RuntimeValue):
        kind, raw = value.kind, value.value
        if kind == "asset" and type(raw) is int and 0 <= raw < 1 << 64:
            return Asset(raw)
        if kind == "entity" and type(raw) is int and 0 <= raw < 1 << 32:
            return Entity(raw)
        if kind == "float" and type(raw) in (int, float):
            return wire_value(float(raw))
        scalar = {"bool": bool, "int": int}
        if kind in scalar and type(raw) is scalar[kind]:
            return wire_value(raw)
        raise ReplicationError(f"Unsupported runtime value: {value!r}")
    if isinstance(value, Value):
        if isinstance(value, Float):
            wire_value(float(value.value))  # includes finite/float32 range validation
        elif isinstance(value, Asset):
            if type(value.guid) is not int or not 0 <= value.guid < 1 << 64:
                raise ReplicationError("Asset must be a uint64 GUID")
        elif isinstance(value, Entity) and (type(value.entity) is not int or not 0 <= value.entity < 1 << 32):
            raise ReplicationError("Entity must be a uint32 identifier")
        return value
    if isinstance(value, bool):
        return Bool(value)
    if isinstance(value, int) and -(1 << 31) <= value < 1 << 32:
        return Int(value)
    if isinstance(value, float) and math.isfinite(value):
        try:
            struct.pack("<f", value)
        except OverflowError as error:
            raise ReplicationError("Float is outside the client's float32 range") from error
        return Float(value)
    raise ReplicationError(f"Value needs a supported, explicit wire type: {value!r}")


def _variables(values: dict) -> dict[int, Value]:
    result = {}
    for identifier, value in sorted(values.items()):
        if not isinstance(identifier, int) or not 0 < identifier < 1 << 32:
            raise ReplicationError(f"Invalid variable identifier: {identifier!r}")
        result[identifier] = wire_value(value)
    return result


@dataclass
class OwnerReplica:
    instances: list[Instance]
    entity_vars: dict[int, Value]

    def frame(self, cmfd: int) -> BitWriter:
        return owner_full_frame(cmfd, self.instances, self.entity_vars)


def from_runtime(runtime, *, state_payloads: dict[tuple[int, int], BitWriter | None] | None = None):
    """Build a replica only after initialization has no unresolved runtime paths.

    `state_payloads[(graph, raw_state)]` must explicitly describe a native active
    state's payload, including None when its verified format has no payload.
    SubScript payloads come from the runtime's child-instance relationships.
    This validates layouts; acceptance by the real client is a separate test.
    """
    snapshot = runtime.snapshot()
    if not snapshot["complete"]:
        count = len(snapshot.get("diagnostics", ()))
        raise ReplicationError(f"Runtime initialization has {count} unresolved diagnostics")
    payloads = state_payloads or {}
    source = sorted(runtime.instances, key=lambda instance: instance.index)
    by_id = {instance.index: instance for instance in source}
    if len(by_id) != len(source) or any(not 0 < index < 1 << 16 for index in by_id):
        raise ReplicationError("Graph instance IDs must be distinct positive uint16 values")
    replicas = []
    for instance in source:
        metadata = instance.graph
        if not 0 <= metadata.index < 1 << 16:
            raise ReplicationError(f"Invalid graph index: {metadata.index}")
        if instance.parent is not None:
            parent_id, state_index = instance.parent
            parent = by_id.get(parent_id)
            if parent is None or parent.graph.state(state_index) is None:
                raise ReplicationError(f"Missing parent instance/state for instance {instance.index}")
            visited = {instance.index}
            ancestor = parent
            while ancestor is not None:
                if ancestor.index in visited:
                    raise ReplicationError("Graph instance parent cycle")
                visited.add(ancestor.index)
                ancestor = by_id.get(ancestor.parent[0]) if ancestor.parent is not None else None
        variables = _variables(instance.variables)
        presence, extra = {}, {}
        for identifier, value in variables.items():
            bit = metadata.presence_bit(identifier)
            if bit is None:
                extra[identifier] = value
            else:
                presence[bit] = value
        active = {}
        for state_index in sorted(instance.active):
            node = metadata.state(state_index)
            if node is None:
                raise ReplicationError(f"Unknown state {state_index} in graph {metadata.index:04X}")
            slot = metadata.owner_slot(state_index)
            if slot is None:
                continue  # client-only and server-only states have no owner state bit
            if node.cls in SUBSCRIPT_CLASSES:
                child = instance.children.get(state_index)
                child_id = child.index if hasattr(child, "index") else child
                if child_id not in by_id or by_id[child_id].parent != (instance.index, state_index):
                    raise ReplicationError(f"Missing SubScript child for state {state_index}")
                active[slot] = subscript(child_id)
            else:
                key = (metadata.index, state_index)
                if key not in payloads:
                    raise ReplicationError(
                        f"Unverified state payload: graph {metadata.index:04X}, "
                        f"state {state_index}, {node.cls}"
                    )
                payload = payloads[key]
                if payload is not None and not isinstance(payload, BitWriter):
                    raise ReplicationError(f"Invalid state payload: {key}")
                active[slot] = payload
        layout = Graph(metadata.index, len(metadata.owner_states()), len(metadata.presence_vars()))
        replicas.append(Instance(instance.index, layout, instance.parent, presence, extra, active))
    return OwnerReplica(replicas, _variables(runtime.entity_vars))
