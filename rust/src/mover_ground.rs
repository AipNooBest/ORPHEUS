//! Ground probe for the retail character mover.
//!
//! It uses the same triangle index as other ground queries and the contact
//! world's shape sweep only when the centre ray has no walkable ground.

use crate::{
    collision_index::{GroundHit, TriangleIndex},
    contact_gjk::{PointSetSize, sweep::MeshProbe},
    contact_math::{TINY, Vec3, add, cross, dot, scale, sub, unrotate},
    contact_world::{ContactWorld, StaticGeometry, StaticShape},
    mover_kinematics::top_speed,
    mover_settings::MoverSettings,
};

const MOVER: u16 = 0x18;
const NOT_GROUND: u16 = 1;
const DISC_RADIUS: f32 = 0.015;
const FACE_REACH: f32 = 0.05;
const FACE_SLACK: f32 = 1.192_092_9e-5;
const RAY_NUDGE: f32 = 0.005;
const RAY_SPREAD: f32 = 0.02;
const RAY_BELOW: f32 = 0.04;
const RAY_TURN: [f32; 2] = [0.951_056_54, 0.309_016_97];
const FLAT_TINY: f32 = 1.175_494_4e-36;
const PLANE_STEEP: f32 = 1.192_092_9e-4;

pub struct GroundProbe {
    slope_cos: f64,
    slope_tan: f64,
    slope_cos32: f32,
    disc: Vec<Vec3>,
}

#[derive(Clone, Copy)]
pub struct ProbePose {
    pub feet: [f64; 3],
    pub velocity: [f64; 3],
    pub spring: f32,
    pub crouched: bool,
    pub airborne: bool,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ProbeResult {
    pub ground: Option<GroundHit>,
    pub non_ground_under: bool,
}

impl GroundProbe {
    pub fn new(base: &MoverSettings) -> Result<Self, PointSetSize> {
        let slope = base.max_slope.to_radians().clamp(0.0, 1.5707845);
        let angle =
            ((base.max_slope * f64::from(0.017_453_292_f32)) as f32).clamp(0.0, 1.570_784_4);
        let count = base.ground_circle_rays;
        if count >= u32::from(u8::MAX) {
            return Err(PointSetSize);
        }
        let mut disc = Vec::new();
        if count != 0 {
            let angle = std::f32::consts::TAU / count as f32;
            let sine = f64::from(angle).sin() as f32;
            let cosine = f64::from(angle).cos() as f32;
            let rim = base.ground_circle_radius as f32 - DISC_RADIUS;
            let (mut s, mut c) = (0.0, 1.0);
            disc.reserve(count as usize + 1);
            for _ in 0..count {
                disc.push([s * rim, DISC_RADIUS, c * rim]);
                (s, c) = (c * sine + s * cosine, c * cosine - s * sine);
            }
            disc.push([0.0; 3]);
        }
        Ok(Self {
            slope_cos: slope.cos(),
            slope_tan: slope.tan(),
            slope_cos32: f64::from(angle).cos() as f32,
            disc,
        })
    }

