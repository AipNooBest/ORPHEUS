"""The physics engine's part of the character's move, ported from the PC 1.74 client (build 104319;
addresses at image base 0x7FF788F10000).

The client's mover asks its physics engine to move the capsule (0x7FF789CCABF0). The engine does not push
the capsule out of the world itself: it keeps contacts between the capsule and the world's shapes (the map's
triangle mesh, its convex polytopes, the props), each contact keeps manifolds (a normal and up to four
points with their separation), and the character's solver turns each manifold into a plane. This module
is that engine part, in float32 as the client computes it:

- `gjk`: the distance between two convex point sets (0x7FF789CD8CE0, simplex solvers 0x7FF789CD9A70,
  0x7FF789CDB340, 0x7FF789CD9C40, witness points 0x7FF789CD7FA0), with the cache it is warm started from.
- `capsule_triangle`: a manifold of the capsule against one triangle of a mesh (0x7FF789CEE5E0): one sided,
  a skin of 0.01 m on the triangle, speculative within that skin, the triangle's neighbours used to skip
  its inner edges. `capsule_hull` against a convex polytope (0x7FF789CE43A0), `capsule_capsule`
  (0x7FF789CD6970), `capsule_sphere` (0x7FF789CD7300), `convex_manifold` the pair dispatch with the
  bodies' frames (0x7FF789CFF970).
- `cluster_manifolds` / `reduce_points`: a mesh contact's manifolds from its triangles' (0x7FF789D0CE00,
  0x7FF789CE2480); `triangle_box`: the BVH query's triangle test (0x7FF789CD79D0).
- `shape_cast`: the sweep when the engine moves a body (0x7FF789CDBA70); `scene_sweep` / `mesh_sweep`: the
  probe's convex sweep of its ground disc through the world's shapes (0x7FF789D1D7E0, 0x7FF789CF3B20),
  `closest_on_triangle` (0x7FF789CD8230).
- `StaticShape` / `StaticWorld`: the map's shapes with their broadphase fat AABBs; `MeshContact`,
  `ConvexContact`: the character's contacts (0x7FF789D0E810, 0x7FF789D006A0); `CharacterBody`: the
  character's move (0x7FF789CC9320) with the broadphase's contact list and the stance fit test
  (0x7FF789CCAC10); `clip_velocity`: the velocity clip after the move.

Every value is a Python float holding a float32; each operation is rounded to float32 the way the SSE code
rounds it (a float64 result of two float32 operands rounds to the same float32). The CPU's rcpps and
rsqrtps estimates are not reproducible across CPUs; `recip_estimate` and `rsqrt_estimate` stand for them
(the correctly rounded value; the client's Newton step follows either way). A few shortcuts skip work
whose result cannot change anything (see `_far`, the contacts' memos and gjk's reach): with and without
them every tick comes out bit-identical.
"""

import math
import struct
from array import array
from functools import partial

_F = struct.Struct("<f")
_F3 = struct.Struct("<3f")
_F6 = struct.Struct("<6f")
_pack, _unpack = _F.pack, _F.unpack
_pack3, _unpack3 = _F3.pack, _F3.unpack
_pack6, _unpack6 = _F6.pack, _F6.unpack

FLT_MAX = 3.4028234663852886e38
TINY = 1.1754943508222875e-35  # xmmword_7FF78B585E10: the engine's "not zero" for squared lengths
EPSILON = 1.1920928955078125e-07


def f32(x: float) -> float:
    return _unpack(_pack(x))[0]


def v3(x: float, y: float, z: float) -> tuple:
    """Three lanes rounded to float32."""
    return _unpack3(_pack3(x, y, z))


def add(a, b) -> tuple:
    return _unpack3(_pack3(a[0] + b[0], a[1] + b[1], a[2] + b[2]))


def sub(a, b) -> tuple:
    return _unpack3(_pack3(a[0] - b[0], a[1] - b[1], a[2] - b[2]))


def mul(a, b) -> tuple:
    return _unpack3(_pack3(a[0] * b[0], a[1] * b[1], a[2] * b[2]))


def scale(a, s: float) -> tuple:
    return _unpack3(_pack3(a[0] * s, a[1] * s, a[2] * s))


def neg(a) -> tuple:
    return (-a[0], -a[1], -a[2])


def dot(a, b) -> float:
    """(x + y) + z of the lane products, as the shufps 99h / 55h pattern sums them."""
    x, y, z = _unpack3(_pack3(a[0] * b[0], a[1] * b[1], a[2] * b[2]))
    return _unpack(_pack(_unpack(_pack(x + y))[0] + z))[0]


def cross(q, p) -> tuple:
    """q x p, as shufps(shufps(p) * q - shufps(q) * p) computes it."""
    a = _unpack6(_pack6(q[1] * p[2], q[2] * p[0], q[0] * p[1], q[2] * p[1], q[0] * p[2], q[1] * p[0]))
    return _unpack3(_pack3(a[0] - a[3], a[1] - a[4], a[2] - a[5]))


def maxps(a: float, b: float) -> float:
    """maxps / maxss: the first operand when it is greater, else the second."""
    return a if a > b else b


def minps(a: float, b: float) -> float:
    return a if a < b else b


def recip_estimate(x: float) -> float:
    """Stands for rcpps (the client refines it with one Newton step)."""
    if x == 0.0:
        return math.copysign(math.inf, x)
    return f32(1.0 / x)


def rsqrt_estimate(x: float) -> float:
    """Stands for rsqrtps (refined with one Newton step by the client)."""
    if x <= 0.0:
        return math.inf if x == 0.0 else math.nan
    return f32(1.0 / math.sqrt(x))


def rsqrt(x: float) -> float:
    """r + r * (0.5 - (x * 0.5) * (r * r)): the engine's normalising reciprocal square root."""
    r = rsqrt_estimate(x)
    t = f32(f32(x * 0.5) * f32(r * r))
    return f32(f32(f32(0.5 - t) * r) + r)


def normalize(a) -> tuple:
    return scale(a, rsqrt(dot(a, a)))


# ---- rigid transforms as the engine applies them

IDENTITY = (0.0, 0.0, 0.0, 1.0)


def rotate(q, v) -> tuple:
    """v turned by the quaternion q = (x, y, z, w): v + 2 q x (w v + q x v) (0x7FF789CFFFB6 and alike)."""
    t = add(scale(v, q[3]), cross(q, v))
    c = cross(q, t)
    return add(add(c, c), v)


def unrotate(q, v) -> tuple:
    """v turned back by q (by its conjugate): v + 2 (w v + v x q) x q, bit for bit the same as turning by
    (-x, -y, -z, w)."""
    t = add(scale(v, q[3]), cross(v, q))
    c = cross(t, q)
    return add(add(c, c), v)


def columns(q) -> tuple:
    """The rotation matrix of q as the engine builds it from the quaternion (GJK 0x7FF789CD8D56)."""
    x, y, z, w = q
    x2, y2, z2 = f32(x + x), f32(y + y), f32(z + z)
    xx, yy, zz = f32(x2 * x), f32(y2 * y), f32(z2 * z)
    zx, yx, zy = f32(z2 * x), f32(y2 * x), f32(z2 * y)
    yw, zw, xw = f32(y2 * w), f32(z2 * w), f32(x2 * w)
    return ((f32(f32(1.0 - yy) - zz), f32(zw + yx), f32(zx - yw)),
            (f32(yx - zw), f32(f32(1.0 - xx) - zz), f32(xw + zy)),
            (f32(yw + zx), f32(zy - xw), f32(f32(1.0 - xx) - yy)))  # fmt: skip


def transform(p, frame) -> tuple:
    """(x c0 + y c1) + z c2 + t, the engine's matrix transform of a point; frame = (c0, c1, c2, t)."""
    c0, c1, c2, t = frame
    x, y, z = p
    if c0 is _COLUMN_X:  # the identity rotation: times 1 and plus a signed 0 are exact, only + t rounds
        x, y, z = _unpack3(_pack3(x, y, z))
        return _unpack3(_pack3(((x + y * 0.0) + z * 0.0) + t[0], ((x * 0.0 + y) + z * 0.0) + t[1],
                               ((x * 0.0 + y * 0.0) + z) + t[2]))  # fmt: skip
    return v3(f32(f32(f32(f32(x * c0[0]) + f32(y * c1[0])) + f32(z * c2[0])) + t[0]),
              f32(f32(f32(f32(x * c0[1]) + f32(y * c1[1])) + f32(z * c2[1])) + t[1]),
              f32(f32(f32(f32(x * c0[2]) + f32(y * c1[2])) + f32(z * c2[2])) + t[2]))  # fmt: skip


_D4 = struct.Struct("<4d")
_IDENTITY_BITS = _D4.pack(*IDENTITY)
IDENTITY_COLUMNS = columns(IDENTITY)  # ((1, 0, 0), (0, 1, 0), (0, 0, 1)), every 0 positive
_COLUMN_X = IDENTITY_COLUMNS[0]


def matrix(translation, rotation) -> tuple:
    """A body frame's matrix; the identity rotation (exactly (+0, +0, +0, 1)) shares IDENTITY_COLUMNS."""
    if _D4.pack(*rotation) == _IDENTITY_BITS:
        return (*IDENTITY_COLUMNS, tuple(translation))
    return (*columns(rotation), tuple(translation))


IDENTITY_FRAME = matrix((0.0, 0.0, 0.0), IDENTITY)


# ---- GJK distance (0x7FF789CD8CE0)

NO_CACHE = (0.0, (255, 255, 255, 255), (0, 0, 0, 0))
EXIT_NO_PROGRESS, EXIT_DUPLICATE, EXIT_LIMIT, EXIT_TINY, EXIT_INSIDE, EXIT_SOLVE = 5, 2, 0, 3, 4, 6


def _identity(p) -> tuple:
    """A point through the identity transform the way the engine multiplies it ((x 1 + y 0) + z 0 + 0 per
    lane): for finite lanes that is x + 0 (a -0.0 becomes +0.0), checked bit for bit."""
    x, y, z = p
    if -FLT_MAX <= x <= FLT_MAX and -FLT_MAX <= y <= FLT_MAX and -FLT_MAX <= z <= FLT_MAX:
        return x + 0.0, y + 0.0, z + 0.0
    return v3(f32(f32(f32(x * 1.0) + f32(y * 0.0)) + f32(z * 0.0)) + 0.0,
              f32(f32(f32(x * 0.0) + f32(y * 1.0)) + f32(z * 0.0)) + 0.0,
              f32(f32(f32(x * 0.0) + f32(y * 0.0)) + f32(z * 1.0)) + 0.0)  # fmt: skip


def _solve2(s: list):
    """The segment simplex (0x7FF789CD9A70): (ok, search direction). s = [vertices..., count] where a
    vertex is [w, wA, wB, a, indexA, indexB]."""
    v1, v2 = s[0], s[1]
    w1, w2 = v1[0], v2[0]
    e12 = sub(w2, w1)
    d12_2 = -dot(e12, w1)
    if d12_2 <= 0.0:
        v1[3] = 1.0
        s[4] = 1
        return True, neg(w1)
    d12_1 = dot(e12, w2)
    if d12_1 <= 0.0:
        v2[3] = 1.0
        s[0] = list(v2)
        s[4] = 1
        return True, neg(w2)
    total = f32(d12_1 + d12_2)
    if total <= 0.0:
        return False, None
    r = recip_estimate(total)
    inverse = f32(f32(2.0 - f32(r * total)) * r) if maxps(-total, total) > TINY else 0.0
    s[4] = 2
    v1[3] = f32(inverse * d12_1)
    v2[3] = f32(inverse * d12_2)
    return True, cross(cross(add(w2, w1), e12), e12)


def _recip3(total: float) -> float:
    """1/total as Solve3 refines rcpps: (r + r) - (r * r) * total."""
    r = recip_estimate(total)
    return f32(f32(r + r) - f32(f32(r * r) * total))


def _solve3(s: list):
    """The triangle simplex (0x7FF789CDB340)."""
    v1, v2, v3_ = s[0], s[1], s[2]
    w1, w2, w3 = v1[0], v2[0], v3_[0]
    e12 = sub(w2, w1)
    e13 = sub(w3, w1)
    d12_2 = -dot(e12, w1)
    d12_1 = dot(e12, w2)
    d13_2 = -dot(e13, w1)
    d13_1 = dot(e13, w3)
    e23 = sub(w3, w2)
    d23_2 = -dot(e23, w2)
    d23_1 = dot(e23, w3)
    n123 = cross(e12, e13)
    d123_1 = dot(cross(w2, w3), n123)
    d123_2 = dot(cross(w3, w1), n123)
    d123_3 = dot(cross(w1, w2), n123)
    if d12_2 <= 0.0 and d13_2 <= 0.0:
        v1[3] = 1.0
        s[4] = 1
        return True, neg(w1)
    if d12_1 <= 0.0 and d23_2 <= 0.0:
        v2[3] = 1.0
        s[0] = list(v2)
        s[4] = 1
        return True, neg(w2)
    if d13_1 <= 0.0 and d23_1 <= 0.0:
        v3_[3] = 1.0
        s[0] = list(v3_)
        s[4] = 1
        return True, neg(w3)
    if d12_1 > 0.0 and d12_2 > 0.0 and d123_3 <= 0.0:
        s[4] = 2
        inverse = _recip3(f32(d12_2 + d12_1))
        v2[3] = f32(inverse * d12_2)
        v1[3] = f32(inverse * d12_1)
        return True, cross(cross(add(w2, w1), e12), e12)
    if d13_1 > 0.0 and d13_2 > 0.0 and d123_2 <= 0.0:
        s[4] = 2
        inverse = _recip3(f32(d13_2 + d13_1))
        v3_[3] = f32(inverse * d13_2)
        v1[3] = f32(inverse * d13_1)
        s[1] = list(v3_)
        return True, cross(cross(add(w3, w1), e13), e13)
    if d23_1 > 0.0 and d23_2 > 0.0 and d123_1 <= 0.0:
        s[4] = 2
        inverse = _recip3(f32(d23_2 + d23_1))
        v3_[3] = f32(inverse * d23_2)
        v2[3] = f32(inverse * d23_1)
        s[0] = list(v3_)
        return True, cross(cross(add(w3, w2), e23), e23)
    total = f32(f32(d123_2 + d123_1) + d123_3)
    if total <= 0.0:
        return False, None
    s[4] = 3
    inverse = _recip3(total)
    v1[3] = f32(inverse * d123_1)
    v2[3] = f32(inverse * d123_2)
    v3_[3] = f32(inverse * d123_3)
    n = n123
    t = -dot(add(add(w2, w1), w3), n)
    return True, (n if t > 0.0 else neg(n))


