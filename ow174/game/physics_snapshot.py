"""Read-only stored Mover/CharacterBody/contact snapshots; no restoration or physics execution.

Static assets use the collision fingerprint's first-seen reference numbering. An explicitly supplied
descriptor delegates full static-content validation to the caller's surrounding capture interval.
"""

import math
import struct
from collections import deque

from ow174.game import collision, contacts, replay_values
from ow174.game.collision_fingerprint import (
    GEOMETRY_FIELDS,
    MESH_FIELDS,
    SHAPE_FIELDS,
    TRIANGLE_FIELDS,
    WORLD_FIELDS,
    CollisionFingerprintError,
    world_fingerprint,
)
from ow174.game.collision_fingerprint import SCHEMA as WORLD_SCHEMA
from ow174.game.commands import Command
from ow174.game.mover import Mod, Mover, MoverData, Snapshot

SCHEMA = "ow174.physics-snapshot.v1"
SCOPE = "stored-mover-physics-only"
MOVER_FIELDS = (
    "data", "values", "position", "on_map", "world", "category", "velocity", "driven", "yaw", "pitch",
    "flags_word", "air_ticks", "spring", "spring2", "spring_steps", "spring_speed", "fall", "landed",
    "crouch_end", "throttles", "input_throttles", "input_tick", "input_frame", "last_command", "mods",
    "history", "corrected", "body", "slope", "slope_cos", "slope_tan", "slope_cos32", "spring_low",
    "spring_high",
)
DATA_FIELDS = (
    "stand_height", "stand_radius", "stand_pogo", "crouch_height", "crouch_radius", "crouch_pogo",
    "ground_circle_radius", "ground_circle_rays", "pogo_frequency", "pogo_damping", "max_slope",
    "step_down", "run_forward", "run_backward", "run_strafe", "crouch_forward", "crouch_backward",
    "crouch_strafe", "accel_ground", "accel_air", "accel_air_touching", "decel_ground_base",
    "decel_ground_base_speed", "decel_ground_per_speed", "decel_air_base", "decel_air_base_speed",
    "decel_air_per_speed", "jump_speed", "gravity_scale", "terminal_velocity", "terminal_velocity_up",
    "ground_gravity_accel", "ground_gravity_decel", "character_collision",
)
BODY_FIELDS = ("radius", "top", "capsule", "world", "category", "position", "aabb", "fat", "moved",
               "contacts", "budget")
MESH_CONTACT_FIELDS = ("shape", "mesh", "category", "box", "candidates")
CONVEX_CONTACT_FIELDS = ("shape", "cache", "first", "sign", "memo")
OBJECT_FIELDS = {
    Mover: MOVER_FIELDS,
    MoverData: DATA_FIELDS,
    Mod: ("index", "op", "value", "start", "end", "priority", "key"),
    Command: ("frame", "forward", "right", "yaw", "pitch", "buttons", "action", "extra", "subframe",
              "click_yaw", "click_pitch"),
    Snapshot: ("frame", "position", "velocity", "yaw", "pitch", "flags", "air_ticks", "gravity",
               "throttles", "input_frame", "input_throttles", "crouch_end", "spring", "spring_speed", "fall"),
    contacts.CharacterBody: BODY_FIELDS,
    contacts.MeshContact: MESH_CONTACT_FIELDS,
    contacts.ConvexContact: CONVEX_CONTACT_FIELDS,
    contacts.Capsule: ("a", "b", "mask", "radius"),
    contacts.Manifold: ("gap", "normal", "points"),
    contacts.Point: ("id", "position", "separation"),
    contacts.Constraint: ("cap", "impulse", "normal", "offset", "soft"),
    contacts.Distance: ("cache", "distance", "exit", "iterations", "lower", "normal", "point_a", "point_b"),
    contacts.Triangle: tuple(sorted(TRIANGLE_FIELDS)),
    collision.Ground: ("mask", "normal", "plane", "surface", "y"),
}
MAX_NODES = 100_000


class PhysicsSnapshotError(ValueError):
    """An unsupported field, value, class or external reference prevents a complete snapshot."""


def _stored(value, cls, fields):
    if type(value) is not cls:
        raise PhysicsSnapshotError(f"unsupported physics class; expected {cls.__name__}")
    try:
        stored = object.__getattribute__(value, "__dict__")
    except AttributeError:
        if set(cls.__slots__) != set(fields):
            raise PhysicsSnapshotError(f"unsupported slots on {cls.__name__}") from None
        try:
            return {name: object.__getattribute__(value, name) for name in fields}
        except AttributeError:
            raise PhysicsSnapshotError(f"uninitialized physics fields on {cls.__name__}") from None
    if type(stored) is not dict or set(stored) != set(fields):
        raise PhysicsSnapshotError(f"unsupported fields on {cls.__name__}")
    return {name: stored[name] for name in fields}


