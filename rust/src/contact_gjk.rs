//! Float32 closest witnesses of convex point sets for retail build 104319.
//!
//! Ports the GJK distance in `ow174/game/contacts.py`. In particular the
//! simplex reductions preserve the reference feature order and lane rounding.
//! Triangle contact and shape-cast policies are separate from this distance.

use std::cmp::Ordering;
use std::error::Error;
use std::fmt;

use crate::contact_math::{TINY, Vec3, add, cross, dot, maxps, normalize, scale, sub};

#[path = "contact_sweep.rs"]
pub mod sweep;

const ABSENT: u8 = u8::MAX;
const MAX_ITERATIONS: u8 = 20;
const FAR_SLACK: f64 = 1e-3;
const INLINE_POINTS: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SimplexCache {
    pub metric: f32,
    pub index_a: [u8; 4],
    pub index_b: [u8; 4],
}

impl SimplexCache {
    pub const EMPTY: Self = Self {
        metric: 0.0,
        index_a: [ABSENT; 4],
        index_b: [0; 4],
    };

    pub fn len(self) -> usize {
        self.index_a
            .iter()
            .position(|&value| value == ABSENT)
            .unwrap_or(4)
    }

    pub fn is_empty(self) -> bool {
        self.index_a[0] == ABSENT
    }
}

