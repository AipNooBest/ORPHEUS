"""A map's static collision as the character mover sees it, and the queries the mover makes.

Where it comes from (the client's map chunk 0x11): each map has one chunk file of type 0x11,
GUID 0x0DD0001100000000 | the map's index (TankLib GetChunkKey). It holds the game's own physics shapes:
    u32 count[4], u64 offset[4]      slot 2: convex polytopes, slot 3: triangle meshes
    mesh (56 B each): u64 nodes, verts, tris; u32 node count, vert count, tri count, depth; f32 area;
        u32 0, root mask, 0
        vertex: f32 x, y, z, 0; triangle (32 B): u32 v[3], i32 neighbour[3], u16 material, u16 surface,
        u16 mask, u16 0; the BVH nodes are not needed here
    polytope (88 B): u64 verts, planes, half edges, faces; f32 centroid[3]; u32 vert, face, edge count;
        f32 volume, area, 3 x f32; u16 mask, u16 material, u32 flag, u32 0
        vertices f32[3]; faces u8 first edge; half edges {s8 twin, u8 origin, u8 face, u8 next}
A triangle's mask is the query categories it stops. The mover queries with its team's proxy bit (0x08
team 0, 0x10 team 1), so only triangles with one of them matter: about a sixth of a map's triangles
(the movement hull). A triangle whose surface has bit 0 is not ground (the client's probe skips it).

The chunk does not hold the props the client makes from the map's entity placeables (fences, crates,
clip boxes): their models' physics shapes are added from props.py, each with the placeable's identifier
when it has one, since such a prop exists only in the game modes that list it.

The mover's part lives in a git-ignored cache, cache/collision/<map>.col. tools/build_collision.py
builds it from the player's own game install. The client launcher can build it on demand; a
server-only deployment reads prepared caches. The cache
keeps the shapes the way the client's physics engine has them (contacts.py): each mesh's triangles in
chunk order with their wing vertices, the polytopes whole, the props as their models' shapes placed with
the placeable's translation, rotation and scale.

Queries: the ground under a point and rays on a grid of 2 m columns of triangles (the polytopes as
the fans of their faces, capsule and sphere props as prisms), and `World.engine`, the engine's shapes
for the character's contacts.
"""

import json
import logging
import lzma
import math
import struct
import tempfile
import threading
import time
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

from ow174.game import contacts, props
from ow174.game.casc import CascError, RecordCatalog, Storage, game_root
from ow174.game.static_ray import MIN_SURFACE_DISTANCE, round_hit
from ow174.paths import COLLISION_CACHE_DIR, DATA_DIR, GAME_PATH_FILE

log = logging.getLogger("ow174.game")

TEAM0, TEAM1 = 0x08, 0x10  # the team proxy categories (STUDominoFlags team0Proxy, team1Proxy)
MOVER = TEAM0 | TEAM1
NOT_GROUND = 0x1  # surface bit: the probe does not stand on it
CHUNK_TYPE = 0x11
KEYS_PATH = DATA_DIR / "collision_keys_174.json"
CACHE_DIR = COLLISION_CACHE_DIR
MAGIC = b"OW174COL"
VERSION = 5  # 2: props with their identifiers; 3: no client-only breakables; 4: the engine's shapes;
# 5: only the props' static shapes, at their nodes (props.static_part, props.at_node)
RAY_GEOMETRY = "expanded-static-rays"
RAY_PROP_SCOPE = "indexed-static-models"
RAY_ROOT_PROP_SCOPE = "indexed-and-authenticated-roots"
ALL_CATEGORIES = 0xFFFF  # original per-triangle and per-shape masks are u16
MAX_CACHE_HEADER = 1 << 20
CELL = 2.0  # metres per grid column
CELL_CACHE = 1024  # columns kept as ready triangle lists
LARGE = 4096  # a triangle over more columns than this is checked by every query instead
EDGE = 1e-7  # tolerance of the inside test, so a point on a seam between two triangles hits one
RAY_EPSILON = 1e-9

# A triangle as the queries use it.
MINX, MAXX, MINY, MAXY, MINZ, MAXZ = range(6)
AX, AY, AZ, BX, BY, BZ, CX, CY, CZ = range(6, 15)
NX, NY, NZ, D, MASK, SURFACE, NUMBER = range(15, 22)


class CollisionError(Exception):
    pass


def chunk_guid(map_guid: int) -> int:
    return 0x0DD0000000000000 | CHUNK_TYPE << 32 | (map_guid & 0xFFFFFFFF)