def _solve4(s: list):
    """The tetrahedron simplex (0x7FF789CD9C40)."""
    w1, w2, w3, w4 = s[0][0], s[1][0], s[2][0], s[3][0]
    if dot(cross(sub(w2, w1), sub(w3, w1)), sub(w1, w4)) < 0.0:
        s[1], s[2] = s[2], s[1]
        w2, w3 = w3, w2
    v1, v2, v3_, v4 = s[0], s[1], s[2], s[3]
    e12 = sub(w2, w1)
    e23 = sub(w3, w2)
    d12_2 = -dot(e12, w1)
    d12_1 = dot(e12, w2)
    e13 = sub(w3, w1)
    d13_2 = -dot(e13, w1)
    d13_1 = dot(e13, w3)
    e14 = sub(w4, w1)
    d14_2 = -dot(e14, w1)
    d14_1 = dot(e14, w4)
    d23_2 = -dot(e23, w2)
    e24 = sub(w4, w2)
    e34 = sub(w4, w3)
    d23_1 = dot(e23, w3)
    d24_2 = -dot(e24, w2)
    d24_1 = dot(e24, w4)
    d34_2 = -dot(e34, w3)
    d34_1 = dot(e34, w4)
    n234 = cross(e24, e23)
    f234_2 = dot(cross(w4, w3), n234)
    f234_4 = dot(cross(w3, w2), n234)
    f234_3 = dot(cross(w2, w4), n234)
    n134 = cross(e13, e14)
    f134_1 = dot(cross(w3, w4), n134)
    f134_3 = dot(cross(w4, w1), n134)
    f134_4 = dot(cross(w1, w3), n134)
    n124 = cross(e14, e12)
    f124_1 = dot(cross(w4, w2), n124)
    f124_4 = dot(cross(w2, w1), n124)
    f124_2 = dot(cross(w1, w4), n124)
    n123 = cross(e12, e13)
    f123_1 = dot(cross(w2, w3), n123)
    f123_2 = dot(cross(w3, w1), n123)
    f123_3 = dot(cross(w1, w2), n123)
    s234 = add(add(w3, w2), w4)
    v234 = dot(s234, n234)
    s134 = add(add(w3, w1), w4)
    v134 = dot(s134, n134)
    v124 = dot(add(add(w4, w1), w2), n124)
    s123 = add(add(w2, w1), w3)
    v123 = dot(s123, n123)
    if d12_2 <= 0.0 and d13_2 <= 0.0 and d14_2 <= 0.0:
        v1[3] = 1.0
        s[4] = 1
        return True, neg(w1)
    if d12_1 <= 0.0 and d23_2 <= 0.0 and d24_2 <= 0.0:
        v2[3] = 1.0
        s[0] = list(v2)
        s[4] = 1
        return True, neg(w2)
    if d13_1 <= 0.0 and d23_1 <= 0.0 and d34_2 <= 0.0:
        v3_[3] = 1.0
        s[0] = list(v3_)
        s[4] = 1
        return True, neg(w3)
    if d14_1 <= 0.0 and d24_1 <= 0.0 and d34_1 <= 0.0:
        v4[3] = 1.0
        s[0] = list(v4)
        s[4] = 1
        return True, neg(w4)
    if d12_1 > 0.0 and d12_2 > 0.0 and f123_3 <= 0.0 and f124_4 <= 0.0:
        s[4] = 2
        inverse = f32(1.0 / f32(d12_2 + d12_1))
        v2[3] = f32(inverse * d12_2)
        v1[3] = f32(inverse * d12_1)
        return True, cross(cross(add(w2, w1), e12), e12)
    if d13_1 > 0.0 and d13_2 > 0.0 and f123_2 <= 0.0 and f134_4 <= 0.0:
        s[4] = 2
        inverse = f32(1.0 / f32(d13_2 + d13_1))
        v3_[3] = f32(inverse * d13_2)
        v1[3] = f32(inverse * d13_1)
        s[1] = list(v3_)
        return True, cross(cross(add(w3, w1), e13), e13)
    if d14_1 > 0.0 and d14_2 > 0.0 and f134_3 <= 0.0 and f124_2 <= 0.0:
        s[4] = 2
        inverse = f32(1.0 / f32(d14_2 + d14_1))
        v4[3] = f32(inverse * d14_2)
        v1[3] = f32(inverse * d14_1)
        s[1] = list(v4)
        return True, cross(cross(add(w4, w1), e14), e14)
    if d23_1 > 0.0 and d23_2 > 0.0 and f123_1 <= 0.0 and f234_4 <= 0.0:
        s[4] = 2
        inverse = f32(1.0 / f32(d23_2 + d23_1))
        v3_[3] = f32(inverse * d23_2)
        v2[3] = f32(inverse * d23_1)
        s[0] = list(v3_)
        return True, cross(cross(add(w3, w2), e23), e23)
    if d24_1 > 0.0 and d24_2 > 0.0 and f124_1 <= 0.0 and f234_3 <= 0.0:
        s[4] = 2
        inverse = f32(1.0 / f32(d24_2 + d24_1))
        v4[3] = f32(inverse * d24_2)
        v2[3] = f32(inverse * d24_1)
        s[0] = list(v4)
        return True, cross(cross(add(w4, w2), e24), e24)
    if d34_1 > 0.0 and d34_2 > 0.0 and f134_1 <= 0.0 and f234_2 <= 0.0:
        s[4] = 2
        inverse = f32(1.0 / f32(d34_2 + d34_1))
        v4[3] = f32(inverse * d34_2)
        v3_[3] = f32(inverse * d34_1)
        s[0], s[1] = list(v3_), list(v4)
        return True, cross(cross(add(w4, w3), e34), e34)
    if v234 <= 0.0 and f234_2 > 0.0 and f234_4 > 0.0 and f234_3 > 0.0:
        s[4] = 3
        inverse = f32(1.0 / f32(f32(f234_4 + f234_2) + f234_3))
        v4[3] = f32(inverse * f234_4)
        v2[3] = f32(inverse * f234_2)
        v3_[3] = f32(inverse * f234_3)
        s[0] = list(v4)
        n = cross(e23, e34)
        return True, (n if -dot(s234, n) > 0.0 else neg(n))
    if v134 <= 0.0 and f134_1 > 0.0 and f134_3 > 0.0 and f134_4 > 0.0:
        s[4] = 3
        inverse = f32(1.0 / f32(f32(f134_3 + f134_1) + f134_4))
        v1[3] = f32(inverse * f134_1)
        v4[3] = f32(inverse * f134_4)
        v3_[3] = f32(inverse * f134_3)
        s[1] = list(v4)
        n = cross(e13, e34)
        return True, (n if -dot(s134, n) > 0.0 else neg(n))
    if v124 <= 0.0 and f124_1 > 0.0 and f124_4 > 0.0 and f124_2 > 0.0:
        s[4] = 3
        inverse = f32(1.0 / f32(f32(f124_4 + f124_1) + f124_2))
        v1[3] = f32(inverse * f124_1)
        v4[3] = f32(inverse * f124_4)
        v2[3] = f32(inverse * f124_2)
        s[2] = list(v4)
        n = cross(e12, e14)
        return True, (n if -dot(add(add(w2, w1), w4), n) > 0.0 else neg(n))
    if v123 <= 0.0 and f123_1 > 0.0 and f123_2 > 0.0 and f123_3 > 0.0:
        s[4] = 3
        inverse = f32(1.0 / f32(f32(f123_2 + f123_1) + f123_3))
        v1[3] = f32(inverse * f123_1)
        v2[3] = f32(inverse * f123_2)
        v3_[3] = f32(inverse * f123_3)
        n = cross(e12, e13)
        return True, (n if -dot(s123, n) > 0.0 else neg(n))
    total = f32(f32(f32(v134 + v234) + v124) + v123)
    if total <= 0.0:
        return False, None
    inverse = f32(1.0 / total)
    s[4] = 4
    v1[3] = f32(inverse * v234)
    v2[3] = f32(inverse * v134)
    v3_[3] = f32(inverse * v124)
    v4[3] = f32(inverse * v123)
    return True, (0.0, 0.0, 0.0)


_SOLVERS = {2: _solve2, 3: _solve3, 4: _solve4}


def _closest(s: list) -> tuple:
    count = s[4]
    if count == 1:
        return s[0][0]
    p = add(scale(s[0][0], s[0][3]), scale(s[1][0], s[1][3]))
    if count >= 3:
        p = add(p, scale(s[2][0], s[2][3]))
    if count == 4:
        p = add(p, scale(s[3][0], s[3][3]))
    return p


def _witness(s: list) -> tuple:
    """The closest points on A and B (0x7FF789CD7FA0)."""
    count = s[4]
    if count == 1:
        return s[0][1], s[0][2]
    if count not in (2, 3, 4):
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    pa = add(scale(s[0][1], s[0][3]), scale(s[1][1], s[1][3]))
    pb = add(scale(s[0][2], s[0][3]), scale(s[1][2], s[1][3]))
    for k in range(2, count):
        pa = add(pa, scale(s[k][1], s[k][3]))
        pb = add(pb, scale(s[k][2], s[k][3]))
    return pa, pb


def _metric(s: list) -> float:
    count = s[4]
    if count == 2:
        e = sub(s[1][0], s[0][0])
        return dot(e, e)
    if count == 3:
        c = cross(sub(s[2][0], s[0][0]), sub(s[1][0], s[0][0]))
        return dot(c, c)
    if count == 4:
        c = cross(sub(s[2][0], s[0][0]), sub(s[1][0], s[0][0]))
        return dot(c, sub(s[3][0], s[0][0]))
    return 0.0


def _copy(s: list) -> list:
    return [list(v) if v is not None else None for v in s[:4]] + [s[4]]


class Distance:
    """What `gjk` returns: the closest points on A and B, the unit normal from A to B (of the last
    search), the distance between the points, the cache to start from next time, how it ended."""

    __slots__ = ("cache", "distance", "exit", "iterations", "lower", "normal", "point_a", "point_b")


FAR_SLACK = 1e-3  # m: a distance this much over the reach is over it whatever the last bits


def gjk(points_a, points_b, cache=NO_CACHE, reach: float | None = None) -> Distance:
    """The distance between the convex hulls of two point sets, both given in one frame (the engine's
    identity transforms). cache = (metric, indexA[4], indexB[4]) from the last call for this pair. With a
    reach, a result clearly beyond it skips the witness points and the normal (the caller reads only the
    cache): distance infinity."""
    origin = _identity(points_b[0])
    a = [sub(_identity(p), origin) for p in points_a]
    b = [(0.0, 0.0, 0.0)] + [sub(_identity(p), origin) for p in points_b[1:]]
    a0 = a[0]
    spread_a = [sub(p, a0) for p in a[1:]]  # the support search's a[k] - a[0], the same every pass
    metric, index_a, index_b = cache
    count = 0
    for k in range(4):
        if index_a[k] == 255:
            break
        count += 1
    s = [None, None, None, None, count]
    for k in range(count):
        wa, wb = a[index_a[k]], b[index_b[k]]
        s[k] = [sub(wb, wa), wa, wb, 0.0, index_a[k], index_b[k]]
    keep = count == 1
    if count > 1:
        now = _metric(s)
        keep = now >= f32(metric * 0.25) and f32(metric * 4.0) >= now and now >= EPSILON  # NaN: flush
    if not keep:
        s = [[sub(b[0], a[0]), a[0], b[0], 0.0, 0, 0], None, None, None, 1]
    normal = (0.0, 0.0, 0.0)
    backup = _copy(s)
    previous = FLT_MAX
    iterations = 0
    exit_code = None
    while True:
        saved = [(s[k][4], s[k][5]) for k in range(s[4])]
        count = s[4]
        if count == 1:
            d = neg(s[0][0])
        else:
            solver = _SOLVERS.get(count)
            ok, d = solver(s) if solver else (True, (0.0, 0.0, 0.0))
            if not ok:
                exit_code = EXIT_SOLVE
                s = backup
                break
        p = _closest(s)
        distance_sq = dot(p, p)
        if previous <= distance_sq:
            exit_code = EXIT_NO_PROGRESS
            s = backup
            break
        if s[4] == 4:
            normal = (0.0, 0.0, 0.0)
            exit_code = EXIT_INSIDE
            break
        if not dot(d, d) > TINY:
            exit_code = EXIT_TINY
            s = backup
            break
        normal = neg(d)
        best, index_a = 0.0, 0
        for k, spread in enumerate(spread_a, 1):
            value = dot(spread, normal)
            if best < value:
                best, index_a = value, k
        best, index_b = 0.0, 0
        for k in range(1, len(b)):  # b[0] is the origin: b[k] - b[0] is b[k]
            value = dot(b[k], d)
            if best < value:
                best, index_b = value, k
        wa, wb = a[index_a], b[index_b]
        s[s[4]] = [sub(wb, wa), wa, wb, 0.0, index_a, index_b]
        iterations += 1
        if (index_a, index_b) in saved:
            exit_code = EXIT_DUPLICATE
            break
        if iterations == 20:
            normal = neg(d)
            exit_code = EXIT_LIMIT
            break
        backup = _copy(s)
        s[4] += 1
        previous = distance_sq
    out = Distance()
    count = s[4]
    if reach is not None:
        p = _closest(s)
        far = dot(p, p)
        if far > (reach + FAR_SLACK) ** 2:
            out.distance = math.inf
            out.lower = math.sqrt(far)
            out.point_a = out.point_b = out.normal = None
            out.iterations, out.exit = iterations, exit_code
            out.cache = (_metric(s), tuple(s[k][4] if k < count else 255 for k in range(4)),
                         tuple(s[k][5] if k < count else 0 for k in range(4)))  # fmt: skip
            return out
    point_a, point_b = _witness(s)
    gap = sub(point_b, point_a)
    out.distance = out.lower = f32(math.sqrt(dot(gap, gap)))
    out.point_a = add(point_a, origin)
    out.point_b = add(point_b, origin)
    out.iterations = iterations
    out.exit = exit_code
    count = s[4]
    out.cache = (_metric(s), tuple(s[k][4] if k < count else 255 for k in range(4)),
                 tuple(s[k][5] if k < count else 0 for k in range(4)))  # fmt: skip
    out.normal = normalize(normal) if dot(normal, normal) > TINY else (0.0, 0.0, 0.0)
    return out


# ---- small helpers of the narrow phase


def atan2_approx(y: float, x: float) -> float:
    """The engine's atan2 (0x7FF789CCB970): z / (1 + 0.28 z^2) and its complement."""
    if x == 0.0:
        if y > 0.0:
            return HALF_PI
        return 0.0 if y == 0.0 else -HALF_PI
    z = f32(y / x)
    if abs(z) < 1.0:
        angle = f32(z / f32(f32(f32(z * ATAN_K) * z) + 1.0))
        if x >= 0.0:
            return angle
        return f32(angle + PI) if y >= 0.0 else f32(angle - PI)
    angle = f32(HALF_PI - f32(z / f32(f32(z * z) + ATAN_K)))
    return angle if y >= 0.0 else f32(angle - PI)


def _splat_tiny(a: float) -> bool:
    """TINY < (a*a + a*a) + a*a: the engine tests a scalar for zero as the squared length of its splat."""
    sq = f32(a * a)
    return f32(f32(sq + sq) + sq) > TINY


