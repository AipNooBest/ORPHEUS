//! Float32 capsule contacts against one mesh triangle for retail build 104319.
//!
//! Ports the one-sided contact, feature selection, face clipping, and edge
//! filtering from `ow174/game/contacts.py`. The mesh broadphase supplies
//! the candidate and keeps its warm simplex cache between calls.

use std::cmp::Ordering;
use std::sync::OnceLock;

use crate::collision_cache::Mesh;
use crate::contact_gjk::{SimplexCache, gjk};
use crate::contact_math::{
    TINY, Vec3, add, cross, dot, maxps, minps, mul, normalize, rsqrt, scale, sub,
};
use crate::contact_round::{
    Capsule, ContactPoint, Feature, FeatureId, FeatureKind, Manifold, clamp01, splat_tiny,
};

pub const SKIN: f32 = 0.01;
pub const TOUCH: f32 = 0.005;
pub(crate) const FACE_COS: f32 = 0.996_194_7;
const FLAT_COS: f32 = 0.999_390_84;
pub(crate) const EDGE_TOLERANCE: f32 = 0.9;
const EDGE_SLOP: f32 = 0.0025;
const BEND: f32 = 0.034_906_585;
const ATAN_K: f32 = 0.28;

#[derive(Debug)]
pub struct ContactTriangle {
    corners: [Vec3; 3],
    wings: [Option<Vec3>; 3],
    normal: Vec3,
    bounds: [Vec3; 2],
    neighbors: OnceLock<[Vec3; 3]>,
    pub index: u32,
    pub mask: u16,
    pub surface: u16,
}

impl ContactTriangle {
    /// Wing k is the opposing vertex across the edge from corner k to k+1.
    pub fn new(
        corners: [Vec3; 3],
        wings: [Option<Vec3>; 3],
        index: u32,
        mask: u16,
        surface: u16,
    ) -> Self {
        let [a, b, c] = corners;
        Self {
            corners,
            wings,
            normal: normalize(cross(sub(b, a), sub(c, a))),
            bounds: [
                std::array::from_fn(|k| minps(minps(a[k], b[k]), c[k])),
                std::array::from_fn(|k| maxps(maxps(a[k], b[k]), c[k])),
            ],
            neighbors: OnceLock::new(),
            index,
            mask,
            surface,
        }
    }

    /// Fetch one triangle in the mesh's scaled local frame.
    pub fn from_mesh(mesh: &Mesh, number: usize, scale: Vec3) -> Option<Self> {
        let record = mesh.triangles.get(number)?;
        let vertex = |number: i32| {
            mesh.vertices
                .get(number as usize)
                .map(|&point| mul(point, scale))
        };
        let [a, b, c] = record.corners;
        let mut wings = [None; 3];
        for (wing, number) in wings.iter_mut().zip(record.wings) {
            if number >= 0 {
                *wing = Some(vertex(number)?);
            }
        }
        Some(Self::new(
            [vertex(a)?, vertex(b)?, vertex(c)?],
            wings,
            u32::try_from(number).ok()?,
            record.mask,
            record.surface,
        ))
    }

    pub fn corners(&self) -> &[Vec3; 3] {
        &self.corners
    }

    pub fn normal(&self) -> Vec3 {
        self.normal
    }

    pub(crate) fn contact_bounds(&self) -> [Vec3; 2] {
        self.bounds
    }

    pub fn neighbor_normals(&self) -> &[Vec3; 3] {
        self.neighbors.get_or_init(|| {
            std::array::from_fn(|edge| {
                let wing = self.wings[edge].unwrap_or(self.corners[(edge + 2) % 3]);
                normalize(cross(
                    sub(self.corners[(edge + 1) % 3], wing),
                    sub(self.corners[edge], wing),
                ))
            })
        })
    }

    fn reject_feature(&self, edge: usize, normal: Vec3) -> bool {
        let corners = self.corners;
        self.wings[edge].is_some()
            && dot(
                cross(normal, self.neighbor_normals()[edge]),
                sub(corners[(edge + 1) % 3], corners[edge]),
            ) < 0.0
    }
}

fn neg(vector: Vec3) -> Vec3 {
    vector.map(|lane| -lane)
}