def parse_chunk(data: bytes, category: int = MOVER) -> tuple[list, list]:
    """The shapes of a collision chunk that stop the category: (meshes, polytopes). A mesh: (vertices,
    triangles) with only its triangles that stop the category, in chunk order, each (a, b, c, wing0,
    wing1, wing2, mask, surface), vertex numbers into the mesh's kept vertices (wings too, -1: none). A
    polytope: (mask, centroid, vertices, planes, faces, half edges)."""
    counts = struct.unpack_from("<4I", data, 0)
    offsets = struct.unpack_from("<4Q", data, 16)
    meshes, hulls = [], []
    for number in range(counts[2]):
        base = offsets[2] + 88 * number
        verts, planes, edges, faces = struct.unpack_from("<4Q", data, base)
        centroid = struct.unpack_from("<3f", data, base + 32)
        vert_count, face_count, edge_count = struct.unpack_from("<3I", data, base + 44)
        mask = struct.unpack_from("<H", data, base + 76)[0]
        if not mask & category:
            continue
        points = [struct.unpack_from("<3f", data, verts + 12 * n) for n in range(vert_count)]
        half = [struct.unpack_from("<bBBB", data, edges + 4 * n) for n in range(edge_count)]
        for face in range(face_count):
            first = edge = data[faces + face]
            for _ in range(edge_count):
                _twin, _origin, owner, following = half[edge]
                if owner != face:
                    raise CollisionError("polytope face loop leaves its face")
                edge = following
                if edge == first:
                    break
        plane_list = [struct.unpack_from("<4f", data, planes + 16 * n) for n in range(face_count)]
        hulls.append((mask, centroid, points, plane_list, list(data[faces : faces + face_count]), half))
    for mesh in range(counts[3]):
        base = offsets[3] + 56 * mesh
        _nodes, verts, tris, _node_count, vert_count, tri_count = struct.unpack_from("<3Q3I", data, base)
        if verts + 16 * vert_count > len(data) or tris + 32 * tri_count > len(data):
            raise CollisionError("mesh runs past the chunk")
        numbers: dict[int, int] = {}
        kept = []
        for record in struct.iter_unpack("<3I3i4H", data[tris : tris + 32 * tri_count]):
            a, b, c, w0, w1, w2, _material, surface, mask, _zero = record
            if not mask & category:
                continue
            renamed = []
            for vertex in (a, b, c, w0, w1, w2):
                if not 0 <= vertex < vert_count:
                    renamed.append(-1)
                    continue
                if vertex not in numbers:
                    numbers[vertex] = len(numbers)
                renamed.append(numbers[vertex])
            if min(renamed[:3]) < 0:
                raise CollisionError("triangle corner out of the mesh")
            kept.append((*renamed, mask, surface))
        vertices = [None] * len(numbers)
        for vertex, number in numbers.items():
            vertices[number] = struct.unpack_from("<3f", data, verts + 16 * vertex)
        meshes.append((vertices, kept))
    return meshes, hulls


class _Writer:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def put(self, fmt: str, *values) -> None:
        self.parts.append(struct.pack("<" + fmt, *values))

    def floats(self, values) -> None:
        self.parts.append(array("f", values).tobytes())

    def mesh(self, vertices, records) -> None:
        self.floats([axis for vertex in vertices for axis in vertex])
        self.parts.append(array("i", [x for record in records for x in record[:6]]).tobytes())
        self.parts.append(array("H", [record[6] for record in records]).tobytes())
        self.parts.append(array("H", [record[7] for record in records]).tobytes())

    def hull(self, mask, centroid, points, planes, faces, half) -> None:
        self.put("4H", mask, len(points), len(planes), len(half))
        self.floats(centroid)
        self.floats([axis for point in points for axis in point])
        self.floats([value for plane in planes for value in plane])
        self.parts.append(bytes(faces))
        self.parts.append(b"".join(struct.pack("<bBBB", *edge) for edge in half))


class _Reader:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.at = 0

    def get(self, fmt: str) -> tuple:
        values = struct.unpack_from("<" + fmt, self.raw, self.at)
        self.at += struct.calcsize("<" + fmt)
        return values

    def array(self, kind: str, count: int) -> array:
        out = array(kind)
        size = out.itemsize * count
        out.frombytes(self.raw[self.at : self.at + size])
        self.at += size
        return out

    def points(self, count: int, width: int = 3) -> list:
        values = self.array("f", width * count)
        return [tuple(values[k : k + width]) for k in range(0, len(values), width)]

    def mesh(self, vertex_count: int, triangle_count: int) -> tuple:
        vertices = self.points(vertex_count)
        corners = self.array("i", 6 * triangle_count)
        masks = self.array("H", triangle_count)
        surfaces = self.array("H", triangle_count)
        records = [(*corners[6 * k : 6 * k + 6], masks[k], surfaces[k]) for k in range(triangle_count)]
        return vertices, records

    def hull(self) -> tuple:
        mask, vert_count, face_count, edge_count = self.get("4H")
        centroid = self.get("3f")
        points = self.points(vert_count)
        planes = self.points(face_count, 4)
        faces = list(self.raw[self.at : self.at + face_count])
        self.at += face_count
        half = [self.get("bBBB") for _ in range(edge_count)]
        return mask, centroid, points, planes, faces, half


KINDS = {"sphere": 0, "capsule": 1, "hull": 2, "mesh": 3}


def pack(
    name: str, meshes: list, hulls: list, source: dict, models=(), placements=(), identifiers=()
) -> bytes:
    """The cache file: magic, version, a JSON header and the LZMA-packed shapes: the chunk's meshes
    (vertices f32 x, y, z; triangles i32 corners and wings; u16 masks; u16 surfaces), its polytopes, the
    props' models (their shapes, props.model_shapes) and the placements (u32 model, u16 group: 0, or k
    when only the game modes that list the header's k-th identifier have it; translation, scale,
    rotation)."""
    out = _Writer()
    for vertices, records in meshes:
        out.mesh(vertices, records)
    for hull in hulls:
        out.hull(*hull)
    for shapes in models:
        out.put("H", len(shapes))
        for shape in shapes:
            kind = shape[0]
            out.put("B", KINDS[kind])
            if kind == "mesh":
                _, mask, vertices, records = shape
                out.put("HII", mask, len(vertices), len(records))
                out.mesh(vertices, records)
            elif kind == "hull":
                out.hull(*shape[1:])
            elif kind == "capsule":
                _, mask, a, b, radius = shape
                out.put("H7f", mask, *a, *b, radius)
            else:
                _, mask, centre, radius = shape
                out.put("H4f", mask, *centre, radius)
    for model, group, translation, scale, rotation in placements:
        out.put("IH10f", model, group, *translation, *scale, *rotation)
    header = {
        "name": name,
        "meshes": [[len(vertices), len(records)] for vertices, records in meshes],
        "hulls": len(hulls),
        "models": len(models),
        "placements": len(placements),
        "triangles": sum(len(records) for _, records in meshes),
        "identifiers": [f"0x{value:016X}" for value in identifiers],
        **source,
    }
    payload = lzma.compress(b"".join(out.parts))
    text = json.dumps(header, sort_keys=True).encode()
    return MAGIC + struct.pack("<II", VERSION, len(text)) + text + payload


