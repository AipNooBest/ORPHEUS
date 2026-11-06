//! Per-proxy contacts for the retail character capsule.
//!
//! Ports the mesh candidate boxes, simplex caches, settled-body memos and
//! capsule casts from `ow174/game/contacts.py`. Geometry is immutable and
//! shared across bodies. Each body owns its candidate and pair state.

use std::sync::Arc;

use crate::contact_gjk::SimplexCache;
use crate::contact_gjk::sweep::{CAST_TOLERANCE, CastFrame, CastOptions, CastShape, shape_cast};
use crate::contact_hull::{HullContactError, capsule_hull, placed_capsule_hull};
use crate::contact_manifolds::{ContactCluster, cluster_manifolds};
use crate::contact_math::{ContactFrame, Vec3, add, maxps, minps, rotate, scale, sub, unrotate};
use crate::contact_mesh::{ContactBounds, ContactMesh};
use crate::contact_round::{Capsule, ContactPoint, Manifold, RoundObstacle, RoundShape};
use crate::contact_triangle::{SKIN, capsule_triangle};
use crate::contact_world::{
    PROXY_MARGIN, StaticGeometry, StaticShape, absolute_columns, oriented_box, overlaps,
};

pub(crate) const LOOSE: f32 = PROXY_MARGIN * 4.0;
const CAST_SAFE: f64 = 0.01;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CapsulePose {
    /// The lowest point of the upright capsule.
    pub position: Vec3,
    pub radius: f32,
    /// Height of the upper segment endpoint above `position`.
    pub top: f32,
}

impl CapsulePose {
    pub fn local_capsule(self) -> Capsule {
        Capsule {
            a: [0.0, self.radius, 0.0],
            b: [0.0, self.top, 0.0],
            radius: self.radius,
        }
    }

    pub fn world_capsule(self) -> Capsule {
        let [x, y, z] = self.position;
        Capsule {
            a: [x, self.radius + y, z],
            b: [x, self.top + y, z],
            radius: self.radius,
        }
    }

    pub fn bounds(self) -> ContactBounds {
        let Capsule { a, b, radius } = self.world_capsule();
        ContactBounds {
            low: std::array::from_fn(|k| minps(a[k], b[k]) - radius),
            high: std::array::from_fn(|k| maxps(a[k], b[k]) + radius),
        }
    }

    pub fn swept_bounds(self, end: Vec3) -> ContactBounds {
        let Capsule { a, b, radius } = self.world_capsule();
        let next = Self {
            position: end,
            ..self
        }
        .world_capsule();
        ContactBounds {
            low: std::array::from_fn(|k| {
                minps(minps(a[k], b[k]), minps(next.a[k], next.b[k])) - radius
            }),
            high: std::array::from_fn(|k| {
                maxps(maxps(a[k], b[k]), maxps(next.a[k], next.b[k])) + radius
            }),
        }
    }

    fn frame(self) -> ContactFrame {
        ContactFrame {
            translation: self.position,
            ..ContactFrame::IDENTITY
        }
    }
}

pub enum ProxyManifolds {
    Mesh(Vec<ContactCluster>),
    Convex(Option<Manifold>),
}

impl ProxyManifolds {
    pub fn iter(&self) -> impl Iterator<Item = (Vec3, &[ContactPoint])> {
        let (mesh, convex) = match self {
            Self::Mesh(groups) => (groups.as_slice(), None),
            Self::Convex(manifold) => (&[][..], manifold.as_ref()),
        };
        mesh.iter()
            .map(|group| (group.normal, group.points()))
            .chain(convex.map(|manifold| (manifold.normal, manifold.points())))
    }
}

#[derive(Clone, Copy)]
struct Memo {
    pose: CapsulePose,
    cache_in: SimplexCache,
    manifold: Manifold,
}

impl Memo {
    fn matches(self, pose: CapsulePose, cache: SimplexCache) -> bool {
        self.pose == pose && self.cache_in == cache
    }

    fn beyond_reach(self, pose: CapsulePose, far: f64) -> bool {
        self.pose == pose && self.manifold.gap.is_some_and(|gap| gap > far)
    }
}

fn far_distance(radius: f32, translation: Vec3) -> f64 {
    let [x, y, z] = translation.map(f64::from);
    f64::from(radius) + f64::from(CAST_TOLERANCE) + CAST_SAFE + (x * x + y * y + z * z).sqrt()
}

#[derive(Clone, Copy)]
struct PlacedFrame {
    frame: ContactFrame,
    matrix: CastFrame,
}

