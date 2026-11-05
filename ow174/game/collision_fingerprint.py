"""Explicit collision-asset identities, independent of cache files and query-cache occupancy.

This is an opt-in research boundary, not a complete match-state snapshot. Callers must not mutate a
World concurrently. Lazy query caches are checked against their source data without warming them.
"""

import hashlib
import math
import re
import struct
import sys
from _thread import LockType
from array import array
from collections import OrderedDict

from ow174.game import collision, contacts
from ow174.paths import ROOT

SCHEMA = "ow174.collision-world.v1"
MODEL_FILES = (
    "ow174/game/collision.py", "ow174/game/contacts.py", "ow174/game/props.py",
    "ow174/game/collision_fingerprint.py",
)
WORLD_FIELDS = frozenset({
    "name", "data", "masks", "surfaces", "grid", "columns", "large", "lock", "lowest",
    "large_tuples", "engine",
})
MESH_FIELDS = frozenset({
    "vertices", "raw", "scale", "inverse_scale", "mask", "cache", "boxes", "corners", "masks",
    "surfaces", "bounds", "large", "grid", "low", "high",
})
SHAPE_FIELDS = frozenset({
    "absolute", "fat_high", "fat_low", "frame", "geometry", "inverse", "kind", "mask", "matrix", "order",
})
TRIANGLE_FIELDS = frozenset({
    "bottom", "corners", "has", "index", "mask", "normal", "sides", "surface", "top", "wings",
})
GEOMETRY_FIELDS = {
    contacts.Hull: frozenset({"centroid", "vertices", "planes", "faces", "edges", "mask"}),
    contacts.Sphere: frozenset({"centre", "radius", "mask"}),
    contacts.Capsule: frozenset({"a", "b", "radius", "mask"}),
}
DESCRIPTOR_FIELDS = frozenset({"schema", "sha256", "model_sha256", "cache_version"})


class CollisionFingerprintError(RuntimeError):
    pass


def _fail(reason):
    raise CollisionFingerprintError(reason)


def _fields(value, cls, names):
    if type(value) is not cls:
        _fail(f"unsupported collision class; expected {cls.__name__}")
    actual = set(vars(value)) if hasattr(value, "__dict__") else set(cls.__slots__)
    if actual != names:
        _fail(f"unsupported fields on {cls.__name__}")
    return {name: getattr(value, name) for name in sorted(names)}


def _emit(hasher, value):
    """Typed, length-delimited encoding; neither repr nor native object addresses enter the hash."""
    kind = type(value)
    if value is None:
        hasher.update(b"n")
    elif kind is bool:
        hasher.update(b"t" if value else b"f")
    elif kind is int:
        raw = str(value).encode("ascii")
        hasher.update(b"i" + str(len(raw)).encode("ascii") + b":" + raw)
    elif kind is float:
        if not math.isfinite(value):
            _fail("nonfinite collision number")
        hasher.update(b"d" + struct.pack("<d", value))
    elif kind is str:
        raw = value.encode("utf-8")
        hasher.update(b"s" + str(len(raw)).encode("ascii") + b":" + raw)
    elif kind is bytes:
        hasher.update(b"b" + str(len(value)).encode("ascii") + b":" + value)
    elif kind is array:
        if value.typecode not in "bBhHiIlLqQfd":
            _fail("unsupported collision array type")
        if value.typecode in "fd" and any(not math.isfinite(item) for item in value):
            _fail("nonfinite collision array")
        canonical = value
        if sys.byteorder != "little" and value.itemsize > 1:
            canonical = array(value.typecode, value)
            canonical.byteswap()
        hasher.update(b"a")
        _emit(hasher, (value.typecode, value.itemsize, len(value)))
        hasher.update(canonical.tobytes())
    elif kind in (tuple, list):
        hasher.update((b"(" if kind is tuple else b"[") + str(len(value)).encode("ascii") + b":")
        for item in value:
            _emit(hasher, item)
    elif kind is dict:
        if any(type(key) is not str for key in value):
            _fail("collision record keys must be strings")
        hasher.update(b"{" + str(len(value)).encode("ascii") + b":")
        for key in sorted(value):
            _emit(hasher, key)
            _emit(hasher, value[key])
    else:
        _fail("unsupported collision value type")


def _digest(value):
    hasher = hashlib.sha256()
    _emit(hasher, value)
    return hasher.hexdigest()