def cache_version(path: Path) -> int | None:
    """The version of a cache file, None when it is not one."""
    try:
        with path.open("rb") as file:
            head = file.read(12)
    except OSError:
        return None
    return struct.unpack_from("<I", head, 8)[0] if len(head) == 12 and head[:8] == MAGIC else None


def _read_cache_header(file) -> dict:
    head = file.read(16)
    if len(head) != 16 or head[:8] != MAGIC:
        raise CollisionError("not a collision cache file")
    version, length = struct.unpack_from("<II", head, 8)
    if version != VERSION:
        raise CollisionError(f"cache version {version}, this server reads {VERSION}")
    if not 0 < length <= MAX_CACHE_HEADER:
        raise CollisionError("invalid collision cache header length")
    text = file.read(length)
    if len(text) != length:
        raise CollisionError("truncated collision cache header")
    header = json.loads(text)
    if not isinstance(header, dict):
        raise CollisionError("invalid collision cache header")
    return header


def unpack(blob: bytes) -> tuple[dict, list, list, list, list]:
    """(header, meshes, polytopes, models, placements) of a cache file."""
    file = BytesIO(blob)
    header = _read_cache_header(file)
    return header, *_unpack_geometry(file.read(), header)


def _unpack_geometry(payload: bytes, header: dict) -> tuple[list, list, list, list]:
    raw = _Reader(lzma.decompress(payload))
    meshes = [raw.mesh(vertex_count, triangle_count) for vertex_count, triangle_count in header["meshes"]]
    hulls = [raw.hull() for _ in range(header["hulls"])]
    models = []
    for _ in range(header["models"]):
        shapes = []
        for _ in range(raw.get("H")[0]):
            kind = raw.get("B")[0]
            if kind == KINDS["mesh"]:
                mask, vertex_count, triangle_count = raw.get("HII")
                shapes.append(("mesh", mask, *raw.mesh(vertex_count, triangle_count)))
            elif kind == KINDS["hull"]:
                shapes.append(("hull", *raw.hull()))
            elif kind == KINDS["capsule"]:
                mask, *values = raw.get("H7f")
                shapes.append(("capsule", mask, tuple(values[0:3]), tuple(values[3:6]), values[6]))
            else:
                mask, *values = raw.get("H4f")
                shapes.append(("sphere", mask, tuple(values[0:3]), values[3]))
        models.append(shapes)
    placements = []
    for _ in range(header["placements"]):
        model, group, *values = raw.get("IH10f")
        placements.append((model, group, tuple(values[0:3]), tuple(values[3:6]), tuple(values[6:10])))
    return meshes, hulls, models, placements


# ---- the engine's shapes of a map (contacts.py)


def _scaled(points, scale) -> list:
    sx, sy, sz = scale
    return [contacts.v3(x * sx, y * sy, z * sz) for x, y, z in points]


def engine_shape(shape, scale=(1.0, 1.0, 1.0)):
    """A model shape (props.model_shapes) as the engine's geometry, times the placeable's scale: a mesh's
    corners as its fetch multiplies them (0x7FF789CF3670); polytopes, capsules and spheres scaled the same
    way, plane offsets and radii by the scale (unverified: every placeable seen has a uniform scale)."""
    kind = shape[0]
    one = tuple(scale) == (1.0, 1.0, 1.0)
    if kind == "mesh":
        _, mask, vertices, records = shape
        if one:
            return contacts.MeshShape(vertices, records, mask)
        return contacts.MeshShape(_scaled(vertices, scale), records, mask, raw=vertices, scale=scale)
    if kind == "hull":
        _, mask, centroid, points, planes, faces, half = shape
        if not one:
            s = scale[0]
            centroid = _scaled([centroid], scale)[0]
            points = _scaled(points, scale)
            planes = [(nx, ny, nz, contacts.f32(w * s)) for nx, ny, nz, w in planes]
        return contacts.Hull(centroid, points, planes, faces, half, mask)
    if kind == "capsule":
        _, mask, a, b, radius = shape
        if not one:
            a, b = _scaled([a, b], scale)
            radius = contacts.f32(radius * scale[0])
        return contacts.Capsule(a, b, radius, mask)
    _, mask, centre, radius = shape
    if not one:
        centre = _scaled([centre], scale)[0]
        radius = contacts.f32(radius * scale[0])
    return contacts.Sphere(centre, radius, mask)


def engine_world(meshes, hulls, models=(), placements=(), groups=frozenset({0})) -> contacts.StaticWorld:
    """The map's shapes in the order the world made them (unverified: the chunk's polytopes, its meshes, then
    the props in placeable order), the props kept when their group is."""
    shapes = []
    for mask, centroid, points, planes, faces, half in hulls:
        hull = contacts.Hull(centroid, points, planes, faces, half, mask)
        shapes.append(contacts.StaticShape(hull, None, len(shapes)))
    for vertices, records in meshes:
        shapes.append(contacts.StaticShape(contacts.MeshShape(vertices, records), None, len(shapes)))
    made: dict[tuple, object] = {}
    for model, group, translation, scale, rotation in placements:
        if group not in groups:
            continue
        frame = (contacts.v3(*translation), tuple(contacts.f32(value) for value in rotation))
        for k, shape in enumerate(models[model]):
            key = (model, k, tuple(scale))
            if key not in made:
                made[key] = engine_shape(shape, scale)
            shapes.append(contacts.StaticShape(made[key], frame, len(shapes)))
    return contacts.StaticWorld(shapes)


