"""The physics props of a map: entity placeables the client makes itself whose models have physics shapes
that stop the character mover. They are not in the map's collision chunk (0x11), which holds the map's
static geometry with its model groups baked in (checked on the Practice Range: 905 model-group instances
with mover shapes are in the chunk to 2 cm, the entity props are not).

What they are (data, 1.74): fences and railings (Practice Range 2600: 137 of them), desks and chairs (04AB,
0572, 04D1 next to the Practice Range spawn), clip boxes (Eichenwalde 1612, only in the free-for-all
modes) ...
The client makes every entity placeable that has a client id (body +104) itself, with its definition's
STUModelComponent model, and keeps one with an identifier (body +8) only when the game mode lists that
identifier (STUGameMode m_A43573F4, the rule of the pending list 0x7FF78969EFD0; Eichenwalde's clip boxes
carry the free-for-all one). Placeables without a client id are the server's (spawn doors, the payload);
we do not make them, so the client does not collide with them either. Client-only breakables (the
STUModelComponent's m_breakableConfig with m_clientOnly = 1: gas bottles, small boxes, 119 definitions)
are left out of the data: they are dynamic bodies, which the mover does not meet (below).

A model's physics shapes are its STUModel m_CB4D298D list (MSTU chunk of the 00C model, an STUv2 blob):
{shape, holder}. The holder's inline STUDominoFlags (STU_933AD0A9) is the shape's mask, packed as the
client packs it (bits of fields 0..12: 0, 8, 12, 7, 6, 5, 2, 9, 1, 3, 4, 10, 11; a field left out keeps
its default, every field on but the sixth: 0x1FDF), which every mesh triangle also carries. A shape stops
the mover when its mask has a team proxy bit (0x08, 0x10), as the static triangles do, and when the
client makes it a static shape: the mover's filter (category 0x08/0x10, include 0x1481, 0x7FF789B463F0)
meets only shapes of category 1, 0x80, 0x400 or 0x1000 (pair filter 0x7FF789D07730). The client's
builders pick a model's parts by the list entry's motion type (m_89AABA94) and give each part a category
from its holder (STUs with their class defaults, the field offsets as the builders read them):
    0, STU_897BADC9 (0x7FF78A9FD400): made when m_983EB565 != 0 (default 3); 0x800 with m_8BE69838
       (default 0), else 1 with m_B4D8D9BB (default 1), else 0x100
    2, STU_E62A2EFC (0x7FF78A9CC460): made when m_E5FB1A8B != 0 (default 3); 0x80 (or 0x1000) with
       m_B4D8D9BB (default 1), else 0x100
    4, STU_82C2BA2C (0x7FF78AA31B80): made when m_8C9E3EE1 != 0 (default 3); 1 (or 0x1000) with
       m_B4D8D9BB (default 0), else 0x100 (a dynamic body: office chairs on wheels, bottles, cones)
Motion type 1 goes to another list (0x7FF78AA3CDE0, not the physics world), type 3 entries have no shape.
Checked against the running client's own world: every prop shape of the Practice Range (762) has the
category this rule gives. The client-only breakables are dynamic bodies too.
A list entry names the model node it hangs on (m_30F925F4): bones STU_CB79EE74 and hardpoints
STU_BE9DDFDF {m_EDF0511C id, m_FF592924 parent, m_7DC1550F translation, m_AF9D3A0C rotation}. The client
puts a static shape in its body's frame with the node's place in the model applied to its geometry (the
desk 04AB's hull hangs 1.62 m up on node 0x8B: the client's hull is there, the placeable's frame its body).
Shapes: triangle meshes (with wing vertices as in chunk 0x11), convex polytopes (planes, centroid, half
edges as in chunk 0x11), capsules and spheres; the physics engine (contacts.py) takes them as they are,
in the placeable's frame (rotation, translation) times its scale; only the ground probe sees capsules and
spheres as 12-sided prisms around the segment. The same transform reproduces the chunk's baked model
groups.

The keys of the files (build 104319) are in data/collision_props_174.json; the geometry comes from the
player's own game install, like the collision chunk.
"""

import json
import math
import struct
from functools import cache

from ow174.paths import DATA_DIR