def _same(actual, expected):
    """Compare cache entries without encoding and hashing both copies."""
    kind = type(expected)
    if type(actual) is not kind:
        return False
    if kind is float:
        return (
            math.isfinite(actual) and actual == expected
            and (actual != 0.0 or math.copysign(1.0, actual) == math.copysign(1.0, expected))
        )
    if kind in (list, tuple):
        return len(actual) == len(expected) and all(
            _same(a, b) for a, b in zip(actual, expected, strict=True)
        )
    if kind is dict:
        return (
            all(type(key) is str for key in actual) and actual.keys() == expected.keys()
            and all(type(key) is str and _same(actual[key], value) for key, value in expected.items())
        )
    if kind in (bool, int, type(None)):
        return actual == expected
    return _digest(actual) == _digest(expected)


def _array(value, typecode):
    if type(value) is not array or value.typecode != typecode:
        _fail("unsupported collision array layout")


def _index(value, count):
    if type(value) is not int or not 0 <= value < count:
        _fail("invalid collision reference")
    return value


def _cell(value):
    if type(value) is not tuple or len(value) != 2 or any(type(item) is not int for item in value):
        _fail("invalid collision grid key")
    return value


def _grid(value, resolve):
    if type(value) is not dict:
        _fail("unsupported collision grid")
    return [(_cell(cell), resolve(value[cell])) for cell in sorted(value, key=_cell)]


def _triangle(world, number):
    number = _index(number, len(world.masks))
    at = 13 * number
    ax, ay, az, bx, by, bz, cx, cy, cz, nx, ny, nz, d = world.data[at:at + 13]
    return (
        min(ax, bx, cx), max(ax, bx, cx), min(ay, by, cy), max(ay, by, cy), min(az, bz, cz),
        max(az, bz, cz), ax, ay, az, bx, by, bz, cx, cy, cz, nx, ny, nz, d,
        world.masks[number], world.surfaces[number], number,
    )


def _world_columns(world):
    if type(world.columns) is not OrderedDict:
        _fail("unsupported collision column cache")
    if type(world.lock) is not LockType:
        _fail("unsupported collision cache lock")
    # Refuse concurrent observation instead of blocking a caller that already holds a server lock.
    if not world.lock.acquire(blocking=False):
        _fail("collision asset is being queried concurrently")
    try:
        columns = list(world.columns.items())
    finally:
        world.lock.release()
    for cell, actual in columns:
        _cell(cell)
        expected = [_triangle(world, number) for number in world.grid.get(cell, ())] + world.large_tuples
        if not _same(actual, expected):
            _fail("collision column cache disagrees with static data")


def _mesh_triangle(mesh, number):
    number = _index(number, len(mesh.masks))
    a, b, c, w0, w1, w2 = mesh.corners[6 * number:6 * number + 6]
    corners = tuple(mesh.vertices[_index(index, len(mesh.vertices))] for index in (a, b, c))
    wings = tuple(mesh.vertices[_index(wing, len(mesh.vertices))] if wing >= 0 else corners[(k + 2) % 3]
                  for k, wing in enumerate((w0, w1, w2)))
    return contacts.Triangle(corners, wings, (w0 >= 0, w1 >= 0, w2 >= 0), number,
                             mesh.surfaces[number], mesh.masks[number])


def _mesh_caches(mesh):
    if type(mesh.cache) is not dict or type(mesh.boxes) is not dict:
        _fail("unsupported mesh query cache")
    for number, triangle in list(mesh.cache.items()):
        actual = _fields(triangle, contacts.Triangle, TRIANGLE_FIELDS)
        expected_triangle = _mesh_triangle(mesh, number)
        expected = _fields(expected_triangle, contacts.Triangle, TRIANGLE_FIELDS)
        if triangle.sides is not None:
            expected["sides"] = expected_triangle.neighbour_normals()  # mutates the new temporary only
        if not _same(actual, expected):
            _fail("mesh triangle cache disagrees with static data")
    for number, actual in list(mesh.boxes.items()):
        number = _index(number, len(mesh.masks))
        indices = mesh.corners[6 * number:6 * number + 3]
        corners = [mesh.vertices[_index(index, len(mesh.vertices))] for index in indices]
        if not _same(actual, contacts.box_constants(*corners)):
            _fail("mesh box cache disagrees with static data")


def _geometry(value):
    if type(value) is contacts.MeshShape:
        record = _fields(value, contacts.MeshShape, MESH_FIELDS)
        for name, code in (("corners", "i"), ("masks", "H"), ("surfaces", "H"), ("bounds", "f"),
                           ("large", "I")):
            _array(record[name], code)
        count = len(value.masks)
        if len(value.corners) != 6 * count or len(value.surfaces) != count or len(value.bounds) != 6 * count:
            _fail("inconsistent mesh array lengths")
        _mesh_caches(value)
        del record["cache"], record["boxes"]
        record["grid"] = _grid(value.grid, lambda entries: _grid_indices(entries, count))
    elif type(value) in GEOMETRY_FIELDS:
        record = _fields(value, type(value), GEOMETRY_FIELDS[type(value)])
    else:
        _fail("unsupported collision geometry class")
    record["class"] = type(value).__name__
    record["kind"] = value.kind
    return record