@dataclass(frozen=True)
class _RoundProp:
    a: tuple[float, float, float]
    b: tuple[float, float, float]
    radius: float
    mask: int


def probe_triangles(
    meshes, hulls, models=(), placements=(), groups=frozenset({0}), *,
    prop_surfaces=False, round_props: list[_RoundProp] | None = None,
) -> tuple:
    """Project static triangles and optionally collect uniform round props separately."""
    triangles, masks, surfaces = [], array("H"), array("H")
    for vertices, records in meshes:
        for a, b, c, _w0, _w1, _w2, mask, surface in records:
            triangles.append((*vertices[a], *vertices[b], *vertices[c]))
            masks.append(mask)
            surfaces.append(surface)
    for hull in hulls:
        for a, b, c, mask in props.shape_triangles(("hull", *hull)):
            triangles.append((*a, *b, *c))
            masks.append(mask)
            surfaces.append(0)
    shapes: dict[tuple[int, bool], list] = {}
    for model, group, translation, scale, rotation in placements:
        if group not in groups:
            continue
        analytic = round_props is not None and 0.0 < scale[0] == scale[1] == scale[2] < math.inf
        key = model, analytic
        if key not in shapes:
            shapes[key] = [
                (*triangle, shape[3][i][-1] if prop_surfaces and shape[0] == "mesh" else 0)
                for shape in models[model]
                if not (analytic and shape[0] in ("sphere", "capsule"))
                for i, triangle in enumerate(props.shape_triangles(shape))
            ]
        for a, b, c, mask, surface in shapes[key]:
            corners = [props.place(p, translation, scale, rotation) for p in (a, b, c)]
            triangles.append(tuple(axis for corner in corners for axis in corner))
            masks.append(mask)
            surfaces.append(surface)
        if analytic:
            for shape in models[model]:
                if shape[0] == "sphere":
                    _, mask, a, radius = shape
                    b = a
                elif shape[0] == "capsule":
                    _, mask, a, b, radius = shape
                else:
                    continue
                round_props.append(_RoundProp(
                    props.place(a, translation, scale, rotation),
                    props.place(b, translation, scale, rotation),
                    radius * scale[0], mask,
                ))
    return triangles, masks, surfaces


def soup_mesh(triangles, masks=None, surfaces=None) -> contacts.MeshShape:
    """A mesh from loose triangles (stand-in worlds): shared corners merged, each edge's wing the third
    corner of the other triangle on it."""
    numbers: dict[tuple, int] = {}
    vertices, corners = [], []
    for triangle in triangles:
        named = []
        for k in range(0, 9, 3):
            point = contacts.v3(*triangle[k : k + 3])
            if point not in numbers:
                numbers[point] = len(vertices)
                vertices.append(point)
            named.append(numbers[point])
        corners.append(named)
    edges: dict[tuple, list] = {}
    for number, (a, b, c) in enumerate(corners):
        for u, w in ((a, b), (b, c), (c, a)):
            edges.setdefault((min(u, w), max(u, w)), []).append(number)
    records = []
    for number, (a, b, c) in enumerate(corners):
        wings = []
        for u, w in ((a, b), (b, c), (c, a)):
            others = [o for o in edges[(min(u, w), max(u, w))] if o != number]
            third = [x for x in corners[others[0]] if x not in (u, w)] if others else []
            wings.append(third[0] if third else -1)
        mask = masks[number] if masks is not None else MOVER
        surface = surfaces[number] if surfaces is not None else 0
        records.append((a, b, c, *wings, mask, surface))
    return contacts.MeshShape(vertices, records)


class Ground:
    """What the mover's probe found: the height of the hit, the surface's upward normal and its flags."""

    __slots__ = ("mask", "normal", "plane", "surface", "y")

    def __init__(self, y: float, triangle: tuple) -> None:
        nx, ny, nz, d = triangle[NX], triangle[NY], triangle[NZ], triangle[D]
        if ny < 0:  # the shapes are two-sided: the normal faces the side the ray came from
            nx, ny, nz, d = -nx, -ny, -nz, -d
        self.y = y
        self.normal = (nx, ny, nz)
        self.plane = (nx, ny, nz, d)
        self.surface = triangle[SURFACE]
        self.mask = triangle[MASK]

    @classmethod
    def at(cls, point, normal, surface: int = 0) -> "Ground":
        """A ground point with its surface's normal (the probe's disc)."""
        ground = cls.__new__(cls)
        nx, ny, nz = normal
        ground.y = point[1]
        ground.normal = (nx, ny, nz)
        ground.plane = (nx, ny, nz, nx * point[0] + ny * point[1] + nz * point[2])
        ground.surface = surface
        ground.mask = MOVER
        return ground

    def height_at(self, x: float, z: float) -> float:
        """Where the vertical line through (x, z) meets the surface's plane."""
        nx, ny, nz, d = self.plane
        return (d - nx * x - nz * z) / ny


@dataclass(frozen=True)
class RayHit:
    distance: float
    point: tuple[float, float, float]
    normal: tuple[float, float, float]
    mask: int
    surface: int