def _clamp01(x: float) -> float:
    return maxps(0.0, minps(x, 1.0))


def closest_segments(a0, a1, b0, b1) -> tuple:
    """The closest points of two segments (0x7FF789CD8720 with radii 0, then 0x7FF789CD8B30)."""
    d1 = sub(a1, a0)
    d2 = sub(b1, b0)
    r = sub(a0, b0)
    a = dot(d1, d1)
    e = dot(d2, d2)
    nz1, nz2 = _splat_tiny(a), _splat_tiny(e)
    if not nz1 and not nz2:
        pa, pb = a0, b0
    else:
        f = dot(r, d2)
        if not nz1:
            s, t = 0.0, _clamp01(f32(f / e))
        else:
            c = dot(r, d1)
            if not nz2:
                t, s = 0.0, _clamp01(f32(-c / a))
            else:
                b = dot(d2, d1)
                denominator = f32(f32(e * a) - f32(b * b))
                if _splat_tiny(denominator):
                    s = _clamp01(f32(f32(f32(b * f) - f32(c * e)) / denominator))
                else:
                    s = 0.0
                t = f32(f32(f32(b * s) + f) / e)
                if t < 0.0:
                    t, s = 0.0, _clamp01(f32(-c / a))
                elif t > 1.0:
                    t, s = 1.0, _clamp01(f32(f32(b - c) / a))
        pa = add(scale(d1, s), a0)
        pb = add(scale(d2, t), b0)
    gap = sub(pb, pa)
    if not dot(gap, gap) > TINY:
        return pa, pa
    n = normalize(gap)
    return add(scale(n, 0.0), pa), sub(pb, scale(n, 0.0))


# ---- capsule against one triangle of a mesh (0x7FF789CEE5E0, wrapper 0x7FF789CEFC20)

SKIN = f32(0.00999999978)  # the triangle's radius (xmmword_7FF78B585A30), also the speculative margin
TOUCH = f32(0.00499999989)  # closer than this the GJK normal is not trusted (xmmword_7FF78B586460)
FACE_COS = f32(0.99619472)  # 5 degrees (dword_7FF78B586030)
FLAT_COS = f32(0.999390841)  # 2 degrees: a clip point on an edge to a flat neighbour is left to it
EDGE_TOLERANCE, EDGE_SLOP = f32(0.899999976), f32(0.00249999994)
BEND = f32(0.0349065848)  # 2 degrees of dihedral angle
HALF_PI, PI, ATAN_K = f32(1.57079637), f32(3.14159274), f32(0.280000001)
FACE, EDGE, VERTEX = 0, 1, 2


class Triangle:
    """A mesh triangle as the engine fetches it (0x7FF789CF3670): its corners, for each edge k (corner k to
    corner k+1) the far corner of the neighbour across it (its own third corner when there is none) and
    whether there is one, the triangle's number, its surface and mask."""

    __slots__ = ("bottom", "corners", "has", "index", "mask", "normal", "sides", "surface", "top", "wings")

    def __init__(self, corners, wings, has, index=0, surface=0, mask=0) -> None:
        self.corners = tuple(corners)
        self.wings = tuple(wings)
        self.has = tuple(has)
        self.index = index
        self.surface = surface
        self.mask = mask
        v0, v1, v2 = self.corners
        self.normal = normalize(cross(sub(v1, v0), sub(v2, v0)))
        self.sides = None  # the neighbour normals, when first needed
        # The corners' max and min per lane as maxps / minps take them (the sweep's AABB filter).
        self.top = tuple(maxps(maxps(v0[k], v1[k]), v2[k]) for k in range(3))
        self.bottom = tuple(minps(minps(v0[k], v1[k]), v2[k]) for k in range(3))

    def neighbour_normals(self) -> tuple:
        """The normal of the neighbour across each edge (0x7FF789CEF970); with no neighbour, the
        triangle's own reversed."""
        if self.sides is None:
            (v0, v1, v2), (w0, w1, w2) = self.corners, self.wings
            self.sides = (normalize(cross(sub(v1, w0), sub(v0, w0))),
                          normalize(cross(sub(v2, w1), sub(v1, w1))),
                          normalize(cross(sub(v0, w2), sub(v2, w2))))  # fmt: skip
        return self.sides


class Point:
    """A manifold point: where, its separation, its feature id (A index, B index, A type, B type,
    the upper dword: the triangle)."""

    __slots__ = ("id", "position", "separation")

    def __init__(self, position, separation, ident) -> None:
        self.position = position
        self.separation = separation
        self.id = ident


class Manifold:
    """A normal and its points. `gap`: with no points because the shapes were beyond reach, their core
    distance (within float32 noise; the sweep reads it to skip casts that cannot hit)."""

    __slots__ = ("gap", "normal", "points")

    def __init__(self, normal=(0.0, 0.0, 0.0), points=None) -> None:
        self.normal = normal
        self.points = points if points is not None else []
        self.gap = None


def _clip(points, side, offset, edge):
    """Keep what is inside one side plane of the triangle; add the crossing as a point of that edge."""
    (q0, id0), (q1, id1) = points
    s0 = f32(dot(q0, side) - offset)
    s1 = f32(dot(q1, side) - offset)
    out = []
    if s0 <= 0.0:
        out.append((q0, id0))
    if s1 <= 0.0:
        out.append((q1, id1))
    if f32(s1 * s0) < 0.0:
        t = f32(s0 / f32(s0 - s1))
        out.append((add(scale(sub(q1, q0), t), q0), (edge, 0, EDGE, EDGE, 0)))
    return out


def _face_contact(tri: Triangle, p0, p1, radius: float, flip: bool) -> Manifold:
    """The capsule's segment clipped to the triangle's prism (0x7FF789CEF2C0): up to two points along the
    face normal."""
    v = tri.corners
    n = tri.normal
    points = [(p0, (0, 0, FACE, VERTEX, tri.index)), (p1, (0, 1, FACE, VERTEX, tri.index))]
    for k in range(3):
        side = normalize(cross(sub(v[(k + 1) % 3], v[k]), n))
        points = _clip(points[:2], side, f32(dot(side, v[k]) + SKIN), k)
        if len(points) < 2:
            return Manifold()
    normal = neg(n) if flip else n
    sides = tri.neighbour_normals()
    total = f32(SKIN + radius)
    out = Manifold(normal)
    for q, ident in points[:2]:
        if ident[2] == EDGE and dot(sides[ident[0]], n) > FLAT_COS:
            continue
        separation = dot(sub(q, v[0]), normal)
        if separation <= total:
            out.points.append(Point(sub(q, scale(normal, radius)), f32(separation - total), ident))
    return out


def _edge_separation(tri: Triangle, p0, p1, edge: int) -> float:
    """The separation along edge x segment, the axis turned away from the third corner (0x7FF789CEFAB0)."""
    v = tri.corners
    k1 = edge + 1 if edge + 1 <= 2 else 0
    k2 = k1 + 1 if k1 + 1 <= 2 else 0
    axis = cross(sub(v[k1], v[edge]), sub(p1, p0))
    length = dot(axis, axis)
    if not length > TINY:
        return -FLT_MAX
    axis = scale(axis, rsqrt(length))
    if dot(sub(v[edge], v[k2]), axis) < 0.0:
        axis = neg(axis)
    return dot(sub(p0, v[edge]), axis)


def _edge_contact(tri: Triangle, p0, p1, radius: float, edge: int) -> Manifold:
    """The capsule's segment against one edge, when they overlap (0x7FF789CEF060)."""
    v = tri.corners
    k1 = edge + 1 if edge + 1 <= 2 else 0
    k2 = k1 + 1 if k1 + 1 <= 2 else 0
    pa, pb = closest_segments(v[edge], v[k1], p0, p1)
    axis = cross(sub(v[k1], v[edge]), sub(p1, p0))
    length = dot(axis, axis)
    if not length > TINY:
        return Manifold()
    axis = scale(axis, rsqrt(length))
    n = neg(axis) if dot(sub(v[edge], v[k2]), axis) < 0.0 else axis
    separation = f32(dot(sub(pb, pa), n) - f32(SKIN + radius))
    if separation > 0.0:
        return Manifold()
    return Manifold(n, [Point(sub(pb, scale(n, radius)), separation, (edge, 0, EDGE, EDGE, 0))])


def capsule_triangle(tri: Triangle, p0, p1, radius: float, cache=NO_CACHE):
    """(manifold, cache): the capsule (segment p0..p1, radius) against one triangle, both in the mesh's
    frame. The cache is the triangle's GJK simplex from the last call (0x7FF789CEE5E0)."""
    seg = sub(p1, p0)
    if not dot(seg, seg) > TINY:
        return Manifold(), cache
    v = tri.corners
    n = tri.normal
    centre = scale(add(p0, p1), 0.5)
    if dot(sub(centre, v[0]), n) < 0.0:
        return Manifold(), cache  # one sided: the capsule's centre is behind the triangle
    reach = f32(radius + SKIN)
    g = gjk(v, (p0, p1), cache, reach)
    cache = g.cache
    if reach < g.distance:
        out = Manifold()
        out.gap = g.lower
        return out, cache
    sides = tri.neighbour_normals()
    index_a, index_b = g.cache[1], g.cache[2]
    count = 4 if index_a[3] != 255 else next(k for k in range(4) if index_a[k] == 255)
    if dot(g.normal, g.normal) > TINY and not g.distance < TOUCH and count != 4:
        normal = normalize(g.normal)
        if dot(n, normal) > FACE_COS:
            face = _face_contact(tri, p0, p1, radius, False)
            if face.points:
                return face, cache
        on_a = [0, 0, 0]
        on_b = [0, 0]
        for k in range(count):
            on_a[index_a[k]] = 1
            on_b[index_b[k]] = 1
        corners_a = on_a[2] + on_a[1] + on_a[0]
        if corners_a == 1:
            k = index_a[0]
            before = k - 1 if k > 0 else 2
            after = k + 1 if k != 2 else 0
            if tri.has[before] and dot(cross(normal, sides[before]), sub(v[k], v[before])) < 0.0:
                return Manifold(), cache
            if tri.has[k] and dot(cross(normal, sides[k]), sub(v[after], v[k])) < 0.0:
                return Manifold(), cache
            a_index, a_type = k, VERTEX
        elif corners_a == 2:
            if on_a[0] and on_a[1]:
                edge, other = 0, 1
            elif on_a[1] and on_a[2]:
                edge, other = 1, 2
            else:
                edge, other = 2, 0
            if tri.has[edge] and dot(cross(normal, sides[edge]), sub(v[other], v[edge])) < 0.0:
                return Manifold(), cache
            a_index, a_type = edge, EDGE
        else:
            a_index, a_type = 0, FACE
        b_index, b_type = (index_b[0], VERTEX) if on_b[0] + on_b[1] == 1 else (0, EDGE)
        point = Point(sub(g.point_b, scale(normal, radius)), f32(g.distance - reach),
                      (a_index, b_index, a_type, b_type, tri.index))  # fmt: skip
        return Manifold(normal, [point]), cache
    # Too close for the GJK normal: separating axes, the face against the three edge x segment axes.
    best, feature, kind = -FLT_MAX, -1, VERTEX
    face_separation = minps(dot(sub(p0, v[0]), n), dot(sub(p1, v[0]), n))
    if best < face_separation:
        best, feature, kind = face_separation, 0, FACE
    directions = (normalize(sub(v[1], v[0])), normalize(sub(v[2], v[1])), normalize(sub(v[0], v[2])))
    for edge in range(3):
        separation = _edge_separation(tri, p0, p1, edge)
        if not f32(f32(EDGE_TOLERANCE * best) + EDGE_SLOP) < separation:
            continue
        side = sides[edge]
        angle = atan2_approx(dot(cross(side, n), directions[edge]), dot(side, n))
        if tri.has[edge]:
            if angle >= -BEND:
                continue
            if not angle >= BEND and f32(dot(side, seg) * dot(n, seg)) >= 0.0:
                continue
        best, feature, kind = separation, edge, EDGE
    if kind == FACE:
        return _face_contact(tri, p0, p1, radius, feature != 0), cache
    if kind == EDGE:
        return _edge_contact(tri, p0, p1, radius, feature), cache
    return Manifold(), cache


# ---- capsule against a convex polytope (0x7FF789CE5150 -> 0x7FF789CE43A0)

HULL_SKIN = SKIN  # the polytope's radius in its GJK proxy (0x7FF789CD81D0)
EDGE_EPSILON = EPSILON  # dword_7FF78B50B050: an edge x segment axis shorter than this is no axis
SPHERE, CAPSULE, HULL, MESH = 0, 1, 2, 3  # the shape types (shape +0x20)


class Hull:
    """A convex polytope as the engine keeps it (0x50 bytes, world +0x670): its centroid (+0), vertices
    (+0x10), face planes (normal, offset; +0x18), each face's first half edge (+0x20) and the half edges
    (+0x28: twin as an offset, origin vertex, face, next half edge), twins side by side (2k, 2k + 1).
    The same data as chunk 0x11's polytopes, float32."""

    __slots__ = ("centroid", "edges", "faces", "mask", "planes", "vertices")
    kind = HULL

    def __init__(self, centroid, vertices, planes, faces, edges, mask=0) -> None:
        self.centroid = tuple(centroid)
        self.vertices = [tuple(v) for v in vertices]
        self.planes = [tuple(p) for p in planes]
        self.faces = list(faces)
        self.edges = [tuple(e) for e in edges]
        self.mask = mask

    def find_edge(self, a: int, b: int) -> int:
        """The even half edge of the edge from vertex a to vertex b, -1 if none (0x7FF789CE9450)."""
        edges = self.edges
        for k, (twin, origin, _face, _next) in enumerate(edges):
            if origin == a and edges[k + twin][1] == b:
                return k & 0xFFFE
        return -1


def _hull_face(hull: Hull, face: int, p0, p1, radius: float) -> Manifold:
    """The segment clipped to the side planes of one face, the points within the skins kept
    (0x7FF789CE4C30)."""
    plane = hull.planes[face]
    n = plane[:3]
    vertices, edges = hull.vertices, hull.edges
    points = [(p0, (face, 0, FACE, VERTEX, 0)), (p1, (face, 1, FACE, VERTEX, 0))]
    first = edge = hull.faces[face]
    start = vertices[edges[first][1]]
    count = 0
    while True:
        following = edges[edge][3]
        end = vertices[edges[following][1]]
        side = cross(sub(end, start), n)
        length = dot(side, side)
        if length > TINY:
            side = scale(side, rsqrt(length))
            at = dot(side, start)
            (q0, id0), (q1, id1) = points[0], points[1]
            s0 = f32(dot(q0, side) - at)
            s1 = f32(dot(q1, side) - at)
            out = []
            if s0 <= 0.0:
                out.append((q0, id0))
            if s1 <= 0.0:
                out.append((q1, id1))
            if f32(s1 * s0) < 0.0:
                t = f32(s0 / f32(s0 - s1))
                out.append((add(scale(sub(q1, q0), t), q0), (edge & 0xFE, 0, EDGE, EDGE, 0)))
            points = out
            count = len(out)
        edge, start = following, end
        if count != 2:
            return Manifold()
        if edge == first:
            break
    total = f32(SKIN + radius)
    w = plane[3]
    out = Manifold(n)
    for q, ident in points:
        separation = f32(f32(dot(q, n) - w) - total)
        if separation <= 0.0:
            out.points.append(Point(sub(q, scale(n, radius)), separation, ident))
    return out


