//! Server-side straight projectile flight and splash for build 104319.
//!
//! Uses the swept target capsules and map ray from the match. Damage and
//! network delivery are separate so a predicted alias never hits twice.
//! The reference is ow174/game/combat.py.

use serde_json::Value as Json;

use crate::{
    collision_index::IndexError,
    game_clock::TICK_PERIOD,
    game_hitscan::{HitDefinition, Impact, TargetPose},
    game_projectiles::authored_number,
    script_expr::EvalError,
    script_graph::Node,
    script_wire::Value,
    static_ray::{Vec3, capsule_entry_distance, segment_distance},
};

const DEFAULT_LIFETIME: f64 = 5.0;
const RING_EPSILON: f64 = 1e-9;

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectileBallistics {
    pub speed: f64,
    pub lifetime: f64,
    pub definition: Option<u64>,
    pub direct: Option<HitDefinition>,
    pub blast: Option<Blast>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Blast {
    pub hit: HitDefinition,
    pub rings: Vec<(f64, f64)>,
}

impl ProjectileBallistics {
    pub fn from_authored(
        node: &Node,
        mut evaluate: impl FnMut(&Json) -> Result<Value, EvalError>,
    ) -> Result<Option<Self>, EvalError> {
        let speed = authored_number(node.field("m_projectileSpeed"), 0.0, &mut evaluate)?;
        if !speed.is_finite() || speed <= 0.0 {
            return Ok(None);
        }
        let mut lifetime = authored_number(
            node.field("m_projectileLifetime"),
            DEFAULT_LIFETIME,
            &mut evaluate,
        )?;
        if lifetime == 0.0 {
            lifetime = DEFAULT_LIFETIME;
        }
        let reach = authored_number(node.field("m_FEC435C6"), 0.0, &mut evaluate)?;
        if reach != 0.0 {
            lifetime = lifetime.min(reach / speed);
        }
        if !lifetime.is_finite() || lifetime <= 0.0 {
            return Ok(None);
        }
        let definition = match node.field("m_projectileEntity.m_entityDef") {
            Some(config) => match evaluate(config)? {
                Value::Asset(definition) => Some(definition),
                _ => None,
            },
            None => None,
        };
        let direct = HitDefinition::from_authored(
            node.field("m_6396149F").unwrap_or(&Json::Null),
            &mut evaluate,
        )?;
        let config = node.field("m_D2D11CE9").unwrap_or(&Json::Null);
        let blast = if let Some(hit) = HitDefinition::from_authored(config, &mut evaluate)? {
            let mut rings = Vec::new();
            if let Some(rows) = config.get("m_0D30E40C").and_then(Json::as_array) {
                for row in rows.iter().filter(|row| row.is_object()) {
                    let radius = authored_number(row.get("m_radius"), 0.0, &mut evaluate)?;
                    let scalar = authored_number(row.get("m_B399CACB"), 0.0, &mut evaluate)?;
                    if radius.is_finite() && scalar.is_finite() {
                        rings.push((radius, scalar));
                    }
                }
            }
            rings.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.total_cmp(&b.1)));
            Some(Blast { hit, rings })
        } else {
            None
        };
        Ok(Some(Self {
            speed,
            lifetime,
            definition,
            direct,
            blast,
        }))
    }
}

impl Blast {
    fn multiplier(&self, distance: f64) -> f64 {
        let Some(&(mut previous, mut scalar)) = self.rings.first() else {
            return 0.0;
        };
        if distance <= previous {
            return scalar;
        }
        for &(radius, next) in &self.rings[1..] {
            if distance <= radius {
                let share = (distance - previous) / (radius - previous).max(RING_EPSILON);
                return scalar + (next - scalar) * share;
            }
            previous = radius;
            scalar = next;
        }
        0.0
    }
}

pub struct ProjectileFlight {
    ballistics: ProjectileBallistics,
    position: Vec3,
    direction: Vec3,
    remaining: f64,
    travelled: f64,
    last_tick: Option<u32>,
    ended: bool,
}