class _TriangleScene:
    """Shared triangle queries on a grid of vertical columns."""

    def __init__(self, triangles, masks=None, surfaces=None, name: str = "") -> None:
        self.name = name
        count = len(triangles)
        self.data = array("d")
        self.masks = array("H", masks if masks is not None else [MOVER] * count)
        self.surfaces = array("H", surfaces if surfaces is not None else [0] * count)
        self.grid: dict[tuple[int, int], array] = {}
        self.columns: OrderedDict = OrderedDict()  # (i, j) -> [triangle tuples], recently used last
        self.large = array("I")  # triangles too big for the grid (catch planes)
        self.lock = threading.Lock()
        lowest = math.inf
        for number, (ax, ay, az, bx, by, bz, cx, cy, cz) in enumerate(triangles):
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length < 1e-12:
                nx, ny, nz, d = 0.0, 0.0, 0.0, 0.0  # degenerate: never hit
            else:
                nx, ny, nz = nx / length, ny / length, nz / length
                d = nx * ax + ny * ay + nz * az
            self.data.extend((ax, ay, az, bx, by, bz, cx, cy, cz, nx, ny, nz, d))
            low_x, high_x = min(ax, bx, cx), max(ax, bx, cx)
            low_z, high_z = min(az, bz, cz), max(az, bz, cz)
            lowest = min(lowest, ay, by, cy)
            first_i, last_i = math.floor(low_x / CELL), math.floor(high_x / CELL)
            first_j, last_j = math.floor(low_z / CELL), math.floor(high_z / CELL)
            if (last_i - first_i + 1) * (last_j - first_j + 1) > LARGE:
                self.large.append(number)
                continue
            for i in range(first_i, last_i + 1):
                for j in range(first_j, last_j + 1):
                    column = self.grid.get((i, j))
                    if column is None:
                        column = self.grid[(i, j)] = array("I")
                    column.append(number)
        self.lowest = lowest if count else 0.0
        self.large_tuples = [self._triangle(number) for number in self.large]  # shared by every column

    def __len__(self) -> int:
        return len(self.masks)

    def _triangle(self, number: int) -> tuple:
        base = 13 * number
        ax, ay, az, bx, by, bz, cx, cy, cz, nx, ny, nz, d = self.data[base : base + 13]
        return (
            min(ax, bx, cx), max(ax, bx, cx), min(ay, by, cy), max(ay, by, cy), min(az, bz, cz),
            max(az, bz, cz), ax, ay, az, bx, by, bz, cx, cy, cz, nx, ny, nz, d,
            self.masks[number], self.surfaces[number], number,
        )  # fmt: skip

    def column(self, i: int, j: int) -> list:
        """The triangles of a grid column, as tuples."""
        with self.lock:
            ready = self.columns.get((i, j))
            if ready is not None:
                self.columns.move_to_end((i, j))
                return ready
            ready = [self._triangle(number) for number in self.grid.get((i, j), ())] + self.large_tuples
            self.columns[(i, j)] = ready
            if len(self.columns) > CELL_CACHE:
                self.columns.popitem(last=False)
            return ready

    def nearby(self, low_x, high_x, low_y, high_y, low_z, high_z, category: int = MOVER) -> list:
        """Triangles whose bounds meet the box, each once."""
        found: dict[int, tuple] = {}
        for i in range(math.floor(low_x / CELL), math.floor(high_x / CELL) + 1):
            for j in range(math.floor(low_z / CELL), math.floor(high_z / CELL) + 1):
                for t in self.column(i, j):
                    if (
                        t[MINX] <= high_x and t[MAXX] >= low_x and t[MINZ] <= high_z and t[MAXZ] >= low_z
                        and t[MINY] <= high_y and t[MAXY] >= low_y and t[MASK] & category
                    ):  # fmt: skip
                        found[t[NUMBER]] = t
        return list(found.values())

    def ground(self, x: float, z: float, top: float, bottom: float, category: int = MOVER) -> Ground | None:
        """The first surface a ray from (x, top, z) straight down to bottom meets: the highest."""
        best, hit = -math.inf, None
        for t in self.column(math.floor(x / CELL), math.floor(z / CELL)):
            if t[MINX] > x or t[MAXX] < x or t[MINZ] > z or t[MAXZ] < z or t[MAXY] < bottom or t[MINY] > top:
                continue
            ny = t[NY]
            if -1e-9 < ny < 1e-9 or not t[MASK] & category:
                continue
            y = (t[D] - t[NX] * x - t[NZ] * z) / ny
            if y > top or y < bottom or y <= best:
                continue
            if inside_xz(t, x, z):
                best, hit = y, t
        return Ground(best, hit) if hit is not None else None

    def cast_ray(self, origin, direction, length: float, category: int = MOVER) -> RayHit | None:
        """First static hit along a unit direction in the supplied geometry and category."""
        ox, oy, oz = origin
        dx, dy, dz = direction
        if (
            not all(math.isfinite(value) for value in (ox, oy, oz, dx, dy, dz, length))
            or length <= 0.0
            or (dx == dy == dz == 0.0)
        ):
            return None
        i, j = math.floor(ox / CELL), math.floor(oz / CELL)
        step_i, step_j = (1 if dx > 0 else -1), (1 if dz > 0 else -1)
        big = math.inf
        next_x = ((i + (step_i > 0)) * CELL - ox) / dx if abs(dx) > RAY_EPSILON else big
        next_z = ((j + (step_j > 0)) * CELL - oz) / dz if abs(dz) > RAY_EPSILON else big
        delta_x = CELL / abs(dx) if abs(dx) > RAY_EPSILON else big
        delta_z = CELL / abs(dz) if abs(dz) > RAY_EPSILON else big
        seen: set[int] = set()
        best, triangle = big, None
        start = 0.0
        while start <= length:
            end = min(next_x, next_z, length)
            low, high = oy + dy * start, oy + dy * end
            low, high = min(low, high) - RAY_EPSILON, max(low, high) + RAY_EPSILON
            for t in self.column(i, j):
                number = t[NUMBER]
                if number in seen or not t[MASK] & category or t[MAXY] < low or t[MINY] > high:
                    continue
                seen.add(number)
                hit = _ray_triangle(ox, oy, oz, dx, dy, dz, t)
                if hit is not None and hit < best:
                    best, triangle = hit, t
            if best <= end:
                break
            if next_x < next_z:
                start, next_x, i = next_x, next_x + delta_x, i + step_i
            else:
                start, next_z, j = next_z, next_z + delta_z, j + step_j
        if triangle is None or best > length:
            return None
        point = (ox + dx * best, oy + dy * best, oz + dz * best)
        nx, ny, nz = triangle[NX], triangle[NY], triangle[NZ]
        normal = (-nx, -ny, -nz) if nx * dx + ny * dy + nz * dz > 0.0 else (nx, ny, nz)
        return RayHit(best, point, normal, triangle[MASK], triangle[SURFACE])


