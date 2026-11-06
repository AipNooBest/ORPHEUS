//! Float32 GJK shape casts for the retail character collision engine.
//!
//! Ports `shape_cast` from `ow174/game/contacts.py` and uses the same simplex
//! solvers as distance queries. The cast maintains its own translated simplex.

use std::cmp::Ordering;

use super::{
    ABSENT, MAX_ITERATIONS, PointSetSize, Points, Simplex, Vertex, identity, neg, support,
};
use crate::contact_math::{
    ContactFrame, TINY, Vec3, add, dot, maxps, minps, mul, normalize, rotate, scale, sub,
};
use crate::contact_mesh::ContactMesh;
use crate::contact_triangle::{SKIN, TOUCH};

pub(crate) const CAST_TOLERANCE: f32 = 0.00125;
const CAST_DEEP: f32 = 0.0001;
const SWEEP_BOX: f32 = 0.02;

/// The cast's matrix transform. ContactFrame's quaternion path has its own
/// operation order and is used for different parts of the movement engine.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CastFrame {
    columns: [Vec3; 3],
    translation: Vec3,
}

impl From<ContactFrame> for CastFrame {
    fn from(frame: ContactFrame) -> Self {
        let [x, y, z, w] = frame.rotation;
        let (x2, y2, z2) = (x + x, y + y, z + z);
        let (xx, yy, zz) = (x2 * x, y2 * y, z2 * z);
        let (zx, yx, zy) = (z2 * x, y2 * x, z2 * y);
        let (yw, zw, xw) = (y2 * w, z2 * w, x2 * w);
        Self {
            columns: [
                [(1.0 - yy) - zz, zw + yx, zx - yw],
                [yx - zw, (1.0 - xx) - zz, xw + zy],
                [yw + zx, zy - xw, (1.0 - xx) - yy],
            ],
            translation: frame.translation,
        }
    }
}

impl CastFrame {
    pub(crate) fn columns(self) -> [Vec3; 3] {
        self.columns
    }

    pub(crate) fn rotate_only(self, [x, y, z]: Vec3) -> Vec3 {
        let [c0, c1, c2] = self.columns;
        add(add(scale(c0, x), scale(c1, y)), scale(c2, z))
    }

    pub fn transform(self, point: Vec3) -> Vec3 {
        add(self.rotate_only(point), self.translation)
    }
}

#[derive(Clone, Copy, Debug)]
pub struct CastShape<'a> {
    pub points: &'a [Vec3],
    pub radius: f32,
    pub frame: Option<CastFrame>,
}

impl CastShape<'_> {
    fn place(self, point: Vec3) -> Vec3 {
        self.frame
            .map_or_else(|| identity(point), |frame| frame.transform(point))
    }
}