    pub fn probe(
        &self,
        rays: &TriangleIndex,
        world: &ContactWorld,
        settings: &MoverSettings,
        pose: ProbePose,
        dt: f64,
        category: u16,
    ) -> Result<ProbeResult, PointSetSize> {
        let [x, y, z] = pose.feet;
        let (radius, _) = capsule(settings, pose.crouched);
        let pogo = pogo(settings, pose.crouched);
        let top = y + pogo + f64::from(pose.spring) + f64::from(radius);
        let mut reach = 0.0;
        if !pose.airborne {
            let [vx, vy, vz] = pose.velocity;
            let speed = (vx * vx + vy * vy + vz * vz)
                .sqrt()
                .min(top_speed(settings, pose.crouched));
            reach = settings.step_down.max(self.slope_tan * dt * speed * 1.1);
        }
        let hit = rays.ground(x, z, top, y - reach, category);
        let mut result = ProbeResult {
            ground: None,
            non_ground_under: hit.is_some_and(|hit| hit.surface & NOT_GROUND != 0),
        };
        if settings.gravity_scale <= 0.0 {
            return Ok(result);
        }
        if hit.is_some_and(|hit| hit.normal[1] > self.slope_cos && hit.surface & NOT_GROUND == 0) {
            result.ground = hit;
            return Ok(result);
        }
        if self.disc.is_empty() {
            return Ok(result);
        }
        let lift = pogo as f32 + pose.spring;
        let body_y = (y + f64::from(lift)) as f32;
        let low_centre = radius + (f64::from(body_y) - y) as f32;
        let start = [x as f32, (y + f64::from(low_centre)) as f32, z as f32];
        let end = [x as f32, (y - f64::from(reach as f32)) as f32, z as f32];
        let Some(cast) = world.scene_sweep(MeshProbe {
            points: &self.disc,
            radius: DISC_RADIUS,
            start,
            end,
            category,
        })?
        else {
            return Ok(result);
        };
        let shape = &world.shapes()[cast.shape as usize];
        let (mut point, mut normal, mut surface) =
            (cast.hit.point, cast.hit.normal.map(f64::from), 0);
        match shape.geometry() {
            StaticGeometry::Mesh(_) => {
                let Some((face_normal, face_surface)) = self.face(shape, point, category) else {
                    return Ok(result);
                };
                normal = face_normal.map(f64::from);
                surface = face_surface;
            }
            StaticGeometry::Hull(_) => {
                let Some((ray_point, ray_normal, ray_surface)) =
                    self.rays(rays, start, point, cast.hit.normal, category)
                else {
                    return Ok(result);
                };
                point = ray_point;
                normal = ray_normal;
                surface = ray_surface;
            }
            StaticGeometry::Round { .. } => {}
        }
        result.non_ground_under |= surface & NOT_GROUND != 0;
        if surface & NOT_GROUND == 0 && normal[1] > f64::from(self.slope_cos32) {
            let y = f64::from(plane_height(
                start,
                end,
                point,
                normal.map(|lane| lane as f32),
            ));
            let [nx, ny, nz] = normal;
            result.ground = Some(GroundHit {
                y,
                normal,
                plane_offset: (nx * x + ny * y) + nz * z,
                mask: MOVER,
                surface,
            });
        }
        Ok(result)
    }

    fn face(&self, shape: &StaticShape, point: Vec3, category: u16) -> Option<(Vec3, u16)> {
        let StaticGeometry::Mesh(mesh) = shape.geometry() else {
            return None;
        };
        let up = [0.0, 1.0, 0.0];
        let (local, up) = shape.frame().map_or((point, up), |frame| {
            (
                unrotate(frame.rotation, sub(point, frame.translation)),
                unrotate(frame.rotation, up),
            )
        });
        let mut limit = (FACE_REACH * FACE_REACH) * 3.0;
        let mut found = None;
        for number in mesh.query(
            sub(local, [FACE_REACH; 3]),
            add(local, [FACE_REACH; 3]),
            category,
        ) {
            let triangle = mesh.triangle(number).expect("candidate from same mesh");
            if triangle.mask & MOVER == 0 {
                continue;
            }
            let [a, b, c] = *triangle.corners();
            let d = sub(closest_on_triangle(a, b, c, local), local);
            let distance = dot(d, d);
            let slack = if found.is_some() {
                -FACE_SLACK
            } else {
                FACE_SLACK
            };
            if distance > limit + slack {
                continue;
            }
            limit = distance;
            found = None;
            let unscaled_normal = cross(sub(b, a), sub(c, a));
            let normal = triangle.normal();
            if triangle.surface & NOT_GROUND == 0
                && dot(unscaled_normal, unscaled_normal) > TINY
                && dot(normal, up) > self.slope_cos32
            {
                found = Some((normal, triangle.surface));
            }
        }
        found
    }