def _hull_edge_separation(hull: Hull, p0, p1, edge: int) -> float:
    """The separation along edge x segment, the axis turned away from the centroid (0x7FF789CE5000)."""
    edges = hull.edges
    start = hull.vertices[edges[edge][1]]
    end = hull.vertices[edges[edge + edges[edge][0]][1]]
    axis = cross(sub(end, start), sub(p1, p0))
    length = dot(axis, axis)
    if not length >= EDGE_EPSILON:
        return -FLT_MAX
    axis = scale(axis, rsqrt(length))
    if dot(sub(start, hull.centroid), axis) < 0.0:
        axis = neg(axis)
    return dot(sub(p0, start), axis)


def _hull_edge(hull: Hull, edge: int, p0, p1, radius: float) -> Manifold:
    """One point between the segment and an edge, at the middle of their closest points
    (0x7FF789CE49A0)."""
    edges = hull.edges
    start = hull.vertices[edges[edge][1]]
    end = hull.vertices[edges[edge + edges[edge][0]][1]]
    pa, pb = closest_segments(start, end, p0, p1)
    axis = cross(sub(end, start), sub(p1, p0))
    length = dot(axis, axis)
    if not length >= EDGE_EPSILON:
        return Manifold()
    axis = scale(axis, rsqrt(length))
    n = neg(axis) if dot(sub(start, hull.centroid), axis) < 0.0 else axis
    separation = f32(dot(sub(pb, pa), n) - f32(radius + SKIN))
    return Manifold(n, [Point(scale(add(pa, pb), 0.5), separation, (edge & 0xFF, 0, EDGE, EDGE, 0))])


def capsule_hull(hull: Hull, p0, p1, radius: float, cache=NO_CACHE):
    """(manifold, cache): the capsule against a convex polytope, both in the polytope's frame
    (0x7FF789CE43A0). GJK first; a face within 5 degrees of its normal clips the segment, else one
    point at the closest features. Closer than 5 mm (or the segment inside): separating axes."""
    reach = f32(SKIN + radius)
    g = gjk(hull.vertices, (p0, p1), cache, reach)
    cache = g.cache
    if reach < g.distance:
        out = Manifold()
        out.gap = g.lower
        return out, cache
    index_a, index_b = g.cache[1], g.cache[2]
    full = 255 not in index_a[:3] and index_a[3] != 255
    if dot(g.normal, g.normal) > TINY and not g.distance < f32(SKIN * 0.5) and not full:
        normal = normalize(g.normal)
        best, face = -FLT_MAX, 0
        for k, plane in enumerate(hull.planes):
            value = _dot_yx(plane, normal)
            if value > best:
                best, face = value, k
        if hull.planes and best > FACE_COS:
            manifold = _hull_face(hull, face, p0, p1, radius)
            if manifold.points:
                return manifold, cache
        a0, b0 = index_a[0], index_b[0]
        other, corners_a, corners_b = -1, 1, 1
        if a0 != 255 and index_a[1] != 255:
            if index_a[2] == 255:
                if index_a[1] != a0:
                    other, corners_a = index_a[1], 2
                if index_b[1] != b0:
                    corners_b = 2
            elif index_a[3] == 255:
                if index_a[1] != a0:
                    other, corners_a = index_a[1], 2
                elif index_a[2] != a0:
                    other, corners_a = index_a[2], 2
                if index_b[1] != b0 or index_b[2] != b0:
                    corners_b = 2
        if corners_a == 1:
            a_index, a_type = a0, VERTEX
        else:
            found = hull.find_edge(a0, other)
            a_index, a_type = (found & 0xFF, EDGE) if found >= 0 else (0, 3)
        b_index, b_type = (b0, VERTEX) if corners_b == 1 else (0, EDGE)
        point = Point(sub(g.point_b, scale(normal, radius)), f32(g.distance - reach),
                      (a_index, b_index, a_type, b_type, 0))  # fmt: skip
        return Manifold(normal, [point]), cache
    # Separating axes: the faces, then the edges whose faces the segment's direction separates.
    best, feature, kind = -FLT_MAX, -1, VERTEX
    for k, plane in enumerate(hull.planes):
        d0, d1 = dot(p0, plane), dot(p1, plane)
        separation = f32((d0 if d0 < d1 else d1) - plane[3])
        if reach < separation:
            return Manifold(), cache
        if best < separation:
            best, feature, kind = separation, k, FACE
    seg = sub(p1, p0)
    edges, planes = hull.edges, hull.planes
    slack = f32(0.5 * TOUCH)
    for k in range(0, len(edges), 2):
        twin, _origin, owner, _next = edges[k]
        if f32(dot(planes[edges[k + twin][2]], seg) * dot(planes[owner], seg)) >= 0.0:
            continue
        separation = _hull_edge_separation(hull, p0, p1, k)
        if reach < separation:
            return Manifold(), cache
        if f32(f32(best * EDGE_TOLERANCE) + slack) < separation:
            best, feature, kind = separation, k, EDGE
    if kind == FACE:
        return _hull_face(hull, feature, p0, p1, radius), cache
    if kind == EDGE:
        return _hull_edge(hull, feature, p0, p1, radius), cache
    return Manifold(), cache


# ---- capsule against capsule (0x7FF789CD6970) and against sphere (0x7FF789CD7300), in the world frame

PARALLEL = f32(0.998782039)  # xmmword_7FF78B5859A0: cos^2 of 2 degrees
X_AXIS = (1.0, 0.0, 0.0)


def _unit_or_x(d, dd: float) -> tuple:
    """d * rsqrt(dd), or (1, 0, 0) when dd is not above TINY (the andps / andnps select)."""
    return scale(d, rsqrt(dd)) if dd > TINY else X_AXIS


def capsule_capsule(a0, a1, ra: float, b0, b1, rb: float) -> Manifold:
    """Capsule A (a0..a1, ra) against capsule B; the normal points from A to B. Nearly parallel segments
    (cos^2 >= 0.998782) give a point for each end of A, else one point between the closest points."""
    d1, d2, r = sub(a1, a0), sub(b1, b0), sub(a0, b0)
    total = f32(rb + ra)
    total_sq = f32(total * total)
    a, e, b = dot(d1, d1), dot(d2, d2), dot(d2, d1)
    c, f = dot(r, d1), dot(r, d2)
    if not _splat_tiny(a) or not _splat_tiny(e):
        pa, pb = a0, b0
        if not _splat_tiny(a):
            if _splat_tiny(e):
                pb = add(scale(d2, _clamp01(f32(f / e))), b0)
        else:
            pa = add(scale(d1, _clamp01(f32(-c / a))), a0)
        d = sub(pb, pa)
        dd = dot(d, d)
        if not dd <= total_sq:
            return Manifold()
        distance = f32(math.sqrt(dd))
        n = _unit_or_x(d, dd)
        h = f32(f32(f32(distance + ra) - rb) * 0.5)
        return Manifold(n, [Point(add(scale(n, h), pa), f32(distance - total), (0, 0, 0, 0, 0))])
    cos_sq = f32(f32(b * b) / f32(e * a))
    if cos_sq < PARALLEL:
        ia, ie = rsqrt(a), rsqrt(e)
        u1, u2 = scale(d1, ia), scale(d2, ie)
        cosine, cu, fu = dot(u2, u1), dot(u1, r), dot(u2, r)
        s = _clamp01(f32(f32(f32(f32(fu * cosine) - cu) * ia) / f32(1.0 - cos_sq)))
        t = f32(f32(f32(s * b) + f) / e)
        if t < 0.0:
            s, t = _clamp01(f32(-c / a)), 0.0
        elif t > 1.0:
            s, t = _clamp01(f32(f32(b - c) / a)), 1.0
        pb = add(scale(d2, t), b0)
        pa = add(scale(d1, s), a0)
        d = sub(pb, pa)
        dd = dot(d, d)
        if not dd <= total_sq:
            return Manifold()
        a_index, a_type = (0, VERTEX) if s == 0.0 else ((1, VERTEX) if s == 1.0 else (0, EDGE))
        b_index, b_type = (0, VERTEX) if t == 0.0 else ((1, VERTEX) if t == 1.0 else (0, EDGE))
        distance = f32(math.sqrt(dd))
        n = _unit_or_x(d, dd)
        h = f32(f32(f32(distance + ra) - rb) * 0.5)
        point = Point(add(scale(n, h), pa), f32(distance - total), (a_index, b_index, a_type, b_type, 0))
        return Manifold(n, [point])
    # Nearly parallel: each end of A against B.
    out = Manifold()
    acc = (0.0, 0.0, 0.0)
    nearest = []
    for k, q in enumerate((a0, a1)):
        t = f32(dot(sub(q, b0), d2) / e)
        if t <= 0.0 or t >= 1.0:
            on_b, b_index = (b0, 0) if t <= 0.0 else (b1, 1)
            s = f32(dot(sub(on_b, a0), d2) / b)
            if s <= 0.0:
                on_a, a_index, a_type = a0, 0, VERTEX
            elif s >= 1.0:
                on_a, a_index, a_type = a1, 1, VERTEX
            else:
                on_a, a_index, a_type = add(scale(d1, s), a0), 0, EDGE
            ident = (a_index, b_index, a_type, VERTEX, 0)
        else:
            on_b, on_a = add(scale(d2, t), b0), q
            ident = (k, 0, VERTEX, EDGE, 0)
        nearest.append(on_a)
        d = sub(on_b, on_a)
        dd = dot(d, d)
        if not dd <= total_sq:
            continue
        if dd > TINY:
            distance = f32(math.sqrt(dd))
            h = f32(f32(f32(distance + ra) - rb) * 0.5)
            position = add(scale(scale(d, rsqrt(dd)), h), on_a)
            separation = f32(distance - total)
        else:
            h = f32(f32(ra - rb) * 0.5)
            position = add((h, h * 0.0, h * 0.0), on_a)
            separation = -total
        out.points.append(Point(position, separation, ident))
        acc = add(acc, d)
    length = dot(acc, acc)
    out.normal = _unit_or_x(acc, length)
    if len(out.points) == 2 and nearest[0] == nearest[1]:
        del out.points[1]
    return out


def capsule_sphere(a0, a1, ra: float, centre, rb: float) -> Manifold:
    """Capsule A against a sphere; the normal points from the capsule to the sphere. The segment's point
    nearest the centre (0x7FF789CD8A60, then 0x7FF789CD8B30 with both radii 0)."""
    e = sub(a1, a0)
    along = dot(sub(centre, a0), e)
    if along <= 0.0:
        pa = a0
    else:
        length = dot(e, e)
        pa = a1 if length <= along else add(scale(e, f32(along / length)), a0)
    gap = sub(centre, pa)
    gg = dot(gap, gap)
    distance = f32(math.sqrt(gg)) if gg > TINY else 0.0
    total = f32(rb + ra)
    if total < distance:
        return Manifold()
    if distance < EPSILON:  # centre on the segment: a normal square to it
        axis = normalize(e)
        n = (axis[2], 0.0, -axis[0]) if axis[2] > 0.5 else (axis[1], -axis[0], 0.0)
        return Manifold(normalize(n), [Point(pa, -total, (0, 0, 0, 0, 0))])
    n = v3(gap[0] / distance, gap[1] / distance, gap[2] / distance)
    position = scale(add(scale(n, f32(ra - rb)), add(pa, centre)), 0.5)
    return Manifold(n, [Point(position, f32(distance - total), (0, 0, 0, 0, 0))])


# ---- convex shapes and their pair narrow phase (0x7FF789CFF970)


class Sphere:
    __slots__ = ("centre", "mask", "radius")
    kind = SPHERE

    def __init__(self, centre, radius: float, mask: int = 0) -> None:
        self.centre = tuple(centre)
        self.radius = f32(radius)
        self.mask = mask


class Capsule:
    __slots__ = ("a", "b", "mask", "radius")
    kind = CAPSULE

    def __init__(self, a, b, radius: float, mask: int = 0) -> None:
        self.a, self.b = tuple(a), tuple(b)
        self.radius = f32(radius)
        self.mask = mask


def _placed(p, frame) -> tuple:
    """A shape point in the world: rotate by the body's quaternion, then add its translation."""
    return add(rotate(frame[1], p), frame[0])


def convex_manifold(a, frame_a, b, frame_b, cache=NO_CACHE):
    """(manifold in the world, cache) of a convex pair, A's type >= B's (0x7FF789CFF970): capsules and
    spheres meet in the world frame; a polytope meets the other shape in its own frame and the result
    goes back to the world. frame = (translation, rotation quaternion)."""
    if a.kind == CAPSULE:
        a0, a1 = _placed(a.a, frame_a), _placed(a.b, frame_a)
        if b.kind == SPHERE:
            return capsule_sphere(a0, a1, a.radius, _placed(b.centre, frame_b), b.radius), cache
        if b.kind == CAPSULE:
            b0, b1 = _placed(b.a, frame_b), _placed(b.b, frame_b)
            return capsule_capsule(a0, a1, a.radius, b0, b1, b.radius), cache
        return Manifold(), cache
    if a.kind == HULL and b.kind == CAPSULE:
        ta, qa = frame_a

        def inside(p):
            return unrotate(qa, sub(_placed(p, frame_b), ta))

        m, cache = capsule_hull(a, inside(b.a), inside(b.b), b.radius, cache)
        for point in m.points:
            point.position = add(rotate(qa, point.position), ta)
        m.normal = rotate(qa, m.normal)
        return m, cache
    return Manifold(), cache


# ---- a mesh contact's manifolds: the triangles' manifolds clustered by normal (0x7FF789D0CE00)

CLUSTER_COS = FACE_COS  # a normal more than 5 degrees from every cluster starts a new one
CLUSTERS = 3
CLUSTER_ROUNDS = 4
KEEP = f32(0.99000001)  # a later candidate point must be 1 % better (dword_7FF78B585FB4)
SPREAD = f32(0.00039999999)  # 2 cm squared / an area of 4 cm2 (dword_7FF78B585FB0)


def _dot_yx(a, b) -> float:
    """(y + x) + z of the lane products: the shufps 55h / 00h / AAh order (the same sum as `dot`)."""
    x, y, z = _unpack3(_pack3(a[0] * b[0], a[1] * b[1], a[2] * b[2]))
    return f32(f32(y + x) + z)