#[derive(Clone, Copy, Debug)]
pub struct CastOptions {
    /// Displacement of shape B in the world frame.
    pub translation: Vec3,
    pub max_fraction: f32,
    pub hit_touching: bool,
    pub shrink: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ShapeHit {
    pub fraction: f32,
    /// Shape A's witness pushed along the normal by A's padded radius.
    pub point: Vec3,
    /// Points from A toward B.
    pub normal: Vec3,
}

#[derive(Clone, Copy, Debug)]
pub struct MeshProbe<'a> {
    pub points: &'a [Vec3],
    pub radius: f32,
    pub start: Vec3,
    pub end: Vec3,
    pub category: u16,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MeshHit {
    pub hit: ShapeHit,
    pub triangle: u32,
}

fn cast_closest(simplex: &Simplex) -> Vec3 {
    let [a, b, ..] = simplex.vertices;
    if simplex.len == 1 {
        return a.gap;
    }
    let mut point = add(scale(b.gap, b.weight), scale(a.gap, a.weight));
    for vertex in &simplex.vertices[2..simplex.len] {
        point = add(point, scale(vertex.gap, vertex.weight));
    }
    point
}

/// Cast B through a fraction of its displacement toward a static A.
///
/// The reference begins from the first support pair for every cast. The
/// strict fraction limit keeps the first hit on exact ties.
pub fn shape_cast(
    a: CastShape<'_>,
    b: CastShape<'_>,
    options: CastOptions,
) -> Result<Option<ShapeHit>, PointSetSize> {
    if a.points.is_empty()
        || b.points.is_empty()
        || a.points.len() > usize::from(ABSENT)
        || b.points.len() > usize::from(ABSENT)
    {
        return Err(PointSetSize);
    }
    let origin = b.place(b.points[0]);
    let points_a = Points::mapped(a.points, |point| sub(a.place(point), origin));
    let mut first = true;
    let points_b = Points::mapped(b.points, |point| {
        if std::mem::take(&mut first) {
            [0.0; 3]
        } else {
            sub(b.place(point), origin)
        }
    });
    let (points_a, points_b) = (points_a.as_slice(), points_b.as_slice());
    let spread_a = Points::mapped(&points_a[1..], |point| sub(point, points_a[0]));
    let radius_a = maxps(a.radius, SKIN);
    let mut sigma = (maxps(b.radius, SKIN) + radius_a) - SKIN;
    let mut target = CAST_TOLERANCE + sigma;
    let mut target_sq = target * target;
    let mut fraction = 0.0;
    let mut previous = f32::MAX;
    let mut stuck = false;
    let mut closest = sub(points_b[0], points_a[0]);
    let mut search = closest;
    let mut simplex = Simplex {
        vertices: [Vertex::default(); 4],
        len: 0,
    };
    let translation = options.translation;
    for _ in 0..MAX_ITERATIONS {
        let squared = dot(closest, closest);
        if squared < target_sq {
            if fraction != 0.0 {
                break;
            }
            if !options.shrink {
                if options.hit_touching {
                    break;
                }
                return Ok(None);
            }
            if previous.partial_cmp(&squared).is_none_or(Ordering::is_gt) {
                stuck = true;
                previous = squared;
            } else if squared.partial_cmp(&CAST_DEEP) != Some(Ordering::Greater) {
                return Ok(None);
            } else {
                stuck = false;
                sigma = squared.sqrt() - TOUCH;
                target = CAST_TOLERANCE + sigma;
                target_sq = target * target;
            }
        }
        let ia = support(spread_a.as_slice(), search);
        let ib = support(&points_b[1..], neg(search));
        let (a, b) = (points_a[usize::from(ia)], points_b[usize::from(ib)]);
        let vn = normalize(search);
        let mut shift = scale(translation, fraction);
        let vp = -dot(sub(sub(a, b), shift), vn);
        let mut moved = false;
        if target < vp && !stuck {
            let vr = -dot(vn, translation);
            if vr < f32::EPSILON {
                return Ok(None);
            }
            let advanced = ((vp - sigma) / vr) + fraction;
            if options.max_fraction <= advanced {
                return Ok(None);
            }
            moved = advanced != fraction;
            fraction = advanced;
            if moved {
                shift = scale(translation, fraction);
            }
        }
        let mut duplicate = false;
        for vertex in &mut simplex.vertices[..simplex.len] {
            if moved {
                vertex.b = add(points_b[usize::from(vertex.index_b)], shift);
                vertex.gap = sub(vertex.b, vertex.a);
            }
            duplicate |= vertex.index_a == ia && vertex.index_b == ib;
        }
        if !duplicate {
            simplex.vertices[simplex.len] = Vertex {
                weight: 1.0,
                ..Vertex::new(a, add(shift, b), ia, ib)
            };
            simplex.len += 1;
        }
        if simplex.len == 1 {
            search = simplex.vertices[0].gap;
        } else {
            let Some(direction) = simplex.search() else {
                return Ok(None);
            };
            search = neg(direction);
            if simplex.len == 4 {
                if !options.hit_touching {
                    return Ok(None);
                }
                break;
            }
        }
        closest = cast_closest(&simplex);
    }
    let witness = if simplex.len == 0 {
        points_a[0]
    } else {
        simplex.weighted(|vertex| vertex.a)
    };
    let normal = if dot(search, search) > TINY {
        normalize(search)
    } else if dot(translation, translation) > TINY {
        neg(normalize(translation))
    } else {
        [0.0; 3]
    };
    Ok(Some(ShapeHit {
        fraction,
        point: add(add(witness, origin), scale(normal, radius_a)),
        normal,
    }))
}

fn quaternion_product([ax, ay, az, aw]: [f32; 4], [bx, by, bz, bw]: [f32; 4]) -> [f32; 4] {
    let first = [aw * bx, az * bx, -ay * bx, -ax * bx];
    let own = [bw * ax, bw * ay, bw * az, bw * aw];
    let second = [-az * by, aw * by, ax * by, -ay * by];
    let third = [ay * bz, -ax * bz, aw * bz, -az * bz];
    std::array::from_fn(|k| ((first[k] + own[k]) + second[k]) + third[k])
}

/// Sweep a probe against the ordered triangles of one placed mesh.
///
/// Candidates use the scaled mesh. Individual casts run in the raw mesh's
/// frame and the winning witness is returned in world coordinates.
pub fn mesh_sweep(
    mesh: &ContactMesh,
    frame: ContactFrame,
    probe: MeshProbe<'_>,
    mut max_fraction: f32,
) -> Result<Option<MeshHit>, PointSetSize> {
    if probe.points.is_empty() || probe.points.len() > usize::from(ABSENT) {
        return Err(PointSetSize);
    }
    let inverse = mesh.inverse_scale();
    let mesh_scale = mesh.scale();
    let local_start = mul(frame.local(probe.start), inverse);
    let mut local_end = mul(frame.local(probe.end), inverse);
    let radius = probe.radius * inverse[0];
    let [x, y, z, w] = frame.rotation;
    let conjugate = [-x, -y, -z, w];
    let turn: CastFrame = ContactFrame {
        translation: rotate(conjugate, sub(probe.start, frame.translation)),
        rotation: quaternion_product(conjugate, ContactFrame::IDENTITY.rotation),
    }
    .into();
    let mut moved = Points::mapped(probe.points, |point| mul(turn.transform(point), inverse));
    let moved = match &mut moved {
        Points::Inline { points, len } => &mut points[..*len],
        Points::Heap(points) => points.as_mut_slice(),
    };
    let mut low = moved[0];
    let mut high = moved[0];
    for point in &moved[1..] {
        low = std::array::from_fn(|k| minps(low[k], point[k]));
        high = std::array::from_fn(|k| maxps(high[k], point[k]));
    }
    let center = scale(add(high, low), 0.5);
    local_end = add(local_end, sub(center, local_start));
    for point in &mut *moved {
        *point = sub(*point, center);
    }
    let translation = sub(local_end, center);
    let half = add(add(scale(sub(high, low), 0.5), [radius; 3]), [SWEEP_BOX; 3]);
    // The reference retains these endpoints until its f32 SAT midpoint and extent rounding.
    let mut box_low =
        std::array::from_fn(|k| f64::from(minps(center[k], local_end[k])) - f64::from(half[k]));
    let mut box_high =
        std::array::from_fn(|k| f64::from(maxps(center[k], local_end[k])) + f64::from(half[k]));
    if mesh_scale != [1.0; 3] {
        for k in 0..3 {
            let a = (box_low[k] * f64::from(mesh_scale[k])) as f32;
            let b = (box_high[k] * f64::from(mesh_scale[k])) as f32;
            box_low[k] = f64::from(a.min(b)) - f64::from(SWEEP_BOX);
            box_high[k] = f64::from(a.max(b)) + f64::from(SWEEP_BOX);
        }
    }
    let b = CastShape {
        points: moved,
        radius: maxps(SKIN, radius),
        frame: Some(
            ContactFrame {
                translation: center,
                ..ContactFrame::IDENTITY
            }
            .into(),
        ),
    };
    let mut nearest = None;
    for triangle in mesh.query_precise(box_low, box_high, probe.category) {
        let points = mesh
            .raw_corners(triangle)
            .expect("the mesh query returned a valid triangle");
        let a = CastShape {
            points: &points,
            radius: SKIN,
            frame: None,
        };
        let options = CastOptions {
            translation,
            max_fraction,
            hit_touching: false,
            shrink: false,
        };
        if let Some(hit) = shape_cast(a, b, options)? {
            max_fraction = hit.fraction;
            nearest = Some(MeshHit { hit, triangle });
        }
    }
    Ok(nearest.map(|MeshHit { hit, triangle }| MeshHit {
        hit: ShapeHit {
            point: frame.place(mul(hit.point, mesh_scale)),
            normal: rotate(frame.rotation, hit.normal),
            ..hit
        },
        triangle,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collision_cache::{Mesh, Triangle};

    const FLOOR: [Vec3; 3] = [[-3.0, 0.0, -3.0], [0.0, 0.0, 3.0], [3.0, 0.0, -3.0]];
    const POINT: [Vec3; 1] = [[0.0; 3]];

    fn frame(translation: Vec3) -> CastFrame {
        ContactFrame {
            translation,
            ..ContactFrame::IDENTITY
        }
        .into()
    }

    fn assert_hit(hit: ShapeHit, expected: [u32; 7]) {
        assert_eq!(hit.fraction.to_bits(), expected[0]);
        assert_eq!(hit.point.map(f32::to_bits), expected[1..4]);
        assert_eq!(hit.normal.map(f32::to_bits), expected[4..7]);
    }

    #[test]
    fn downward_cast_keeps_padded_witness_and_strict_fraction_limit() {
        let a = CastShape {
            points: &FLOOR,
            radius: SKIN,
            frame: None,
        };
        let b = CastShape {
            points: &POINT,
            radius: 0.5,
            frame: Some(frame([0.0, 1.0, 0.0])),
        };
        let options = CastOptions {
            translation: [0.0, -2.0, 0.0],
            max_fraction: 1.0,
            hit_touching: false,
            shrink: false,
        };
        let hit = shape_cast(a, b, options).unwrap().unwrap();
        assert_hit(
            hit,
            [
                0x3e80_0000,
                0,
                0x3c23_d74a,
                0,
                0x8000_0000,
                0x3f80_0000,
                0x8000_0000,
            ],
        );
        assert!(
            shape_cast(
                a,
                b,
                CastOptions {
                    max_fraction: hit.fraction,
                    ..options
                }
            )
            .unwrap()
            .is_none()
        );
    }

    #[test]
    fn touching_and_shrunk_overlap_match_the_reference() {
        let a = CastShape {
            points: &POINT,
            radius: 0.5,
            frame: None,
        };
        let b = CastShape {
            frame: Some(frame([0.0, 0.75, 0.0])),
            ..a
        };
        let options = CastOptions {
            translation: [0.0, 1.0, 0.0],
            max_fraction: 1.0,
            hit_touching: true,
            shrink: false,
        };
        let hit = shape_cast(a, b, options).unwrap().unwrap();
        assert_hit(hit, [0, 0, 0x3eff_ffff, 0, 0, 0x3f7f_ffff, 0]);
        assert!(
            shape_cast(
                a,
                b,
                CastOptions {
                    hit_touching: false,
                    ..options
                }
            )
            .unwrap()
            .is_none()
        );
        let shrunk = shape_cast(
            a,
            b,
            CastOptions {
                translation: [0.0, -1.0, 0.0],
                hit_touching: false,
                shrink: true,
                ..options
            },
        )
        .unwrap()
        .unwrap();
        assert_hit(shrunk, [0x3ba3_d681, 0, 0x3f00_0000, 0, 0, 0x3f80_0000, 0]);
    }

    #[test]
    fn frames_transform_both_support_sets_in_the_casts_matrix_order() {
        let q = std::f32::consts::FRAC_1_SQRT_2;
        let frame = |translation| {
            Some(
                ContactFrame {
                    translation,
                    rotation: [0.0, q, 0.0, q],
                }
                .into(),
            )
        };
        let a = CastShape {
            points: &FLOOR,
            radius: SKIN,
            frame: frame([0.2, 0.5, 0.3]),
        };
        let segment = [[0.1, 0.0, 0.0], [0.0, 0.2, 0.0]];
        let b = CastShape {
            points: &segment,
            radius: 0.2,
            frame: frame([0.3, 1.5, 0.5]),
        };
        let options = CastOptions {
            translation: [0.0, -2.0, 0.0],
            max_fraction: 1.0,
            hit_touching: false,
            shrink: false,
        };
        let hit = shape_cast(a, b, options).unwrap().unwrap();
        assert_hit(
            hit,
            [
                0x3ecc_cccc,
                0x3e99_9998,
                0x3f02_8f5c,
                0x3ecc_cccd,
                0x8000_0000,
                0x3f80_0000,
                0,
            ],
        );
    }

    #[test]
    fn mesh_probe_keeps_first_face_and_restores_placed_witness() {
        let floor = |y, mesh_scale| {
            let vertices = vec![
                [-2.0, y, -2.0],
                [2.0, y, -2.0],
                [2.0, y, 2.0],
                [-2.0, y, 2.0],
            ];
            let triangles = [[0, 2, 1], [0, 3, 2]]
                .map(|corners| Triangle {
                    corners,
                    wings: [-1; 3],
                    mask: 1,
                    surface: 0,
                })
                .into();
            ContactMesh::new(
                Mesh {
                    vertices,
                    triangles,
                },
                mesh_scale,
                1,
            )
            .unwrap()
        };
        let disc = [
            [0.4, 0.015, 0.0],
            [0.0, 0.015, 0.4],
            [-0.4, 0.015, 0.0],
            [0.0, 0.015, -0.4],
            [0.0; 3],
        ];
        let probe = MeshProbe {
            points: &disc,
            radius: 0.015,
            start: [0.0, 2.0, 0.0],
            end: [0.0, 0.5, 0.0],
            category: 1,
        };
        let mesh = floor(1.0, [1.0; 3]);
        let hit = mesh_sweep(&mesh, ContactFrame::IDENTITY, probe, 1.0)
            .unwrap()
            .unwrap();
        assert_eq!(hit.triangle, 0);
        assert_hit(
            hit.hit,
            [0x3f28_1b4f, 0xb300_0000, 0x3f81_47ae, 0, 0, 0x3f7f_ffff, 0],
        );

        let mesh = floor(0.2, [1.25, 0.75, 1.5]);
        let quarter = std::f32::consts::FRAC_1_SQRT_2;
        let placed = ContactFrame {
            translation: [0.3, 1.0, 0.0],
            rotation: [0.0, quarter, 0.0, quarter],
        };
        let hit = mesh_sweep(&mesh, placed, probe, 1.0).unwrap().unwrap();
        assert_eq!(hit.triangle, 0);
        assert_hit(
            hit.hit,
            [
                0x3f0f_87da,
                0x37aa_8000,
                0x3f94_28f5,
                0x3c97_c090,
                0xbb97_b346,
                0x3f7f_fe98,
                0xbb97_b348,
            ],
        );
    }
}