impl From<ContactFrame> for PlacedFrame {
    fn from(frame: ContactFrame) -> Self {
        Self {
            frame,
            matrix: frame.into(),
        }
    }
}

#[derive(Clone, Copy)]
struct LocalBox {
    inverse: ContactFrame,
    absolute: [Vec3; 3],
}

impl From<ContactFrame> for LocalBox {
    fn from(frame: ContactFrame) -> Self {
        let [x, y, z, w] = frame.rotation;
        let rotation = [-x, -y, -z, w];
        let inverse = ContactFrame {
            translation: rotate(rotation, frame.translation).map(|lane| 0.0 - lane),
            rotation,
        };
        Self {
            inverse,
            absolute: absolute_columns(inverse.into()),
        }
    }
}

struct Candidate {
    number: u32,
    cache: SimplexCache,
    memo: Option<Memo>,
}

pub struct MeshContact {
    mesh: Arc<ContactMesh>,
    category: u16,
    placement: Option<PlacedFrame>,
    inverse_box: Option<LocalBox>,
    gathered: Option<ContactBounds>,
    candidates: Vec<Candidate>,
}

impl MeshContact {
    fn new(mesh: Arc<ContactMesh>, category: u16, frame: Option<ContactFrame>) -> Self {
        Self {
            mesh,
            category,
            placement: frame.map(Into::into),
            inverse_box: frame.map(Into::into),
            gathered: None,
            candidates: Vec::new(),
        }
    }

    pub fn gathered_bounds(&self) -> Option<ContactBounds> {
        self.gathered
    }

    pub fn candidates(&self) -> impl Iterator<Item = (u32, SimplexCache)> + '_ {
        self.candidates
            .iter()
            .map(|entry| (entry.number, entry.cache))
    }

    fn local_box(&self, bounds: ContactBounds) -> ContactBounds {
        if let Some(local) = self.inverse_box {
            return oriented_box(bounds, local.inverse, local.absolute);
        }
        let center = scale(add(bounds.low, bounds.high), 0.5);
        let half = scale(sub(bounds.high, bounds.low), 0.5);
        ContactBounds {
            low: sub(center, half),
            high: add(half, center),
        }
    }

    fn gather(&mut self, bounds: ContactBounds) {
        let local = self.local_box(bounds);
        if let Some(old) = self.gathered {
            let outer = ContactBounds {
                low: sub(local.low, [LOOSE; 3]),
                high: add(local.high, [LOOSE; 3]),
            };
            if encloses(old, local) && encloses(outer, old) {
                return;
            }
        }
        let half = add(scale(sub(local.high, local.low), 0.5), [PROXY_MARGIN; 3]);
        let center = scale(add(local.high, local.low), 0.5);
        let next = ContactBounds {
            low: sub(center, half),
            high: add(half, center),
        };
        let mut old = std::mem::take(&mut self.candidates).into_iter().peekable();
        self.candidates = self
            .mesh
            .query(next.low, next.high, self.category)
            .into_iter()
            .map(|number| {
                while old.peek().is_some_and(|entry| entry.number < number) {
                    old.next();
                }
                let cache = if old.peek().is_some_and(|entry| entry.number == number) {
                    old.next().expect("matching entry").cache
                } else {
                    SimplexCache::EMPTY
                };
                Candidate {
                    number,
                    cache,
                    memo: None,
                }
            })
            .collect();
        self.gathered = Some(next);
    }

    fn manifolds(&mut self, pose: CapsulePose) -> Vec<ContactCluster> {
        self.gather(pose.bounds());
        let mut capsule = pose.world_capsule();
        if let Some(placement) = self.placement {
            capsule.a = placement.frame.local(capsule.a);
            capsule.b = placement.frame.local(capsule.b);
        }
        let mut found = Vec::new();
        for entry in &mut self.candidates {
            let manifold =
                if let Some(memo) = entry.memo.filter(|memo| memo.matches(pose, entry.cache)) {
                    memo.manifold
                } else {
                    let triangle = self
                        .mesh
                        .triangle(entry.number)
                        .expect("gather returned a triangle");
                    let cache_in = entry.cache;
                    let (manifold, cache) = capsule_triangle(triangle, capsule, cache_in);
                    entry.cache = cache;
                    entry.memo = Some(Memo {
                        pose,
                        cache_in,
                        manifold,
                    });
                    manifold
                };
            if !manifold.points().is_empty() {
                found.push(manifold);
            }
        }
        let mut groups = cluster_manifolds(&found);
        if let Some(placement) = self.placement {
            for group in &mut groups {
                group.normal = rotate(placement.frame.rotation, group.normal);
            }
        }
        groups
    }

    fn cast(&mut self, pose: CapsulePose, swept: ContactBounds, translation: Vec3) -> Option<f32> {
        self.gather(swept);
        let local = self.local_box(swept);
        let (moved, segment, frame_a, frame_b) = self.placement.map_or_else(
            || (translation, pose.world_capsule(), None, None),
            |placement| {
                (
                    unrotate(placement.frame.rotation, translation),
                    pose.local_capsule(),
                    Some(placement.matrix),
                    Some(pose.frame().into()),
                )
            },
        );
        let low: Vec3 = std::array::from_fn(|k| minps(local.low[k], local.low[k] + moved[k]));
        let high: Vec3 = std::array::from_fn(|k| maxps(local.high[k], local.high[k] + moved[k]));
        let (low, high) = (low.map(f64::from), high.map(f64::from));
        let far = far_distance(pose.radius, translation);
        let points = [segment.a, segment.b];
        let b = CastShape {
            points: &points,
            radius: pose.radius,
            frame: frame_b,
        };
        let mut max_fraction = 1.0;
        let mut nearest = None;
        for candidate in &self.candidates {
            let triangle = self
                .mesh
                .triangle(candidate.number)
                .expect("gather returned a triangle");
            let [bottom, top] = triangle.contact_bounds();
            if !overlaps(
                low,
                high,
                ContactBounds {
                    low: bottom,
                    high: top,
                },
            ) || candidate
                .memo
                .is_some_and(|memo| memo.beyond_reach(pose, far))
            {
                continue;
            }
            let a = CastShape {
                points: triangle.corners(),
                radius: SKIN,
                frame: frame_a,
            };
            let options = CastOptions {
                translation,
                max_fraction,
                hit_touching: false,
                shrink: true,
            };
            let hit =
                shape_cast(a, b, options).expect("triangle and capsule point counts are fixed");
            if let Some(hit) = hit.filter(|hit| hit.fraction < max_fraction) {
                max_fraction = hit.fraction;
                nearest = Some(hit.fraction);
            }
        }
        nearest
    }
}