def reduce_points(normal, points: list) -> list:
    """At most four of a cluster's points (0x7FF789CE2480): the one farthest along a tangent, the one
    farthest from it, the one making the largest area with those two, the one farthest outside their
    triangle; a candidate must beat the last best by 1 %."""
    count = len(points)
    if count <= 0:
        return []
    nx, ny, nz = normal
    minus_x = f32(0.0 - nx)
    tangent = (ny, minus_x, 0.0) if maxps(f32(0.0 - ny), ny) > 0.5 else (nz, 0.0, minus_x)
    tangent = normalize(tangent)
    best, first = -FLT_MAX, -1
    for k, point in enumerate(points):
        value = _dot_yx(tangent, point.position)
        if f32(value * KEEP) > best:
            best, first = value, k
    out = [points[first]]
    p1 = points[first].position
    best, second = -FLT_MAX, -1
    for k, point in enumerate(points):
        d = sub(point.position, p1)
        value = _dot_yx(d, d)
        if f32(value * KEEP) > best:
            best, second = value, k
    if not best >= SPREAD:
        return out
    out.append(points[second])
    p2 = points[second].position
    e = sub(p2, p1)
    best, third = -FLT_MAX, -1
    for k, point in enumerate(points):
        value = abs(_dot_yx(cross(e, sub(point.position, p1)), normal))
        if f32(value * KEEP) > best:
            best, third = value, k
    if not best >= SPREAD:
        return out
    out.append(points[third])
    p3 = points[third].position
    area = _dot_yx(cross(e, sub(p3, p1)), normal)
    inverse = f32(1.0 / area)
    e32, e13 = sub(p3, p2), sub(p1, p3)
    best, fourth = FLT_MAX, -1
    for k, point in enumerate(points):
        p = point.position
        a = _dot_yx(cross(e32, sub(p, p2)), normal)
        b = _dot_yx(cross(e13, sub(p, p3)), normal)
        c = _dot_yx(cross(e, sub(p, p1)), normal)
        value = minps(f32(a * inverse), minps(f32(b * inverse), f32(c * inverse)))
        if f32(value * KEEP) < best:
            best, fourth = value, k
    if best > 0.0 or not abs(f32(area * best)) >= SPREAD:
        return out
    out.append(points[fourth])
    return out


def cluster_manifolds(manifolds: list) -> list:
    """The mesh contact's manifolds from its triangles' (0x7FF789D0CE00): up to three normals (k-means on
    the sphere, at most four rounds), each with the reduced points of the triangles nearest to it."""
    count = len(manifolds)
    if count == 0:
        return []
    reps = [manifolds[0].normal]
    for m in manifolds[1:]:
        if len(reps) >= CLUSTERS:
            break
        best = -FLT_MAX
        for rep in reps:
            best = maxps(best, _dot_yx(m.normal, rep))
        if not best >= CLUSTER_COS:
            reps.append(m.normal)
    assigned = [-1] * count
    for _ in range(CLUSTER_ROUNDS):
        sums = [(0.0, 0.0, 0.0)] * len(reps)
        changed = False
        for i, m in enumerate(manifolds):
            best, nearest = -FLT_MAX, 0
            for j, rep in enumerate(reps):
                length = dot(rep, rep)
                if length == 0.0 or length != length:
                    continue
                value = _dot_yx(m.normal, rep)
                if value > best:
                    best, nearest = value, j
            sums[nearest] = add(m.normal, sums[nearest])
            if assigned[i] != nearest:
                assigned[i] = nearest
                changed = True
        if not changed:
            break
        reps = [normalize(s) if dot(s, s) > TINY else (0.0, 0.0, 0.0) for s in sums]
    groups = [[] for _ in reps]
    for i, m in enumerate(manifolds):
        groups[assigned[i]].extend(m.points)
    return [Manifold(reps[j], reduce_points(reps[j], group)) for j, group in enumerate(groups) if group]


# ---- the triangles a mesh contact looks at (0x7FF789D0EF80, query 0x7FF789CF3900, test 0x7FF789CD79D0)

MARGIN = f32(0.100000001)  # the capsule shape's margin (shape +0x80, shape def +0x40)
LOOSE = f32(MARGIN * 4.0)  # a mesh contact's box this much too big is gathered again
AXIS_SLOP = TOUCH  # the edge axes of the triangle-box test allow 5 mm


def _abs_vec(a) -> tuple:
    """maxps(0 - a, a) per lane."""
    return tuple(maxps(f32(0.0 - x), x) for x in a)


def box_constants(v0, v1, v2) -> tuple:
    """What the triangle-box test needs of the triangle alone: its normal (and that normal's lanes made
    positive) and for each edge its direction, the half of |direction x the next edge| per lane and the
    direction's lanes made positive in the two rotated orders the test multiplies them in."""
    e0, e1, e2 = sub(v1, v0), sub(v2, v1), sub(v0, v2)
    n = cross(e0, e1)
    edges = []
    for edge, span in ((e0, e2), (e1, e0), (e2, e1)):
        u = normalize(edge)
        span_half = tuple(f32(x * 0.5) for x in _abs_vec(cross(u, span)))
        au = _abs_vec(u)
        edges.append((u, span_half, (au[2], au[0], au[1]), (au[1], au[2], au[0])))
    return n, _abs_vec(n), edges


def triangle_box(v0, v1, v2, low, high, constants=None) -> bool:
    """Whether a triangle meets a box (0x7FF789CD79D0): the box's axes, the triangle's normal, and its
    three edges crossed with the box's axes (with 5 mm of slack)."""
    centre = scale(add(low, high), 0.5)
    half = scale(sub(high, low), 0.5)
    a, b, c = sub(v0, centre), sub(v1, centre), sub(v2, centre)
    for k in range(3):
        top = maxps(maxps(a[k], b[k]), c[k])
        bottom = minps(minps(a[k], b[k]), c[k])
        if maxps(f32(bottom - half[k]), f32(f32(0.0 - half[k]) - top)) > 0.0:
            return False
    n, abs_n, edges = constants if constants is not None else box_constants(v0, v1, v2)
    s = dot(n, a)
    if f32(maxps(f32(0.0 - s), s) - dot(abs_n, half)) > 0.0:
        return False
    hy, hz = (half[1], half[2], half[0]), (half[2], half[0], half[1])
    for (u, span_half, uz, uy), pair in zip(edges, (add(c, a), add(b, a), add(c, b)), strict=True):
        centre_axes = _abs_vec(cross(u, pair))
        for k in range(3):
            radius = f32(f32(uz[k] * hy[k]) + f32(uy[k] * hz[k]))
            gap = f32(f32(f32(centre_axes[k] * 0.5) - span_half[k]) - radius)
            if gap > AXIS_SLOP:
                return False
    return True


# ---- the sweep when the engine moves a body (shape cast 0x7FF789CDBA70)

CAST_TOLERANCE = f32(0.00124999997)
CAST_SLOP = TOUCH  # the target distance shrinks to 5 mm under an initial overlap
CAST_DEEP = f32(9.99999975e-05)
CAST_ITERATIONS = 20


def shape_cast(
    points_a,
    radius_a: float,
    points_b,
    radius_b: float,
    translation,
    t_max: float,
    hit_touching: bool = False,
    shrink: bool = True,
    frame_a=None,
    frame_b=None,
    full: bool = False,
):
    """The fraction of `translation` B can move before it comes within the target distance of A (the radii,
    each at least 1 cm, less 1 cm), or None when it does not within t_max (GJK ray cast). Each point set
    goes through its body's frame (matrix(translation, rotation); None: the identity), as the cast's
    input +0x40..+0x70. hit_touching / shrink are the input's +0xA0 / +0xA1 (the mover's sweeps: 0 / 1).
    full: (fraction, point, normal) as the cast writes them (0x7FF789CDC270..0x7FF789CDC407): the normal
    is the last search direction normalised (from A toward B; minus the translation's direction when that
    is 0), the point A's witness pushed out by A's radius along it (on A's surface with its 1 cm). Checked
    against the client's own function: 6000 casts, fraction, point and normal bit for bit."""
    place_a = _identity if frame_a is None else partial(transform, frame=frame_a)
    place_b = _identity if frame_b is None else partial(transform, frame=frame_b)
    origin = place_b(points_b[0])
    a = [sub(place_a(p), origin) for p in points_a]
    b = [(0.0, 0.0, 0.0)] + [sub(place_b(p), origin) for p in points_b[1:]]
    a0 = a[0]
    spread_a = [sub(p, a0) for p in a[1:]]
    ra = maxps(radius_a, SKIN)
    sigma = f32(f32(maxps(radius_b, SKIN) + ra) - SKIN)
    target = f32(CAST_TOLERANCE + sigma)
    target_sq = f32(target * target)
    r = translation
    lam = 0.0
    previous = FLT_MAX
    stuck = False
    v = sub(b[0], a[0])
    search = v
    s = [None, None, None, None, 0]
    iterations = 0
    while True:
        distance_sq = dot(v, v)
        if distance_sq < target_sq:
            if lam != 0.0:
                break
            if not shrink:
                if hit_touching:
                    break
                return None
            if not previous <= distance_sq:  # still getting closer: refine the distance first
                stuck = True
                previous = distance_sq
            elif not distance_sq > CAST_DEEP:
                return None
            else:  # overlapping: aim 5 mm closer than now
                stuck = False
                sigma = f32(f32(math.sqrt(distance_sq)) - CAST_SLOP)
                target = f32(CAST_TOLERANCE + sigma)
                target_sq = f32(target * target)
        best, index_a = 0.0, 0
        for k, spread in enumerate(spread_a, 1):
            value = dot(spread, search)
            if best < value:
                best, index_a = value, k
        best, index_b = 0.0, 0
        away = neg(search)
        for k in range(1, len(b)):
            value = dot(b[k], away)
            if best < value:
                best, index_b = value, k
        wa, wb = a[index_a], b[index_b]
        vn = normalize(search)
        shift = scale(r, lam)
        p = sub(sub(wa, wb), shift)
        vp = -dot(p, vn)
        moved = False
        if target < vp and not stuck:
            vr = -dot(vn, r)
            if vr < EPSILON:
                return None
            advanced = f32(f32(f32(vp - sigma) / vr) + lam)
            if t_max <= advanced:
                return None
            moved = advanced != lam
            lam = advanced
            if moved:
                shift = scale(r, lam)
        duplicate = False
        for k in range(s[4]):
            vertex = s[k]
            if moved:  # the B points go along with lam; unchanged lam leaves them as they are
                vertex[2] = add(b[vertex[5]], shift)
                vertex[0] = sub(vertex[2], vertex[1])
            if vertex[4] == index_a and vertex[5] == index_b:
                duplicate = True
        if not duplicate:
            wb_shifted = add(shift, wb)
            s[s[4]] = [sub(wb_shifted, wa), wa, wb_shifted, 1.0, index_a, index_b]
            s[4] += 1
        count = s[4]
        if count == 1:
            search = s[0][0]
        else:
            solver = _SOLVERS.get(count)
            if solver is not None:
                ok, d = solver(s)
                search = neg(d) if ok else search
                if not ok:
                    return None
            if s[4] == 4:
                if not hit_touching:
                    return None
                break
        v = _cast_closest(s)
        iterations += 1
        if iterations >= CAST_ITERATIONS:
            break
    if not full:
        return lam
    witness = a[0]  # no simplex yet (a touch at the start)
    count = s[4]
    if count:
        witness = scale(s[0][1], s[0][3])
        for k in range(1, count):
            witness = add(witness, scale(s[k][1], s[k][3]))
    if dot(search, search) > TINY:
        normal = scale(search, rsqrt(dot(search, search)))
    elif dot(r, r) > TINY:
        normal = neg(scale(r, rsqrt(dot(r, r))))
    else:
        normal = (0.0, 0.0, 0.0)
    return lam, add(add(witness, origin), scale(normal, ra)), normal


def _cast_closest(s: list) -> tuple:
    """The simplex's closest point as the cast sums it: w2 a2 + a1 w1, then + w3 a3, + w4 a4."""
    count = s[4]
    if count == 1:
        return s[0][0]
    if count not in (2, 3, 4):
        return (0.0, 0.0, 0.0)
    p = add(scale(s[1][0], s[1][3]), scale(s[0][0], s[0][3]))
    for k in range(2, count):
        p = add(p, scale(s[k][0], s[k][3]))
    return p


# ---- the world's shapes as the engine keeps them


def _negative(x: float) -> bool:
    """The sign bit, as movmskps reads it (-0.0 counts)."""
    return x < 0.0 or (x == 0.0 and math.copysign(1.0, x) < 0.0)


def overlaps(a_low, a_high, b_low, b_high) -> bool:
    """Whether two fat AABBs meet: no sign bit in (a.high - b.low) | (b.high - a.low) (0x7FF789D110E0). The
    float64 difference of two float32 values has the float32 difference's sign, -0.0 included."""
    for k in range(3):
        d = a_high[k] - b_low[k]
        if d < 0.0 or (d == 0.0 and math.copysign(1.0, d) < 0.0):
            return False
        d = b_high[k] - a_low[k]
        if d < 0.0 or (d == 0.0 and math.copysign(1.0, d) < 0.0):
            return False
    return True


