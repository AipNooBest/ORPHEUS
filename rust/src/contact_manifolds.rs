//! Reduction of triangle contacts into ordered mesh manifolds for build 104319.
//!
//! Ports the bounded spherical clustering and point selection of
//! `ow174/game/contacts.py`, with float32 thresholds and source-order ties.

use std::cmp::Ordering;

use crate::contact_math::{TINY, Vec3, add, cross, dot, maxps, minps, normalize, sub};
use crate::contact_round::{ContactPoint, Manifold};
use crate::contact_triangle::FACE_COS;

const CLUSTERS: usize = 3;
const ROUNDS: usize = 4;
const KEEP: f32 = 0.99;
const SPREAD: f32 = 0.0004;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ContactCluster {
    pub normal: Vec3,
    points: [ContactPoint; 4],
    len: usize,
}

impl ContactCluster {
    fn empty(normal: Vec3) -> Self {
        Self {
            normal,
            points: [ContactPoint::default(); 4],
            len: 0,
        }
    }

    fn push(&mut self, point: ContactPoint) {
        self.points[self.len] = point;
        self.len += 1;
    }

    pub fn points(&self) -> &[ContactPoint] {
        &self.points[..self.len]
    }
}

fn highest(points: &[ContactPoint], score: impl Fn(Vec3) -> f32) -> (usize, f32) {
    let mut best = -f32::MAX;
    // The reference keeps the final point if no tangent comparison succeeds.
    let mut selected = points.len() - 1;
    for (index, point) in points.iter().enumerate() {
        let value = score(point.position);
        if value * KEEP > best {
            best = value;
            selected = index;
        }
    }
    (selected, best)
}

fn enough_spread(value: f32) -> bool {
    value.partial_cmp(&SPREAD).is_some_and(Ordering::is_ge)
}

/// Keep at most four points by tangent extent, distance, area and outside area.
pub fn reduce_points(normal: Vec3, points: &[ContactPoint]) -> ContactCluster {
    let mut out = ContactCluster::empty(normal);
    if points.is_empty() {
        return out;
    }
    let [nx, ny, nz] = normal;
    let minus_x = 0.0 - nx;
    let tangent = if maxps(0.0 - ny, ny) > 0.5 {
        [ny, minus_x, 0.0]
    } else {
        [nz, 0.0, minus_x]
    };
    let tangent = normalize(tangent);
    let (first, _) = highest(points, |point| dot(tangent, point));
    let p1 = points[first].position;
    out.push(points[first]);

    let (second, best) = highest(points, |point| {
        let displacement = sub(point, p1);
        dot(displacement, displacement)
    });
    if !enough_spread(best) {
        return out;
    }
    let p2 = points[second].position;
    out.push(points[second]);

    let edge = sub(p2, p1);
    let (third, best) = highest(points, |point| {
        dot(cross(edge, sub(point, p1)), normal).abs()
    });
    if !enough_spread(best) {
        return out;
    }
    let p3 = points[third].position;
    out.push(points[third]);

    let area = dot(cross(edge, sub(p3, p1)), normal);
    let inverse = 1.0 / area;
    let e32 = sub(p3, p2);
    let e13 = sub(p1, p3);
    let mut best = f32::MAX;
    let mut fourth = points.len() - 1;
    for (index, point) in points.iter().enumerate() {
        let p = point.position;
        let a = dot(cross(e32, sub(p, p2)), normal);
        let b = dot(cross(e13, sub(p, p3)), normal);
        let c = dot(cross(edge, sub(p, p1)), normal);
        let value = minps(a * inverse, minps(b * inverse, c * inverse));
        if value * KEEP < best {
            best = value;
            fourth = index;
        }
    }
    if best <= 0.0 && enough_spread((area * best).abs()) {
        out.push(points[fourth]);
    }
    out
}