def _descriptor(value):
    fields = {"schema", "sha256", "model_sha256", "cache_version"}
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != fields:
        raise PhysicsSnapshotError("invalid World descriptor fields")
    if type(value["schema"]) is not str or value["schema"] != WORLD_SCHEMA:
        raise PhysicsSnapshotError("unsupported World descriptor schema")
    if type(value["cache_version"]) is not int or value["cache_version"] != collision.VERSION:
        raise PhysicsSnapshotError("unsupported World descriptor version")
    for name in ("sha256", "model_sha256"):
        text = value[name]
        if type(text) is not str or len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise PhysicsSnapshotError("invalid World descriptor digest")
    return {name: value[name] for name in ("schema", "sha256", "model_sha256", "cache_version")}


class _StaticReferences:
    def __init__(self, world):
        _stored(world, collision.World, WORLD_FIELDS)
        engine = world.engine
        _stored(engine, contacts.StaticWorld, ("shapes", "huge", "grid"))
        if type(engine.shapes) is not list:
            raise PhysicsSnapshotError("unsupported static shape sequence")
        self.world, self.engine = world, engine
        self.refs = {id(world): (world, {"static": "world"}),
                     id(engine): (engine, {"static": "engine"})}
        self.geometry = {}
        shape_count = 0
        for shape in engine.shapes:
            if id(shape) in self.refs:
                if self.refs[id(shape)][0] is not shape:
                    raise PhysicsSnapshotError("ambiguous static identity")
                continue
            _stored(shape, contacts.StaticShape, SHAPE_FIELDS)
            self.refs[id(shape)] = (shape, {"static": "shape", "index": shape_count})
            shape_count += 1
            geometry = shape.geometry
            if id(geometry) not in self.geometry:
                cls = type(geometry)
                fields = MESH_FIELDS if cls is contacts.MeshShape else GEOMETRY_FIELDS.get(cls)
                if fields is None:
                    raise PhysicsSnapshotError("unsupported static geometry type")
                _stored(geometry, cls, fields)
                number = len(self.geometry)
                self.geometry[id(geometry)] = (geometry, number)
                self.refs[id(geometry)] = (geometry, {"static": "geometry", "index": number})
                if cls is contacts.MeshShape:
                    if type(geometry.cache) is not dict:
                        raise PhysicsSnapshotError("unsupported static triangle cache")
                    for index, triangle in geometry.cache.items():
                        if type(index) is not int or not 0 <= index < len(geometry.masks):
                            raise PhysicsSnapshotError("invalid cached static triangle index")
                        _stored(triangle, contacts.Triangle, TRIANGLE_FIELDS)
                        existing = self.refs.get(id(triangle))
                        reference = {"static": "triangle", "geometry": number, "index": index}
                        if existing is not None and existing[1] != reference:
                            raise PhysicsSnapshotError("one triangle aliases different static indices")
                        self.refs[id(triangle)] = (triangle, reference)

    def reference(self, value):
        entry = self.refs.get(id(value))
        if entry is not None and entry[0] is value:
            return dict(entry[1])
        return None

    def require_shape(self, value):
        reference = self.reference(value)
        if reference is None or reference["static"] != "shape":
            raise PhysicsSnapshotError("contact references a shape outside the mover's World")

    def candidate_triangles(self, contact):
        self.require_shape(contact.shape)
        if contact.mesh is not contact.shape.geometry or type(contact.mesh) is not contacts.MeshShape:
            raise PhysicsSnapshotError("mesh contact does not reference its static mesh")
        reference = self.reference(contact.mesh)
        if reference is None or reference["static"] != "geometry":
            raise PhysicsSnapshotError("contact references unlisted static geometry")
        if type(contact.candidates) is not list:
            raise PhysicsSnapshotError("unsupported mesh candidates")
        result = []
        for entry in contact.candidates:
            if type(entry) is not list or len(entry) not in (2, 5):
                raise PhysicsSnapshotError("unsupported mesh candidate layout")
            number = entry[0]
            if type(number) is not int or not 0 <= number < len(contact.mesh.masks):
                raise PhysicsSnapshotError("mesh candidate references an invalid triangle")
            result.append({"static": "triangle", "geometry": reference["index"], "index": number})
        return result