fn atan2_approx(y: f32, x: f32) -> f32 {
    use std::f32::consts::{FRAC_PI_2, PI};
    if x == 0.0 {
        if y > 0.0 {
            return FRAC_PI_2;
        }
        return if y == 0.0 { 0.0 } else { -FRAC_PI_2 };
    }
    let z = y / x;
    if z.abs() < 1.0 {
        let angle = z / (((z * ATAN_K) * z) + 1.0);
        if x >= 0.0 {
            return angle;
        }
        return if y >= 0.0 { angle + PI } else { angle - PI };
    }
    let angle = FRAC_PI_2 - (z / ((z * z) + ATAN_K));
    if y >= 0.0 { angle } else { angle - PI }
}

/// Closest points of two finite segments in the contact engine's f32 order.
pub fn closest_segments(a0: Vec3, a1: Vec3, b0: Vec3, b1: Vec3) -> (Vec3, Vec3) {
    let (pa, pb) = closest_parameters(a0, a1, b0, b1);
    let gap = sub(pb, pa);
    if dot(gap, gap).partial_cmp(&TINY) != Some(Ordering::Greater) {
        return (pa, pa);
    }
    let n = normalize(gap);
    (add(scale(n, 0.0), pa), sub(pb, scale(n, 0.0)))
}

pub(crate) fn closest_parameters(a0: Vec3, a1: Vec3, b0: Vec3, b1: Vec3) -> (Vec3, Vec3) {
    let d1 = sub(a1, a0);
    let d2 = sub(b1, b0);
    let r = sub(a0, b0);
    let a = dot(d1, d1);
    let e = dot(d2, d2);
    let pa;
    let pb;
    let (nonzero_a, nonzero_b) = (splat_tiny(a), splat_tiny(e));
    if !nonzero_a && !nonzero_b {
        (pa, pb) = (a0, b0);
    } else {
        let f = dot(r, d2);
        let (s, t) = if !nonzero_a {
            (0.0, clamp01(f / e))
        } else {
            let c = dot(r, d1);
            if !nonzero_b {
                (clamp01(-c / a), 0.0)
            } else {
                let b = dot(d2, d1);
                let denominator = (e * a) - (b * b);
                let s = if splat_tiny(denominator) {
                    clamp01(((b * f) - (c * e)) / denominator)
                } else {
                    0.0
                };
                let t = ((b * s) + f) / e;
                if t < 0.0 {
                    (clamp01(-c / a), 0.0)
                } else if t > 1.0 {
                    (clamp01((b - c) / a), 1.0)
                } else {
                    (s, t)
                }
            }
        };
        (pa, pb) = (add(scale(d1, s), a0), add(scale(d2, t), b0));
    }
    (pa, pb)
}

#[derive(Clone, Copy)]
pub(crate) struct ClipPoint {
    pub position: Vec3,
    pub feature: FeatureId,
}

pub(crate) fn edge_feature(edge: usize) -> FeatureId {
    FeatureId {
        a: Feature::edge(edge as u8),
        b: Feature::edge(0),
        triangle: 0,
    }
}

pub(crate) fn clip_segment(
    points: [ClipPoint; 2],
    side: Vec3,
    offset: f32,
    edge: usize,
) -> Option<[ClipPoint; 2]> {
    let [q0, q1] = points.map(|point| point.position);
    let s0 = dot(q0, side) - offset;
    let s1 = dot(q1, side) - offset;
    let keep = match (s0 <= 0.0, s1 <= 0.0) {
        (true, true) => return Some(points),
        (true, false) => points[0],
        (false, true) => points[1],
        (false, false) => return None,
    };
    if (s1 * s0).partial_cmp(&0.0) != Some(Ordering::Less) {
        return None;
    }
    let t = s0 / (s0 - s1);
    let crossing = ClipPoint {
        position: add(scale(sub(q1, q0), t), q0),
        feature: edge_feature(edge),
    };
    Some([keep, crossing])
}