#[derive(Debug, PartialEq)]
pub struct FlightEnd {
    pub point: Vec3,
    pub travelled: f64,
    pub impacts: Vec<Impact>,
}

impl ProjectileFlight {
    pub fn new(
        ballistics: ProjectileBallistics,
        position: Vec3,
        direction: Vec3,
        fired_at: Option<u32>,
    ) -> Option<Self> {
        if !ballistics.speed.is_finite()
            || ballistics.speed <= 0.0
            || !ballistics.lifetime.is_finite()
            || ballistics.lifetime <= 0.0
            || !(ballistics.speed * ballistics.lifetime).is_finite()
            || !position.into_iter().chain(direction).all(f64::is_finite)
            || (direction.iter().map(|value| value * value).sum::<f64>() - 1.0).abs() > 1e-6
        {
            return None;
        }
        Some(Self {
            remaining: ballistics.lifetime,
            ballistics,
            position,
            direction,
            travelled: 0.0,
            last_tick: fired_at,
            ended: false,
        })
    }

    /// Advance the elapsed portion of a tick, even when it ends inside the
    /// tick due to range or lifetime. Static geometry wins only a closer hit.
    pub fn advance(
        &mut self,
        tick: u32,
        targets: &[(u32, TargetPose)],
        wall: impl FnOnce(Vec3, Vec3, f64) -> Result<Option<f64>, IndexError>,
    ) -> Result<Option<FlightEnd>, IndexError> {
        if self.ended {
            return Ok(None);
        }
        let elapsed = self.last_tick.map_or(1, |last| tick.saturating_sub(last));
        if elapsed == 0 {
            return Ok(None);
        }
        let duration = (f64::from(elapsed) * TICK_PERIOD.as_secs_f64()).min(self.remaining);
        let reach = self.ballistics.speed * duration;
        let mut closest = None;
        for &(entity, target) in targets {
            let [a, b] = target.segment();
            if let Some(distance) =
                capsule_entry_distance(self.position, self.direction, reach, a, b, target.radius)
            {
                if closest.is_none_or(|(best, _)| distance < best) {
                    closest = Some((distance, Some(entity)));
                }
            }
        }
        if let Some(distance) = wall(self.position, self.direction, reach)?
            .filter(|distance| (0.0..=reach).contains(distance))
        {
            if closest.is_none_or(|(best, _)| distance < best) {
                closest = Some((distance, None));
            }
        }
        self.last_tick = Some(tick);
        let Some((distance, target)) = closest else {
            self.move_forward(reach);
            self.travelled += duration;
            self.remaining -= duration;
            return Ok((self.remaining <= 0.0).then(|| self.finish(Vec::new())));
        };
        self.move_forward(distance);
        self.travelled += distance / self.ballistics.speed;
        let mut impacts = Vec::new();
        if let (Some(entity), Some(direct)) = (
            target,
            self.ballistics.direct.filter(|hit| hit.amount > 0.0),
        ) {
            impacts.push(Impact {
                entity,
                distance,
                damage: direct.amount,
                critical: false,
                loadout: direct.loadout,
            });
        }
        if let Some(blast) = self
            .ballistics
            .blast
            .as_ref()
            .filter(|blast| blast.hit.amount > 0.0)
        {
            for &(entity, pose) in targets {
                let [a, b] = pose.segment();
                let distance = (segment_distance(self.position, a, b) - pose.radius).max(0.0);
                let damage = blast.hit.amount * blast.multiplier(distance);
                if damage > 0.0 {
                    impacts.push(Impact {
                        entity,
                        distance,
                        damage,
                        critical: false,
                        loadout: blast.hit.loadout,
                    });
                }
            }
        }
        Ok(Some(self.finish(impacts)))
    }

    fn move_forward(&mut self, distance: f64) {
        for (position, direction) in self.position.iter_mut().zip(self.direction) {
            *position += direction * distance;
        }
    }

