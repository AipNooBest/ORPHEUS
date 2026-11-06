//! Float32 capsule contacts against a convex hull for retail build 104319.
//!
//! Ports the clipped face, GJK witness, and separating-axis contact paths
//! from `ow174/game/contacts.py`. Shape A is the hull.

use std::cmp::Ordering;
use std::error::Error;
use std::fmt;

use crate::collision_cache::{HalfEdge, Hull};
use crate::contact_gjk::{PointSetSize, SimplexCache, gjk};
use crate::contact_math::{
    ContactFrame, TINY, Vec3, add, cross, dot, minps, mul, normalize, rotate, rsqrt, scale, sub,
};
use crate::contact_round::{Capsule, ContactPoint, Feature, FeatureId, FeatureKind, Manifold};
use crate::contact_triangle::{
    ClipPoint, EDGE_TOLERANCE, FACE_COS, SKIN, TOUCH, clip_segment, closest_segments, edge_feature,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HullContactError {
    PointSetSize(PointSetSize),
    InvalidTopology,
}

impl fmt::Display for HullContactError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PointSetSize(error) => fmt::Display::fmt(error, f),
            Self::InvalidTopology => f.write_str("invalid half-edge topology for hull contact"),
        }
    }
}

impl Error for HullContactError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::PointSetSize(error) => Some(error),
            Self::InvalidTopology => None,
        }
    }
}

impl From<PointSetSize> for HullContactError {
    fn from(value: PointSetSize) -> Self {
        Self::PointSetSize(value)
    }
}

fn plane_normal([x, y, z, _]: [f32; 4]) -> Vec3 {
    [x, y, z]
}

fn edge_at(hull: &Hull, edge: usize) -> Result<HalfEdge, HullContactError> {
    hull.half_edges
        .get(edge)
        .copied()
        .ok_or(HullContactError::InvalidTopology)
}

fn twin_at(hull: &Hull, index: usize, edge: HalfEdge) -> Result<HalfEdge, HullContactError> {
    let twin = index
        .checked_add_signed(isize::from(edge.twin))
        .ok_or(HullContactError::InvalidTopology)?;
    edge_at(hull, twin)
}

fn origin(hull: &Hull, edge: HalfEdge) -> Result<Vec3, HullContactError> {
    hull.vertices
        .get(usize::from(edge.origin))
        .copied()
        .ok_or(HullContactError::InvalidTopology)
}

fn plane_at(hull: &Hull, face: usize) -> Result<[f32; 4], HullContactError> {
    hull.planes
        .get(face)
        .copied()
        .ok_or(HullContactError::InvalidTopology)
}

fn face_contact(hull: &Hull, face: usize, capsule: Capsule) -> Result<Manifold, HullContactError> {
    let plane = plane_at(hull, face)?;
    let normal = plane_normal(plane);
    let index = u8::try_from(face).map_err(|_| HullContactError::InvalidTopology)?;
    let face_feature = Feature {
        index,
        kind: FeatureKind::Face,
    };
    let mut points = std::array::from_fn(|endpoint| ClipPoint {
        position: [capsule.a, capsule.b][endpoint],
        feature: FeatureId {
            a: face_feature,
            b: Feature::vertex(endpoint as u8),
            triangle: 0,
        },
    });
    let first = usize::from(
        *hull
            .faces
            .get(face)
            .ok_or(HullContactError::InvalidTopology)?,
    );
    let mut edge = first;
    let mut start = origin(hull, edge_at(hull, edge)?)?;
    let mut began = false;
    for _ in 0..hull.half_edges.len().min(256) {
        let next = usize::from(edge_at(hull, edge)?.next);
        let end = origin(hull, edge_at(hull, next)?)?;
        let side = cross(sub(end, start), normal);
        let length = dot(side, side);
        if length > TINY {
            let side = scale(side, rsqrt(length));
            let Some(clipped) = clip_segment(points, side, dot(side, start), edge & 0xfe) else {
                return Ok(Manifold::default());
            };
            points = clipped;
            began = true;
        }
        if !began {
            return Ok(Manifold::default());
        }
        (edge, start) = (next, end);
        if edge == first {
            let reach = SKIN + capsule.radius;
            let mut out = Manifold::with_normal(normal);
            for ClipPoint { position, feature } in points {
                let separation = (dot(position, normal) - plane[3]) - reach;
                if separation <= 0.0 {
                    out.push(ContactPoint {
                        position: sub(position, scale(normal, capsule.radius)),
                        separation,
                        feature,
                    });
                }
            }
            return Ok(out);
        }
    }
    Err(HullContactError::InvalidTopology)
}