def _placement_groups(header: dict, identifiers: frozenset[int]) -> frozenset[int]:
    return frozenset([0, *(k + 1 for k, text in enumerate(header["identifiers"])
                          if int(text, 16) in identifiers)])


class World(_TriangleScene):
    """A movement world with the static physics engine for character contacts."""

    def __init__(self, triangles, masks=None, surfaces=None, name: str = "", engine=None) -> None:
        super().__init__(triangles, masks, surfaces, name)
        if engine is None:  # loose triangles (stand-in worlds): one mesh
            mesh = soup_mesh(triangles, self.masks, self.surfaces)
            engine = contacts.StaticWorld([contacts.StaticShape(mesh)] if len(triangles) else [])
        self.engine = engine

    @classmethod
    def load(cls, path: str | Path, identifiers: frozenset[int] = frozenset()) -> "World":
        """A cache file's movement world for a mode that keeps these placeable identifiers."""
        with Path(path).open("rb") as file:
            header = _read_cache_header(file)
            if header.get("geometry") == RAY_GEOMETRY:
                raise CollisionError("a ray geometry cache cannot be loaded as a movement world")
            meshes, hulls, models, placements = _unpack_geometry(file.read(), header)
        groups = _placement_groups(header, identifiers)
        engine = engine_world(meshes, hulls, models, placements, groups)
        triangles, masks, surfaces = probe_triangles(meshes, hulls, models, placements, groups)
        return cls(triangles, masks, surfaces, header.get("name", ""), engine)


class RayWorld(_TriangleScene):
    """Explicit static query geometry from the core chunk and indexed static props."""

    def __init__(self, triangles, masks=None, surfaces=None, name: str = "", *, round_props=()) -> None:
        super().__init__(triangles, masks, surfaces, name)
        self.round_props = tuple(round_props)

    @classmethod
    def load(cls, map_guid: int, mode: int | None = None) -> "RayWorld":
        """Load an expanded cache or report it unavailable before any query can return a miss."""
        try:
            with ray_cache_path(map_guid).open("rb") as file:
                header = _read_cache_header(file)
                _check_ray_header(header, map_guid)
                meshes, hulls, models, placements = _unpack_geometry(file.read(), header)
        except FileNotFoundError as error:
            raise CollisionError(f"no expanded ray geometry for map {map_guid:#x}") from error
        groups = _placement_groups(header, props.mode_identifiers(mode))
        round_props: list[_RoundProp] = []
        triangles, masks, surfaces = probe_triangles(
            meshes, hulls, models, placements, groups, prop_surfaces=True, round_props=round_props,
        )
        return cls(triangles, masks, surfaces, header.get("name", ""), round_props=round_props)

    def cast_ray(self, origin, direction, length: float, category: int = MOVER) -> RayHit | None:
        hit = super().cast_ray(origin, direction, length, category)
        best = length if hit is None else hit.distance
        for shape in self.round_props:
            if not shape.mask & category:
                continue
            result = round_hit(origin, direction, best, shape.a, shape.b, shape.radius)
            if result is not None:
                distance, normal = result
                if hit is None or distance < best:
                    point = tuple(a + distance * axis for a, axis in zip(origin, direction, strict=True))
                    hit = RayHit(distance, point, normal, shape.mask, 0)
                    best = distance
        return hit


def _ray_triangle(ox, oy, oz, dx, dy, dz, t) -> float | None:
    """Moeller-Trumbore intersection, both sides."""
    ax, ay, az = t[AX], t[AY], t[AZ]
    e1x, e1y, e1z = t[BX] - ax, t[BY] - ay, t[BZ] - az
    e2x, e2y, e2z = t[CX] - ax, t[CY] - ay, t[CZ] - az
    px, py, pz = dy * e2z - dz * e2y, dz * e2x - dx * e2z, dx * e2y - dy * e2x
    det = e1x * px + e1y * py + e1z * pz
    if -1e-12 < det < 1e-12:
        return None
    inv = 1.0 / det
    sx, sy, sz = ox - ax, oy - ay, oz - az
    u = (sx * px + sy * py + sz * pz) * inv
    if u < 0.0 or u > 1.0:
        return None
    qx, qy, qz = sy * e1z - sz * e1y, sz * e1x - sx * e1z, sx * e1y - sy * e1x
    v = (dx * qx + dy * qy + dz * qz) * inv
    if v < 0.0 or u + v > 1.0:
        return None
    hit = (e2x * qx + e2y * qy + e2z * qz) * inv
    return hit if hit > MIN_SURFACE_DISTANCE else None