class MeshShape:
    """A triangle mesh in its own frame (the map's mesh: the identity frame): float32 corners (a prop's
    already times its scale, as the engine's fetch multiplies them, 0x7FF789CF3670), for each triangle its
    corner and wing vertex numbers (-1: no neighbour across that edge), mask and surface. The engine walks
    its BVH; the BVH of every map visits the triangles in increasing number (checked on the data), so the
    candidates are the triangles that pass the box test, in number order. Kept in flat arrays (a map has
    up to 200,000 triangles); a triangle's Triangle is made when first needed."""

    CELL = 2.0
    LARGE = 4096  # a triangle over more grid cells than this is checked by every query instead
    kind = MESH

    def __init__(self, vertices, triangles, mask: int = 0xFFFF, raw=None, scale=(1.0, 1.0, 1.0)) -> None:
        self.vertices = vertices  # [(x, y, z)] float32
        self.raw = raw if raw is not None else vertices  # before the scale: what the sweeps cast against
        self.scale = tuple(scale)
        self.inverse_scale = tuple(f32(1.0 / s) for s in scale)  # a scale other than 1: unverified
        self.mask = mask
        self.cache: dict[int, Triangle] = {}
        self.boxes: dict[int, tuple] = {}  # the triangle-box test's constants, when first needed
        self.corners = array("i")  # a, b, c, wing0, wing1, wing2 per triangle
        self.masks, self.surfaces = array("H"), array("H")
        self.bounds = array("f")  # low x, y, z, high x, y, z per triangle (float32, exact)
        grid: dict[tuple, array] = {}
        self.large = array("I")
        cell = self.CELL
        low_all, high_all = [FLT_MAX] * 3, [-FLT_MAX] * 3
        for number, (a, b, c, w0, w1, w2, own, surface) in enumerate(triangles):
            self.corners.extend((a, b, c, w0, w1, w2))
            self.masks.append(own)
            self.surfaces.append(surface)
            pa, pb, pc = vertices[a], vertices[b], vertices[c]
            low = (min(pa[0], pb[0], pc[0]), min(pa[1], pb[1], pc[1]), min(pa[2], pb[2], pc[2]))
            high = (max(pa[0], pb[0], pc[0]), max(pa[1], pb[1], pc[1]), max(pa[2], pb[2], pc[2]))
            self.bounds.extend(low + high)
            for k in range(3):
                low_all[k] = min(low_all[k], low[k])
                high_all[k] = max(high_all[k], high[k])
            columns_ = range(math.floor(low[0] / cell), math.floor(high[0] / cell) + 1)
            rows = range(math.floor(low[2] / cell), math.floor(high[2] / cell) + 1)
            if len(columns_) * len(rows) > self.LARGE:
                self.large.append(number)
                continue
            for i in columns_:
                for j in rows:
                    column = grid.get((i, j))
                    if column is None:
                        column = grid[(i, j)] = array("I")
                    column.append(number)
        self.grid = grid
        self.low, self.high = tuple(low_all), tuple(high_all)

    def __len__(self) -> int:
        return len(self.masks)

    @property
    def records(self) -> list:
        """The triangles as (a, b, c, wing0, wing1, wing2, mask, surface)."""
        c = self.corners
        return [(*c[6 * k : 6 * k + 6], self.masks[k], self.surfaces[k]) for k in range(len(self.masks))]

    def raw_corners(self, number: int) -> tuple:
        a, b, c = self.corners[6 * number : 6 * number + 3]
        raw = self.raw
        return raw[a], raw[b], raw[c]

    def triangle(self, number: int) -> Triangle:
        tri = self.cache.get(number)
        if tri is None:
            a, b, c, w0, w1, w2 = self.corners[6 * number : 6 * number + 6]
            v = self.vertices
            corners = (v[a], v[b], v[c])
            wings = tuple(v[w] if w >= 0 else corners[(k + 2) % 3] for k, w in enumerate((w0, w1, w2)))
            tri = self.cache[number] = Triangle(corners, wings, (w0 >= 0, w1 >= 0, w2 >= 0), number,
                                                self.surfaces[number], self.masks[number])  # fmt: skip
        return tri

    def query(self, low, high, category: int) -> list:
        """The triangles the engine's BVH query returns for a box (0x7FF789CF3900): mask & category and the
        exact triangle-box test (0x7FF789CD79D0), in number order."""
        found = set(self.large)
        cell, slack = self.CELL, 1e-3
        columns_ = range(math.floor((low[0] - slack) / cell), math.floor((high[0] + slack) / cell) + 1)
        rows = range(math.floor((low[2] - slack) / cell), math.floor((high[2] + slack) / cell) + 1)
        grid = self.grid
        for i in columns_:
            for j in rows:
                found.update(grid.get((i, j), ()))
        out = []
        bounds, corners, masks, v, boxes = self.bounds, self.corners, self.masks, self.vertices, self.boxes
        lx, ly, lz = low[0] - slack, low[1] - slack, low[2] - slack
        hx, hy, hz = high[0] + slack, high[1] + slack, high[2] + slack
        for number in sorted(found):
            at = 6 * number
            if bounds[at] > hx or bounds[at + 3] < lx or bounds[at + 1] > hy or bounds[at + 4] < ly:
                continue
            if bounds[at + 2] > hz or bounds[at + 5] < lz or not masks[number] & category:
                continue
            p0, p1, p2 = v[corners[at]], v[corners[at + 1]], v[corners[at + 2]]
            constants = boxes.get(number)
            if constants is None:
                constants = boxes[number] = box_constants(p0, p1, p2)
            if triangle_box(p0, p1, p2, low, high, constants):
                out.append(number)
        return out


def _local_box(low, high) -> tuple:
    """A box through the identity transform (0x7FF789CD7DF0): centre and half extents, then back."""
    centre = scale(add(low, high), 0.5)
    half = scale(sub(high, low), 0.5)
    return sub(centre, half), add(half, centre)


def absolute_columns(rotation) -> tuple:
    return tuple(_abs_vec(c) for c in columns(rotation))


def local_box(low, high, translation, rotation, absolute=None) -> tuple:
    """A box through a transform (0x7FF789CD7DF0): the centre turned and moved, the half extents through
    the absolute rotation matrix (`absolute`: absolute_columns(rotation), when the caller keeps it)."""
    c0, c1, c2 = absolute or absolute_columns(rotation)
    centre = add(rotate(rotation, scale(add(low, high), 0.5)), translation)
    hx, hy, hz = scale(sub(high, low), 0.5)
    extent = v3(f32(f32(f32(hy * c1[0]) + f32(hx * c0[0])) + f32(hz * c2[0])),
                f32(f32(f32(hy * c1[1]) + f32(hx * c0[1])) + f32(hz * c2[1])),
                f32(f32(f32(hy * c1[2]) + f32(hx * c0[2])) + f32(hz * c2[2])))  # fmt: skip
    return sub(centre, extent), add(extent, centre)


def inverse(frame) -> tuple:
    """(translation, rotation) of a body frame's inverse, as the mesh contact builds it (0x7FF789D0EFE6):
    the conjugate, and 0 - the translation turned by it."""
    t, q = frame
    conjugate = (-q[0], -q[1], -q[2], q[3])
    turned = rotate(conjugate, t)
    return v3(0.0 - turned[0], 0.0 - turned[1], 0.0 - turned[2]), conjugate


STATIC_MARGIN = MARGIN  # Unverified: the map's shapes are made with the default shape def (margin 0.1)


def shape_aabb(geometry, frame) -> tuple:
    """A shape's AABB in the world (0x7FF789D057B0: polytope 0x7FF789CE94B0, capsule 0x7FF789CEBA90, sphere
    0x7FF789CEBB20; a mesh's box through the frame like 0x7FF789CD7DF0, unverified for rotated props)."""
    m = matrix(*frame) if frame is not None else IDENTITY_FRAME
    c0, c1, c2, t = m
    kind = geometry.kind
    if kind == HULL:
        low, high = [FLT_MAX] * 3, [-FLT_MAX] * 3
        for x, y, z in geometry.vertices:
            for k in range(3):
                w = f32(f32(f32(x * c0[k]) + f32(y * c1[k])) + f32(z * c2[k]))
                low[k] = minps(low[k], w)
                high[k] = maxps(high[k], w)
        lo = v3(f32(low[0] - SKIN) + t[0], f32(low[1] - SKIN) + t[1], f32(low[2] - SKIN) + t[2])
        return lo, v3(f32(SKIN + high[0]) + t[0], f32(SKIN + high[1]) + t[1], f32(SKIN + high[2]) + t[2])
    if kind == CAPSULE:
        a, b = transform(geometry.a, m), transform(geometry.b, m)
        r = geometry.radius
        return (v3(minps(a[0], b[0]) - r, minps(a[1], b[1]) - r, minps(a[2], b[2]) - r),
                v3(maxps(a[0], b[0]) + r, maxps(a[1], b[1]) + r, maxps(a[2], b[2]) + r))  # fmt: skip
    if kind == SPHERE:
        c = transform(geometry.centre, m)
        r = geometry.radius
        return v3(c[0] - r, c[1] - r, c[2] - r), v3(c[0] + r, c[1] + r, c[2] + r)
    # A mesh's box is its BVH root's: the triangles' box grown by their 1 cm radius (0x7FF789CF3360; the
    # running client's boxes of the Practice Range's meshes are so, measured).
    low = v3(f32(geometry.low[0] - SKIN), f32(geometry.low[1] - SKIN), f32(geometry.low[2] - SKIN))
    high = v3(f32(geometry.high[0] + SKIN), f32(geometry.high[1] + SKIN), f32(geometry.high[2] + SKIN))
    if frame is None:
        return low, high
    return local_box(low, high, *frame)


class StaticShape:
    """A shape of the map as the broadphase keeps it: the geometry in its body's frame ((translation,
    rotation); None: the identity), its AABB grown by the margin (the proxy's fat AABB) and its place
    in proxy order (the order the world made its shapes in, unverified: the chunk's polytopes, its mesh, then
    the props as placed)."""

    __slots__ = ("absolute", "fat_high", "fat_low", "frame", "geometry", "inverse", "kind", "mask", "matrix",
                 "order")  # fmt: skip

    def __init__(self, geometry, frame=None, order: int = 0) -> None:
        self.geometry = geometry
        self.kind = geometry.kind
        self.frame = (tuple(frame[0]), tuple(frame[1])) if frame is not None else None
        self.matrix = matrix(*self.frame) if frame is not None else None
        self.inverse = inverse(self.frame) if frame is not None else None
        self.absolute = absolute_columns(self.inverse[1]) if frame is not None else None  # for local_box
        self.order = order
        self.mask = geometry.mask
        low, high = shape_aabb(geometry, self.frame)
        m = STATIC_MARGIN
        self.fat_low = v3(low[0] - m, low[1] - m, low[2] - m)
        self.fat_high = v3(high[0] + m, high[1] + m, high[2] + m)


class StaticWorld:
    """The map's static shapes in proxy order, with a grid over their fat AABBs for the broadphase."""

    CELL = 4.0
    HUGE = 1024  # a shape over more cells than this is a candidate of every query (the map's mesh)

    def __init__(self, shapes) -> None:
        self.shapes = list(shapes)
        self.huge: list = []
        self.grid: dict[tuple, list] = {}
        cell = self.CELL
        for shape in self.shapes:
            lo, hi = shape.fat_low, shape.fat_high
            columns_ = range(math.floor(lo[0] / cell), math.floor(hi[0] / cell) + 1)
            rows = range(math.floor(lo[2] / cell), math.floor(hi[2] / cell) + 1)
            if len(columns_) * len(rows) > self.HUGE:
                self.huge.append(shape)
                continue
            for i in columns_:
                for j in rows:
                    self.grid.setdefault((i, j), []).append(shape)

    def query(self, low, high) -> list:
        """The shapes whose fat AABB meets the box, in proxy order."""
        found = {id(shape): shape for shape in self.huge}
        cell = self.CELL
        for i in range(math.floor(low[0] / cell) - 1, math.floor(high[0] / cell) + 2):
            for j in range(math.floor(low[2] / cell) - 1, math.floor(high[2] / cell) + 2):
                for shape in self.grid.get((i, j), ()):
                    found[id(shape)] = shape
        out = [s for s in found.values() if overlaps(low, high, s.fat_low, s.fat_high)]
        out.sort(key=lambda s: s.order)
        return out


# ---- the character's contacts: one per shape it is near, each with its narrow phase and its sweep


class MeshContact:
    """The character's contact with one mesh (0x7FF789D0E810): the triangles near the capsule (re-queried
    only when the capsule leaves a box 0.1 m larger than it, or that box gets 0.4 m too loose), each with
    its GJK cache. A prop's mesh works in its own frame: the capsule goes in by unrotate, the normals come
    back by rotate (0x7FF789D0EA60, 0x7FF789D0C160)."""

    sign = 1.0  # the mesh is shape A: its normals point at the capsule

    def __init__(self, shape, category: int) -> None:
        if not isinstance(shape, StaticShape):
            shape = StaticShape(shape)
        self.shape = shape
        self.mesh = shape.geometry
        self.category = category
        self.box = None
        self.candidates: list = []  # [[triangle number, GJK cache]]

    def _local(self, low, high) -> tuple:
        shape = self.shape
        if shape.frame is None:
            return _local_box(low, high)
        return local_box(low, high, *shape.inverse, shape.absolute)

    def gather(self, low, high) -> None:
        """0x7FF789D0EF80, with the capsule shape's current AABB."""
        lo, hi = self._local(low, high)
        box = self.box
        if box is not None:
            blo, bhi = box
            inside = blo[0] <= lo[0] and blo[1] <= lo[1] and blo[2] <= lo[2]
            if inside and hi[0] <= bhi[0] and hi[1] <= bhi[1] and hi[2] <= bhi[2]:
                far_lo = _unpack3(_pack3(lo[0] - LOOSE, lo[1] - LOOSE, lo[2] - LOOSE))
                far_hi = _unpack3(_pack3(hi[0] + LOOSE, hi[1] + LOOSE, hi[2] + LOOSE))
                tight = far_lo[0] <= blo[0] and far_lo[1] <= blo[1] and far_lo[2] <= blo[2]
                if tight and bhi[0] <= far_hi[0] and bhi[1] <= far_hi[1] and bhi[2] <= far_hi[2]:
                    return
        half = add(scale(sub(hi, lo), 0.5), (MARGIN, MARGIN, MARGIN))
        centre = scale(add(hi, lo), 0.5)
        self.box = (sub(centre, half), add(half, centre))
        old = {entry[0]: entry[1] for entry in self.candidates}
        self.candidates = [[n, old.get(n, NO_CACHE)] for n in self.mesh.query(*self.box, self.category)]

    def manifolds(self, body) -> list:
        """The contact's manifolds for the capsule where the body is (0x7FF789D0BD90, 0x7FF789D0CE00)."""
        self.gather(*body.aabb)
        p0, p1 = body.points(body.position)
        frame = self.shape.frame
        if frame is not None:
            t, q = frame
            p0, p1 = unrotate(q, sub(p0, t)), unrotate(q, sub(p1, t))
        radius = body.radius
        found = []
        mesh = self.mesh
        key = (body.position, radius, body.top)
        for entry in self.candidates:
            # [number, cache, the last call's capsule, its cache in, its manifold]: the same capsule and a
            # cache that came back unchanged give the same manifold and cache again (a body at rest).
            if len(entry) == 5 and entry[2] == key and entry[3] == entry[1]:
                manifold = entry[4]
            else:
                cache_in = entry[1]
                manifold, entry[1] = capsule_triangle(mesh.triangle(entry[0]), p0, p1, radius, cache_in)
                entry[2:] = [key, cache_in, manifold]
            if manifold.points:
                found.append(manifold)
        out = cluster_manifolds(found)
        if frame is not None:
            for manifold in out:
                manifold.normal = rotate(q, manifold.normal)  # the points stay local: the solver reads none
        return out

    def update(self, aabb, p0, p1, radius: float) -> list:
        """The manifolds for a capsule in the identity frame (the map's mesh)."""
        self.gather(*aabb)
        found = []
        for entry in self.candidates:
            manifold, entry[1] = capsule_triangle(self.mesh.triangle(entry[0]), p0, p1, radius, entry[1])
            if manifold.points:
                found.append(manifold)
        return cluster_manifolds(found)

    def cast(self, body, swept, translation, start):
        """The sweep's fraction against this mesh (0x7FF789D0E410), or None: the triangles whose AABB meets
        the swept box (and the box moved by the translation, in the mesh's frame), each cast."""
        self.gather(*swept)
        lo, hi = self._local(*swept)
        frame = self.shape.frame
        if frame is None:
            moved = translation
            p0, p1 = body.points(start)
            frames = (None, None)
        else:
            moved = unrotate(frame[1], translation)
            p0, p1 = body.local_points()
            frames = (self.shape.matrix, matrix(start, IDENTITY))
        lo = tuple(minps(lo[k], f32(lo[k] + moved[k])) for k in range(3))
        hi = tuple(maxps(hi[k], f32(hi[k] + moved[k])) for k in range(3))
        best = None
        t_max = 1.0
        radius = body.radius
        mesh = self.mesh
        segment = (p0, p1)
        here, far = (start, radius, body.top), _far(radius, translation)
        for entry in self.candidates:
            tri = mesh.triangle(entry[0])
            if not overlaps(lo, hi, tri.bottom, tri.top):  # the sign bits of (top - lo) | (hi - bottom)
                continue
            if len(entry) == 5 and entry[2] == here and entry[4].gap is not None and entry[4].gap > far:
                continue  # too far to come within reach this move: its cast returns None (see _far)
            t = shape_cast(tri.corners, SKIN, segment, radius, translation, t_max, False, True, *frames)
            if t is not None and t < t_max:
                best = t_max = t
        return best