    fn finish(&mut self, impacts: Vec<Impact>) -> FlightEnd {
        self.ended = true;
        FlightEnd {
            point: self.position,
            travelled: self.travelled,
            impacts,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collision_index::{SceneTriangle, TriangleIndex};

    const LOADOUT: u64 = 0x0b90_0000_0000_008e;

    fn helix() -> ProjectileBallistics {
        let hit = HitDefinition {
            amount: 40.0,
            loadout: Some(LOADOUT),
            critical: 1.0,
            falloff: None,
        };
        ProjectileBallistics {
            speed: 50.0,
            lifetime: 2.0,
            definition: Some(0x0400_0000_0000_072c),
            direct: Some(hit),
            blast: Some(Blast {
                hit: HitDefinition {
                    amount: 80.0,
                    ..hit
                },
                rings: vec![(1.0, 1.0), (3.0, 0.5)],
            }),
        }
    }

    fn target(x: f64, z: f64) -> TargetPose {
        TargetPose {
            feet: [x, 0.0, z],
            height: 2.0,
            radius: 0.5,
            eye: 1.55,
        }
    }

    #[test]
    fn helix_sweeps_missed_ticks_and_applies_direct_damage_before_splash() {
        let targets = [
            (1, target(0.0, 3.0)),
            (2, target(2.5, 2.5)),
            (3, target(4.0, 2.5)),
        ];
        let mut flight =
            ProjectileFlight::new(helix(), [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], None).unwrap();
        assert!(
            flight
                .advance(1000, &targets, |_, _, _| Ok(None))
                .unwrap()
                .is_none()
        );
        assert!(
            flight
                .advance(1000, &targets, |_, _, _| panic!("duplicate tick"))
                .unwrap()
                .is_none()
        );
        let end = flight
            .advance(1004, &targets, |_, _, _| Ok(None))
            .unwrap()
            .unwrap();
        assert_eq!(end.point, [0.0, 1.0, 2.5]);
        assert!((end.travelled - 0.05).abs() < 1e-12);
        assert_eq!(
            end.impacts
                .iter()
                .map(|hit| (hit.entity, hit.damage, hit.critical))
                .collect::<Vec<_>>(),
            [(1, 40.0, false), (1, 80.0, false), (2, 60.0, false)]
        );
        assert!(
            end.impacts
                .iter()
                .all(|impact| impact.loadout == Some(LOADOUT))
        );
        assert!(
            flight
                .advance(1005, &targets, |_, _, _| panic!("already ended"))
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn walls_win_closer_impact_and_last_partial_tick_is_swept() {
        let world = TriangleIndex::new([SceneTriangle {
            vertices: [[-5.0, 0.0, 1.0], [5.0, 0.0, 1.0], [0.0, 5.0, 1.0]],
            mask: 0x18,
            surface: 0,
        }])
        .unwrap();
        let mut flight =
            ProjectileFlight::new(helix(), [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], Some(1000)).unwrap();
        let end = flight
            .advance(
                1002,
                &[(1, target(0.0, 3.0))],
                |origin, direction, reach| {
                    world
                        .cast_ray(origin, direction, reach, 0x18)
                        .map(|hit| hit.map(|hit| hit.distance))
                },
            )
            .unwrap()
            .unwrap();
        assert_eq!(end.point, [0.0, 1.0, 1.0]);
        assert_eq!(end.impacts.len(), 1); // splash reaches the capsule beyond the wall
        assert_eq!(end.impacts[0].damage, 70.0);
        let short = ProjectileBallistics {
            lifetime: 0.01,
            ..helix()
        };
        let mut flight =
            ProjectileFlight::new(short.clone(), [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], None).unwrap();
        let end = flight
            .advance(1000, &[(1, target(0.0, 1.0))], |_, _, _| Ok(None))
            .unwrap()
            .unwrap();
        assert_eq!((end.point[2], end.travelled), (0.5, 0.01));
        assert_eq!(end.impacts[0].damage, 40.0);
        let mut flight =
            ProjectileFlight::new(short, [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], None).unwrap();
        let end = flight
            .advance(1000, &[(1, target(1.0, 1.0))], |_, _, _| Ok(None))
            .unwrap()
            .unwrap();
        assert_eq!((end.point[2], end.travelled), (0.5, 0.01));
        assert!(end.impacts.is_empty());
    }
}