PROPS_PATH = DATA_DIR / "collision_props_174.json"
CHUNK_MAGIC = 0xF123456F
CHUNK_TAG = 0xC65C
MOVER = 0x18  # the team proxy categories
# STUDominoFlags fields in order and the mask bit each one sets (packed as in the model meshes' triangles).
FLAG_FIELDS = (0xED819119, 0x34858FD7, 0x1E5EEC85, 0x7E48C526, 0xE552CFAA, 0xB97596D2, 0x27CD5BF8,
               0x61BB5D6E, 0xDEF2CFF6, 0xA4F2490B, 0xD7E9C136, 0x13F177D2, 0xD60821FE)  # fmt: skip
FLAG_BITS = (0, 8, 12, 7, 6, 5, 2, 9, 1, 3, 4, 10, 11)
DEFAULT_MASK = 0x1FDF
SHAPES, SHAPE, HOLDER, FLAGS = 0xCB4D298D, 0xB7C8314A, 0x9B0424EB, 0xCBCB2AB9
MESH, CONVEX, CAPSULE, SPHERE = 0x537E56E4, 0xB3800E70, 0x4C8D9F5E, 0x4614D0E5
VERTICES, TRIANGLES, HALF_EDGES, FACES = 0x88FCECD7, 0x0D50490E, 0x036FF5AD, 0xDEBE73A9
POINT_A, POINT_B, CENTRE, RADIUS = 0x10EFDE90, 0xC320C90B, 0xA8D12F28, 0xED61D926
PLANES, CENTROID = 0x0674FD43, 0x7D1FC63E
MOTION, NODE = 0x89AABA94, 0x30F925F4  # the list entry's motion type and node number
NODE_CLASSES = (0xCB79EE74, 0xBE9DDFDF)  # bones and hardpoints of the model
NODE_ID, PARENT, TRANSLATION, ROTATION = 0xEDF0511C, 0xFF592924, 0x7DC1550F, 0xAF9D3A0C
STATIC, SENSOR = 0xB4D8D9BB, 0x8BE69838
# By motion type: (the holder class its builder reads, the holder's enum that makes the part (0: none,
# default 3), m_B4D8D9BB's default in that class).
BUILDERS = {0: (0x897BADC9, 0x983EB565, 1), 2: (0xE62A2EFC, 0xE5FB1A8B, 1), 4: (0x82C2BA2C, 0x8C9E3EE1, 0)}
SIDES = 12  # of the prisms that stand in for capsules and spheres
_F32 = struct.Struct("<f")


def _f32(value: float) -> float:
    return _F32.unpack(_F32.pack(value))[0]


class PropError(Exception):
    pass


@cache
def props_data() -> dict:
    if not PROPS_PATH.is_file():
        return {"maps": {}, "defs": {}, "models": {}, "modes": {}}
    return json.loads(PROPS_PATH.read_text(encoding="utf-8"))


def mode_identifiers(mode_guid: int | None) -> frozenset[int]:
    """The placeable identifiers a game mode keeps."""
    if mode_guid is None:
        return frozenset()
    return frozenset(int(item, 16) for item in props_data()["modes"].get(f"0x{mode_guid:016X}", ()))


# ---- the model file


def chunks(data: bytes) -> dict[str, bytes]:
    """The top-level chunks of a chunked file (teChunkedData: 00C models): tag -> payload."""
    if len(data) < 16:
        raise PropError("not a chunked file")
    magic, _ident, size, _ = struct.unpack_from("<4I", data, 0)
    if magic != CHUNK_MAGIC:
        raise PropError("not a chunked file")
    out = {}
    position = 16
    while position + 16 <= min(size, len(data)):
        ident, serialized, length, tag, _version = struct.unpack_from("<IiiHH", data, position)
        if tag != CHUNK_TAG:
            break
        name = struct.pack("<I", ident)[::-1].decode("latin-1")
        out.setdefault(name, data[position + 16 : position + 16 + length])
        position += 16 + max(serialized, length)
    return out