impl Default for SimplexCache {
    fn default() -> Self {
        Self::EMPTY
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum Exit {
    IterationLimit = 0,
    Duplicate = 2,
    TinyDirection = 3,
    Inside = 4,
    NoProgress = 5,
    UnsolvedSimplex = 6,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Witness {
    pub point_a: Vec3,
    pub point_b: Vec3,
    /// Unit direction from A to B of the last non-tiny search.
    pub normal: Vec3,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Distance {
    pub distance: f32,
    /// The reference retains an f64 square root for the far early exit.
    pub lower: f64,
    pub witness: Option<Witness>,
    pub cache: SimplexCache,
    pub iterations: u8,
    pub exit: Exit,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PointSetSize;

impl fmt::Display for PointSetSize {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("GJK needs 1 to 255 points per shape")
    }
}

impl Error for PointSetSize {}

#[derive(Clone, Copy, Default)]
struct Vertex {
    gap: Vec3,
    a: Vec3,
    b: Vec3,
    weight: f32,
    index_a: u8,
    index_b: u8,
}

impl Vertex {
    fn new(a: Vec3, b: Vec3, index_a: u8, index_b: u8) -> Self {
        Self {
            gap: sub(b, a),
            a,
            b,
            weight: 0.0,
            index_a,
            index_b,
        }
    }
}

#[derive(Clone, Copy)]
struct Simplex {
    vertices: [Vertex; 4],
    len: usize,
}

impl Simplex {
    fn from_cache(a: &[Vec3], b: &[Vec3], cache: SimplexCache) -> Self {
        let mut simplex = Self {
            vertices: [Vertex::default(); 4],
            len: 0,
        };
        for k in 0..cache.len() {
            let (ia, ib) = (cache.index_a[k], cache.index_b[k]);
            let (Some(&a), Some(&b)) = (a.get(usize::from(ia)), b.get(usize::from(ib))) else {
                simplex.len = 0;
                break;
            };
            simplex.vertices[k] = Vertex::new(a, b, ia, ib);
            simplex.len += 1;
        }
        let keep = match simplex.len {
            0 => false,
            1 => true,
            _ => {
                let metric = simplex.metric();
                metric >= cache.metric * 0.25
                    && cache.metric * 4.0 >= metric
                    && metric >= f32::EPSILON
            }
        };
        if !keep {
            simplex.vertices[0] = Vertex::new(a[0], b[0], 0, 0);
            simplex.len = 1;
        }
        simplex
    }

    fn cache(self) -> SimplexCache {
        let mut cache = SimplexCache {
            metric: self.metric(),
            ..SimplexCache::EMPTY
        };
        for k in 0..self.len {
            cache.index_a[k] = self.vertices[k].index_a;
            cache.index_b[k] = self.vertices[k].index_b;
        }
        cache
    }

    fn metric(self) -> f32 {
        let [a, b, c, d] = self.vertices.map(|vertex| vertex.gap);
        match self.len {
            2 => {
                let e = sub(b, a);
                dot(e, e)
            }
            3 => {
                let normal = cross(sub(c, a), sub(b, a));
                dot(normal, normal)
            }
            4 => dot(cross(sub(c, a), sub(b, a)), sub(d, a)),
            _ => 0.0,
        }
    }

    fn weighted(&self, lane: impl Fn(Vertex) -> Vec3) -> Vec3 {
        let first = self.vertices[0];
        if self.len == 1 {
            return lane(first);
        }
        let mut point = scale(lane(first), first.weight);
        for &vertex in &self.vertices[1..self.len] {
            point = add(point, scale(lane(vertex), vertex.weight));
        }
        point
    }

    fn closest(&self) -> Vec3 {
        self.weighted(|vertex| vertex.gap)
    }

    fn set<const N: usize>(&mut self, indices: [usize; N], weights: [f32; N]) {
        let old = self.vertices;
        for k in 0..N {
            self.vertices[k] = Vertex {
                weight: weights[k],
                ..old[indices[k]]
            };
        }
        self.len = N;
    }

    fn vertex_region(&mut self, index: usize) -> Vec3 {
        self.set([index], [1.0]);
        neg(self.vertices[0].gap)
    }

    fn search(&mut self) -> Option<Vec3> {
        match self.len {
            1 => Some(neg(self.vertices[0].gap)),
            2 => self.solve_segment(),
            3 => self.solve_triangle(),
            4 => self.solve_tetrahedron(),
            _ => unreachable!("a GJK simplex has one to four points"),
        }
    }

    fn solve_segment(&mut self) -> Option<Vec3> {
        let [a, b, _, _] = self.vertices.map(|vertex| vertex.gap);
        let ab = sub(b, a);
        let weight_b = -dot(ab, a);
        if weight_b <= 0.0 {
            return Some(self.vertex_region(0));
        }
        let weight_a = dot(ab, b);
        if weight_a <= 0.0 {
            return Some(self.vertex_region(1));
        }
        let total = weight_a + weight_b;
        if total <= 0.0 {
            return None;
        }
        let r = 1.0 / total;
        let inverse = if maxps(-total, total) > TINY {
            (2.0 - r * total) * r
        } else {
            0.0
        };
        self.set([0, 1], [inverse * weight_a, inverse * weight_b]);
        Some(edge_direction(a, b, ab))
    }

    fn solve_triangle(&mut self) -> Option<Vec3> {
        let [a, b, c, _] = self.vertices.map(|vertex| vertex.gap);
        let ab = Edge::new(a, b);
        let ac = Edge::new(a, c);
        let bc = Edge::new(b, c);
        let normal = cross(ab.direction, ac.direction);
        let face = FaceWeights::new(a, b, c, normal);
        if ab.end <= 0.0 && ac.end <= 0.0 {
            return Some(self.vertex_region(0));
        }
        if ab.start <= 0.0 && bc.end <= 0.0 {
            return Some(self.vertex_region(1));
        }
        if ac.start <= 0.0 && bc.start <= 0.0 {
            return Some(self.vertex_region(2));
        }
        for (edge, opposite, indices, endpoints) in [
            (ab, face.c, [0, 1], [a, b]),
            (ac, face.b, [0, 2], [a, c]),
            (bc, face.a, [2, 1], [b, c]),
        ] {
            if edge.interior() && opposite <= 0.0 {
                let r = reciprocal3(edge.end + edge.start);
                let weights = [r * edge.start, r * edge.end];
                let ordered = if indices == [2, 1] {
                    [weights[1], weights[0]]
                } else {
                    weights
                };
                self.set(indices, ordered);
                return Some(edge_direction(endpoints[0], endpoints[1], edge.direction));
            }
        }
        let total = (face.b + face.a) + face.c;
        if total <= 0.0 {
            return None;
        }
        let r = reciprocal3(total);
        self.set([0, 1, 2], [r * face.a, r * face.b, r * face.c]);
        Some(toward_origin(normal, add(add(b, a), c)))
    }

    fn solve_tetrahedron(&mut self) -> Option<Vec3> {
        let [a, b, c, d] = self.vertices.map(|vertex| vertex.gap);
        if dot(cross(sub(b, a), sub(c, a)), sub(a, d)) < 0.0 {
            self.vertices.swap(1, 2);
        }
        let [a, b, c, d] = self.vertices.map(|vertex| vertex.gap);
        let ab = Edge::new(a, b);
        let ac = Edge::new(a, c);
        let ad = Edge::new(a, d);
        let bc = Edge::new(b, c);
        let bd = Edge::new(b, d);
        let cd = Edge::new(c, d);

        let n_bdc = cross(bd.direction, bc.direction);
        let n_acd = cross(ac.direction, ad.direction);
        let n_adb = cross(ad.direction, ab.direction);
        let n_abc = cross(ab.direction, ac.direction);
        let bdc = FaceWeights::new(b, d, c, n_bdc);
        let acd = FaceWeights::new(a, c, d, n_acd);
        let adb = FaceWeights::new(a, d, b, n_adb);
        let abc = FaceWeights::new(a, b, c, n_abc);
        let sum_bcd = add(add(c, b), d);
        let sum_acd = add(add(c, a), d);
        let sum_adb = add(add(d, a), b);
        let sum_abc = add(add(b, a), c);
        let side_bcd = dot(sum_bcd, n_bdc);
        let side_acd = dot(sum_acd, n_acd);
        let side_adb = dot(sum_adb, n_adb);
        let side_abc = dot(sum_abc, n_abc);

        if ab.end <= 0.0 && ac.end <= 0.0 && ad.end <= 0.0 {
            return Some(self.vertex_region(0));
        }
        if ab.start <= 0.0 && bc.end <= 0.0 && bd.end <= 0.0 {
            return Some(self.vertex_region(1));
        }
        if ac.start <= 0.0 && bc.start <= 0.0 && cd.end <= 0.0 {
            return Some(self.vertex_region(2));
        }
        if ad.start <= 0.0 && bd.start <= 0.0 && cd.start <= 0.0 {
            return Some(self.vertex_region(3));
        }
        for (edge, opposing, indices, endpoints, reversed) in [
            (ab, [abc.c, adb.b], [0, 1], [a, b], false),
            (ac, [abc.b, acd.c], [0, 2], [a, c], false),
            (ad, [acd.b, adb.c], [0, 3], [a, d], false),
            (bc, [abc.a, bdc.b], [2, 1], [b, c], true),
            (bd, [adb.a, bdc.c], [3, 1], [b, d], true),
            (cd, [acd.a, bdc.a], [2, 3], [c, d], false),
        ] {
            if edge.interior() && opposing[0] <= 0.0 && opposing[1] <= 0.0 {
                let r = 1.0 / (edge.end + edge.start);
                let weights = [r * edge.start, r * edge.end];
                let ordered = if reversed {
                    [weights[1], weights[0]]
                } else {
                    weights
                };
                self.set(indices, ordered);
                return Some(edge_direction(endpoints[0], endpoints[1], edge.direction));
            }
        }
        if side_bcd <= 0.0 && bdc.interior() {
            let r = 1.0 / ((bdc.b + bdc.a) + bdc.c);
            self.set([3, 1, 2], [r * bdc.b, r * bdc.a, r * bdc.c]);
            let normal = cross(bc.direction, cd.direction);
            return Some(toward_origin(normal, sum_bcd));
        }
        if side_acd <= 0.0 && acd.interior() {
            let r = 1.0 / ((acd.b + acd.a) + acd.c);
            self.set([0, 3, 2], [r * acd.a, r * acd.c, r * acd.b]);
            let normal = cross(ac.direction, cd.direction);
            return Some(toward_origin(normal, sum_acd));
        }
        if side_adb <= 0.0 && adb.interior() {
            let r = 1.0 / ((adb.b + adb.a) + adb.c);
            self.set([0, 1, 3], [r * adb.a, r * adb.c, r * adb.b]);
            let normal = cross(ab.direction, ad.direction);
            return Some(toward_origin(normal, add(add(b, a), d)));
        }
        if side_abc <= 0.0 && abc.interior() {
            let r = 1.0 / ((abc.b + abc.a) + abc.c);
            self.set([0, 1, 2], [r * abc.a, r * abc.b, r * abc.c]);
            return Some(toward_origin(n_abc, sum_abc));
        }
        let total = ((side_acd + side_bcd) + side_adb) + side_abc;
        if total <= 0.0 {
            return None;
        }
        let r = 1.0 / total;
        self.set(
            [0, 1, 2, 3],
            [r * side_bcd, r * side_acd, r * side_adb, r * side_abc],
        );
        Some([0.0; 3])
    }
}

fn neg(value: Vec3) -> Vec3 {
    value.map(|lane| -lane)
}

fn reciprocal3(total: f32) -> f32 {
    let r = 1.0 / total;
    (r + r) - (r * r) * total
}

fn edge_direction(a: Vec3, b: Vec3, edge: Vec3) -> Vec3 {
    cross(cross(add(b, a), edge), edge)
}

fn toward_origin(normal: Vec3, summed_points: Vec3) -> Vec3 {
    if -dot(summed_points, normal) > 0.0 {
        normal
    } else {
        neg(normal)
    }
}

#[derive(Clone, Copy)]
struct Edge {
    direction: Vec3,
    start: f32,
    end: f32,
}

impl Edge {
    fn new(a: Vec3, b: Vec3) -> Self {
        let direction = sub(b, a);
        Self {
            direction,
            start: dot(direction, b),
            end: -dot(direction, a),
        }
    }

    fn interior(self) -> bool {
        self.start > 0.0 && self.end > 0.0
    }
}

#[derive(Clone, Copy)]
struct FaceWeights {
    a: f32,
    b: f32,
    c: f32,
}

impl FaceWeights {
    fn new(a: Vec3, b: Vec3, c: Vec3, normal: Vec3) -> Self {
        Self {
            a: dot(cross(b, c), normal),
            b: dot(cross(c, a), normal),
            c: dot(cross(a, b), normal),
        }
    }

    fn interior(self) -> bool {
        self.a > 0.0 && self.b > 0.0 && self.c > 0.0
    }
}

enum Points {
    Inline {
        points: [Vec3; INLINE_POINTS],
        len: usize,
    },
    Heap(Vec<Vec3>),
}

impl Points {
    fn mapped(input: &[Vec3], mut transform: impl FnMut(Vec3) -> Vec3) -> Self {
        if input.len() <= INLINE_POINTS {
            let mut points = [[0.0; 3]; INLINE_POINTS];
            for (out, &point) in points.iter_mut().zip(input) {
                *out = transform(point);
            }
            Self::Inline {
                points,
                len: input.len(),
            }
        } else {
            Self::Heap(input.iter().copied().map(transform).collect())
        }
    }

    fn as_slice(&self) -> &[Vec3] {
        match self {
            Self::Inline { points, len } => &points[..*len],
            Self::Heap(points) => points,
        }
    }
}

fn identity([x, y, z]: Vec3) -> Vec3 {
    // Retain the identity transform's signed-zero behavior.
    if x.is_finite() && y.is_finite() && z.is_finite() {
        [x + 0.0, y + 0.0, z + 0.0]
    } else {
        [
            ((x * 1.0 + y * 0.0) + z * 0.0) + 0.0,
            ((x * 0.0 + y * 1.0) + z * 0.0) + 0.0,
            ((x * 0.0 + y * 0.0) + z * 1.0) + 0.0,
        ]
    }
}

fn support(spreads: &[Vec3], direction: Vec3) -> u8 {
    let mut best = 0.0;
    let mut index = 0;
    for (k, &spread) in spreads.iter().enumerate() {
        let value = dot(spread, direction);
        if best < value {
            best = value;
            index = (k + 1) as u8;
        }
    }
    index
}

/// Closest witnesses of convex hulls expressed in a common frame. A stale
/// out-of-range cache is reset. When a reach is given, the reference's far
/// early exit can return only its lower estimate and updated cache.
pub fn gjk(
    points_a: &[Vec3],
    points_b: &[Vec3],
    cache: SimplexCache,
    reach: Option<f64>,
) -> Result<Distance, PointSetSize> {
    if points_a.is_empty()
        || points_b.is_empty()
        || points_a.len() > usize::from(ABSENT)
        || points_b.len() > usize::from(ABSENT)
    {
        return Err(PointSetSize);
    }
    let origin = identity(points_b[0]);
    let centered_a = Points::mapped(points_a, |point| sub(identity(point), origin));
    let mut first = true;
    let centered_b = Points::mapped(points_b, |point| {
        if std::mem::take(&mut first) {
            [0.0; 3]
        } else {
            sub(identity(point), origin)
        }
    });
    let (a, b) = (centered_a.as_slice(), centered_b.as_slice());
    let spread_a = Points::mapped(&a[1..], |point| sub(point, a[0]));

    let mut simplex = Simplex::from_cache(a, b, cache);
    let mut backup = simplex;
    let mut previous = f32::MAX;
    let mut iterations = 0;
    let mut normal = [0.0; 3];
    let exit = loop {
        let saved = simplex;
        let Some(direction) = simplex.search() else {
            simplex = backup;
            break Exit::UnsolvedSimplex;
        };
        let point = simplex.closest();
        let squared = dot(point, point);
        if previous <= squared {
            simplex = backup;
            break Exit::NoProgress;
        }
        if simplex.len == 4 {
            normal = [0.0; 3];
            break Exit::Inside;
        }
        if dot(direction, direction).partial_cmp(&TINY) != Some(Ordering::Greater) {
            simplex = backup;
            break Exit::TinyDirection;
        }

        normal = neg(direction);
        let ia = support(spread_a.as_slice(), normal);
        let ib = support(&b[1..], direction);
        simplex.vertices[simplex.len] = Vertex::new(a[usize::from(ia)], b[usize::from(ib)], ia, ib);
        iterations += 1;
        if saved.vertices[..saved.len]
            .iter()
            .any(|vertex| vertex.index_a == ia && vertex.index_b == ib)
        {
            break Exit::Duplicate;
        }
        if iterations == MAX_ITERATIONS {
            break Exit::IterationLimit;
        }
        backup = simplex;
        simplex.len += 1;
        previous = squared;
    };

    let cache = simplex.cache();
    if let Some(reach) = reach {
        let closest = simplex.closest();
        let squared = f64::from(dot(closest, closest));
        let limit = reach + FAR_SLACK;
        if squared > limit * limit {
            return Ok(Distance {
                distance: f32::INFINITY,
                lower: squared.sqrt(),
                witness: None,
                cache,
                iterations,
                exit,
            });
        }
    }
    let point_a = simplex.weighted(|vertex| vertex.a);
    let point_b = simplex.weighted(|vertex| vertex.b);
    let gap = sub(point_b, point_a);
    let distance = dot(gap, gap).sqrt();
    Ok(Distance {
        distance,
        lower: f64::from(distance),
        witness: Some(Witness {
            point_a: add(point_a, origin),
            point_b: add(point_b, origin),
            normal: if dot(normal, normal) > TINY {
                normalize(normal)
            } else {
                [0.0; 3]
            },
        }),
        cache,
        iterations,
        exit,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const FLOOR: [Vec3; 3] = [[0.0; 3], [4.0, 0.0, 0.0], [0.0, 0.0, 4.0]];

    #[test]
    fn triangle_segment_distance_preserves_witnesses_and_feature_order_on_warm_start() {
        let capsule = [[1.0, 0.3, 1.0], [1.0, 1.5, 1.0]];
        let cold = gjk(&FLOOR, &capsule, SimplexCache::EMPTY, None).unwrap();
        assert_eq!(cold.distance.to_bits(), 0x3e99_999a);
        assert_eq!(cold.lower.to_bits(), 0x3fd3_3333_4000_0000);
        assert_eq!(cold.cache.index_a, [0, 1, 2, ABSENT]);
        assert_eq!(cold.cache.index_b, [0; 4]);
        assert_eq!(cold.cache.metric, 256.0);
        assert_eq!((cold.exit, cold.iterations), (Exit::Duplicate, 3));
        assert_eq!(
            cold.witness,
            Some(Witness {
                point_a: [1.0, 0.0, 1.0],
                point_b: [1.0, 0.3, 1.0],
                normal: [0.0, 1.0, 0.0],
            })
        );

        let warm = gjk(&FLOOR, &capsule, cold.cache, None).unwrap();
        assert_eq!(
            warm,
            Distance {
                iterations: 1,
                ..cold
            }
        );
        let far = gjk(&FLOOR, &capsule, cold.cache, Some(0.1)).unwrap();
        assert_eq!(far.distance, f32::INFINITY);
        assert_eq!(far.lower.to_bits(), 0x3fd3_3333_3999_9999);
        assert!(far.witness.is_none());
        assert_eq!(
            (far.cache, far.exit, far.iterations),
            (warm.cache, warm.exit, warm.iterations)
        );
    }

    #[test]
    fn intersecting_triangle_and_capsule_retains_tetrahedron_order_and_rounding() {
        let capsule = [[1.0, -0.3, 1.0], [1.0, 1.5, 1.0]];
        let hit = gjk(&FLOOR, &capsule, SimplexCache::EMPTY, None).unwrap();
        assert_eq!((hit.exit, hit.iterations), (Exit::Inside, 3));
        assert_eq!(hit.cache.index_a, [0, 2, 1, 1]);
        assert_eq!(hit.cache.index_b, [0, 0, 1, 0]);
        assert_eq!(hit.cache.metric.to_bits(), 0x41e6_6666);
        assert_eq!(hit.distance.to_bits(), 0x3400_ff02);
        let witness = hit.witness.unwrap();
        assert_eq!(
            witness.point_a.map(f32::to_bits),
            [0x3f80_0000, 0, 0x3f7f_fffe]
        );
        assert_eq!(witness.point_b, [1.0, 0.0, 1.0]);
        assert_eq!(witness.normal, [0.0; 3]);
    }

    #[test]
    fn point_pairs_keep_sse_normal_rounding_and_flush_stale_indices() {
        let a = [[0.0; 3]];
        let b = [[3.0, 4.0, 0.0]];
        let cold = gjk(&a, &b, SimplexCache::EMPTY, None).unwrap();
        assert_eq!(
            (cold.distance, cold.exit, cold.iterations),
            (5.0, Exit::Duplicate, 1)
        );
        let witness = cold.witness.unwrap();
        assert_eq!((witness.point_a, witness.point_b), (a[0], b[0]));
        assert_eq!(
            witness.normal.map(f32::to_bits),
            [0x3f19_9999, 0x3f4c_cccc, 0]
        );
        let stale = SimplexCache {
            index_a: [3, ABSENT, ABSENT, ABSENT],
            ..SimplexCache::EMPTY
        };
        assert_eq!(gjk(&a, &b, stale, None), Ok(cold));

        let zero = gjk(
            &[[-0.0, 0.0, -0.0]],
            &[[0.0, -0.0, 0.0]],
            SimplexCache::EMPTY,
            None,
        )
        .unwrap();
        assert_eq!((zero.exit, zero.iterations), (Exit::TinyDirection, 0));
        assert_eq!(zero.witness.unwrap().point_a.map(f32::to_bits), [0; 3]);
        assert_eq!(gjk(&[], &b, SimplexCache::EMPTY, None), Err(PointSetSize));
        assert_eq!(
            gjk(&a, &[[0.0; 3]; 256], SimplexCache::EMPTY, None),
            Err(PointSetSize)
        );
    }
}