    fn rays(
        &self,
        rays: &TriangleIndex,
        start: Vec3,
        point: Vec3,
        normal: Vec3,
        category: u16,
    ) -> Option<(Vec3, [f64; 3], u16)> {
        let [hx, hy, hz] = point;
        let [mut dx, mut dz] = flat_unit(hx - start[0], hz - start[2]).unwrap_or([0.0, 1.0]);
        let [nx, nz] = flat_unit(-normal[0], -normal[2])
            .unwrap_or([dx, dz])
            .map(|lane| lane * RAY_NUDGE);
        let [sine, cosine] = RAY_TURN;
        let bottom = f64::from(hy - RAY_BELOW);
        let mut found = None;
        for _ in 0..5 {
            let x = hx + (dx * RAY_SPREAD + nx);
            let z = hz + (dz * RAY_SPREAD + nz);
            if let Some(hit) = rays.ground(
                f64::from(x),
                f64::from(z),
                f64::from(start[1]),
                bottom,
                category,
            ) {
                let ground =
                    hit.surface & NOT_GROUND == 0 && hit.normal[1] > f64::from(self.slope_cos32);
                if found.is_none() || ground {
                    found = Some(([x, hit.y as f32, z], hit.normal, hit.surface));
                    if ground {
                        break;
                    }
                }
            }
            (dx, dz) = (dz * sine + dx * cosine, dz * cosine - dx * sine);
        }
        found
    }
}

pub fn pogo(settings: &MoverSettings, crouched: bool) -> f64 {
    if crouched {
        settings.crouch_pogo
    } else {
        settings.stand_pogo
    }
}

/// Radius and distance between the sphere centres at normal body scale.
pub fn capsule(settings: &MoverSettings, crouched: bool) -> (f32, f32) {
    let (radius, height) = if crouched {
        (settings.crouch_radius as f32, settings.crouch_height as f32)
    } else {
        (settings.stand_radius as f32, settings.stand_height as f32)
    };
    let r = radius.max(0.02);
    let rest = height - (pogo(settings, crouched) as f32 + (radius + radius));
    (r, (r * 0.1).max(rest))
}

fn flat_unit(x: f32, z: f32) -> Option<[f32; 2]> {
    let length = x * x + z * z;
    if length < FLAT_TINY || length.is_nan() {
        return None;
    }
    let r = (1.0 / f64::from(length).sqrt()) as f32;
    let k = (3.0 - (r * r) * length) * (0.5 * r);
    Some([x * k, z * k])
}

fn plane_height(start: Vec3, end: Vec3, point: Vec3, normal: Vec3) -> f32 {
    let direction = sub(end, start);
    let across = -dot(direction, normal);
    if across >= PLANE_STEEP {
        let r = 1.0 / across;
        let inverse = (r + r) - (r * r) * across;
        let t = inverse * (dot(normal, start) - dot(point, normal));
        if (0.0..=1.0).contains(&t) {
            return t * direction[1] + start[1];
        }
    }
    point[1]
}

fn closest_on_triangle(a: Vec3, b: Vec3, c: Vec3, p: Vec3) -> Vec3 {
    let (pa, pb, pc) = (sub(a, p), sub(b, p), sub(c, p));
    let (ab, ac, cb) = (sub(pb, pa), sub(pc, pa), sub(pc, pb));
    let (d1, d2) = (-dot(ab, pa), -dot(ac, pa));
    let (ab_b, ac_c) = (dot(ab, pb), dot(ac, pc));
    let (cb_b, cb_c) = (-dot(cb, pb), dot(cb, pc));
    if d1 <= 0.0 && d2 <= 0.0 {
        return a;
    }
    if ab_b <= 0.0 && cb_b <= 0.0 {
        return b;
    }
    if ac_c <= 0.0 && cb_c <= 0.0 {
        return c;
    }
    let n = cross(ab, ac);
    let (va, vb, vc) = (
        dot(cross(pb, pc), n),
        dot(cross(pc, pa), n),
        dot(cross(pa, pb), n),
    );
    let edge = |a, b, left, right| {
        let inverse = guarded_inverse(left + right);
        add(scale(b, inverse * left), scale(a, inverse * right))
    };
    if ab_b > 0.0 && d1 > 0.0 && vc <= 0.0 {
        return edge(a, b, d1, ab_b);
    }
    if ac_c > 0.0 && d2 > 0.0 && vb <= 0.0 {
        return edge(a, c, d2, ac_c);
    }
    if cb_c > 0.0 && cb_b > 0.0 && va <= 0.0 {
        return edge(b, c, cb_b, cb_c);
    }
    let inverse = guarded_inverse((vb + va) + vc);
    add(
        add(scale(b, inverse * vb), scale(a, inverse * va)),
        scale(c, inverse * vc),
    )
}

fn guarded_inverse(total: f32) -> f32 {
    if total.abs() > TINY {
        let r = 1.0 / total;
        (2.0 - r * total) * r
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use std::{collections::BTreeSet, fs::File, path::PathBuf, sync::Arc};

    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{
        collision_cache::{Mesh, Triangle, read_movement_cache},
        collision_index::SceneTriangle,
        collision_scene::CollisionScene,
        contact_mesh::ContactMesh,
    };

    #[test]
    fn edge_sweep_supports_walkable_faces_and_rejects_non_ground_surfaces() {
        let vertices = [[0.0, 0.0, 0.0], [0.0, 0.0, 3.0], [3.0, 0.0, 0.0]];
        let settings = MoverSettings::default();
        let probe = GroundProbe::new(&settings).unwrap();
        for (x, surface, height, under) in [
            (1.0, 0, Some(0.0), false),
            (-0.125, 0, Some(0.009994149208068848), false),
            (-0.5, 0, None, false),
            (1.0, 1, None, true),
        ] {
            let rays = TriangleIndex::new([SceneTriangle {
                vertices,
                mask: MOVER,
                surface,
            }])
            .unwrap();
            let mesh = Mesh {
                vertices: vertices
                    .map(|vertex| vertex.map(|axis| axis as f32))
                    .to_vec(),
                triangles: vec![Triangle {
                    corners: [0, 1, 2],
                    wings: [-1; 3],
                    mask: MOVER,
                    surface,
                }],
            };
            let mesh = Arc::new(ContactMesh::new(mesh, [1.0; 3], MOVER).unwrap());
            let world = ContactWorld::new(vec![
                StaticShape::new(StaticGeometry::Mesh(mesh), None).unwrap(),
            ])
            .unwrap();
            let pose = ProbePose {
                feet: [x, 0.125, 1.0],
                velocity: [0.0; 3],
                spring: 0.0,
                crouched: false,
                airborne: false,
            };
            let result = probe
                .probe(&rays, &world, &settings, pose, 0.016, MOVER)
                .unwrap();
            assert_eq!(result.ground.map(|ground| ground.y), height);
            assert_eq!(result.non_ground_under, under);
        }
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range movement cache"]
    fn practice_range_ground_probes_match_python() {
        let path = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap())
            .join("collision/0688.col");
        let cache = read_movement_cache(File::open(path).unwrap()).unwrap();
        let identifiers = BTreeSet::new();
        let scene = CollisionScene::movement_probe(&cache, &identifiers).unwrap();
        let world = ContactWorld::from_movement_cache(cache, &identifiers).unwrap();
        let rays = scene.triangles();
        let base = MoverSettings::default();
        let probe = GroundProbe::new(&base).unwrap();
        let mut digest = Sha256::new();
        let (mut hits, mut under_count) = (0, 0);
        for n in 0..1536 {
            let x = 54.0 + f64::from(n * 39 % 161 - 80) / 4.0;
            let z = 42.0 + f64::from(n * 67 % 177 - 88) / 4.0;
            let category = [8, 16, 24][(n % 3) as usize];
            let centre = rays.ground(x, z, 20.0, -8.0, category);
            let reference = rays.ground(
                x + f64::from(n % 3 - 1) * 0.25,
                z + 0.25,
                20.0,
                -8.0,
                category,
            );
            let level = if n % 2 == 0 { centre } else { reference };
            let y = level.map_or(1.0, |hit| (hit.y * 64.0 + 0.5).floor() / 64.0)
                + [-0.25, -0.046875, 0.0, 0.0625, 0.25, 0.375, 0.875][(n % 7) as usize];
            let pose = ProbePose {
                feet: [x, y, z],
                velocity: [
                    f64::from(n % 19 - 9) / 2.0,
                    f64::from(n % 13 - 6) / 2.0,
                    f64::from(n % 23 - 11) / 2.0,
                ],
                spring: [0.0, 0.0625, -0.0625, 0.125][(n % 4) as usize],
                crouched: n % 4 == 1,
                airborne: n % 5 == 1,
            };
            let dt = [0.016, 0.032, 0.064][(n / 3 % 3) as usize];
            let settings = if n % 23 == 1 {
                MoverSettings {
                    gravity_scale: 0.0,
                    ..base
                }
            } else {
                base
            };
            let result = probe
                .probe(rays, &world, &settings, pose, dt, category)
                .unwrap();
            digest.update([
                u8::from(result.ground.is_some()),
                u8::from(result.non_ground_under),
            ]);
            under_count += u32::from(result.non_ground_under);
            if let Some(ground) = result.ground {
                hits += 1;
                digest.update(ground.mask.to_le_bytes());
                digest.update(ground.surface.to_le_bytes());
                for value in std::iter::once(ground.y)
                    .chain(ground.normal)
                    .chain([ground.plane_offset])
                {
                    digest.update(((value * 1048576.0 + 0.5).floor() as i64).to_le_bytes());
                }
            }
        }
        assert_eq!((hits, under_count), (668, 78));
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "367fd3e07cef5b4e5ab3d8515db432805ba2c825a2cec293797c5cb4d653ff5c"
        );
    }
}
