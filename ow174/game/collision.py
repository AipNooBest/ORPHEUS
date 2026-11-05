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

The server reads the chunk from the player's own game install (casc.py) once per map and keeps the
mover's part in a git-ignored cache, cache/collision/<map>.col (tools/build_collision.py builds them all;
the server builds a missing or outdated one the first time a match is played on that map). The cache
keeps the shapes the way the client's physics engine has them (contacts.py): each mesh's triangles in
chunk order with their wing vertices, the polytopes whole, the props as their models' shapes placed with
the placeable's translation, rotation and scale.

Queries: the ground under a point (a vertical segment, what the mover's probe casts) on a grid of 2 m
columns of triangles (the polytopes as the fans of their faces, capsule and sphere props as prisms), and
`World.engine`, the engine's shapes for the character's contacts.
"""

import json
import logging
import lzma
import math
import struct
import threading
import time
from array import array
from collections import OrderedDict
from pathlib import Path

from ow174.game import contacts, props
from ow174.game.casc import CascError, Storage, game_root
from ow174.paths import DATA_DIR, GAME_PATH_FILE, ROOT

log = logging.getLogger("ow174.game")

TEAM0, TEAM1 = 0x08, 0x10  # the team proxy categories (STUDominoFlags team0Proxy, team1Proxy)
MOVER = TEAM0 | TEAM1
NOT_GROUND = 0x1  # surface bit: the probe does not stand on it
CHUNK_TYPE = 0x11
KEYS_PATH = DATA_DIR / "collision_keys_174.json"
CACHE_DIR = ROOT / "cache" / "collision"
MAGIC = b"OW174COL"
VERSION = 5  # 2: props with their identifiers; 3: no client-only breakables; 4: the engine's shapes;
# 5: only the props' static shapes, at their nodes (props.static_part, props.at_node)
CELL = 2.0  # metres per grid column
CELL_CACHE = 1024  # columns kept as ready triangle lists
LARGE = 4096  # a triangle over more columns than this is checked by every query instead
EDGE = 1e-7  # tolerance of the inside test, so a point on a seam between two triangles hits one

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


def unpack(blob: bytes) -> tuple[dict, list, list, list, list]:
    """(header, meshes, polytopes, models, placements) of a cache file."""
    if blob[:8] != MAGIC:
        raise CollisionError("not a collision cache file")
    version, length = struct.unpack_from("<II", blob, 8)
    if version != VERSION:
        raise CollisionError(f"cache version {version}, this server reads {VERSION}")
    header = json.loads(blob[16 : 16 + length])
    raw = _Reader(lzma.decompress(blob[16 + length :]))
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
    return header, meshes, hulls, models, placements


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


def probe_triangles(meshes, hulls, models=(), placements=(), groups=frozenset({0})) -> tuple:
    """The triangles of the ground probe: (triangles as 9-tuples, masks, surfaces)."""
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
    shapes: dict[int, list] = {}
    for model, group, translation, scale, rotation in placements:
        if group not in groups:
            continue
        if model not in shapes:
            shapes[model] = [triangle for shape in models[model] for triangle in props.shape_triangles(shape)]
        for a, b, c, mask in shapes[model]:
            corners = [props.place(p, translation, scale, rotation) for p in (a, b, c)]
            triangles.append(tuple(axis for corner in corners for axis in corner))
            masks.append(mask)
            surfaces.append(0)
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


class World:
    """Triangles on a grid of vertical columns."""

    def __init__(self, triangles, masks=None, surfaces=None, name: str = "", engine=None) -> None:
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
        if engine is None:  # loose triangles (stand-in worlds): one mesh
            mesh = soup_mesh(triangles, self.masks, self.surfaces)
            engine = contacts.StaticWorld([contacts.StaticShape(mesh)] if len(triangles) else [])
        self.engine = engine

    @classmethod
    def load(cls, path: str | Path, identifiers: frozenset[int] = frozenset()) -> "World":
        """A cache file's world for a game mode that keeps these placeable identifiers."""
        header, meshes, hulls, models, placements = unpack(Path(path).read_bytes())
        listed = [k + 1 for k, text in enumerate(header["identifiers"]) if int(text, 16) in identifiers]
        groups = frozenset([0, *listed])
        engine = engine_world(meshes, hulls, models, placements, groups)
        triangles, masks, surfaces = probe_triangles(meshes, hulls, models, placements, groups)
        return cls(triangles, masks, surfaces, header.get("name", ""), engine)

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
    """Map GUID -> {"ekey", "ckey", "size"} of its collision chunk in build 104319's storage."""
    if not KEYS_PATH.is_file():
        return {}
    table = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
    return {int(key, 16): value for key, value in table.items() if key.startswith("0x")}


def cache_path(map_guid: int) -> Path:
    return CACHE_DIR / f"{map_guid & 0xFFFFFFFF:04X}.col"


def installed_game() -> Path | None:
    """The game install the player picked on the first start (game_path.txt), if it has its data."""
    if not GAME_PATH_FILE.is_file():
        return None
    try:
        return game_root(GAME_PATH_FILE.read_text(encoding="utf-8").strip())
    except CascError:
        return None


def build(map_guid: int, storage: Storage, name: str = "") -> Path:
    """Read a map's collision chunk and its props from the game and write its cache file; returns the
    path."""
    keys = collision_keys().get(map_guid)
    if keys is None:
        raise CollisionError(f"no collision chunk known for map {map_guid:#x}")
    data = storage.read(bytes.fromhex(keys["ekey"]), bytes.fromhex(keys["ckey"]))
    meshes, hulls = parse_chunk(data)
    try:
        models, placed = props.map_shapes(
            map_guid, lambda item: storage.read(bytes.fromhex(item["ekey"]), bytes.fromhex(item["ckey"]))
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
    blob = pack(name, meshes, hulls, source, models, placements, identifiers)
    path = cache_path(map_guid)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(blob)
    temporary.replace(path)
    return path


KEEP_WORLDS = 4  # maps kept loaded after their matches; a running match keeps its own
# (map GUID, the mode's placeable identifiers) -> World or None (none to be had), recently used last
_worlds: OrderedDict = OrderedDict()
_pending: set[tuple] = set()
_loading = threading.Lock()
_building = threading.Lock()  # one cache build at a time


def _load(map_guid: int, name: str, build_missing: bool, identifiers: frozenset[int]) -> World | None:
    path = cache_path(map_guid)
    try:
        if build_missing and map_guid in collision_keys():
            with _building:
                if cache_version(path) != VERSION and (root := installed_game()) is not None:
                    started = time.monotonic()
                    build(map_guid, Storage(root), name)
                    log.info("[game] built the collision of %s in %.1f s", name or hex(map_guid),
                             time.monotonic() - started)  # fmt: skip
        if path.is_file():
            return World.load(path, identifiers)
        log.warning("[game] no collision for %s: bodies walk on a flat floor", name or hex(map_guid))
    except (OSError, CascError, CollisionError, lzma.LZMAError, ValueError) as error:
        log.warning("[game] no collision for %s: %s", name or hex(map_guid), error)
    return None


def _keep(key: tuple, world: World | None) -> None:
    with _loading:
        _worlds[key] = world
        _worlds.move_to_end(key)
        while len(_worlds) > KEEP_WORLDS:
            _worlds.popitem(last=False)
        _pending.discard(key)


def world_for(
    map_guid: int, name: str = "", build_missing: bool = True, mode: int | None = None
) -> World | None:
    """The map's collision for a game mode (its props that need an identifier only when the mode lists
    it), loaded once (and built from the game install when its cache is missing or outdated); None when
    there is none to be had (the mover then walks on a flat floor)."""
    identifiers = props.mode_identifiers(mode)
    key = (map_guid, identifiers)
    with _loading:
        if key in _worlds:
            _worlds.move_to_end(key)
            return _worlds[key]
    world = _load(map_guid, name, build_missing, identifiers)
    _keep(key, world)
    return world


def world_when_ready(map_guid: int, name: str = "", mode: int | None = None) -> World | None:
    """The map's collision for a game mode if it is loaded; else it starts loading it in the background
    and returns None for now, so a match never waits for it."""
    identifiers = props.mode_identifiers(mode)
    key = (map_guid, identifiers)
    with _loading:
        if key in _worlds:
            _worlds.move_to_end(key)
            return _worlds[key]
        if key in _pending:
            return None
        _pending.add(key)
    threading.Thread(
        target=lambda: _keep(key, _load(map_guid, name, True, identifiers)), name="collision", daemon=True
    ).start()
    return None