/// Group neighboring triangle manifolds into at most three ordered normals.
pub fn cluster_manifolds(manifolds: &[Manifold]) -> Vec<ContactCluster> {
    let Some(first) = manifolds.first() else {
        return Vec::new();
    };
    let mut reps = [[0.0; 3]; CLUSTERS];
    reps[0] = first.normal;
    let mut count = 1;
    for manifold in &manifolds[1..] {
        if count == CLUSTERS {
            break;
        }
        let best = reps[..count].iter().fold(-f32::MAX, |best, &normal| {
            maxps(best, dot(manifold.normal, normal))
        });
        if !best.partial_cmp(&FACE_COS).is_some_and(Ordering::is_ge) {
            reps[count] = manifold.normal;
            count += 1;
        }
    }
    let mut assigned = vec![usize::MAX; manifolds.len()];
    for _ in 0..ROUNDS {
        let mut sums = [[0.0; 3]; CLUSTERS];
        let active = reps.map(|normal| {
            let length = dot(normal, normal);
            length != 0.0 && !length.is_nan()
        });
        let mut changed = false;
        for (manifold, selected) in manifolds.iter().zip(&mut assigned) {
            let mut best = -f32::MAX;
            let mut nearest = 0;
            for (index, &normal) in reps[..count].iter().enumerate() {
                if !active[index] {
                    continue;
                }
                let value = dot(manifold.normal, normal);
                if value > best {
                    best = value;
                    nearest = index;
                }
            }
            sums[nearest] = add(manifold.normal, sums[nearest]);
            changed |= *selected != nearest;
            *selected = nearest;
        }
        if !changed {
            break;
        }
        for (rep, sum) in reps[..count].iter_mut().zip(sums) {
            *rep = if dot(sum, sum) > TINY {
                normalize(sum)
            } else {
                [0.0; 3]
            };
        }
    }
    let mut groups: [Vec<ContactPoint>; CLUSTERS] = std::array::from_fn(|_| Vec::new());
    for (manifold, group) in manifolds.iter().zip(assigned) {
        groups[group].extend_from_slice(manifold.points());
    }
    groups
        .into_iter()
        .zip(reps)
        .filter(|(group, _)| !group.is_empty())
        .map(|(group, normal)| reduce_points(normal, &group))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::contact_round::FeatureId;

    const UP: Vec3 = [0.0, 1.0, 0.0];

    fn point(position: Vec3, triangle: u32) -> ContactPoint {
        ContactPoint {
            position,
            separation: -0.1,
            feature: FeatureId {
                triangle,
                ..FeatureId::default()
            },
        }
    }

    fn ids(cluster: &ContactCluster) -> Vec<u32> {
        cluster
            .points()
            .iter()
            .map(|point| point.feature.triangle)
            .collect()
    }

    #[test]
    fn point_reduction_keeps_hysteresis_and_ordered_quad_corners() {
        let points = [
            [0.0; 3],
            [1.0, 0.0, 0.0],
            [1.01, 0.0, 0.0],
            [-1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
            [0.0, 0.0, -1.0],
            [0.5, 0.0, 0.5],
        ]
        .into_iter()
        .enumerate()
        .map(|(id, position)| point(position, id as u32))
        .collect::<Vec<_>>();
        assert_eq!(ids(&reduce_points(UP, &points)), [1, 3, 4, 5]);
        assert_eq!(ids(&reduce_points([0.0; 3], &points)), [6, 3]);
    }

    #[test]
    fn reduction_uses_distance_and_area_thresholds() {
        let a = point([0.0; 3], 0);
        let close = point([0.019, 0.0, 0.0], 1);
        let spread = point([0.021, 0.0, 0.0], 2);
        assert_eq!(ids(&reduce_points(UP, &[a, close])), [1]);
        assert_eq!(ids(&reduce_points(UP, &[a, close, spread])), [2, 0]);
        assert!(reduce_points(UP, &[]).points().is_empty());
    }

    #[test]
    fn clustering_preserves_three_representatives_and_original_point_ids() {
        let manifolds = [
            UP,
            [0.04, 0.9992, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0],
            [0.75, 0.0, 0.66],
        ]
        .into_iter()
        .enumerate()
        .map(|(id, normal)| {
            Manifold::one(
                normal,
                point([(id as f64 / 3.0) as f32, 0.0, 0.0], id as u32),
            )
        })
        .collect::<Vec<_>>();
        let result = cluster_manifolds(&manifolds);
        let [a, b, c] = result.as_slice() else {
            panic!("expected three clusters");
        };
        assert_eq!(a.normal.map(f32::to_bits), [0x3ca3_df6d, 0x3f7f_f2e2, 0]);
        assert_eq!(b.normal.map(f32::to_bits), [0x3f6f_87f6, 0, 0x3eb4_acc5]);
        assert_eq!(c.normal, [0.0, 0.0, 1.0]);
        assert_eq!([ids(a), ids(b), ids(c)], [vec![1, 0], vec![4, 2], vec![3]]);
        assert!(cluster_manifolds(&[Manifold::with_normal(UP)]).is_empty());
        assert!(cluster_manifolds(&[]).is_empty());
    }
}