pub(crate) fn encloses(outer: ContactBounds, inner: ContactBounds) -> bool {
    (0..3).all(|k| outer.low[k] <= inner.low[k] && inner.high[k] <= outer.high[k])
}

pub struct ConvexContact {
    geometry: StaticGeometry,
    placement: Option<PlacedFrame>,
    cache: SimplexCache,
    memo: Option<Memo>,
}

impl ConvexContact {
    pub fn simplex_cache(&self) -> SimplexCache {
        self.cache
    }

    fn manifolds(&mut self, pose: CapsulePose) -> Result<Option<Manifold>, HullContactError> {
        let manifold = if let Some(memo) = self.memo.filter(|memo| memo.matches(pose, self.cache)) {
            memo.manifold
        } else {
            let cache_in = self.cache;
            let (manifold, cache) = match &self.geometry {
                StaticGeometry::Hull(hull) => {
                    if let Some(placement) = self.placement {
                        placed_capsule_hull(
                            hull,
                            placement.frame,
                            pose.local_capsule(),
                            pose.frame(),
                            cache_in,
                        )?
                    } else {
                        capsule_hull(hull, pose.world_capsule(), cache_in)?
                    }
                }
                StaticGeometry::Round { shape, mask } => {
                    let obstacle = RoundObstacle {
                        shape: *shape,
                        frame: self
                            .placement
                            .map_or(ContactFrame::IDENTITY, |placement| placement.frame),
                        mask: *mask,
                    };
                    (
                        obstacle.manifold(pose.local_capsule(), pose.frame()),
                        cache_in,
                    )
                }
                StaticGeometry::Mesh(_) => unreachable!("mesh state belongs to MeshContact"),
            };
            self.cache = cache;
            self.memo = Some(Memo {
                pose,
                cache_in,
                manifold,
            });
            manifold
        };
        Ok((!manifold.points().is_empty()).then_some(manifold))
    }