fn find_edge(hull: &Hull, a: u8, b: u8) -> Result<Option<u8>, HullContactError> {
    for (index, &edge) in hull.half_edges.iter().enumerate() {
        if edge.origin == a && twin_at(hull, index, edge)?.origin == b {
            return Ok(Some((index & 0xfe) as u8));
        }
    }
    Ok(None)
}

fn witness_feature(hull: &Hull, cache: SimplexCache) -> Result<FeatureId, HullContactError> {
    let count = cache.len();
    let a0 = cache.index_a[0];
    let b0 = cache.index_b[0];
    let a = if let Some(other) = cache.index_a[1..count]
        .iter()
        .copied()
        .find(|&index| index != a0)
    {
        find_edge(hull, a0, other)?.map_or(
            Feature {
                index: 0,
                kind: FeatureKind::Unclassified,
            },
            Feature::edge,
        )
    } else {
        Feature::vertex(a0)
    };
    let b = if cache.index_b[1..count].iter().any(|&index| index != b0) {
        Feature::edge(0)
    } else {
        Feature::vertex(b0)
    };
    Ok(FeatureId { a, b, triangle: 0 })
}

#[derive(Clone, Copy)]
struct EdgeAxis {
    index: usize,
    start: Vec3,
    end: Vec3,
    normal: Vec3,
}

impl EdgeAxis {
    fn new(index: usize, start: Vec3, end: Vec3, segment: Vec3, centroid: Vec3) -> Option<Self> {
        let axis = cross(sub(end, start), segment);
        let length = dot(axis, axis);
        if !matches!(
            length.partial_cmp(&f32::EPSILON),
            Some(Ordering::Equal | Ordering::Greater)
        ) {
            return None;
        }
        let axis = scale(axis, rsqrt(length));
        let normal = if dot(sub(start, centroid), axis) < 0.0 {
            axis.map(|value| -value)
        } else {
            axis
        };
        Some(Self {
            index,
            start,
            end,
            normal,
        })
    }

    fn contact(self, capsule: Capsule) -> Manifold {
        let (pa, pb) = closest_segments(self.start, self.end, capsule.a, capsule.b);
        Manifold::one(
            self.normal,
            ContactPoint {
                position: scale(add(pa, pb), 0.5),
                separation: dot(sub(pb, pa), self.normal) - (capsule.radius + SKIN),
                feature: edge_feature(self.index),
            },
        )
    }
}

enum Axis {
    Face(usize),
    Edge(EdgeAxis),
}

fn overlap_contact(
    hull: &Hull,
    capsule: Capsule,
    reach: f32,
) -> Result<Manifold, HullContactError> {
    let mut best = -f32::MAX;
    let mut selected = None;
    for (index, &plane) in hull.planes.iter().enumerate() {
        let normal = plane_normal(plane);
        let separation = minps(dot(capsule.a, normal), dot(capsule.b, normal)) - plane[3];
        if reach < separation {
            return Ok(Manifold::default());
        }
        if best < separation {
            best = separation;
            selected = Some(Axis::Face(index));
        }
    }
    let segment = sub(capsule.b, capsule.a);
    for (index, &edge) in hull.half_edges.iter().enumerate().step_by(2) {
        let twin = twin_at(hull, index, edge)?;
        let a = plane_normal(plane_at(hull, usize::from(twin.face))?);
        let b = plane_normal(plane_at(hull, usize::from(edge.face))?);
        if dot(a, segment) * dot(b, segment) >= 0.0 {
            continue;
        }
        let start = origin(hull, edge)?;
        let end = origin(hull, twin)?;
        let Some(axis) = EdgeAxis::new(index, start, end, segment, hull.centroid) else {
            continue;
        };
        let separation = dot(sub(capsule.a, start), axis.normal);
        if reach < separation {
            return Ok(Manifold::default());
        }
        if best * EDGE_TOLERANCE + (0.5 * TOUCH) < separation {
            best = separation;
            selected = Some(Axis::Edge(axis));
        }
    }
    match selected {
        Some(Axis::Face(face)) => face_contact(hull, face, capsule),
        Some(Axis::Edge(edge)) => Ok(edge.contact(capsule)),
        None => Ok(Manifold::default()),
    }
}