class Structured:
    """An STUv2 blob, as much of it as the shapes need: each instance's type hash and where its fields are.
    Header: bags {i32 count, i32 offset} of instance infos (16 B: hash, ..., size), inline types and field
    bags (each a bag of {hash, size}), then i32 dynamic data size and offset, i32 data offset. An instance
    in the data: i32 field bag, then each field of the bag (a size of 0 means an i32 size first). Embedded
    instances are i32 indexes, arrays an i32 offset into the dynamic data {i32 count, u32, i64 start}."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        infos = self._bag(0, 16)
        self.hashes = [struct.unpack_from("<I", data, at)[0] for at in infos]
        sizes = [struct.unpack_from("<i", data, at + 12)[0] for at in infos]
        self.bags = [
            [struct.unpack_from("<Ii", data, inner) for inner in self._bag(at, 8)] for at in self._bag(16, 8)
        ]
        dynamic_size, dynamic_at, body_at = struct.unpack_from("<3i", data, 24)
        self.dynamic = data[dynamic_at : dynamic_at + dynamic_size] if dynamic_size > 0 else b""
        self.body = data[body_at:]
        self.fields = []
        position = 0
        for size in sizes:
            self.fields.append(self._fields(position))
            position += size

    def _bag(self, at: int, stride: int) -> list[int]:
        count, offset = struct.unpack_from("<ii", self.data, at)
        if count < 0 or offset < 0 or offset + stride * count > len(self.data):
            raise PropError("bad structured data")
        return [offset + stride * k for k in range(count)]

    def _fields(self, position: int) -> dict:
        found = {}
        bag = struct.unpack_from("<i", self.body, position)[0]
        position += 4
        if not 0 <= bag < len(self.bags):
            return found
        for field, size in self.bags[bag]:
            if size == 0:
                size = struct.unpack_from("<i", self.body, position)[0]
                position += 4
            found[field] = (position, size)
            position += size
        return found

    def raw(self, fields: dict, field: int) -> bytes | None:
        place = fields.get(field)
        return None if place is None else self.body[place[0] : place[0] + place[1]]

    def ref(self, instance: int, field: int) -> int | None:
        value = self.raw(self.fields[instance], field)
        if value is None or len(value) < 4:
            return None
        index = struct.unpack_from("<i", value, 0)[0]
        return index if 0 <= index < len(self.hashes) else None

    def array(self, instance: int, field: int, stride: int) -> list[bytes]:
        value = self.raw(self.fields[instance], field)
        if value is None or len(value) < 4:
            return []
        offset = struct.unpack_from("<i", value, 0)[0]
        if offset < 0 or offset + 16 > len(self.dynamic):
            return []
        count = struct.unpack_from("<i", self.dynamic, offset)[0]
        start = struct.unpack_from("<q", self.dynamic, offset + 8)[0]
        return [self.dynamic[start + stride * k : start + stride * (k + 1)] for k in range(max(0, count))]

    def inline(self, instance: int, field: int) -> dict | None:
        place = self.fields[instance].get(field)
        return None if place is None else self._fields(place[0])


def _vector(raw: bytes | None) -> tuple:
    return struct.unpack_from("<3f", raw, 0) if raw is not None and len(raw) >= 12 else (0.0, 0.0, 0.0)


def _float(raw: bytes | None) -> float:
    return struct.unpack_from("<f", raw, 0)[0] if raw is not None and len(raw) >= 4 else 0.0


def _mask(stu: Structured, holder: int | None) -> int:
    flags = stu.inline(holder, FLAGS) if holder is not None else None
    mask = DEFAULT_MASK
    for field, bit in zip(FLAG_FIELDS, FLAG_BITS, strict=True):
        value = stu.raw(flags, field) if flags is not None else None
        if value:
            mask = mask | 1 << bit if value[0] else mask & ~(1 << bit)
    return mask


def _byte(stu: Structured, fields: dict, field: int, default: int) -> int:
    raw = stu.raw(fields, field)
    return raw[0] if raw else default


def static_part(stu: Structured, wrapper: int, holder: int | None) -> bool:
    """Whether the client makes a model's list entry a static shape (category 1, 0x80 or 0x1000), the
    only ones the character mover meets (see the module notes)."""
    motion = _byte(stu, stu.fields[wrapper], MOTION, 0)
    builder = BUILDERS.get(motion)
    if builder is None or holder is None or stu.hashes[holder] != builder[0]:
        return False
    holder_class, made, static_default = builder
    fields = stu.fields[holder]
    if not _byte(stu, fields, made, 3) or not _byte(stu, fields, STATIC, static_default):
        return False
    return not (holder_class == 0x897BADC9 and _byte(stu, fields, SENSOR, 0))


def nodes(stu: Structured) -> dict[int, tuple]:
    """The model's nodes: number -> (translation, rotation xyzw, parent number or None)."""
    out = {}
    for instance, kind in enumerate(stu.hashes):
        if kind not in NODE_CLASSES:
            continue
        fields = stu.fields[instance]
        ident = stu.raw(fields, NODE_ID)
        if not ident or len(ident) < 4:
            continue
        rotation = stu.raw(fields, ROTATION)
        parent = stu.raw(fields, PARENT)
        out[struct.unpack_from("<I", ident, 0)[0]] = (
            _vector(stu.raw(fields, TRANSLATION)),
            struct.unpack_from("<4f", rotation, 0)
            if rotation and len(rotation) >= 16
            else (0.0, 0.0, 0.0, 1.0),
            struct.unpack_from("<I", parent, 0)[0] if parent and len(parent) >= 4 else None,
        )
    return out