    fn cast(&self, pose: CapsulePose, translation: Vec3) -> Result<Option<f32>, HullContactError> {
        if matches!(self.geometry, StaticGeometry::Hull(_))
            && self
                .memo
                .is_some_and(|memo| memo.beyond_reach(pose, far_distance(pose.radius, translation)))
        {
            return Ok(None);
        }
        let (capsule, static_frame, body_frame) = self.placement.map_or_else(
            || (pose.world_capsule(), None, None),
            |placement| {
                (
                    pose.local_capsule(),
                    Some(placement.matrix),
                    Some(pose.frame().into()),
                )
            },
        );
        let endpoints = [capsule.a, capsule.b];
        let body = CastShape {
            points: &endpoints,
            radius: pose.radius,
            frame: body_frame,
        };
        let options = CastOptions {
            translation,
            max_fraction: 1.0,
            hit_touching: false,
            shrink: true,
        };
        let hit = match &self.geometry {
            StaticGeometry::Hull(hull) => shape_cast(
                CastShape {
                    points: &hull.vertices,
                    radius: SKIN,
                    frame: static_frame,
                },
                body,
                options,
            )?,
            StaticGeometry::Round { shape, .. } => {
                let (points, len, radius) = match shape {
                    RoundShape::Sphere(sphere) => ([sphere.center, [0.0; 3]], 1, sphere.radius),
                    RoundShape::Capsule(capsule) => ([capsule.a, capsule.b], 2, capsule.radius),
                };
                shape_cast(
                    body,
                    CastShape {
                        points: &points[..len],
                        radius,
                        frame: static_frame,
                    },
                    CastOptions {
                        translation: translation.map(|lane| -lane),
                        ..options
                    },
                )?
            }
            StaticGeometry::Mesh(_) => unreachable!("mesh state belongs to MeshContact"),
        };
        Ok(hit.map(|hit| hit.fraction))
    }
}

pub enum ProxyContact {
    Mesh(MeshContact),
    Convex(ConvexContact),
}

impl ProxyContact {
    pub fn new(shape: &StaticShape, category: u16) -> Self {
        let frame = shape.frame();
        match shape.geometry() {
            StaticGeometry::Mesh(mesh) => {
                Self::Mesh(MeshContact::new(Arc::clone(mesh), category, frame))
            }
            geometry => Self::Convex(ConvexContact {
                geometry: geometry.clone(),
                placement: frame.map(Into::into),
                cache: SimplexCache::EMPTY,
                memo: None,
            }),
        }
    }

    pub fn normal_sign(&self) -> f32 {
        match self {
            Self::Mesh(_) => 1.0,
            Self::Convex(contact) if matches!(contact.geometry, StaticGeometry::Hull(_)) => 1.0,
            Self::Convex(_) => -1.0,
        }
    }

    pub fn manifolds(&mut self, pose: CapsulePose) -> Result<ProxyManifolds, HullContactError> {
        match self {
            Self::Mesh(contact) => Ok(ProxyManifolds::Mesh(contact.manifolds(pose))),
            Self::Convex(contact) => Ok(ProxyManifolds::Convex(contact.manifolds(pose)?)),
        }
    }

