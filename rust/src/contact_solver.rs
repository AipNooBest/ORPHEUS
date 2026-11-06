//! Float32 position and velocity projection for the character capsule.
//!
//! Uses the bounded sequential-impulse solve and the one/two-plane velocity
//! cases in `ow174/game/contacts.py`.

use std::cmp::Ordering;

use crate::{
    contact_math::{TINY, Vec3, add, dot, maxps, minps, normalize, scale, sub},
    contact_triangle::TOUCH,
};

const GS_PASSES: u32 = 200;
const CLIP_PARALLEL: f32 = 0.998_782_04;
const CLIP_QP_TOLERANCE: f32 = 3.046_174e-8;
const QUICK_PLANES: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ContactPlane {
    pub normal: Vec3,
    pub offset: f32,
}

pub(crate) struct Constraint {
    pub(crate) plane: ContactPlane,
    pub(crate) impulse: f32,
}

impl From<ContactPlane> for Constraint {
    fn from(plane: ContactPlane) -> Self {
        Self {
            plane,
            impulse: 0.0,
        }
    }
}

pub(crate) fn project_target(target: Vec3, constraints: &mut [Constraint]) -> (Vec3, u32) {
    let mut position = target;
    if constraints.is_empty() {
        return (position, 0);
    }
    for pass in 0..GS_PASSES {
        let mut worst = 0.0;
        for constraint in &mut *constraints {
            let ContactPlane { normal, offset } = constraint.plane;
            let gap = dot(normal, position) - offset;
            let value = minps(-(gap + TOUCH) + constraint.impulse, f32::MAX);
            let impulse = maxps(0.0, value);
            let delta = impulse - constraint.impulse;
            constraint.impulse = impulse;
            position = add(position, scale(normal, delta));
            worst = maxps(worst, maxps(0.0 - delta, delta));
        }
        if worst < TOUCH {
            return (position, pass);
        }
    }
    (position, GS_PASSES)
}

pub fn clip_velocity(velocity: Vec3, planes: &[ContactPlane]) -> Vec3 {
    match planes {
        [] => velocity,
        [plane] => {
            let normal = plane.normal;
            sub(velocity, scale(normal, minps(dot(normal, velocity), 0.0)))
        }
        _ if planes.len() <= QUICK_PLANES => {
            clip_iterative(velocity, planes).unwrap_or_else(|| {
                let mut impulses = [0.0; QUICK_PLANES];
                clip_qp(velocity, planes, &mut impulses[..planes.len()])
            })
        }
        _ => clip_qp(velocity, planes, &mut vec![0.0; planes.len()]),
    }
}

fn clip_qp(velocity: Vec3, planes: &[ContactPlane], impulses: &mut [f32]) -> Vec3 {
    let tolerance = dot(velocity, velocity) * CLIP_QP_TOLERANCE;
    let mut out = velocity;
    for _ in 0..64 {
        let mut worst = 0.0;
        for (plane, impulse) in planes.iter().zip(&mut *impulses) {
            let new = maxps(*impulse - dot(plane.normal, out), 0.0);
            let delta = new - *impulse;
            *impulse = new;
            out = add(scale(plane.normal, delta), out);
            worst = maxps(worst, delta * delta);
        }
        if worst < tolerance {
            break;
        }
    }
    out
}

fn clip_iterative(velocity: Vec3, planes: &[ContactPlane]) -> Option<Vec3> {
    if dot(velocity, velocity) < TINY {
        return Some(velocity);
    }
    let unit = normalize(velocity);
    let mut moved = false;
    for (index, plane) in planes.iter().enumerate() {
        let n = plane.normal;
        if dot(n, unit) < 0.0 {
            moved = true;
            let result = sub(velocity, scale(n, dot(velocity, n)));
            if clear(result, planes, index, planes.len()) {
                return Some(result);
            }
        }
    }
    if !moved {
        return Some(velocity);
    }
    for (i, first) in planes.iter().enumerate() {
        let ni = first.normal;
        let di = dot(velocity, ni);
        if di >= 0.0 {
            continue;
        }
        for (j, second) in planes.iter().enumerate().skip(i + 1) {
            let nj = second.normal;
            let dj = dot(velocity, nj);
            if dj >= 0.0 {
                continue;
            }
            let cosine = dot(ni, nj);
            let squared = cosine * cosine;
            if squared > CLIP_PARALLEL {
                continue;
            }
            let inverse = 1.0 / (1.0 - squared);
            let a = (cosine * dj - di) * inverse;
            let b = (cosine * di - dj) * inverse;
            if a < 0.0 || b < 0.0 {
                continue;
            }
            let result = add(add(scale(ni, a), velocity), scale(nj, b));
            if clear(result, planes, i, j) {
                return Some(result);
            }
        }
    }
    None
}

fn clear(velocity: Vec3, planes: &[ContactPlane], first: usize, second: usize) -> bool {
    planes.iter().enumerate().all(|(index, plane)| {
        index == first
            || index == second
            || dot(plane.normal, velocity).partial_cmp(&0.0) != Some(Ordering::Less)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clips_against_one_two_and_many_planes_in_order() {
        let planes =
            [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]].map(|normal| ContactPlane {
                normal,
                offset: 0.0,
            });
        let velocity = [-3.0, -2.0, -1.0];
        assert_eq!(clip_velocity(velocity, &planes[..1]), [0.0, -2.0, -1.0]);
        assert_eq!(clip_velocity(velocity, &planes[..2]), [0.0, 0.0, -1.0]);
        assert_eq!(clip_velocity(velocity, &planes), [0.0; 3]);
        let many = planes.repeat(3);
        assert_eq!(clip_velocity(velocity, &many), [0.0; 3]);
        assert_eq!(clip_velocity([1.0, 2.0, 3.0], &many), [1.0, 2.0, 3.0]);
    }
}