/// Capsule contact in the hull's local frame with a reusable simplex cache.
pub fn capsule_hull(
    hull: &Hull,
    capsule: Capsule,
    cache: SimplexCache,
) -> Result<(Manifold, SimplexCache), HullContactError> {
    let reach = SKIN + capsule.radius;
    let distance = gjk(
        &hull.vertices,
        &[capsule.a, capsule.b],
        cache,
        Some(f64::from(reach)),
    )?;
    let cache = distance.cache;
    if reach < distance.distance {
        let mut out = Manifold::default();
        out.gap = Some(distance.lower);
        return Ok((out, cache));
    }
    let witness = distance.witness.filter(|witness| {
        dot(witness.normal, witness.normal) > TINY
            && distance.distance.partial_cmp(&TOUCH) != Some(Ordering::Less)
            && cache.len() != 4
    });
    if let Some(witness) = witness {
        let normal = normalize(witness.normal);
        let mut best = -f32::MAX;
        let mut face = 0;
        for (index, &plane) in hull.planes.iter().enumerate() {
            let [x, y, z] = mul(plane_normal(plane), normal);
            let similarity = (y + x) + z;
            if similarity > best {
                best = similarity;
                face = index;
            }
        }
        if best > FACE_COS {
            let contact = face_contact(hull, face, capsule)?;
            if !contact.points().is_empty() {
                return Ok((contact, cache));
            }
        }
        let feature = witness_feature(hull, cache)?;
        let point = ContactPoint {
            position: sub(witness.point_b, scale(normal, capsule.radius)),
            separation: distance.distance - reach,
            feature,
        };
        return Ok((Manifold::one(normal, point), cache));
    }
    Ok((overlap_contact(hull, capsule, reach)?, cache))
}