def _grid_indices(entries, count):
    _array(entries, "I")
    for entry in entries:
        _index(entry, count)
    return entries


def _engine(engine):
    _fields(engine, contacts.StaticWorld, frozenset({"shapes", "huge", "grid"}))
    if type(engine.shapes) is not list or type(engine.huge) is not list:
        _fail("unsupported static shape sequence")
    shape_numbers, geometry_numbers = {}, {}
    shapes, geometries = [], []

    def shape_number(shape):
        entry = shape_numbers.get(id(shape))
        if entry is None or entry[0] is not shape:
            _fail("static grid references an unlisted shape")
        return entry[1]

    for shape in engine.shapes:
        if id(shape) in shape_numbers:
            continue
        record = _fields(shape, contacts.StaticShape, SHAPE_FIELDS)
        geometry = shape.geometry
        if id(geometry) not in geometry_numbers:
            geometry_numbers[id(geometry)] = (geometry, len(geometries))
            geometries.append(_geometry(geometry))
        record["geometry"] = geometry_numbers[id(geometry)][1]
        shape_numbers[id(shape)] = (shape, len(shapes))
        shapes.append(record)

    def shape_list(values):
        if type(values) is not list:
            _fail("unsupported static grid values")
        return [shape_number(shape) for shape in values]

    return {
        "shapes": shapes, "geometry": geometries,
        "shape_sequence": shape_list(engine.shapes), "huge": shape_list(engine.huge),
        "grid": _grid(engine.grid, shape_list),
    }


def _record(world):
    record = _fields(world, collision.World, WORLD_FIELDS)
    if type(world.name) is not str:
        _fail("collision world name must be text")
    for name, code in (("data", "d"), ("masks", "H"), ("surfaces", "H"), ("large", "I")):
        _array(record[name], code)
    count = len(world.masks)
    if len(world.data) != 13 * count or len(world.surfaces) != count:
        _fail("inconsistent world array lengths")
    _world_columns(world)
    del record["columns"], record["lock"]
    record["grid"] = _grid(world.grid, lambda entries: _grid_indices(entries, count))
    record["engine"] = _engine(world.engine)
    record["layout"] = {
        "world_cell": collision.CELL, "world_large": collision.LARGE,
        "mesh_cell": contacts.MeshShape.CELL, "mesh_large": contacts.MeshShape.LARGE,
        "engine_cell": contacts.StaticWorld.CELL, "engine_huge": contacts.StaticWorld.HUGE,
    }
    return record


def world_fingerprint(world) -> dict:
    """Hash the complete supported static asset and validate, without changing, its lazy caches."""
    try:
        geometry = _digest(_record(world))
        sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in MODEL_FILES}
        model = _digest(sources)
        return {"schema": SCHEMA, "sha256": geometry, "model_sha256": model,
                "cache_version": collision.VERSION}
    except CollisionFingerprintError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OSError, OverflowError) as error:
        reason = f"cannot fingerprint collision asset: {type(error).__name__}"
        raise CollisionFingerprintError(reason) from error


def _descriptor_key(value):
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or set(value) != DESCRIPTOR_FIELDS):
        _fail("invalid collision asset descriptor fields")
    if value["schema"] != SCHEMA or type(value["schema"]) is not str:
        _fail("unsupported collision asset schema")
    for name in ("sha256", "model_sha256"):
        if type(value[name]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None:
            _fail("invalid collision asset digest")
    if type(value["cache_version"]) is not int or value["cache_version"] != collision.VERSION:
        _fail("unsupported collision cache version")
    return tuple(value[name] for name in sorted(DESCRIPTOR_FIELDS))


class WorldRegistry:
    """Only worlds explicitly registered by the caller can satisfy an asset read."""

    def __init__(self) -> None:
        self._worlds = {}

    def register(self, world) -> dict:
        descriptor = world_fingerprint(world)
        self._worlds[_descriptor_key(descriptor)] = world
        return dict(descriptor)

    def resolve(self, descriptor) -> collision.World:
        key = _descriptor_key(descriptor)
        world = self._worlds.get(key)
        if world is None:
            _fail("collision asset was not explicitly registered")
        if world_fingerprint(world) != descriptor:
            _fail("registered collision asset changed")
        return world