fn face_contact(triangle: &ContactTriangle, capsule: Capsule) -> Manifold {
    let v = triangle.corners;
    let n = triangle.normal;
    let mut points = [capsule.a, capsule.b].map(|position| ClipPoint {
        position,
        feature: FeatureId {
            a: Feature::default(),
            b: Feature::vertex(0),
            triangle: triangle.index,
        },
    });
    points[1].feature.b = Feature::vertex(1);
    for (edge, point) in v.into_iter().enumerate() {
        let side = normalize(cross(sub(v[(edge + 1) % 3], point), n));
        let Some(clipped) = clip_segment(points, side, dot(side, point) + SKIN, edge) else {
            return Manifold::default();
        };
        points = clipped;
    }
    let sides = triangle.neighbor_normals();
    let reach = SKIN + capsule.radius;
    let mut out = Manifold::with_normal(n);
    for ClipPoint { position, feature } in points {
        if feature.a.kind == FeatureKind::Edge
            && dot(sides[usize::from(feature.a.index)], n) > FLAT_COS
        {
            continue;
        }
        let separation = dot(sub(position, v[0]), n);
        if separation <= reach {
            out.push(ContactPoint {
                position: sub(position, scale(n, capsule.radius)),
                separation: separation - reach,
                feature,
            });
        }
    }
    out
}

fn edge_axis(v: [Vec3; 3], segment: Vec3, edge: usize) -> Option<Vec3> {
    let axis = cross(sub(v[(edge + 1) % 3], v[edge]), segment);
    let length = dot(axis, axis);
    if length.partial_cmp(&TINY) != Some(Ordering::Greater) {
        return None;
    }
    let axis = scale(axis, rsqrt(length));
    Some(if dot(sub(v[edge], v[(edge + 2) % 3]), axis) < 0.0 {
        neg(axis)
    } else {
        axis
    })
}

fn edge_contact(v: [Vec3; 3], capsule: Capsule, normal: Vec3, edge: usize) -> Manifold {
    let (pa, pb) = closest_segments(v[edge], v[(edge + 1) % 3], capsule.a, capsule.b);
    let separation = dot(sub(pb, pa), normal) - (SKIN + capsule.radius);
    if separation > 0.0 {
        return Manifold::default();
    }
    Manifold::one(
        normal,
        ContactPoint {
            position: sub(pb, scale(normal, capsule.radius)),
            separation,
            feature: edge_feature(edge),
        },
    )
}

fn witness_feature(
    triangle: &ContactTriangle,
    normal: Vec3,
    cache: SimplexCache,
) -> Option<FeatureId> {
    let mut on_triangle = 0_u8;
    let mut on_capsule = 0_u8;
    for i in 0..cache.len() {
        on_triangle |= 1 << cache.index_a[i];
        on_capsule |= 1 << cache.index_b[i];
    }
    let a = match on_triangle.count_ones() {
        1 => {
            let corner = usize::from(cache.index_a[0]);
            if triangle.reject_feature((corner + 2) % 3, normal)
                || triangle.reject_feature(corner, normal)
            {
                return None;
            }
            Feature::vertex(cache.index_a[0])
        }
        2 => {
            let edge = match on_triangle {
                0b011 => 0,
                0b110 => 1,
                _ => 2,
            };
            if triangle.reject_feature(edge, normal) {
                return None;
            }
            Feature::edge(edge as u8)
        }
        _ => Feature::default(),
    };
    let b = if on_capsule.count_ones() == 1 {
        Feature::vertex(cache.index_b[0])
    } else {
        Feature::edge(0)
    };
    Some(FeatureId {
        a,
        b,
        triangle: triangle.index,
    })
}

fn overlap_contact(triangle: &ContactTriangle, capsule: Capsule, segment: Vec3) -> Manifold {
    let v = triangle.corners;
    let n = triangle.normal;
    let sides = triangle.neighbor_normals();
    let face = minps(dot(sub(capsule.a, v[0]), n), dot(sub(capsule.b, v[0]), n));
    let mut best = -f32::MAX;
    let mut use_face = false;
    let mut use_edge = None;
    if best < face {
        best = face;
        use_face = true;
    }
    for (edge, &side) in sides.iter().enumerate() {
        let Some(axis) = edge_axis(v, segment, edge) else {
            continue;
        };
        let separation = dot(sub(capsule.a, v[edge]), axis);
        if (EDGE_TOLERANCE * best + EDGE_SLOP).partial_cmp(&separation) != Some(Ordering::Less) {
            continue;
        }
        if triangle.wings[edge].is_some() {
            let direction = normalize(sub(v[(edge + 1) % 3], v[edge]));
            let angle = atan2_approx(dot(cross(side, n), direction), dot(side, n));
            if angle >= -BEND
                || (angle.partial_cmp(&BEND).is_none_or(Ordering::is_lt)
                    && dot(side, segment) * dot(n, segment) >= 0.0)
            {
                continue;
            }
        }
        best = separation;
        use_edge = Some((edge, axis));
    }
    if let Some((edge, axis)) = use_edge {
        edge_contact(v, capsule, axis, edge)
    } else if use_face {
        face_contact(triangle, capsule)
    } else {
        Manifold::default()
    }
}