class ConvexContact:
    """The character's contact with a polytope, a capsule or a sphere (0x7FF789D006A0): the pair's GJK
    cache, its manifold and its sweep (0x7FF789D00570). A polytope is shape A (types in descending order,
    0x7FF789D20590); against a sphere or a capsule the character is A (unverified for capsules: equal types
    keep the pair order, and the character's proxy is taken to be the smaller)."""

    def __init__(self, shape) -> None:
        self.shape = shape
        self.cache = NO_CACHE
        self.first = shape.kind == HULL
        self.sign = 1.0 if self.first else -1.0
        self.memo = None  # (the body's capsule, the cache in, the manifolds) of the last call

    def manifolds(self, body) -> list:
        key = (body.position, body.radius, body.top)
        memo = self.memo
        if memo is not None and memo[0] == key and memo[1] == self.cache:
            return memo[2]  # the same capsule and an unchanged cache: the same result
        cache_in = self.cache
        m = self._manifolds(body)
        out = [m] if m.points else []
        self.memo = (key, cache_in, out, m.gap)
        return out

    def _manifolds(self, body) -> Manifold:
        shape = self.shape
        geometry = shape.geometry
        if shape.frame is None and self.first:  # the map's polytopes: the identity frame
            p0, p1 = body.points(body.position)
            m, self.cache = capsule_hull(geometry, p0, p1, body.radius, self.cache)
        else:
            frame = shape.frame or ((0.0, 0.0, 0.0), IDENTITY)
            mine = (body.position, IDENTITY)
            if self.first:
                m, self.cache = convex_manifold(geometry, frame, body.capsule, mine, self.cache)
            else:
                m, self.cache = convex_manifold(body.capsule, mine, geometry, frame, self.cache)
        return m

    def cast(self, body, swept, translation, start):
        shape = self.shape
        geometry = shape.geometry
        radius = body.radius
        memo = self.memo
        here = (start, radius, body.top)
        if self.first and memo is not None and memo[3] is not None and memo[0] == here:  # noqa: SIM102
            if memo[3] > _far(radius, translation):
                return None  # too far to come within reach this move (see _far)
        if shape.frame is None:
            points, frames = body.points(start), (None, None)
        else:
            points = body.local_points()
            frames = (shape.matrix, matrix(start, IDENTITY))
        if self.first:
            return shape_cast(geometry.vertices, SKIN, points, radius, translation, 1.0, False, True, *frames)
        other = [geometry.centre] if geometry.kind == SPHERE else [geometry.a, geometry.b]
        return shape_cast(points, radius, other, geometry.radius, neg(translation), 1.0, False, True,
                          frames[1], frames[0])  # fmt: skip


CAST_SAFE = 0.01  # m: a shape this much past the cast's target distance cannot reach it by float noise


def _far(radius: float, translation) -> float:
    """The core distance from which a sweep of the capsule (radius r, moving by `translation`) against a
    triangle or a polytope (radius 0.01) returns None: the cast (0x7FF789CDBA70) hits only where the
    distance comes down to its target r + 0.00125, which a shape farther than the target + the move's
    length + CAST_SAFE never does; its first conservative advance then goes past t_max = 1. The round's
    manifolds just measured that distance at the very place the sweep starts from."""
    tx, ty, tz = translation
    return radius + CAST_TOLERANCE + CAST_SAFE + math.sqrt(tx * tx + ty * ty + tz * tz)


def make_contact(shape, category: int):
    return MeshContact(shape, category) if shape.kind == MESH else ConvexContact(shape)


# ---- the character's body and its move (0x7FF789CCABF0 -> 0x7FF789CC9320)

SLOP = TOUCH  # contacts are solved to 5 mm of overlap of the skins (xmmword_7FF78B585950)
GS_PASSES = 200
ROUNDS_MAX = 20
FIT_SHRINK = f32(0.0199999996)  # the stance fit test's capsule is this much thinner (xmmword_7FF78B523558)


class Constraint:
    __slots__ = ("cap", "impulse", "normal", "offset", "soft")

    def __init__(self, normal, offset: float, cap: float, soft: bool) -> None:
        self.normal = normal
        self.offset = offset
        self.cap = cap
        self.impulse = 0.0
        self.soft = soft


class CharacterBody:
    """The character's physics body: a capsule standing on `position` (its lowest point), the segment
    from (0, r, 0) to (0, top, 0) in its frame (no rotation), its broadphase proxy (fat AABB) and its
    contacts with the map's shapes, newest first, as the engine keeps them: a contact is made when the
    proxy's fat AABB starts to meet a shape's (in proxy order, each put at the head of the list) and
    dropped by the move when they no longer meet."""

    def __init__(self, radius: float, top: float, contacts=(), world: StaticWorld | None = None,
                 category: int = 0x18) -> None:  # fmt: skip
        self.radius = f32(radius)
        self.top = f32(top)
        self.capsule = Capsule((0.0, self.radius, 0.0), (0.0, self.top, 0.0), self.radius)
        self.world = world
        self.category = category
        self.position = (0.0, 0.0, 0.0)
        self.aabb = None
        self.fat = None
        self.moved = False  # the proxy is in the broadphase's move buffer
        self.contacts = list(contacts)
        self.budget = ROUNDS_MAX  # mover +0x14E4: the rounds the next move may take (cvar on: adapted)

    def points(self, position) -> tuple:
        """The capsule's segment in the world (rotation identity: the engine's quaternion product)."""
        x, y, z = position
        return (x, f32(self.radius + y), z), (x, f32(self.top + y), z)

    def local_points(self) -> tuple:
        return self.capsule.a, self.capsule.b

    def _single(self, position) -> tuple:
        """The shape's AABB at a position (0x7FF789CEBA90)."""
        a, b = self.points(position)
        r = self.radius
        high = _unpack3(_pack3(maxps(a[0], b[0]) + r, maxps(a[1], b[1]) + r, maxps(a[2], b[2]) + r))
        low = _unpack3(_pack3(minps(a[0], b[0]) - r, minps(a[1], b[1]) - r, minps(a[2], b[2]) - r))
        return low, high

    def _swept(self, start, end) -> tuple:
        """The shape's AABB over a move (0x7FF789CEBC60)."""
        a0, b0 = self.points(start)
        a1, b1 = self.points(end)
        r = self.radius
        high = _unpack3(_pack3(*(maxps(maxps(a0[k], b0[k]), maxps(a1[k], b1[k])) + r for k in range(3))))
        low = _unpack3(_pack3(*(minps(minps(a0[k], b0[k]), minps(a1[k], b1[k])) - r for k in range(3))))
        return low, high

    # ---- the broadphase

    def _proxy_move(self, low, high) -> None:
        """The proxy's fat AABB follows the shape's AABB when it leaves it or gets four margins too loose
        (0x7FF789CD65F0 -> 0x7FF789CDD820)."""
        m = MARGIN
        fat = self.fat
        if fat is not None:
            fl, fh = fat
            inside = fl[0] <= low[0] and high[0] <= fh[0] and fl[1] <= low[1] and high[1] <= fh[1]
            if inside and fl[2] <= low[2] and high[2] <= fh[2]:
                far_lo = _unpack3(_pack3(low[0] - LOOSE, low[1] - LOOSE, low[2] - LOOSE))
                far_hi = _unpack3(_pack3(high[0] + LOOSE, high[1] + LOOSE, high[2] + LOOSE))
                tight = far_lo[0] <= fl[0] and far_lo[1] <= fl[1] and far_lo[2] <= fl[2]
                if tight and fh[0] <= far_hi[0] and fh[1] <= far_hi[1] and fh[2] <= far_hi[2]:
                    return
        self.fat = (v3(low[0] - m, low[1] - m, low[2] - m), v3(high[0] + m, high[1] + m, high[2] + m))
        self.moved = True

    def update_pairs(self) -> None:
        """New contacts for the shapes the moved proxy now meets (0x7FF789CF5800), in proxy order, each at
        the head of the list. The pair filter (0x7FF789D07730) wants the body's category in the shape's
        mask (and the shape's category in the body's mask, unverified: always so for map shapes)."""
        if not self.moved:
            return
        self.moved = False
        if self.world is None:
            return
        have = {id(contact.shape) for contact in self.contacts}
        category = self.category
        for shape in self.world.query(*self.fat):
            if id(shape) not in have and shape.mask & category:
                self.contacts.insert(0, make_contact(shape, category))

    def teleport(self, position) -> None:
        """Set the body where the mover puts it (0x7FF789D11B60 -> 0x7FF789D10BD0): nothing when it is
        there already, else its AABB and its proxy follow and the pairs wait for the next update."""
        position = tuple(position)
        if self.aabb is not None and position == self.position:
            return
        self.position = position
        self.aabb = self._single(position)
        self._proxy_move(*self.aabb)

    def set_capsule(self, radius: float, top: float) -> None:
        """A new stance's capsule (0x7FF789CCAC00 -> 0x7FF789D072F0): its AABB, the proxy and the pairs at
        once; the contacts stay."""
        self.radius = f32(radius)
        self.top = f32(top)
        self.capsule = Capsule((0.0, self.radius, 0.0), (0.0, self.top, 0.0), self.radius)
        self.aabb = self._single(self.position)
        self._proxy_move(*self.aabb)
        self.update_pairs()

    def fits(self, radius: float, top: float) -> bool:
        """Whether a capsule (0, r, 0)..(0, top, 0) fits where the body is (0x7FF789CCAC10 -> 0x7FF789CCA300,
        overlap query 0x7FF789D24DC0 with the body's filter): no shape within max(0.005, r - 0.02) of its
        segment."""
        radius, top = f32(radius), f32(top)
        if not radius >= SKIN:
            return False
        reach = maxps(TOUCH, f32(radius - FIT_SHRINK))
        x, y, z = self.position
        p0, p1 = (x, f32(radius + y), z), (x, f32(top + y), z)
        if self.world is None:
            return True
        low = v3(minps(p0[0], p1[0]) - reach, minps(p0[1], p1[1]) - reach, minps(p0[2], p1[2]) - reach)
        high = v3(maxps(p0[0], p1[0]) + reach, maxps(p0[1], p1[1]) + reach, maxps(p0[2], p1[2]) + reach)
        category = self.category
        shapes = [shape for shape in self.world.query(low, high) if shape.mask & category]
        return not any(_overlap(shape, p0, p1, reach, category) for shape in shapes)

    # ---- the move

    def _set_position(self, target) -> tuple:
        """Move toward a target (0x7FF789D102D0): the proxy takes the swept AABB and the pairs update; the
        sweep stops the move at the first contact; contacts whose fat AABBs no longer meet are dropped."""
        start = self.position
        swept = self._swept(start, target)
        self._proxy_move(*swept)
        self.update_pairs()
        translation = sub(target, start)
        still = translation[0] == 0.0 and translation[1] == 0.0 and translation[2] == 0.0
        fraction = 1.0
        dead = []
        fat_low, fat_high = self.fat
        for contact in self.contacts:
            shape = contact.shape
            if not overlaps(fat_low, fat_high, shape.fat_low, shape.fat_high):
                dead.append(contact)
                continue
            if still:  # no translation: the body stays where it is whatever the casts say
                continue
            t = contact.cast(self, swept, translation, start)
            if t is not None:
                fraction = minps(fraction, t)
        for contact in dead:
            self.contacts.remove(contact)
        moved = add(scale(translation, fraction), start)
        if all(math.isfinite(x) for x in moved):
            self.teleport(moved)
        return self.position

    def move(self, displacement, dt: float, rounds: int, adaptive: bool = True):
        """The engine's character move: (final position, clip planes, touching). Each round updates the
        contacts at the current position, turns every manifold into a plane through its deepest point,
        projects the target out of the planes (Gauss-Seidel, 5 mm slop, 5 mm tolerance) and moves the body
        there; it stops when a round moves less than 5 mm."""
        if adaptive:
            rounds = min(self.budget, rounds)
        start = self.position
        target = add(add(scale((0.0, 0.0, 0.0), dt), start), displacement)
        current = start
        constraints: list = []
        passes_total = 0
        tolerance = TOUCH
        tolerance_sq = f32(tolerance * tolerance)
        for _ in range(max(0, rounds)):
            self.update_pairs()
            constraints = []
            for contact in self.contacts:
                sign = contact.sign
                for manifold in contact.manifolds(self):
                    n = manifold.normal if sign > 0.0 else neg(manifold.normal)
                    deepest = manifold.points[0].separation
                    for point in manifold.points[1:]:
                        deepest = minps(deepest, point.separation)
                    constraints.append(Constraint(n, f32(dot(n, current) - deepest), WORLD_CAP, False))
            p = target
            passes = 0
            if constraints:
                while True:
                    worst = 0.0
                    for c in constraints:
                        n = c.normal
                        gap = f32(dot(n, p) - c.offset)
                        value = minps(f32(-f32(gap + SLOP) + c.impulse), c.cap)
                        impulse = maxps(0.0, value)
                        delta = f32(impulse - c.impulse)
                        c.impulse = impulse
                        p = add(p, scale(n, delta))
                        worst = maxps(worst, maxps(f32(0.0 - delta), delta))
                    if worst < tolerance:
                        break
                    passes += 1
                    if passes >= GS_PASSES:
                        break
            passes_total += passes
            moved = self._set_position(p)
            step = sub(moved, current)
            current = moved
            if dot(step, step) < tolerance_sq:
                break
        if adaptive:
            limit = min(rounds * 100, 500)
            if passes_total > limit:
                self.budget = max(int(self.budget / 2), 1)
            else:
                self.budget = min(self.budget * 2, ROUNDS_MAX)
        hard = [c for c in constraints if c.impulse > 0.0 and not c.soft and c.impulse < c.cap]
        planes = [(c.normal, c.offset) for c in hard]
        touching = any(c.impulse > 0.0 for c in constraints)
        return current, planes, touching