def inside_xz(t: tuple, x: float, z: float) -> bool:
    """Whether (x, z) lies in the triangle seen from above, either winding."""
    ax, az, bx, bz, cx, cz = t[AX], t[AZ], t[BX], t[BZ], t[CX], t[CZ]
    e0 = (bx - ax) * (z - az) - (bz - az) * (x - ax)
    e1 = (cx - bx) * (z - bz) - (cz - bz) * (x - bx)
    e2 = (ax - cx) * (z - cz) - (az - cz) * (x - cx)
    return (e0 >= -EDGE and e1 >= -EDGE and e2 >= -EDGE) or (e0 <= EDGE and e1 <= EDGE and e2 <= EDGE)


def flat_world(height: float, half_size: float = 5000.0) -> World:
    """An endless floor at that height: the stand-in when a map's collision is not available."""
    low, high = -half_size, half_size
    return World(
        [
            (low, height, low, low, height, high, high, height, high),
            (low, height, low, high, height, high, high, height, low),
        ],
        name="flat",
    )


# ---- Building and loading the caches


def collision_keys() -> dict[int, dict]:
    """Map GUID -> direct or bundled key record of its collision chunk in build 104319's storage."""
    if not KEYS_PATH.is_file():
        return {}
    table = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
    return {int(key, 16): value for key, value in table.items() if key.startswith("0x")}


def cache_path(map_guid: int) -> Path:
    return CACHE_DIR / f"{map_guid & 0xFFFFFFFF:04X}.col"


def ray_cache_path(map_guid: int) -> Path:
    return cache_path(map_guid).with_suffix(".rays.col")