def _rotate32(q, v) -> tuple:
    """v turned by the quaternion q in float32: v + 2 q x (w v + q x v)."""
    x, y, z, w = q
    vx, vy, vz = v
    cx, cy, cz = _f32(y * vz - z * vy), _f32(z * vx - x * vz), _f32(x * vy - y * vx)
    ux, uy, uz = _f32(_f32(w * vx) + cx), _f32(_f32(w * vy) + cy), _f32(_f32(w * vz) + cz)
    tx, ty, tz = _f32(y * uz - z * uy), _f32(z * ux - x * uz), _f32(x * uy - y * ux)
    return _f32(vx + _f32(tx + tx)), _f32(vy + _f32(ty + ty)), _f32(vz + _f32(tz + tz))


def node_frame(table: dict, number: int | None) -> tuple | None:
    """(translation, rotation) of a node in the model's frame, its parents applied; None for the root
    (no translation, no turn)."""
    translation, rotation = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)
    seen = set()
    while number is not None and number in table and number not in seen:
        seen.add(number)
        t, q, parent = table[number]
        translation = tuple(_f32(a + b) for a, b in zip(_rotate32(q, translation), t, strict=True))
        x1, y1, z1, w1 = q
        x2, y2, z2, w2 = rotation
        rotation = (
            _f32(w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2),
            _f32(w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2),
            _f32(w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2),
            _f32(w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2),
        )
        number = parent
    if rotation == (0.0, 0.0, 0.0, 1.0) and all(abs(v) < 1e-12 for v in translation):
        return None
    return translation, rotation


def _moved(point, frame) -> tuple:
    t, q = frame
    x, y, z = _rotate32(q, point)
    return _f32(x + t[0]), _f32(y + t[1]), _f32(z + t[2])