/// One-sided contact in the triangle's local frame. Shape A is the triangle.
pub fn capsule_triangle(
    triangle: &ContactTriangle,
    capsule: Capsule,
    cache: SimplexCache,
) -> (Manifold, SimplexCache) {
    let segment = sub(capsule.b, capsule.a);
    if dot(segment, segment).partial_cmp(&TINY) != Some(Ordering::Greater) {
        return (Manifold::default(), cache);
    }
    let center = scale(add(capsule.a, capsule.b), 0.5);
    if dot(sub(center, triangle.corners[0]), triangle.normal) < 0.0 {
        return (Manifold::default(), cache);
    }
    let reach = capsule.radius + SKIN;
    let distance = gjk(
        &triangle.corners,
        &[capsule.a, capsule.b],
        cache,
        Some(f64::from(reach)),
    )
    .expect("a triangle and a segment are valid GJK point counts");
    let cache = distance.cache;
    if reach < distance.distance {
        let mut out = Manifold::default();
        out.gap = Some(distance.lower);
        return (out, cache);
    }
    if let Some(witness) = distance.witness {
        if dot(witness.normal, witness.normal) > TINY
            && distance.distance.partial_cmp(&TOUCH) != Some(Ordering::Less)
            && cache.len() != 4
        {
            let normal = normalize(witness.normal);
            if dot(triangle.normal, normal) > FACE_COS {
                let face = face_contact(triangle, capsule);
                if !face.points().is_empty() {
                    return (face, cache);
                }
            }
            let Some(feature) = witness_feature(triangle, normal, cache) else {
                return (Manifold::default(), cache);
            };
            let point = ContactPoint {
                position: sub(witness.point_b, scale(normal, capsule.radius)),
                separation: distance.distance - reach,
                feature,
            };
            return (Manifold::one(normal, point), cache);
        }
    }
    (overlap_contact(triangle, capsule, segment), cache)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collision_cache::Triangle;

    const CORNERS: [Vec3; 3] = [[0.0; 3], [0.0, 0.0, 4.0], [4.0, 0.0, 0.0]];
    const FLAT_WING: Vec3 = [-2.0, 0.0, 2.0];
    const UP: Vec3 = [0.0, 1.0, 0.0];

    fn triangle(wing: Option<Vec3>) -> ContactTriangle {
        ContactTriangle::new(CORNERS, [wing, None, None], 7, 8, 11)
    }

    fn capsule(a: Vec3, b: Vec3) -> Capsule {
        Capsule { a, b, radius: 0.5 }
    }

    fn assert_point(point: &ContactPoint, values: [u32; 4], feature: FeatureId) {
        assert_eq!(point.position.map(f32::to_bits), values[..3]);
        assert_eq!(point.separation.to_bits(), values[3]);
        assert_eq!(point.feature, feature);
    }

    fn face(endpoint: u8) -> FeatureId {
        FeatureId {
            a: Feature::default(),
            b: Feature::vertex(endpoint),
            triangle: 7,
        }
    }

    #[test]
    fn face_points_and_far_gap_preserve_feature_order_and_warm_cache() {
        let tri = triangle(None);
        let body = capsule([1.0, 0.2, 1.0], [2.0, 0.2, 1.0]);
        let (hit, cache) = capsule_triangle(&tri, body, SimplexCache::EMPTY);
        assert_eq!(hit.normal, UP);
        assert_eq!(hit.points().len(), 2);
        assert_eq!(hit.gap, None);
        assert_eq!(cache.metric, 256.0);
        assert_eq!(cache.index_a, [0, 1, 2, 255]);
        assert_point(
            &hit.points()[0],
            [0x3f80_0000, 0xbe99_999a, 0x3f80_0000, 0xbe9e_b852],
            face(0),
        );
        assert_point(
            &hit.points()[1],
            [0x4000_0000, 0xbe99_999a, 0x3f80_0000, 0xbe9e_b852],
            face(1),
        );

        let (far, far_cache) =
            capsule_triangle(&tri, capsule([1.0, 4.0, 1.0], [2.0, 4.0, 1.0]), cache);
        assert_eq!(far_cache, cache);
        assert!(far.points().is_empty());
        assert_eq!(far.gap, Some(4.0));
        for ignored in [
            capsule([1.0, -0.2, 1.0], [2.0, -0.2, 1.0]),
            capsule([1.0, 0.2, 1.0], [1.0, 0.2, 1.0]),
        ] {
            assert_eq!(
                capsule_triangle(&tri, ignored, cache),
                (Manifold::default(), cache)
            );
        }
    }

    #[test]
    fn clipped_and_witness_edges_obey_neighbor_winding() {
        let open = triangle(None);
        let flat = triangle(Some(FLAT_WING));
        let clipped = capsule([1.0, 0.2, 1.0], [-0.2, 0.2, 1.0]);
        let (open_hit, cache) = capsule_triangle(&open, clipped, SimplexCache::EMPTY);
        let (flat_hit, flat_cache) = capsule_triangle(&flat, clipped, SimplexCache::EMPTY);
        assert_eq!(open_hit.points().len(), 2);
        assert_point(
            &open_hit.points()[1],
            [0xbc23_d700, 0xbe99_999a, 0x3f80_0000, 0xbe9e_b852],
            edge_feature(0),
        );
        assert_eq!(flat_hit.points(), &open_hit.points()[..1]);
        assert_eq!(flat_cache, cache);

        let body = capsule([-0.3, 0.25, 1.0], [-0.3, 1.25, 1.0]);
        let (open_hit, cache) = capsule_triangle(&open, body, SimplexCache::EMPTY);
        assert_eq!(
            open_hit.normal.map(f32::to_bits),
            [0xbf44_aa26, 0x3f23_e31f, 0x8000_0000]
        );
        assert_point(
            &open_hit.points()[0],
            [0x3dac_4230, 0xbd8f_8c7c, 0x3f80_0000, 0xbdf4_b5dc],
            FeatureId {
                a: Feature::edge(0),
                b: Feature::vertex(0),
                triangle: 7,
            },
        );
        assert_eq!(
            capsule_triangle(&flat, body, SimplexCache::EMPTY),
            (Manifold::default(), cache)
        );
    }

    #[test]
    fn deep_overlap_prefers_a_real_edge_and_skips_a_flat_shared_edge() {
        let body = capsule([0.001, -0.1, 1.0], [0.001, 0.8, 1.0]);
        let (open, cache) = capsule_triangle(&triangle(None), body, SimplexCache::EMPTY);
        let (flat, same) = capsule_triangle(&triangle(Some(FLAT_WING)), body, SimplexCache::EMPTY);
        assert_eq!(cache, same);
        assert_eq!(cache.len(), 4);
        assert_eq!(open.normal, [-1.0, 0.0, 0.0]);
        assert_point(
            &open.points()[0],
            [0x3f00_4189, 0, 0x3f80_0000, 0xbf02_d0e5],
            edge_feature(0),
        );
        assert_eq!(flat.normal, UP);
        assert_point(
            &flat.points()[0],
            [0x3a83_126f, 0xbf19_999a, 0x3f80_0000, 0xbf1c_28f6],
            face(0),
        );
    }

    #[test]
    fn mesh_fetch_applies_contact_scale_and_keeps_metadata() {
        let mesh = Mesh {
            vertices: CORNERS.into_iter().chain([FLAT_WING]).collect(),
            triangles: vec![Triangle {
                corners: [0, 1, 2],
                wings: [3, -1, -1],
                mask: 8,
                surface: 11,
            }],
        };
        let tri = ContactTriangle::from_mesh(&mesh, 0, [2.0, 3.0, 4.0]).unwrap();
        assert_eq!(
            *tri.corners(),
            [[0.0; 3], [0.0, 0.0, 16.0], [8.0, 0.0, 0.0]]
        );
        assert_eq!(
            (tri.index, tri.mask, tri.surface, tri.normal()),
            (0, 8, 11, UP)
        );
        assert_eq!(
            *tri.neighbor_normals(),
            [UP, [0.0, -1.0, 0.0], [0.0, -1.0, 0.0]]
        );
        assert!(ContactTriangle::from_mesh(&mesh, 1, [1.0; 3]).is_none());
    }
}