def _overlap(shape, p0, p1, reach: float, category: int) -> bool:
    """The stance fit test against one shape (0x7FF789D05710), the segment in the shape's frame: mesh
    triangles (0x7FF789CF44A0 -> 0x7FF789CEDCF0) and polytopes (0x7FF789CEA060) by GJK distance < reach,
    capsules (0x7FF789CDC480) and spheres (0x7FF789CDC7F0) by the squared distance."""
    if shape.frame is not None:
        t, q = shape.frame
        p0, p1 = unrotate(q, sub(p0, t)), unrotate(q, sub(p1, t))
    geometry = shape.geometry
    kind = shape.kind
    if kind == MESH:
        low = v3(minps(p0[0], p1[0]) - reach, minps(p0[1], p1[1]) - reach, minps(p0[2], p1[2]) - reach)
        high = v3(maxps(p0[0], p1[0]) + reach, maxps(p0[1], p1[1]) + reach, maxps(p0[2], p1[2]) + reach)
        for number in geometry.query(low, high, category):
            if gjk(geometry.triangle(number).corners, (p0, p1)).distance < reach:
                return True
        return False
    if kind == HULL:
        return gjk(geometry.vertices, (p0, p1)).distance < reach
    if kind == CAPSULE:
        total = f32(geometry.radius + reach)
        a0, a1 = geometry.a, geometry.b
        if not _splat_tiny(dot(sub(a1, a0), sub(a1, a0))) and not _splat_tiny(dot(sub(p1, p0), sub(p1, p0))):
            r = sub(a0, p0)
            return dot(r, r) < total
        pa, pb = _closest_parameters(a0, a1, p0, p1)
        d = sub(pb, pa)
        return dot(d, d) < f32(total * total)
    e = sub(p1, p0)
    along = dot(sub(geometry.centre, p0), e)
    length = dot(e, e)
    if along <= 0.0 or not length > TINY:
        nearest = p0
    elif length <= along:
        nearest = p1
    else:
        nearest = add(scale(e, f32(along / length)), p0)
    d = sub(geometry.centre, nearest)
    total = f32(reach + geometry.radius)
    return dot(d, d) < f32(total * total)


def _closest_parameters(a0, a1, b0, b1) -> tuple:
    """The closest points of two segments, the first part of 0x7FF789CD8720 (closest_segments without its
    final normalisation)."""
    d1 = sub(a1, a0)
    d2 = sub(b1, b0)
    r = sub(a0, b0)
    a = dot(d1, d1)
    e = dot(d2, d2)
    nz1, nz2 = _splat_tiny(a), _splat_tiny(e)
    if not nz1 and not nz2:
        return a0, b0
    f = dot(r, d2)
    if not nz1:
        s, t = 0.0, _clamp01(f32(f / e))
    else:
        c = dot(r, d1)
        if not nz2:
            t, s = 0.0, _clamp01(f32(-c / a))
        else:
            b = dot(d2, d1)
            denominator = f32(f32(e * a) - f32(b * b))
            s = _clamp01(f32(f32(f32(b * f) - f32(c * e)) / denominator)) if _splat_tiny(denominator) else 0.0
            t = f32(f32(f32(b * s) + f) / e)
            if t < 0.0:
                t, s = 0.0, _clamp01(f32(-c / a))
            elif t > 1.0:
                t, s = 1.0, _clamp01(f32(f32(b - c) / a))
    return add(scale(d1, s), a0), add(scale(d2, t), b0)


# How far a hard contact may push the capsule in one solve: the other shape's +0x84 (shape def +0x44); the
# world's shapes are made with the default def, FLT_MAX (xmmword_7FF78B566EA0).
WORLD_CAP = FLT_MAX


# ---- the velocity left after the move (0x7FF789CCAB00 -> 0x7FF789CCAD50)

CLIP_PARALLEL = f32(0.998782039)


def clip_velocity(v, planes) -> tuple:
    """The requested velocity without what goes into the contact planes: one plane v - n min(0, v.n);
    up to eight: each plane alone, then each pair; zero when none leaves the others alone."""
    count = len(planes)
    if count <= 0:
        return v
    if count == 1:
        n = planes[0][0]
        return sub(v, scale(n, minps(dot(n, v), 0.0)))
    if count > 8:  # Unverified: not traced in full; the QP alone (0x7FF789CCAE79)
        return _clip_qp(v, planes)
    result, zero = _clip_iterative(v, planes)
    if zero and v != (0.0, 0.0, 0.0):
        return _clip_qp(v, planes)
    return result


CLIP_QP_TOLERANCE = f32(3.04617416e-08)


def _clip_qp(v, planes) -> tuple:
    """Projected Gauss-Seidel on the velocity, at most 64 passes (0x7FF789CCB3A0)."""
    tolerance = f32(dot(v, v) * CLIP_QP_TOLERANCE)
    impulses = [0.0] * len(planes)
    out = v
    for _ in range(64):
        worst = 0.0
        for i, (n, _offset) in enumerate(planes):
            old = impulses[i]
            new = maxps(f32(old - dot(n, out)), 0.0)
            impulses[i] = new
            delta = f32(new - old)
            out = add(scale(n, delta), out)
            worst = maxps(worst, f32(delta * delta))
        if worst < tolerance:
            break
    return out


def _clip_iterative(v, planes):
    """0x7FF789CCAFF0: (velocity, came out as the zero vector)."""
    if dot(v, v) < TINY:
        return v, False
    u = normalize(v)
    moved = False
    count = len(planes)
    for i in range(count):
        n = planes[i][0]
        if dot(n, u) < 0.0:
            moved = True
            w = sub(v, scale(n, dot(v, n)))
            if all(not dot(planes[j][0], w) < 0.0 for j in range(count) if j != i):
                return w, False
    if not moved:
        return v, False
    for i in range(count - 1):
        ni = planes[i][0]
        di = dot(v, ni)
        if di >= 0.0:
            continue
        for j in range(i + 1, count):
            nj = planes[j][0]
            dj = dot(v, nj)
            if dj >= 0.0:
                continue
            c = dot(ni, nj)
            c2 = f32(c * c)
            if c2 > CLIP_PARALLEL:
                continue
            inverse = f32(1.0 / f32(1.0 - c2))
            a = f32(f32(f32(c * dj) - di) * inverse)
            b = f32(f32(f32(c * di) - dj) * inverse)
            if a < 0.0 or b < 0.0:
                continue
            w = add(add(scale(ni, a), v), scale(nj, b))
            if all(not dot(planes[k][0], w) < 0.0 for k in range(count) if k not in (i, j)):
                return w, False
    return (0.0, 0.0, 0.0), True


# ---- the probe's sweep through the world (its ground disc: 0x7FF789D25900 -> 0x7FF789D1D7E0)

SWEEP_BOX = f32(0.0199999996)  # the mesh sweep's box margin (xmmword_7FF78B585E40)
SWEEP_REACH = 0.05  # m: the candidates' box over the points' reach (any margin over the 1.25 cm target)


def quaternion_product(a, b) -> tuple:
    """a b, lane for lane as the mesh sweep turns its points (0x7FF789CF3D0A..0x7FF789CF3DA5): ((b.x (a.w,
    a.z, -a.y, -a.x) + b.w a) + b.y (-a.z, a.w, a.x, -a.y)) + b.z (a.y, -a.x, a.w, -a.z)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    first = (f32(aw * bx), f32(az * bx), f32(-ay * bx), f32(-ax * bx))
    own = (f32(bw * ax), f32(bw * ay), f32(bw * az), f32(bw * aw))
    second = (f32(-az * by), f32(aw * by), f32(ax * by), f32(-ay * by))
    third = (f32(ay * bz), f32(-ax * bz), f32(aw * bz), f32(-az * bz))
    return tuple(f32(f32(f32(first[k] + own[k]) + second[k]) + third[k]) for k in range(4))


def mesh_sweep(shape, start, end, points, radius: float, category: int, t_max: float = 1.0):
    """A convex sweep against a mesh (0x7FF789CF3B20): `points` (radius `radius`, no rotation) at `start`
    moved to `end`. In the mesh's frame (turned back by its rotation, times the inverse scale) the points
    are recentred on their box; each triangle the swept box meets is cast (0x7FF789CECE40: the GJK ray
    cast, the triangle's radius 0.01, no shrink) with the best fraction so far as its limit, so of equal
    hits the first stays (unverified: the BVH's order is not kept, it matters only for equal fractions). The
    point comes back times the scale, turned and moved, the normal turned. (fraction, point, normal,
    triangle) or None."""
    mesh = shape.geometry
    t, q = shape.frame or ((0.0, 0.0, 0.0), IDENTITY)
    inverse = mesh.inverse_scale
    local_start = mul(unrotate(q, sub(start, t)), inverse)
    local_end = mul(unrotate(q, sub(end, t)), inverse)
    r = f32(radius * inverse[0])
    conjugate = (-q[0], -q[1], -q[2], q[3])
    turn = (*columns(quaternion_product(conjugate, IDENTITY)), rotate(conjugate, sub(start, t)))
    moved = [mul(transform(p, turn), inverse) for p in points]
    low = high = moved[0]
    for p in moved[1:]:
        low = (minps(low[0], p[0]), minps(low[1], p[1]), minps(low[2], p[2]))
        high = (maxps(high[0], p[0]), maxps(high[1], p[1]), maxps(high[2], p[2]))
    centre = scale(add(high, low), 0.5)
    local_end = add(local_end, sub(centre, local_start))
    moved = [sub(p, centre) for p in moved]
    translation = sub(local_end, centre)
    half = add(add(scale(sub(high, low), 0.5), (r, r, r)), (SWEEP_BOX, SWEEP_BOX, SWEEP_BOX))
    box_low = tuple(minps(centre[k], local_end[k]) - half[k] for k in range(3))
    box_high = tuple(maxps(centre[k], local_end[k]) + half[k] for k in range(3))
    if mesh.scale != (1.0, 1.0, 1.0):  # the query runs on the scaled corners
        ends = (mul(box_low, mesh.scale), mul(box_high, mesh.scale))
        box_low = tuple(min(ends[0][k], ends[1][k]) - SWEEP_BOX for k in range(3))
        box_high = tuple(max(ends[0][k], ends[1][k]) + SWEEP_BOX for k in range(3))
    frame_b = matrix(centre, IDENTITY)
    radius_b = maxps(SKIN, r)
    best = None
    for number in mesh.query(box_low, box_high, category):
        hit = shape_cast(mesh.raw_corners(number), SKIN, moved, radius_b, translation, t_max, False, False,
                         None, frame_b, full=True)  # fmt: skip
        if hit is not None:
            t_max = hit[0]
            best = (hit, number)
    if best is None:
        return None
    (fraction, point, normal), number = best
    return fraction, add(rotate(q, mul(point, mesh.scale)), t), rotate(q, normal), number


def scene_sweep(world: StaticWorld, start, end, points, radius: float, category: int):
    """The probe's convex sweep through the world (0x7FF789D25900 -> 0x7FF789D1D7E0 -> 0x7FF789D22360 ->
    0x7FF789D05490): `points` (radius `radius`) in a frame at `start` with no rotation, moved to `end`,
    against each shape the swept box meets whose mask has the category: meshes by mesh_sweep, polytopes,
    capsules and spheres by the GJK ray cast in their frames (0x7FF789CE9A00, 0x7FF789CEBEB0,
    0x7FF789CEC7F0; no shrink). The broadphase hands the best fraction on as the next shape's limit, so a
    later shape counts only when it is strictly closer (unverified: proxy order for the tree's, which matters
    only for equal fractions). (fraction, point, normal, shape, triangle or -1) or None."""
    translation = sub(end, start)
    reach = max(max(abs(p[0]), abs(p[1]), abs(p[2])) for p in points) + radius + SWEEP_REACH
    low = tuple(min(start[k], end[k]) - reach for k in range(3))
    high = tuple(max(start[k], end[k]) + reach for k in range(3))
    frame_b = matrix(start, IDENTITY)
    radius_b = maxps(SKIN, radius)
    best = None
    t_max = 1.0
    for shape in world.query(low, high):
        if not shape.mask & category:
            continue
        geometry = shape.geometry
        if shape.kind == MESH:
            hit = mesh_sweep(shape, start, end, points, radius, category, t_max)
            if hit is not None:
                t_max = hit[0]
                best = (*hit[:3], shape, hit[3])
            continue
        if shape.kind == HULL:
            corners, own = geometry.vertices, SKIN
        elif shape.kind == CAPSULE:
            corners, own = (geometry.a, geometry.b), geometry.radius
        else:
            corners, own = (geometry.centre,), geometry.radius
        hit = shape_cast(corners, own, points, radius_b, translation, t_max, False, False, shape.matrix,
                         frame_b, full=True)  # fmt: skip
        if hit is not None:
            t_max = hit[0]
            best = (*hit, shape, -1)
    return best


def _guarded_inverse(total: float) -> float:
    """1/total refined from rcpps, 0 when |total| is not over TINY (0x7FF789CD853D and alike)."""
    if not maxps(f32(0.0 - total), total) > TINY:
        return 0.0
    r = recip_estimate(total)
    return f32(f32(2.0 - f32(r * total)) * r)


def closest_on_triangle(a, b, c, p) -> tuple:
    """The triangle's point nearest p (0x7FF789CD8230): the corner, edge or face region of p by the
    barycentric tests, each division by a refined rcpps."""
    pa, pb, pc = sub(a, p), sub(b, p), sub(c, p)
    ab, ac, cb = sub(pb, pa), sub(pc, pa), sub(pc, pb)
    d1, d2 = -dot(ab, pa), -dot(ac, pa)
    ab_b, ac_c = dot(ab, pb), dot(ac, pc)
    cb_b, cb_c = -dot(cb, pb), dot(cb, pc)
    n = cross(ab, ac)
    va = dot(cross(pb, pc), n)
    vb = dot(cross(pc, pa), n)
    vc = dot(cross(pa, pb), n)
    if d1 <= 0.0 and d2 <= 0.0:
        return a
    if ab_b <= 0.0 and cb_b <= 0.0:
        return b
    if ac_c <= 0.0 and cb_c <= 0.0:
        return c
    if ab_b > 0.0 and d1 > 0.0 and vc <= 0.0:
        inverse = _guarded_inverse(f32(d1 + ab_b))
        return add(scale(b, f32(inverse * d1)), scale(a, f32(inverse * ab_b)))
    if ac_c > 0.0 and d2 > 0.0 and vb <= 0.0:
        inverse = _guarded_inverse(f32(d2 + ac_c))
        return add(scale(c, f32(inverse * d2)), scale(a, f32(inverse * ac_c)))
    if cb_c > 0.0 and cb_b > 0.0 and va <= 0.0:
        inverse = _guarded_inverse(f32(cb_b + cb_c))
        return add(scale(c, f32(inverse * cb_b)), scale(b, f32(inverse * cb_c)))
    inverse = _guarded_inverse(f32(f32(vb + va) + vc))
    return add(add(scale(b, f32(inverse * vb)), scale(a, f32(inverse * va))), scale(c, f32(inverse * vc)))
