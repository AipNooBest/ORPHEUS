//! Float32 capsule contacts against static round shapes for retail build 104319.
//!
//! Ports the round narrow phase of `ow174/game/contacts.py`. The caller filters
//! the obstacle mask, runs the broadphase, and applies the returned contact
//! constraints. Static surface rays use a separate representation.

use crate::collision_cache::{Placement, Shape};
use crate::contact_math::{
    ContactFrame, TINY, Vec3, add, dot, maxps, minps, mul, normalize, rsqrt, scale, sub,
};

const PARALLEL: f32 = f32::from_bits(0x3f7f_b02e);
const X_AXIS: Vec3 = [1.0, 0.0, 0.0];

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Capsule {
    pub a: Vec3,
    pub b: Vec3,
    pub radius: f32,
}

impl Capsule {
    fn placed(self, frame: ContactFrame) -> Self {
        Self {
            a: frame.place(self.a),
            b: frame.place(self.b),
            radius: self.radius,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sphere {
    pub center: Vec3,
    pub radius: f32,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum RoundShape {
    Capsule(Capsule),
    Sphere(Sphere),
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RoundObstacle {
    pub shape: RoundShape,
    pub frame: ContactFrame,
    pub mask: u16,
}

impl RoundObstacle {
    /// Apply a cache placement as the reference contact engine does.
    pub fn from_placement(shape: &Shape, placement: Placement) -> Option<Self> {
        let scale = placement.scale;
        let (shape, mask) = match *shape {
            Shape::Capsule { a, b, radius, mask } => (
                RoundShape::Capsule(Capsule {
                    a: mul(a, scale),
                    b: mul(b, scale),
                    radius: radius * scale[0],
                }),
                mask,
            ),
            Shape::Sphere {
                center,
                radius,
                mask,
            } => (
                RoundShape::Sphere(Sphere {
                    center: mul(center, scale),
                    radius: radius * scale[0],
                }),
                mask,
            ),
            _ => return None,
        };
        Some(Self {
            shape,
            mask,
            frame: ContactFrame {
                translation: placement.translation,
                rotation: placement.rotation,
            },
        })
    }

    pub fn manifold(self, body: Capsule, body_frame: ContactFrame) -> Manifold {
        let body = body.placed(body_frame);
        match self.shape {
            RoundShape::Sphere(sphere) => capsule_sphere(
                body,
                Sphere {
                    center: self.frame.place(sphere.center),
                    radius: sphere.radius,
                },
            ),
            RoundShape::Capsule(capsule) => capsule_capsule(body, capsule.placed(self.frame)),
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
#[repr(u8)]
pub enum FeatureKind {
    #[default]
    Face = 0,
    Edge = 1,
    Vertex = 2,
    Unclassified = 3,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Feature {
    pub index: u8,
    pub kind: FeatureKind,
}

impl Feature {
    fn along_segment(parameter: f32) -> Self {
        if parameter == 0.0 {
            Self::vertex(0)
        } else if parameter == 1.0 {
            Self::vertex(1)
        } else {
            Self::edge(0)
        }
    }

    pub(crate) fn edge(index: u8) -> Self {
        Self {
            index,
            kind: FeatureKind::Edge,
        }
    }

    pub(crate) fn vertex(index: u8) -> Self {
        Self {
            index,
            kind: FeatureKind::Vertex,
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FeatureId {
    pub a: Feature,
    pub b: Feature,
    pub triangle: u32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct ContactPoint {
    pub position: Vec3,
    pub separation: f32,
    pub feature: FeatureId,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Manifold {
    /// Points from shape A toward shape B.
    pub normal: Vec3,
    /// Lower distance estimate for an empty manifold beyond contact reach.
    pub gap: Option<f64>,
    points: [ContactPoint; 2],
    len: usize,
}

impl Manifold {
    pub fn points(&self) -> &[ContactPoint] {
        &self.points[..self.len]
    }

    pub(crate) fn with_normal(normal: Vec3) -> Self {
        Self {
            normal,
            ..Self::default()
        }
    }

    pub(crate) fn one(normal: Vec3, point: ContactPoint) -> Self {
        let mut manifold = Self::with_normal(normal);
        manifold.push(point);
        manifold
    }

    pub(crate) fn push(&mut self, point: ContactPoint) {
        self.points[self.len] = point;
        self.len += 1;
    }
}

pub(crate) fn splat_tiny(value: f32) -> bool {
    let square = value * value;
    (square + square) + square > TINY
}

pub(crate) fn clamp01(value: f32) -> f32 {
    maxps(0.0, minps(value, 1.0))
}

fn unit_or_x(vector: Vec3, squared: f32) -> Vec3 {
    if squared > TINY {
        scale(vector, rsqrt(squared))
    } else {
        X_AXIS
    }
}

/// World-space contact with the capsule as body A and the sphere as body B.
pub fn capsule_sphere(capsule: Capsule, sphere: Sphere) -> Manifold {
    let segment = sub(capsule.b, capsule.a);
    let along = dot(sub(sphere.center, capsule.a), segment);
    let nearest = if along <= 0.0 {
        capsule.a
    } else {
        let length = dot(segment, segment);
        if length <= along {
            capsule.b
        } else {
            add(scale(segment, along / length), capsule.a)
        }
    };
    let gap = sub(sphere.center, nearest);
    let squared = dot(gap, gap);
    let distance = if squared > TINY { squared.sqrt() } else { 0.0 };
    let total = sphere.radius + capsule.radius;
    if total < distance {
        return Manifold::default();
    }
    let (normal, position, separation) = if distance < f32::EPSILON {
        let axis = normalize(segment);
        let normal = if axis[2] > 0.5 {
            [axis[2], 0.0, -axis[0]]
        } else {
            [axis[1], -axis[0], 0.0]
        };
        (normalize(normal), nearest, -total)
    } else {
        let normal = gap.map(|lane| lane / distance);
        let position = scale(
            add(
                scale(normal, capsule.radius - sphere.radius),
                add(nearest, sphere.center),
            ),
            0.5,
        );
        (normal, position, distance - total)
    };
    Manifold::one(
        normal,
        ContactPoint {
            position,
            separation,
            feature: FeatureId::default(),
        },
    )
}

struct CapsulePair {
    a: Capsule,
    b: Capsule,
    d1: Vec3,
    d2: Vec3,
    length_a: f32,
    length_b: f32,
    across: f32,
    radii: f32,
}

impl CapsulePair {
    fn new(a: Capsule, b: Capsule) -> Self {
        let d1 = sub(a.b, a.a);
        let d2 = sub(b.b, b.a);
        Self {
            a,
            b,
            d1,
            d2,
            length_a: dot(d1, d1),
            length_b: dot(d2, d2),
            across: dot(d2, d1),
            radii: b.radius + a.radius,
        }
    }

    fn at_a(&self, parameter: f32) -> Vec3 {
        add(scale(self.d1, parameter), self.a.a)
    }

    fn at_b(&self, parameter: f32) -> Vec3 {
        add(scale(self.d2, parameter), self.b.a)
    }

    fn within_reach(&self, squared: f32) -> bool {
        squared <= self.radii * self.radii
    }

    fn midpoint_shift(&self, distance: f32) -> f32 {
        ((distance + self.a.radius) - self.b.radius) * 0.5
    }

    fn one_point(&self, a: Vec3, b: Vec3, feature: FeatureId) -> Manifold {
        let gap = sub(b, a);
        let squared = dot(gap, gap);
        if !self.within_reach(squared) {
            return Manifold::default();
        }
        let distance = squared.sqrt();
        let normal = unit_or_x(gap, squared);
        Manifold::one(
            normal,
            ContactPoint {
                position: add(scale(normal, self.midpoint_shift(distance)), a),
                separation: distance - self.radii,
                feature,
            },
        )
    }

    fn parallel(&self) -> Manifold {
        let mut out = Manifold::default();
        let mut normal_sum = [0.0; 3];
        let mut nearest = [[0.0; 3]; 2];
        for (k, (endpoint, nearest_a)) in [self.a.a, self.a.b]
            .into_iter()
            .zip(&mut nearest)
            .enumerate()
        {
            let t = dot(sub(endpoint, self.b.a), self.d2) / self.length_b;
            let (a, b, feature) = if t <= 0.0 || t >= 1.0 {
                let (b, b_index) = if t <= 0.0 {
                    (self.b.a, 0)
                } else {
                    (self.b.b, 1)
                };
                let s = dot(sub(b, self.a.a), self.d2) / self.across;
                let (a, s) = if s <= 0.0 {
                    (self.a.a, 0.0)
                } else if s >= 1.0 {
                    (self.a.b, 1.0)
                } else {
                    (self.at_a(s), s)
                };
                (
                    a,
                    b,
                    FeatureId {
                        a: Feature::along_segment(s),
                        b: Feature::vertex(b_index),
                        triangle: 0,
                    },
                )
            } else {
                (
                    endpoint,
                    self.at_b(t),
                    FeatureId {
                        a: Feature::vertex(k as u8),
                        b: Feature::edge(0),
                        triangle: 0,
                    },
                )
            };
            *nearest_a = a;
            let gap = sub(b, a);
            let squared = dot(gap, gap);
            if !self.within_reach(squared) {
                continue;
            }
            let (position, separation) = if squared > TINY {
                let distance = squared.sqrt();
                let position = add(
                    scale(scale(gap, rsqrt(squared)), self.midpoint_shift(distance)),
                    a,
                );
                (position, distance - self.radii)
            } else {
                let shift = (self.a.radius - self.b.radius) * 0.5;
                (add([shift, shift * 0.0, shift * 0.0], a), -self.radii)
            };
            out.push(ContactPoint {
                position,
                separation,
                feature,
            });
            normal_sum = add(normal_sum, gap);
        }
        out.normal = unit_or_x(normal_sum, dot(normal_sum, normal_sum));
        if out.len == 2 && nearest[0] == nearest[1] {
            out.len = 1;
            out.points[1] = ContactPoint::default();
        }
        out
    }
}

/// World-space capsule pair. Nearly parallel axes can contribute two points.
pub fn capsule_capsule(a: Capsule, b: Capsule) -> Manifold {
    let pair = CapsulePair::new(a, b);
    let offset = sub(a.a, b.a);
    let c = dot(offset, pair.d1);
    let f = dot(offset, pair.d2);
    match (splat_tiny(pair.length_a), splat_tiny(pair.length_b)) {
        (false, false) => return pair.one_point(a.a, b.a, FeatureId::default()),
        (false, true) => {
            return pair.one_point(
                a.a,
                pair.at_b(clamp01(f / pair.length_b)),
                FeatureId::default(),
            );
        }
        (true, false) => {
            return pair.one_point(
                pair.at_a(clamp01(-c / pair.length_a)),
                b.a,
                FeatureId::default(),
            );
        }
        (true, true) => {}
    }
    let cos_sq = (pair.across * pair.across) / (pair.length_b * pair.length_a);
    if cos_sq < PARALLEL {
        let ia = rsqrt(pair.length_a);
        let u1 = scale(pair.d1, ia);
        let u2 = scale(pair.d2, rsqrt(pair.length_b));
        let cosine = dot(u2, u1);
        let cu = dot(u1, offset);
        let fu = dot(u2, offset);
        let mut s = clamp01((((fu * cosine) - cu) * ia) / (1.0 - cos_sq));
        let mut t = ((s * pair.across) + f) / pair.length_b;
        if t < 0.0 {
            s = clamp01(-c / pair.length_a);
            t = 0.0;
        } else if t > 1.0 {
            s = clamp01((pair.across - c) / pair.length_a);
            t = 1.0;
        }
        pair.one_point(
            pair.at_a(s),
            pair.at_b(t),
            FeatureId {
                a: Feature::along_segment(s),
                b: Feature::along_segment(t),
                triangle: 0,
            },
        )
    } else {
        pair.parallel()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BODY: Capsule = Capsule {
        a: [0.0; 3],
        b: [0.0, 1.0, 0.0],
        radius: 0.5,
    };

    fn assert_point(point: &ContactPoint, position: [u32; 3], separation: u32, id: [u8; 4]) {
        assert_eq!(point.position.map(f32::to_bits), position);
        assert_eq!(point.separation.to_bits(), separation);
        assert_eq!(
            [
                point.feature.a.index,
                point.feature.b.index,
                point.feature.a.kind as u8,
                point.feature.b.kind as u8,
            ],
            id
        );
        assert_eq!(point.feature.triangle, 0);
    }

    #[test]
    fn sphere_contact_and_axial_normal_match_python() {
        let contact = |center| {
            capsule_sphere(
                BODY,
                Sphere {
                    center,
                    radius: 0.2,
                },
            )
        };
        let oblique = contact([0.55, 0.37, 0.29]);
        assert_eq!(
            oblique.normal.map(f32::to_bits),
            [0x3f62_731e, 0, 0x3eee_cd2d]
        );
        assert_eq!(oblique.points().len(), 1);
        assert_point(
            &oblique.points()[0],
            [0x3ed0_bc23, 0x3ebd_70a4, 0x3e5c_1ed5],
            0xbda0_3628,
            [0; 4],
        );

        let axial = contact([0.0, 0.25, 0.0]);
        assert_eq!(
            axial.normal.map(f32::to_bits),
            [0x3f80_0000, 0x8000_0000, 0]
        );
        assert_point(&axial.points()[0], [0, 0x3e80_0000, 0], 0xbf33_3333, [0; 4]);
        assert_eq!(contact([2.0, 0.0, 0.0]), Manifold::default());
    }

    #[test]
    fn parallel_capsules_keep_ordered_endpoints_and_remove_duplicate_positions() {
        let contact = |a, b| capsule_capsule(BODY, Capsule { a, b, radius: 0.2 });
        let parallel = contact([0.3, 0.0, 0.0], [0.3, 1.0, 0.0]);
        assert_eq!(parallel.normal, X_AXIS);
        assert_eq!(parallel.points().len(), 2);
        assert_point(
            &parallel.points()[0],
            [0x3e99_999a, 0, 0],
            0xbecc_cccc,
            [0, 0, 2, 2],
        );
        assert_point(
            &parallel.points()[1],
            [0x3e99_999a, 0x3f80_0000, 0],
            0xbecc_cccc,
            [1, 1, 2, 2],
        );

        let duplicate = contact([0.3, 1.0, 0.0], [0.3, 2.0, 0.0]);
        assert_eq!(duplicate.points().len(), 1);
        assert_point(
            &duplicate.points()[0],
            [0x3e99_999a, 0x3f80_0000, 0],
            0xbecc_cccc,
            [1, 0, 2, 2],
        );
        let miss = contact([3.0, 0.0, 0.0], [3.0, 1.0, 0.0]);
        assert_eq!(miss.normal, X_AXIS);
        assert!(miss.points().is_empty());
    }

    #[test]
    fn crossing_and_collapsed_capsules_match_python() {
        let cross = capsule_capsule(
            BODY,
            Capsule {
                a: [-0.5, 0.65, 0.3],
                b: [0.5, 0.65, 0.3],
                radius: 0.2,
            },
        );
        assert_eq!(cross.normal, [0.0, 0.0, 1.0]);
        assert_eq!(cross.points().len(), 1);
        assert_point(
            &cross.points()[0],
            [0, 0x3f26_6666, 0x3e99_999a],
            0xbecc_cccc,
            [0, 0, 1, 1],
        );

        let collapsed = capsule_capsule(
            Capsule { b: BODY.a, ..BODY },
            Capsule {
                a: [0.3, -0.7, 0.07],
                b: [0.3, 1.0, 0.07],
                radius: 0.2,
            },
        );
        assert_eq!(
            collapsed.normal.map(f32::to_bits),
            [0x3f79_4da9, 0x344f_c0b7, 0x3e68_aee2]
        );
        assert_eq!(collapsed.points().len(), 1);
        assert_point(
            &collapsed.points()[0],
            [0x3e97_974d, 0x337c_a6d5, 0x3d8d_7c26],
            0xbec8_ac90,
            [0; 4],
        );
    }

    #[test]
    fn placed_cache_capsule_uses_contact_scale_and_both_frames() {
        let shape = Shape::Capsule {
            a: [0.0, -0.1, 0.0],
            b: [0.35, 0.1, 0.15],
            radius: 0.125,
            mask: 0x08,
        };
        let obstacle = RoundObstacle::from_placement(
            &shape,
            Placement {
                model: 0,
                group: 0,
                translation: [0.4, -0.1, 0.1],
                scale: [2.0; 3],
                rotation: [
                    0.0,
                    f32::from_bits(0x3f35_04f1),
                    0.0,
                    f32::from_bits(0x3f35_04f5),
                ],
            },
        )
        .unwrap();
        assert_eq!(obstacle.mask, 0x08);
        let contact = obstacle.manifold(
            BODY,
            ContactFrame {
                translation: [-0.1, 0.1, 0.1],
                ..ContactFrame::IDENTITY
            },
        );
        assert_eq!(
            contact.normal.map(f32::to_bits),
            [0x3f49_8e73, 0xbf1d_c96f, 0xbc72_14b1]
        );
        assert_eq!(contact.points().len(), 1);
        assert_point(
            &contact.points()[0],
            [0x3e80_3a18, 0xbe32_86a0, 0x3dbf_54c5],
            0xbde0_db38,
            [0, 0, 2, 1],
        );
    }
}