def model_shapes(data: bytes, category: int = MOVER) -> list:
    """The physics shapes of a 00C model that stop the category, in model space, as the engine keeps them:
    ("mesh", mask, vertices, triangles (a, b, c, wing0, wing1, wing2, mask, surface: the 32 B records of
    chunk 0x11, wing = the far corner of the neighbour across each edge, -1 none)), ("hull", mask,
    centroid, vertices, planes, faces, half edges), ("capsule", mask, a, b, radius), ("sphere", mask,
    centre, radius). A mesh's triangles carry their own masks; the other shapes the holder's. Only the
    static ones (static_part), each moved to its node's place in the model."""
    payload = chunks(data).get("MSTU")
    if payload is None or len(payload) < 16:
        return []
    offset, size = struct.unpack_from("<qq", payload, 0)
    stu = Structured(payload[offset : offset + size])
    table = nodes(stu)
    out = []
    for item in stu.array(0, SHAPES, 8):
        wrapper = struct.unpack_from("<i", item, 0)[0]
        if not 0 <= wrapper < len(stu.hashes):
            continue
        shape = stu.ref(wrapper, SHAPE)
        if shape is None:
            continue
        holder = stu.ref(wrapper, HOLDER)
        if not static_part(stu, wrapper, holder):
            continue
        node = stu.raw(stu.fields[wrapper], NODE)
        frame = node_frame(table, struct.unpack_from("<I", node, 0)[0]) if node and len(node) >= 4 else None
        before = len(out)
        mask = _mask(stu, holder)
        kind, fields = stu.hashes[shape], stu.fields[shape]
        if kind == MESH:
            points = [_vector(raw) for raw in stu.array(shape, VERTICES, 16)]
            raw = b"".join(stu.array(shape, TRIANGLES, 1))
            records = []
            for k in range(0, len(raw) - 31, 32):
                a, b, c, w0, w1, w2, _material, surface, own, _ = struct.unpack_from("<3I3i4H", raw, k)
                if own & category and max(a, b, c) < len(points):
                    wings = tuple(w if 0 <= w < len(points) else -1 for w in (w0, w1, w2))
                    records.append((a, b, c, *wings, own, surface))
            if records:
                out.append(("mesh", mask, points, records))
        elif not mask & category:
            continue
        elif kind == CONVEX:  # faces as loops of half edges {s8 twin, u8 origin, u8 face, u8 next}
            points = [_vector(raw) for raw in stu.array(shape, VERTICES, 12)]
            planes = [struct.unpack_from("<4f", raw, 0) for raw in stu.array(shape, PLANES, 16)]
            edges = b"".join(stu.array(shape, HALF_EDGES, 1))
            half = [struct.unpack_from("<bBBB", edges, 4 * n) for n in range(len(edges) // 4)]
            faces = list(b"".join(stu.array(shape, FACES, 1)))
            if points and len(planes) == len(faces):
                out.append(("hull", mask, _vector(stu.raw(fields, CENTROID)), points, planes, faces, half))
        elif kind == CAPSULE:
            a, b = _vector(stu.raw(fields, POINT_A)), _vector(stu.raw(fields, POINT_B))
            out.append(("capsule", mask, a, b, _float(stu.raw(fields, RADIUS))))
        elif kind == SPHERE:
            out.append(("sphere", mask, _vector(stu.raw(fields, CENTRE)), _float(stu.raw(fields, RADIUS))))
        if frame is not None and len(out) > before:
            out[-1] = at_node(out[-1], frame)
    return out


def at_node(shape, frame) -> tuple:
    """A model shape moved to its node's place in the model (float32)."""
    kind = shape[0]
    if kind == "mesh":
        _, mask, points, records = shape
        return ("mesh", mask, [_moved(p, frame) for p in points], records)
    if kind == "hull":
        _, mask, centroid, points, planes, faces, half = shape
        t, q = frame
        turned = []
        for nx, ny, nz, w in planes:
            n = _rotate32(q, (nx, ny, nz))
            turned.append(
                (*n, _f32(w + _f32(_f32(_f32(n[0] * t[0]) + _f32(n[1] * t[1])) + _f32(n[2] * t[2]))))
            )
        return (
            "hull",
            mask,
            _moved(centroid, frame),
            [_moved(p, frame) for p in points],
            turned,
            faces,
            half,
        )
    if kind == "capsule":
        _, mask, a, b, radius = shape
        return ("capsule", mask, _moved(a, frame), _moved(b, frame), radius)
    _, mask, centre, radius = shape
    return ("sphere", mask, _moved(centre, frame), radius)


def shape_triangles(shape) -> list:
    """A shape as model-space triangles for the ground probe: [(a, b, c, mask)]; polytopes as the fans of
    their faces, capsules and spheres as prisms."""
    kind = shape[0]
    if kind == "mesh":
        points, records = shape[2], shape[3]
        return [(points[a], points[b], points[c], own) for a, b, c, _w0, _w1, _w2, own, _s in records]
    if kind == "hull":
        _, mask, _centroid, points, _planes, faces, half = shape
        out = []
        for first in faces:
            loop, edge = [], first
            for _ in range(len(half)):
                _twin, origin, _face, following = half[edge]
                loop.append(points[origin])
                edge = following
                if edge == first:
                    break
            out += [(loop[0], loop[k], loop[k + 1], mask) for k in range(1, len(loop) - 1)]
        return out
    if kind == "capsule":
        _, mask, a, b, radius = shape
        return [(*triangle, mask) for triangle in prism(a, b, radius)]
    _, mask, centre, radius = shape
    return [(*triangle, mask) for triangle in prism(centre, centre, radius)]


def model_triangles(data: bytes, category: int = MOVER) -> list:
    """The physics shapes of a 00C model as model-space triangles that stop the category:
    [((ax, ay, az), (bx, by, bz), (cx, cy, cz), mask)]."""
    return [triangle for shape in model_shapes(data, category) for triangle in shape_triangles(shape)]


def prism(a, b, radius: float, sides: int = SIDES) -> list:
    """A capsule (or a sphere, a == b) as a closed prism around its segment that its round parts touch:
    the ends pushed out by the radius, the sides tangent to the radius."""
    axis = [b[i] - a[i] for i in range(3)]
    length = math.sqrt(sum(x * x for x in axis))
    axis = [x / length for x in axis] if length > 1e-6 else [0.0, 1.0, 0.0]
    a = tuple(a[i] - axis[i] * radius for i in range(3))
    b = tuple(b[i] + axis[i] * radius for i in range(3))
    u = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    dot = sum(u[i] * axis[i] for i in range(3))
    u = [u[i] - axis[i] * dot for i in range(3)]
    norm = math.sqrt(sum(x * x for x in u))
    u = [x / norm for x in u]
    v = [axis[1] * u[2] - axis[2] * u[1], axis[2] * u[0] - axis[0] * u[2], axis[0] * u[1] - axis[1] * u[0]]
    reach = radius / math.cos(math.pi / sides)
    ring_a, ring_b = [], []
    for k in range(sides):
        angle = 2 * math.pi * k / sides
        offset = [reach * (math.cos(angle) * u[i] + math.sin(angle) * v[i]) for i in range(3)]
        ring_a.append(tuple(a[i] + offset[i] for i in range(3)))
        ring_b.append(tuple(b[i] + offset[i] for i in range(3)))
    out = []
    for k in range(sides):
        j = (k + 1) % sides
        out += [(ring_a[k], ring_a[j], ring_b[j]), (ring_a[k], ring_b[j], ring_b[k])]
    for k in range(1, sides - 1):
        out += [(ring_a[0], ring_a[k], ring_a[k + 1]), (ring_b[0], ring_b[k + 1], ring_b[k])]
    return out


# ---- the map's entity placeables


def placements(data: bytes) -> list[tuple]:
    """The ENTITY chunk's placeables that the client makes itself: [(definition, identifier, translation,
    scale, rotation)]. A placeable: 24 bytes {uuid, u16 flags, u8, u8 type, u32 size}, then its body
    {u64 definition, u64 identifier 1, u64 identifier 2, f32 translation[3], scale[3], rotation xyzw, ...,
    u16 client id at +104}."""
    count, _, offset = struct.unpack_from("<3I", data, 0)
    out = []
    position = offset
    for _ in range(count):
        if position + 24 + 112 > len(data):
            break
        size = struct.unpack_from("<I", data, position + 20)[0]
        body = position + 24
        definition, identifier = struct.unpack_from("<QQ", data, body)
        values = struct.unpack_from("<10f", data, body + 24)
        if struct.unpack_from("<H", data, body + 104)[0]:
            out.append((definition, identifier, values[0:3], values[3:6], values[6:10]))
        if size == 0:
            break
        position += size
    return out


def _rotate(q, v) -> tuple:
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))


def place(point, translation, scale, rotation) -> tuple:
    scaled = (point[0] * scale[0], point[1] * scale[1], point[2] * scale[2])
    x, y, z = _rotate(rotation, scaled)
    return (x + translation[0], y + translation[1], z + translation[2])


def map_shapes(map_guid: int, read) -> tuple[list, list]:
    """A map's props: (models: each model's shapes (model_shapes), placements: [(model index, identifier
    (0 or the one the game mode must list), translation, scale, rotation)] in placeable order).
    `read(keys)` returns a file's bytes."""
    data = props_data()
    entry = data["maps"].get(f"0x{map_guid:016X}")
    if entry is None:
        return [], []
    models: list = []
    index: dict[str, int] = {}
    placed = []
    for definition, identifier, translation, scale, rotation in placements(read(entry)):
        model = data["defs"].get(f"0x{definition:016X}")
        if model is None:
            continue
        if model not in index:
            index[model] = len(models)
            models.append(model_shapes(read(data["models"][model])))
        if models[index[model]]:
            placed.append((index[model], identifier, translation, scale, rotation))
    return models, placed