class _Graph:
    def __init__(self, static):
        self.static = static
        self.nodes = []
        self.identities = {}

    def value(self, value):
        cls = type(value)
        if value is None or cls in (bool, int, float, str, bytes):
            return value
        reference = self.static.reference(value)
        if reference is not None:
            return reference
        known = self.identities.get(id(value))
        if known is not None:
            if known[0] is not value:
                raise PhysicsSnapshotError("ambiguous physics object identity")
            return {"node": known[1]}
        if len(self.nodes) >= MAX_NODES:
            raise PhysicsSnapshotError("physics snapshot exceeds node budget")
        if cls not in OBJECT_FIELDS and cls not in (dict, list, tuple, deque):
            raise PhysicsSnapshotError("unsupported physics value type or foreign static reference")
        number = len(self.nodes)
        self.identities[id(value)] = (value, number)
        node = {"class": cls.__name__}
        self.nodes.append(node)
        if cls is dict:
            node["entries"] = [[self.value(key), self.value(nested)] for key, nested in value.items()]
        elif cls in (list, tuple, deque):
            node["items"] = [self.value(item) for item in value]
            if cls is deque:
                node["maxlen"] = value.maxlen
        else:
            fields = _stored(value, cls, OBJECT_FIELDS[cls])
            if cls is contacts.CharacterBody:
                if fields["world"] is not None and fields["world"] is not self.static.engine:
                    raise PhysicsSnapshotError("CharacterBody references a different static World")
                if type(fields["contacts"]) is not list:
                    raise PhysicsSnapshotError("unsupported CharacterBody contact sequence")
                if any(type(c) not in (contacts.MeshContact, contacts.ConvexContact)
                       for c in fields["contacts"]):
                    raise PhysicsSnapshotError("unsupported CharacterBody contact type")
            elif cls is contacts.MeshContact:
                node["candidate_triangles"] = self.static.candidate_triangles(value)
                node["class_values"] = {"sign": contacts.MeshContact.sign}
            elif cls is contacts.ConvexContact:
                self.static.require_shape(fields["shape"])
            elif cls is contacts.Capsule:
                node["class_values"] = {"kind": contacts.Capsule.kind}
            node["fields"] = [[name, self.field_value(cls, name, nested)] for name, nested in fields.items()]
        return {"node": number}

    def field_value(self, cls, name, value):
        # gjk(reach=...) explicitly returns +inf for this field when the shapes are beyond reach.
        # It normally stays local to narrowphase. Preserve the known sentinel if a stored graph has it.
        if cls is contacts.Distance and name == "distance" and type(value) is float and value == math.inf:
            return {"ieee754_sentinel": "gjk.distance.beyond_reach",
                    "float64_bits": struct.pack(">d", value).hex()}
        return self.value(value)


def _capture_physics(value, world, scope, world_descriptor):
    try:
        static = _StaticReferences(world)
        descriptor = _descriptor(world_fingerprint(world) if world_descriptor is None else world_descriptor)
        graph = _Graph(static)
        root = graph.value(value)
        state = replay_values.encode({"root": root, "nodes": graph.nodes})
        document = {"schema": SCHEMA, "scope": scope, "world_descriptor": descriptor,
                    "world_validation": "fingerprinted" if world_descriptor is None else "caller_supplied",
                    "state": state}
        identities = tuple(sorted(((number, obj) for obj, number in graph.identities.values()),
                                  key=lambda item: item[0]))
        return document, identities
    except PhysicsSnapshotError:
        raise
    except (CollisionFingerprintError, AttributeError, IndexError, KeyError, TypeError, ValueError,
            OverflowError, RecursionError) as error:
        raise PhysicsSnapshotError(f"cannot snapshot stored physics: {type(error).__name__}") from error


def _capture_mover(mover, world_descriptor):
    world = _stored(mover, Mover, MOVER_FIELDS)["world"]
    return _capture_physics(mover, world, SCOPE, world_descriptor)


def snapshot_character_body_with_identities(body, world, *, world_descriptor=None) -> tuple:
    """Capture the same contact state for a body driven outside the player mover."""
    _stored(body, contacts.CharacterBody, BODY_FIELDS)
    return _capture_physics(body, world, "stored-character-body-only", world_descriptor)


def snapshot_mover(mover, *, world_descriptor=None) -> dict:
    """Export all supported stored physics fields without running or restoring the simulation.

    Explicit descriptors are trusted only as asset identities. The caller must validate the actual
    World against that descriptor around its complete capture interval. This function still checks
    static reference closure and exact supported object schemas on each invocation.
    """
    return _capture_mover(mover, world_descriptor)[0]


def snapshot_mover_with_identities(mover, *, world_descriptor=None) -> tuple:
    """Return the same document plus ephemeral (local node id, original object) identity references.

    The document and identity table come from a single capture. References are not a serialized
    artifact and must not be persisted as replay values; a surrounding exporter can use them to
    reject unsupported shared mutable state across independently captured Mover artifacts.
    """
    return _capture_mover(mover, world_descriptor)