    pub fn cast(
        &mut self,
        pose: CapsulePose,
        swept: ContactBounds,
        translation: Vec3,
    ) -> Result<Option<f32>, HullContactError> {
        match self {
            Self::Mesh(contact) => Ok(contact.cast(pose, swept, translation)),
            Self::Convex(contact) => contact.cast(pose, translation),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collision_cache::{Mesh, Triangle};
    use crate::contact_round::{FeatureId, Sphere};

    fn pose(position: Vec3) -> CapsulePose {
        CapsulePose {
            position,
            radius: 0.5,
            top: 1.5,
        }
    }

    fn triangle(corners: [i32; 3], wings: [i32; 3]) -> Triangle {
        Triangle {
            corners,
            wings,
            mask: 1,
            surface: 0,
        }
    }

    #[test]
    fn gather_keeps_sorted_intersection_cache_and_retires_old_candidates() {
        let vertices = [0.0, 0.5, 1.02]
            .into_iter()
            .flat_map(|x| [[x, 0.0, -0.15], [x, 0.0, 0.15], [x + 0.16, 0.0, 0.0]])
            .collect();
        let triangles = [0, 3, 6]
            .map(|i| triangle([i, i + 1, i + 2], [-1; 3]))
            .into();
        let mesh = Arc::new(
            ContactMesh::new(
                Mesh {
                    vertices,
                    triangles,
                },
                [1.0; 3],
                1,
            )
            .unwrap(),
        );
        let mut contact = MeshContact::new(mesh, 1, None);
        let first = ContactBounds {
            low: [0.0, -0.1, -0.25],
            high: [0.8, 0.1, 0.25],
        };
        contact.gather(first);
        assert_eq!(
            contact.candidates().map(|(id, _)| id).collect::<Vec<_>>(),
            [0, 1]
        );
        let warm = SimplexCache {
            metric: 2.0,
            index_a: [0, 1, 255, 255],
            index_b: [0; 4],
        };
        contact.candidates[1].cache = warm;
        contact.candidates[1].memo = Some(Memo {
            pose: pose([0.0; 3]),
            cache_in: warm,
            manifold: Manifold::default(),
        });
        contact.gather(first);
        assert!(contact.candidates[1].memo.is_some());
        contact.gather(ContactBounds {
            low: [0.45, -0.1, -0.25],
            high: [1.4, 0.1, 0.25],
        });
        assert_eq!(
            contact.candidates().collect::<Vec<_>>(),
            [(1, warm), (2, SimplexCache::EMPTY)]
        );
        assert!(contact.candidates.iter().all(|entry| entry.memo.is_none()));
    }

    #[test]
    fn convex_memo_requires_the_same_pose_and_cache() {
        let geometry = StaticGeometry::Round {
            shape: RoundShape::Sphere(Sphere {
                center: [0.2, 1.0, 0.0],
                radius: 0.4,
            }),
            mask: 1,
        };
        let shape = StaticShape::new(geometry, None).unwrap();
        let ProxyContact::Convex(mut contact) = ProxyContact::new(&shape, 1) else {
            panic!("convex contact");
        };
        let body = pose([0.0; 3]);
        let got = contact.manifolds(body).unwrap().unwrap();
        let mut point = got.points()[0];
        point.feature = FeatureId {
            triangle: 37,
            ..point.feature
        };
        contact.memo.as_mut().unwrap().manifold = Manifold::one(got.normal, point);
        assert_eq!(
            contact.manifolds(body).unwrap().unwrap().points()[0]
                .feature
                .triangle,
            37
        );
        contact.cache = SimplexCache {
            metric: 3.0,
            ..SimplexCache::EMPTY
        };
        assert_eq!(
            contact.manifolds(body).unwrap().unwrap().points()[0]
                .feature
                .triangle,
            0
        );
        contact.memo.as_mut().unwrap().manifold = Manifold::one(got.normal, point);
        let taller = CapsulePose { top: 1.6, ..body };
        assert_eq!(
            contact.manifolds(taller).unwrap().unwrap().points()[0]
                .feature
                .triangle,
            0
        );
    }

    #[test]
    fn body_casts_match_separated_and_shrinking_overlap_fractions() {
        let mesh = Mesh {
            vertices: vec![
                [-10.0, -1.0, 5.0],
                [10.0, -1.0, 5.0],
                [10.0, 10.0, 5.0],
                [-10.0, 10.0, 5.0],
            ],
            triangles: vec![
                triangle([0, 2, 1], [3, -1, -1]),
                triangle([0, 3, 2], [-1, -1, 1]),
            ],
        };
        let geometry = StaticGeometry::Mesh(Arc::new(ContactMesh::new(mesh, [1.0; 3], 1).unwrap()));
        let wall = StaticShape::new(geometry, None).unwrap();
        for (z, target, manifolds, fraction) in
            [(3.0, 5.0, 0, 0x3f40_0000), (4.495, 4.695, 1, 0x3ccc_cd4e)]
        {
            let mut contact = ProxyContact::new(&wall, 1);
            let body = pose([0.0, 0.0, z]);
            let end = [0.0, 0.0, target];
            assert_eq!(contact.normal_sign(), 1.0);
            assert_eq!(contact.manifolds(body).unwrap().iter().count(), manifolds);
            assert_eq!(
                contact
                    .cast(body, body.swept_bounds(end), sub(end, body.position))
                    .unwrap()
                    .unwrap()
                    .to_bits(),
                fraction
            );
        }

        let geometry = StaticGeometry::Round {
            shape: RoundShape::Sphere(Sphere {
                center: [0.0, 1.0, 2.0],
                radius: 0.25,
            }),
            mask: 1,
        };
        let obstacle = StaticShape::new(geometry, None).unwrap();
        let mut contact = ProxyContact::new(&obstacle, 1);
        let body = pose([0.0; 3]);
        let end = [0.0, 0.0, 2.0];
        assert_eq!(contact.normal_sign(), -1.0);
        assert_eq!(
            contact
                .cast(body, body.swept_bounds(end), end)
                .unwrap()
                .unwrap()
                .to_bits(),
            0x3f21_47ae
        );
    }
}
