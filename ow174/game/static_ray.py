"""Analytic surface intersections for static sphere and capsule rays."""

import math

Vec3 = tuple[float, float, float]
MIN_SURFACE_DISTANCE = 1e-6  # Match the positive-distance cutoff of triangle rays.


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    ax, ay, az = a
    bx, by, bz = b
    return ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx


def _along(origin: Vec3, direction: Vec3, distance: float) -> Vec3:
    return (
        origin[0] + distance * direction[0],
        origin[1] + distance * direction[1],
        origin[2] + distance * direction[2],
    )


def _line_roots(offset: Vec3, velocity: Vec3, radius: float) -> tuple[float, ...]:
    """Parameters where a line crosses the surface about the origin."""
    vx, vy, vz = velocity
    speed = math.hypot(vx, vy, vz)
    if not 0.0 < speed < math.inf:
        return ()
    unit = vx / speed, vy / speed, vz / speed
    midpoint = -_dot(offset, unit)
    closest = math.hypot(*_along(offset, unit, midpoint))
    if not closest <= radius:
        return ()
    ratio = closest / radius
    half = radius * math.sqrt((1.0 - ratio) * (1.0 + ratio))
    return (midpoint - half) / speed, (midpoint + half) / speed


def round_hit(
    origin: Vec3,
    direction: Vec3,
    length: float,
    a: Vec3,
    b: Vec3,
    radius: float,
) -> tuple[float, Vec3] | None:
    """Return the first positive surface distance and ray-facing normal along a unit direction.

    The capsule surrounds the segment from a to b. A sphere has coincident endpoints. A ray starting inside
    the shape hits its exit surface if that surface is in range.
    """
    if (
        length <= 0.0
        or radius <= 0.0
        or direction == (0.0, 0.0, 0.0)
        or not all(math.isfinite(value) for value in (*origin, *direction, *a, *b, length, radius))
    ):
        return None
    offset = _sub(origin, a)
    ax, ay, az = _sub(b, a)
    span = math.hypot(ax, ay, az)
    if not math.isfinite(span):
        return None

    roots = _line_roots(offset, direction, radius)
    axis = (0.0, 0.0, 0.0)
    if span > 0.0:
        axis = ax / span, ay / span, az / span
        axial_origin = _dot(offset, axis)
        axial_direction = _dot(direction, axis)
        # Each cap contributes only its outer hemisphere. The cross products preserve the
        # perpendicular distances to the axis for the cylinder without subtracting projections.
        candidates = [t for t in roots if axial_origin + t * axial_direction <= 0.0]
        candidates.extend(
            t
            for t in _line_roots(_sub(origin, b), direction, radius)
            if axial_origin + t * axial_direction >= span
        )
        candidates.extend(
            t
            for t in _line_roots(_cross(offset, axis), _cross(direction, axis), radius)
            if 0.0 <= axial_origin + t * axial_direction <= span
        )
        roots = tuple(candidates)

    distance = min((t for t in roots if MIN_SURFACE_DISTANCE < t <= length), default=None)
    if distance is None:
        return None
    point = _along(offset, direction, distance)
    projection = max(0.0, min(span, _dot(point, axis)))
    nx, ny, nz = normal = _along(point, axis, -projection)
    magnitude = math.hypot(nx, ny, nz)
    if not 0.0 < magnitude < math.inf:
        return None
    if _dot(normal, direction) > 0.0:
        magnitude = -magnitude
    return distance, (nx / magnitude, ny / magnitude, nz / magnitude)