/// Capsule contact between placed bodies, with manifold coordinates in world space.
pub fn placed_capsule_hull(
    hull: &Hull,
    hull_frame: ContactFrame,
    capsule: Capsule,
    capsule_frame: ContactFrame,
    cache: SimplexCache,
) -> Result<(Manifold, SimplexCache), HullContactError> {
    let capsule = Capsule {
        a: hull_frame.local(capsule_frame.place(capsule.a)),
        b: hull_frame.local(capsule_frame.place(capsule.b)),
        radius: capsule.radius,
    };
    let (local, cache) = capsule_hull(hull, capsule, cache)?;
    let mut world = Manifold::with_normal(rotate(hull_frame.rotation, local.normal));
    world.gap = local.gap;
    for point in local.points() {
        world.push(ContactPoint {
            position: hull_frame.place(point.position),
            ..*point
        });
    }
    Ok((world, cache))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cube() -> Hull {
        let vertices = vec![
            [-1.0, -1.0, -1.0],
            [-1.0, -1.0, 1.0],
            [-1.0, 1.0, -1.0],
            [-1.0, 1.0, 1.0],
            [1.0, -1.0, -1.0],
            [1.0, -1.0, 1.0],
            [1.0, 1.0, -1.0],
            [1.0, 1.0, 1.0],
        ];
        let planes = vec![
            [-1.0, 0.0, 0.0, 1.0],
            [1.0, 0.0, 0.0, 1.0],
            [0.0, -1.0, 0.0, 1.0],
            [0.0, 1.0, 0.0, 1.0],
            [0.0, 0.0, -1.0, 1.0],
            [0.0, 0.0, 1.0, 1.0],
        ];
        let half_edges = [
            (1, 0, 0, 2),
            (-1, 1, 2, 16),
            (1, 1, 0, 4),
            (-1, 3, 5, 19),
            (1, 3, 0, 6),
            (-1, 2, 3, 20),
            (1, 2, 0, 0),
            (-1, 0, 4, 23),
            (1, 4, 1, 10),
            (-1, 6, 4, 17),
            (1, 6, 1, 12),
            (-1, 7, 3, 22),
            (1, 7, 1, 14),
            (-1, 5, 5, 21),
            (1, 5, 1, 8),
            (-1, 4, 2, 18),
            (1, 0, 2, 15),
            (-1, 4, 4, 7),
            (1, 5, 2, 1),
            (-1, 1, 5, 13),
            (1, 3, 3, 11),
            (-1, 7, 5, 3),
            (1, 6, 3, 5),
            (-1, 2, 4, 9),
        ]
        .into_iter()
        .map(|(twin, origin, face, next)| HalfEdge {
            twin,
            origin,
            face,
            next,
        })
        .collect();
        Hull {
            mask: 1,
            centroid: [0.0; 3],
            vertices,
            planes,
            faces: vec![0, 8, 16, 5, 7, 19],
            half_edges,
        }
    }

    fn capsule(a: Vec3, b: Vec3) -> Capsule {
        Capsule { a, b, radius: 0.5 }
    }

    fn assert_point(point: &ContactPoint, values: [u32; 4], id: [u8; 4]) {
        assert_eq!(point.position.map(f32::to_bits), values[..3]);
        assert_eq!(point.separation.to_bits(), values[3]);
        assert_eq!(
            [
                point.feature.a.index,
                point.feature.b.index,
                point.feature.a.kind as u8,
                point.feature.b.kind as u8
            ],
            id
        );
        assert_eq!(point.feature.triangle, 0);
    }

    #[test]
    fn clips_face_endpoints_and_preserves_the_far_lower_bound() {
        let hull = cube();
        let body = capsule([1.2, -0.3, 0.0], [1.2, 0.3, 0.0]);
        let (manifold, cache) = capsule_hull(&hull, body, SimplexCache::EMPTY).unwrap();
        assert_eq!(manifold.normal, [1.0, 0.0, 0.0]);
        assert_eq!(manifold.points().len(), 2);
        assert_point(
            &manifold.points()[0],
            [0x3f33_3334, 0xbe99_999a, 0, 0xbe9e_b850],
            [1, 0, 0, 2],
        );
        assert_point(
            &manifold.points()[1],
            [0x3f33_3334, 0x3e99_999a, 0, 0xbe9e_b850],
            [1, 1, 0, 2],
        );
        assert_eq!(cache.metric.to_bits(), 0x412c_28f5);
        assert_eq!(
            (cache.index_a, cache.index_b),
            ([4, 7, 255, 255], [1, 0, 0, 0])
        );
        let far = capsule([3.0, -0.3, 0.0], [3.0, 0.3, 0.0]);
        let (far, _) = capsule_hull(&hull, far, cache).unwrap();
        assert!(far.points().is_empty());
        assert_eq!(far.gap.unwrap().to_bits(), 0x3fff_ffff_dfff_fff0);
    }

    #[test]
    fn witness_preserves_even_half_edge_and_unclassified_fallback() {
        let mut hull = cube();
        let body = capsule([1.25, 1.1, 0.0], [1.25, 1.4, 0.0]);
        let (edge, cache) = capsule_hull(&hull, body, SimplexCache::EMPTY).unwrap();
        assert_eq!(
            edge.normal.map(f32::to_bits),
            [0x3f6d_b0a5, 0x3ebe_26ed, 0x8000_0000]
        );
        assert_point(
            &edge.points()[0],
            [0x3f49_27ae, 0x3f6a_0fdf, 0, 0xbe76_8500],
            [10, 0, 1, 2],
        );
        hull.half_edges.clear();
        hull.faces.clear();
        hull.planes.clear();
        let (unknown, other_cache) = capsule_hull(&hull, body, SimplexCache::EMPTY).unwrap();
        assert_eq!(other_cache, cache);
        assert_point(
            &unknown.points()[0],
            [0x3f49_27ae, 0x3f6a_0fdf, 0, 0xbe76_8500],
            [0, 0, 3, 2],
        );
    }

    #[test]
    fn deep_overlap_uses_face_axes_and_rejects_a_nonclosing_loop() {
        let mut hull = cube();
        let body = capsule([0.9, 0.2, 0.0], [1.15, 0.65, 0.2]);
        let (overlap, cache) = capsule_hull(&hull, body, SimplexCache::EMPTY).unwrap();
        assert_eq!(cache.len(), 4);
        assert_eq!(overlap.normal, [1.0, 0.0, 0.0]);
        assert_eq!(overlap.points().len(), 2);
        assert_point(
            &overlap.points()[0],
            [0x3ecc_cccc, 0x3e4c_cccd, 0, 0xbf1c_28f6],
            [1, 0, 0, 2],
        );

        hull.half_edges[10].next = 10;
        let body = capsule([1.2, -0.3, 0.0], [1.2, 0.3, 0.0]);
        assert_eq!(
            capsule_hull(&hull, body, SimplexCache::EMPTY),
            Err(HullContactError::InvalidTopology)
        );
    }
}