def _check_ray_header(header: dict, map_guid: int) -> None:
    key = collision_keys().get(map_guid)
    if (
        not key
        or header.get("geometry") != RAY_GEOMETRY
        or header.get("category_mask") != ALL_CATEGORIES
        or header.get("prop_scope") not in (RAY_PROP_SCOPE, RAY_ROOT_PROP_SCOPE)
        or header.get("map") != f"0x{map_guid:016X}"
        or header.get("chunk") != f"0x{chunk_guid(map_guid):016X}"
        or header.get("ckey") != key["ckey"]
    ):
        raise CollisionError(f"expanded ray geometry does not match map {map_guid:#x}")
    if header["prop_scope"] == RAY_ROOT_PROP_SCOPE:
        placement = props.placement_record(map_guid)
        digest = header.get("prop_records_sha256")
        if (
            not placement
            or header.get("prop_placement_ckey") != placement["ckey"]
            or type(digest) is not str
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise CollisionError(f"expanded ray prop provenance does not match map {map_guid:#x}")


def ray_cache_ready(map_guid: int, *, prop_records: RecordCatalog | None = None) -> bool:
    """Whether the side cache header identifies the expected expanded geometry."""
    try:
        with ray_cache_path(map_guid).open("rb") as file:
            header = _read_cache_header(file)
            _check_ray_header(header, map_guid)
    except (OSError, CollisionError, ValueError):
        return False
    return prop_records is None or (
        header["prop_scope"] == RAY_ROOT_PROP_SCOPE and header["prop_records_sha256"] == prop_records.sha256
    )


def installed_game() -> Path | None:
    """The game install the player picked on the first start (game_path.txt), if it has its data."""
    if not GAME_PATH_FILE.is_file():
        return None
    try:
        return game_root(GAME_PATH_FILE.read_text(encoding="utf-8").strip())
    except CascError:
        return None


def build(
    map_guid: int, storage: Storage, name: str = "", *,
    ray_geometry: bool = False, prop_records: RecordCatalog | None = None,
) -> Path:
    """Read a map's collision chunk and its props from the game and write its cache file; returns the
    path."""
    if prop_records is not None and not ray_geometry:
        raise CollisionError("supplemental prop records require an expanded ray cache")
    keys = collision_keys().get(map_guid)
    if keys is None:
        raise CollisionError(f"no collision chunk known for map {map_guid:#x}")
    data = storage.read_record(keys)
    category = ALL_CATEGORIES if ray_geometry else MOVER
    meshes, hulls = parse_chunk(data, category)
    try:
        models, placed = props.map_shapes(
            map_guid, storage.read_record, category, records=prop_records.records if prop_records else None,
        )
    except (props.PropError, struct.error, IndexError, KeyError) as error:
        raise CollisionError(f"the props of map {map_guid:#x}: {error}") from error
    identifiers = sorted({identifier for _, identifier, *_ in placed if identifier})
    placements = [
        (model, identifiers.index(identifier) + 1 if identifier else 0, translation, scale, rotation)
        for model, identifier, translation, scale, rotation in placed
    ]
    source = {
        "map": f"0x{map_guid:016X}",
        "chunk": f"0x{chunk_guid(map_guid):016X}",
        "ckey": keys["ckey"],
        "props": len(placements),
    }
    if ray_geometry:
        source.update(geometry=RAY_GEOMETRY, category_mask=category, prop_scope=RAY_PROP_SCOPE)
    if prop_records is not None:
        source.update(
            prop_scope=RAY_ROOT_PROP_SCOPE,
            prop_records_sha256=prop_records.sha256,
            prop_placement_ckey=props.placement_record(map_guid)["ckey"],
        )
    blob = pack(name, meshes, hulls, source, models, placements, identifiers)
    path = ray_cache_path(map_guid) if ray_geometry else cache_path(map_guid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_bytes(blob)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


KEEP_WORLDS = 4  # maps kept loaded after their matches; a running match keeps its own
# (map GUID, the mode's placeable identifiers) -> terminal request handle, recently used last
_worlds: OrderedDict = OrderedDict()
_pending: set[tuple] = set()
_requests: dict = {}  # pending handles; a provider retains its own handle after LRU eviction
_loading = threading.Lock()
_building = threading.Lock()  # one cache build at a time


@dataclass(frozen=True)
class LoadState:
    """An atomic resource read: pending and unavailable cannot stand in for an empty map."""

    status: str
    world: World | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("pending", "ready", "unavailable"):
            raise ValueError(f"unknown collision state: {self.status}")
        if (self.status == "ready") != (self.world is not None):
            raise ValueError("only ready collision states must have a World")
        if self.status == "ready" and not isinstance(self.world, World):
            raise ValueError("ready collision states need a World")
        if self.status == "unavailable" and not self.error:
            raise ValueError("unavailable collision states need an error")


class CollisionProvider(Protocol):
    def poll(self, map_guid: int, name: str = "", mode: int | None = None) -> LoadState: ...


@dataclass
class _LoadHandle:
    state: LoadState


class StaticCollisionProvider:
    """An explicitly supplied world, for artificial scenes and isolated protocol tests."""

    def __init__(self, world: World) -> None:
        self.state = LoadState("ready", world)

    def poll(self, map_guid: int, name: str = "", mode: int | None = None) -> LoadState:
        return self.state


class AsyncCollisionProvider:
    def __init__(self, *, build_missing: bool = True) -> None:
        self._build_missing = build_missing
        self._handles: dict[tuple, _LoadHandle] = {}

    def poll(self, map_guid: int, name: str = "", mode: int | None = None) -> LoadState:
        key = (map_guid, props.mode_identifiers(mode))
        handle = self._handles.get(key)
        if handle is None:
            handle = _request_world(map_guid, name, mode, build_missing=self._build_missing)
            self._handles[key] = handle
        with _loading:
            return handle.state


def default_collision_provider() -> CollisionProvider:
    return AsyncCollisionProvider()


def _read_cache(map_guid: int, name: str, build_missing: bool, identifiers: frozenset[int]) -> World:
    path = cache_path(map_guid)
    if build_missing and map_guid in collision_keys():
        with _building:
            if cache_version(path) != VERSION and (root := installed_game()) is not None:
                started = time.monotonic()
                build(map_guid, Storage(root), name)
                log.info("[game] built the collision of %s in %.1f s", name or hex(map_guid),
                         time.monotonic() - started)  # fmt: skip
    if path.is_file():
        return World.load(path, identifiers)
    raise CollisionError(f"no collision cache for {name or hex(map_guid)}")


def _load(map_guid: int, name: str, build_missing: bool, identifiers: frozenset[int]) -> World | None:
    try:
        return _read_cache(map_guid, name, build_missing, identifiers)
    except (OSError, CascError, CollisionError, lzma.LZMAError, ValueError) as error:
        log.warning("[game] no collision for %s: %s", name or hex(map_guid), error)
    return None


def _keep(key: tuple, world: World | None, error: str = "") -> LoadState:
    state = LoadState("ready", world) if world is not None else LoadState(
        "unavailable", error=error or f"collision unavailable for map {key[0]:#x}"
    )
    with _loading:
        handle = _requests.pop(key, None)
        if handle is None:
            handle = _LoadHandle(state)
        else:
            handle.state = state
        _worlds[key] = handle
        _worlds.move_to_end(key)
        while len(_worlds) > KEEP_WORLDS:
            _worlds.popitem(last=False)
        _pending.discard(key)
    return state


def world_for(
    map_guid: int, name: str = "", build_missing: bool = True, mode: int | None = None
) -> World | None:
    """The map's collision for a game mode (its props that need an identifier only when the mode lists
    it), loaded once (and built from the game install when its cache is missing or outdated); None when
    there is none to be had (the mover then walks on a flat floor)."""
    identifiers = props.mode_identifiers(mode)
    key = (map_guid, identifiers, build_missing)
    with _loading:
        if key in _worlds:
            _worlds.move_to_end(key)
            return _worlds[key].state.world
    world = _load(map_guid, name, build_missing, identifiers)
    _keep(key, world)
    return world


def _request_world(map_guid: int, name: str, mode: int | None, *, build_missing: bool = True) -> _LoadHandle:
    identifiers = props.mode_identifiers(mode)
    # A cache-only miss must not block a later development build or join a worker
    # that can access a game installation.
    key = (map_guid, identifiers, build_missing)
    with _loading:
        if key in _worlds:
            _worlds.move_to_end(key)
            return _worlds[key]
        if key in _pending:
            return _requests[key]
        handle = _LoadHandle(LoadState("pending"))
        _requests[key] = handle
        _pending.add(key)

    def load() -> None:
        try:
            world = _read_cache(map_guid, name, build_missing, identifiers)
        except Exception as error:  # noqa: BLE001 - all worker failures must publish a terminal state
            # A failed worker must publish a terminal state instead of leaving the map pending forever.
            detail = f"{type(error).__name__}: {error}"
            log.warning("[game] no collision for %s: %s", name or hex(map_guid), detail)
            _keep(key, None, detail)
        else:
            _keep(key, world)

    try:
        threading.Thread(target=load, name="collision", daemon=True).start()
    except RuntimeError as error:
        _keep(key, None, f"could not start collision loader: {error}")
    return handle


def load_state(map_guid: int, name: str = "", mode: int | None = None) -> LoadState:
    """Request a map in the background and return its current state without waiting for I/O."""
    handle = _request_world(map_guid, name, mode)
    with _loading:
        return handle.state


def world_when_ready(map_guid: int, name: str = "", mode: int | None = None) -> World | None:
    """Legacy optional-world query. Live matches use the structured provider and pin their World."""
    return load_state(map_guid, name, mode).world
