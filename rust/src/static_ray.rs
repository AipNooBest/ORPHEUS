//! Static primitive intersections for retail build 104319.
//!
//! Ports the analytic kernels from `ow174/game/static_ray.py` and
//! `ow174/game/collision.py`. Scene indexing, prop placement, category filtering
//! and ties between distinct shapes belong to the world that calls these kernels.

pub type Vec3 = [f64; 3];

pub const MIN_SURFACE_DISTANCE: f64 = 1e-6;
const TRIANGLE_EPSILON: f64 = 1e-12;
const GROUND_EDGE_EPSILON: f64 = 1e-7;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SurfaceHit {
    pub distance: f64,
    pub normal: Vec3,
}

fn sub(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn dot(a: Vec3, b: Vec3) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn cross([ax, ay, az]: Vec3, [bx, by, bz]: Vec3) -> Vec3 {
    [ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx]
}

fn along(origin: Vec3, direction: Vec3, distance: f64) -> Vec3 {
    [
        origin[0] + distance * direction[0],
        origin[1] + distance * direction[1],
        origin[2] + distance * direction[2],
    ]
}

fn norm([x, y, z]: Vec3) -> f64 {
    // Keep the range stability of Python's math.hypot without squaring large coordinates.
    x.hypot(y).hypot(z)
}

fn valid_ray(origin: Vec3, direction: Vec3, length: f64) -> bool {
    length > 0.0
        && length.is_finite()
        && direction != [0.0; 3]
        && origin.into_iter().chain(direction).all(f64::is_finite)
}

fn line_roots(offset: Vec3, velocity: Vec3, radius: f64) -> Option<[f64; 2]> {
    let speed = norm(velocity);
    if speed == 0.0 || !speed.is_finite() {
        return None;
    }
    let unit = velocity.map(|value| value / speed);
    let midpoint = -dot(offset, unit);
    let closest = norm(along(offset, unit, midpoint));
    if closest > radius || !closest.is_finite() {
        return None;
    }
    let ratio = closest / radius;
    let half = radius * ((1.0 - ratio) * (1.0 + ratio)).sqrt();
    Some([(midpoint - half) / speed, (midpoint + half) / speed])
}

fn nearest_root(
    roots: Option<[f64; 2]>,
    length: f64,
    on_surface: impl Fn(f64) -> bool,
) -> Option<f64> {
    roots?.into_iter().find(|&distance| {
        MIN_SURFACE_DISTANCE < distance && distance <= length && on_surface(distance)
    })
}

/// Intersect the capsule around segment a--b. Coincident endpoints are a sphere.
/// Direction is a unit vector. A ray starting inside can hit the exit surface.
pub fn round_hit(
    origin: Vec3,
    direction: Vec3,
    length: f64,
    a: Vec3,
    b: Vec3,
    radius: f64,
) -> Option<SurfaceHit> {
    if !valid_ray(origin, direction, length)
        || radius <= 0.0
        || !radius.is_finite()
        || !a.into_iter().chain(b).all(f64::is_finite)
    {
        return None;
    }

    let offset = sub(origin, a);
    let axis = sub(b, a);
    let span = norm(axis);
    if !span.is_finite() {
        return None;
    }
    let first_roots = line_roots(offset, direction, radius);
    let (axis, distance) = if span == 0.0 {
        ([0.0; 3], nearest_root(first_roots, length, |_| true)?)
    } else {
        let axis = axis.map(|value| value / span);
        let axial_origin = dot(offset, axis);
        let axial_direction = dot(direction, axis);
        let axial_at = |distance| axial_origin + distance * axial_direction;
        // Each cap contributes only its outer hemisphere. Cross products
        // preserve the perpendicular distances to the cylinder's axis.
        let first = nearest_root(first_roots, length, |distance| axial_at(distance) <= 0.0);
        let second = nearest_root(
            line_roots(sub(origin, b), direction, radius),
            length,
            |distance| axial_at(distance) >= span,
        );
        let cylinder = nearest_root(
            line_roots(cross(offset, axis), cross(direction, axis), radius),
            length,
            |distance| (0.0..=span).contains(&axial_at(distance)),
        );
        let distance = [first, second, cylinder]
            .into_iter()
            .flatten()
            .min_by(f64::total_cmp)?;
        (axis, distance)
    };

    let point = along(offset, direction, distance);
    let projection = dot(point, axis).clamp(0.0, span);
    let normal = along(point, axis, -projection);
    let mut magnitude = norm(normal);
    if magnitude == 0.0 || !magnitude.is_finite() {
        return None;
    }
    if dot(normal, direction) > 0.0 {
        magnitude = -magnitude;
    }
    Some(SurfaceHit {
        distance,
        normal: normal.map(|value| value / magnitude),
    })
}

/// Two-sided Möller-Trumbore distance along a ray, before the caller's length bound.
pub fn triangle_distance(origin: Vec3, direction: Vec3, &[a, b, c]: &[Vec3; 3]) -> Option<f64> {
    let edge1 = sub(b, a);
    let edge2 = sub(c, a);
    let p = cross(direction, edge2);
    let det = dot(edge1, p);
    if -TRIANGLE_EPSILON < det && det < TRIANGLE_EPSILON {
        return None;
    }
    let inv = 1.0 / det;
    let s = sub(origin, a);
    let u = dot(s, p) * inv;
    if !(0.0..=1.0).contains(&u) {
        return None;
    }
    let q = cross(s, edge1);
    let v = dot(direction, q) * inv;
    if v < 0.0 || u + v > 1.0 {
        return None;
    }
    let hit = dot(edge2, q) * inv;
    (hit > MIN_SURFACE_DISTANCE).then_some(hit)
}

/// Intersect a triangle with a bounded ray and orient its normal toward the ray.
pub fn triangle_hit(
    origin: Vec3,
    direction: Vec3,
    length: f64,
    vertices: &[Vec3; 3],
) -> Option<SurfaceHit> {
    if !valid_ray(origin, direction, length) {
        return None;
    }
    let distance = triangle_distance(origin, direction, vertices)?;
    if distance > length {
        return None;
    }
    let [a, b, c] = *vertices;
    let cross = cross(sub(b, a), sub(c, a));
    let magnitude = dot(cross, cross).sqrt();
    let mut normal = if magnitude < TRIANGLE_EPSILON {
        [0.0; 3]
    } else {
        cross.map(|value| value / magnitude)
    };
    if dot(normal, direction) > 0.0 {
        normal = normal.map(|value| -value);
    }
    Some(SurfaceHit { distance, normal })
}

/// Test the vertical projection of a point against a triangle, either winding.
pub fn inside_xz(&[a, b, c]: &[Vec3; 3], x: f64, z: f64) -> bool {
    let edge = |from: Vec3, to: Vec3| {
        (to[0] - from[0]) * (z - from[2]) - (to[2] - from[2]) * (x - from[0])
    };
    let edges = [edge(a, b), edge(b, c), edge(c, a)];
    edges.into_iter().all(|value| value >= -GROUND_EDGE_EPSILON)
        || edges.into_iter().all(|value| value <= GROUND_EDGE_EPSILON)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ORIGIN: Vec3 = [0.0; 3];
    const FORWARD: Vec3 = [0.0, 0.0, 1.0];
    const BACK: Vec3 = [0.0, 0.0, -1.0];

    fn assert_hit(hit: Option<SurfaceHit>, distance: f64, normal: Vec3) {
        let SurfaceHit {
            distance: found,
            normal: found_normal,
        } = hit.unwrap();
        assert!((found - distance).abs() <= 8.0 * f64::EPSILON * distance.abs().max(1.0));
        for (found, expected) in found_normal.into_iter().zip(normal) {
            assert!((found - expected).abs() <= 8.0 * f64::EPSILON);
        }
    }

    #[test]
    fn sphere_hits_tangents_exits_and_the_exact_end_of_the_segment() {
        let center = [0.0, 0.0, 5.0];
        assert_hit(
            round_hit(ORIGIN, FORWARD, 4.0, center, center, 1.0),
            4.0,
            BACK,
        );
        assert!(round_hit(ORIGIN, FORWARD, 3.99, center, center, 1.0).is_none());
        assert_hit(
            round_hit([1.0, 0.0, 0.0], FORWARD, 5.0, center, center, 1.0),
            5.0,
            [1.0, 0.0, 0.0],
        );
        assert!(round_hit([1.01, 0.0, 0.0], FORWARD, 8.0, center, center, 1.0).is_none());
        assert_hit(
            round_hit(center, FORWARD, 2.0, center, center, 1.0),
            1.0,
            BACK,
        );
        assert!(round_hit([0.0, 0.0, 4.0], BACK, 8.0, center, center, 1.0).is_none());
    }

    #[test]
    fn capsule_distinguishes_cylinder_caps_and_parallel_rays() {
        let a = [0.0, -1.0, 5.0];
        let b = [0.0, 1.0, 5.0];
        assert_hit(
            round_hit([0.6, 0.0, 0.0], FORWARD, 8.0, a, b, 1.0),
            4.2,
            [0.6, 0.0, -0.8],
        );
        assert_hit(
            round_hit([0.0, 1.6, 0.0], FORWARD, 8.0, a, b, 1.0),
            4.2,
            [0.0, 0.6, -0.8],
        );
        assert_hit(
            round_hit([0.6, -5.0, 5.0], [0.0, 1.0, 0.0], 8.0, a, b, 1.0),
            3.2,
            [0.6, -0.8, 0.0],
        );
        assert_hit(
            round_hit([0.0, 0.0, 5.0], FORWARD, 2.0, a, b, 1.0),
            1.0,
            BACK,
        );
        assert!(round_hit([0.0, 2.01, 0.0], FORWARD, 8.0, a, b, 1.0).is_none());
    }

    #[test]
    fn oblique_capsule_hits_match_python_at_ordinary_and_large_scale() {
        let direction = [0.2672612419124244, 0.5345224838248488, 0.8017837257372732];
        assert_hit(
            round_hit(
                [1.25, -0.6, 0.17],
                direction,
                10.0,
                [1.0, 3.0, 6.0],
                [3.0, 4.0, 7.0],
                2.0,
            ),
            5.90188677099896,
            [
                0.5617696079708732,
                -0.39860600498179577,
                -0.7249332109599506,
            ],
        );
        assert_hit(
            round_hit(
                [1e140, -5e140, 2e140],
                direction,
                2e141,
                [3e140, -2e140, 7e140],
                [4e140, 1e140, 6e140],
                1.3e140,
            ),
            4.932218752654481e140,
            [
                -0.5244685313706268,
                -0.27970629351048426,
                -0.8041748248811104,
            ],
        );
    }

    #[test]
    fn triangle_hits_both_faces_and_keeps_the_positive_cutoff() {
        let triangle = [[-2.0, -1.0, 6.0], [2.0, -1.0, 6.0], [0.0, 2.0, 6.0]];
        assert_hit(triangle_hit(ORIGIN, FORWARD, 6.0, &triangle), 6.0, BACK);
        assert_hit(
            triangle_hit([0.0, 0.0, 7.0], BACK, 1.0, &triangle),
            1.0,
            FORWARD,
        );
        assert!(triangle_hit([0.0, 0.0, 7.0], BACK, 0.99, &triangle).is_none());
        assert!(triangle_distance([0.0, 0.0, 6.0], FORWARD, &triangle).is_none());
        assert!(triangle_distance([3.0, 0.0, 0.0], FORWARD, &triangle).is_none());
        assert!(triangle_distance(ORIGIN, FORWARD, &[[0.0; 3]; 3]).is_none());
    }

    #[test]
    fn invalid_input_and_ground_seams_match_the_reference_guards() {
        let center = [0.0, 0.0, 5.0];
        assert!(round_hit(ORIGIN, ORIGIN, 8.0, center, center, 1.0).is_none());
        assert!(round_hit(ORIGIN, FORWARD, f64::INFINITY, center, center, 1.0).is_none());
        assert!(round_hit(ORIGIN, FORWARD, 8.0, center, center, 0.0).is_none());
        assert!(round_hit(ORIGIN, FORWARD, 8.0, center, [f64::NAN, 0.0, 5.0], 1.0).is_none());

        let floor = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]];
        assert!(inside_xz(&floor, 0.5, -1e-8));
        assert!(inside_xz(&[floor[2], floor[1], floor[0]], 0.5, -1e-8));
        assert!(!inside_xz(&floor, 0.5, -1e-4));
        assert!(triangle_hit([0.25, 1.0, 0.25], [0.0, -1.0, 0.0], f64::INFINITY, &floor).is_none());
    }
}
