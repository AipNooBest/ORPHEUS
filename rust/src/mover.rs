//! Server-side movement for an authored 104319 character capsule.

use std::{collections::BTreeMap, sync::Arc};

use crate::{
    collision_index::TriangleIndex,
    commands::Command,
    contact_body::CharacterBody,
    contact_cache::CapsulePose,
    contact_gjk::PointSetSize,
    contact_hull::HullContactError,
    contact_solver::clip_velocity,
    contact_world::ContactWorld,
    mover_ground::{GroundProbe, ProbePose, capsule, pogo},
    mover_kinematics::{Ground, MotionInput, Poses, VelocityStep, poses},
    mover_modifiers::{Modifier, ModifierKey, ModifierTimeline},
    mover_posture::{Posture, PostureStep, TOUCHING, snap},
    mover_settings::MoverSettings,
    world::{Movement, NO_INPUT},
};

const ABYSS: f64 = 100.0;
const EPSILON: f64 = 1.1754943508222875e-36;

pub struct Mover {
    base: MoverSettings,
    category: u16,
    probe: GroundProbe,
    body: CharacterBody,
    posture: Posture,
    modifiers: ModifierTimeline,
    last_command: Option<Command>,
    last_input: Option<u32>,
    pose: Movement,
}

impl Mover {
    pub fn new(
        base: MoverSettings,
        world: Arc<ContactWorld>,
        position: [f64; 3],
        yaw: i32,
        category: u16,
    ) -> Result<Self, PointSetSize> {
        let position = position.map(snap);
        let (radius, segment) = capsule(&base, false);
        let [x, y, z] = position;
        let lift = pogo(&base, false) as f32;
        let pose = CapsulePose {
            position: [x as f32, (y + f64::from(lift)) as f32, z as f32],
            radius,
            top: segment + radius,
        };
        Ok(Self {
            base,
            category,
            probe: GroundProbe::new(&base)?,
            body: CharacterBody::new(world, pose, category),
            posture: Posture::new(&base),
            modifiers: ModifierTimeline::default(),
            last_command: None,
            last_input: None,
            pose: Movement {
                position,
                yaw,
                gravity: base.gravity_scale,
                ..Movement::default()
            },
        })
    }

    pub fn pose(&self) -> Movement {
        self.pose
    }

    pub(crate) fn crouched(&self) -> bool {
        self.posture.crouched()
    }

    pub fn track_modifiers(&mut self, frame: u32, active: &BTreeMap<ModifierKey, Modifier>) {
        self.modifiers.track(frame, active);
    }

    /// Simulate the newest command and any reconstructible poses before it.
    /// The callback can advance the body's statescript at each posed frame.
    pub fn step(
        &mut self,
        rays: &TriangleIndex,
        newest: Command,
        dt: f64,
        after_pose: impl FnMut(&mut Self, Command),
    ) -> Result<(), HullContactError> {
        let mut pending = self.pending_poses(newest);
        self.step_pending(rays, &mut pending, dt, usize::MAX, after_pose)?;
        Ok(())
    }

    pub(crate) fn pending_poses(&self, newest: Command) -> Poses {
        poses(self.last_command, newest)
    }

    pub(crate) fn step_pending(
        &mut self,
        rays: &TriangleIndex,
        pending: &mut Poses,
        dt: f64,
        limit: usize,
        mut after_pose: impl FnMut(&mut Self, Command),
    ) -> Result<usize, HullContactError> {
        let mut stepped = 0;
        for command in pending.by_ref().take(limit) {
            self.step_pose(rays, command, dt)?;
            after_pose(self, command);
            stepped += 1;
        }
        if stepped != 0 && pending.len() == 0 {
            self.last_command = Some(pending.newest());
        }
        Ok(stepped)
    }

    fn step_pose(
        &mut self,
        rays: &TriangleIndex,
        command: Command,
        dt: f64,
    ) -> Result<(), HullContactError> {
        let active = self.modifiers.at(command.frame);
        let settings = active.settings(&self.base);
        let input = MotionInput::from(&command);
        let probe = self.probe.probe(
            rays,
            self.body.world(),
            &settings,
            ProbePose {
                feet: self.pose.position,
                velocity: self.pose.velocity,
                spring: self.posture.spring_offset(),
                crouched: self.posture.crouched(),
                airborne: self.posture.ground() != Ground::Standing,
            },
            dt,
            self.category,
        )?;
        self.posture.advance(
            PostureStep {
                command: &command,
                base: &self.base,
                values: &settings,
                mods: &active,
                probe,
                dt,
                pitch_held: input.pitch_held,
            },
            &mut self.pose.position,
            &mut self.pose.velocity,
            |radius, top| self.body.try_resize(radius, top),
        )?;
        let ground = self.posture.ground();
        let velocity = VelocityStep {
            base: &self.base,
            settings: &settings,
            input,
            ground,
            crouched: self.posture.crouched(),
            dt,
        }
        .advance(self.pose.velocity);
        if ground != Ground::Standing {
            self.posture.apply_fall(settings.gravity_scale, dt);
        }
        let normal = probe.ground.map_or([0.0, 1.0, 0.0], |hit| hit.normal);
        let vertical = self.move_body(rays, velocity, normal, ground == Ground::Standing, dt)?;
        self.posture.finish_fall(settings.gravity_scale, vertical);

        if input.throttles != [0; 2] {
            self.last_input = Some(command.frame);
            self.pose.input_throttles = input.throttles;
        }
        self.pose.frame = Some(command.frame);
        self.pose.input_frame = match self.last_input {
            Some(frame) if frame == command.frame => None,
            frame => Some(frame.unwrap_or(NO_INPUT)),
        };
        self.pose.throttles = input.throttles;
        self.pose.yaw = input.yaw;
        self.pose.pitch = i32::from(input.pitch);
        self.pose.flags = self.posture.flags;
        self.pose.air_ticks = self.posture.air_ticks;
        self.pose.crouch_frame = self.posture.crouch_end;
        self.pose.gravity = settings.gravity_scale;
        self.pose.spring = self.posture.spring_steps().map(i32::from);
        self.pose.spring_speed = f64::from(self.posture.spring_speed());
        self.pose.fall = self.posture.fall;
        Ok(())
    }

    fn move_body(
        &mut self,
        rays: &TriangleIndex,
        velocity: [f64; 3],
        normal: [f64; 3],
        grounded: bool,
        dt: f64,
    ) -> Result<f64, HullContactError> {
        let [x, y, z] = velocity;
        let velocity = if grounded && (x * x + y * y) + z * z >= EPSILON {
            along_ground([x, z], normal)
        } else {
            velocity
        };
        let [x, y, z] = velocity.map(|axis| axis as f32);
        let dt = dt as f32;
        let rise = self.posture.spring_speed();
        let displacement = [
            ((0.0 * rise + x) * dt) + 0.0,
            ((rise + y) * dt) + 0.0,
            ((0.0 * rise + z) * dt) + 0.0,
        ];
        let movement = self.body.move_capsule(displacement, dt)?;
        let [mut x, mut y, mut z] = clip_velocity([x, y, z], movement.planes).map(f64::from);
        if grounded {
            let squared = (x * x + y * y) + z * z;
            let flat = x * x + z * z;
            if squared >= EPSILON && flat >= EPSILON {
                let factor = (squared / flat).sqrt();
                (x, y, z) = (x * factor, 0.0, z * factor);
            }
        }
        self.posture.flags =
            (self.posture.flags & !TOUCHING) | (u32::from(movement.touching) * TOUCHING);
        let lift = pogo(&self.base, self.posture.crouched()) as f32 + self.posture.spring_offset();
        let [px, py, pz] = movement.position;
        let feet = f64::from(py - lift);
        let floor = rays.lowest() - ABYSS;
        self.pose.position = [f64::from(px), feet.max(floor), f64::from(pz)].map(snap);
        let [px, py, pz] = self.pose.position;
        self.body
            .teleport([px as f32, (py + f64::from(lift)) as f32, pz as f32]);
        if feet < floor {
            y = 0.0;
        }
        self.pose.velocity = [x, y, z].map(snap);
        Ok(y)
    }
}

fn along_ground([x, z]: [f64; 2], [nx, ny, nz]: [f64; 3]) -> [f64; 3] {
    let speed = (x * x + z * z).sqrt();
    let along = x * nx + z * nz;
    let projected = [x - nx * along, -ny * along, z - nz * along];
    let (px, pz) = (-z, x);
    let length = (px * px + pz * pz).sqrt();
    if length * length < EPSILON || speed <= 0.0 {
        return projected;
    }
    let (px, pz) = (px / length, pz / length);
    let direction = [-pz * ny, pz * nx - px * nz, px * ny];
    let [dx, dy, dz] = direction;
    let norm = (dx * dx + dy * dy + dz * dz).sqrt();
    if norm <= 0.0 {
        return projected;
    }
    let sign = if (dx * projected[0] + dy * projected[1]) + dz * projected[2] < 0.0 {
        -norm
    } else {
        norm
    };
    direction.map(|axis| axis / sign * speed)
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeSet,
        fs::{self, File},
        path::PathBuf,
    };

    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{
        collision_cache::read_movement_cache,
        collision_scene::CollisionScene,
        commands::{CROUCH, JUMP},
        mover_modifiers::Operation,
        mover_settings::MoverCatalog,
        world::movement_record,
    };

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range movement cache"]
    fn practice_range_player_movement_records_match_python() {
        let runtime = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let cache =
            read_movement_cache(File::open(runtime.join("collision/0688.col")).unwrap()).unwrap();
        let ids = BTreeSet::new();
        let scene = CollisionScene::movement_probe(&cache, &ids).unwrap();
        let world = Arc::new(ContactWorld::from_movement_cache(cache, &ids).unwrap());
        let catalog_path = runtime.join("data/game_movers_174.json");
        let settings = MoverCatalog::load(&catalog_path).unwrap();
        let records: BTreeMap<String, serde_json::Value> =
            serde_json::from_slice(&fs::read(catalog_path).unwrap()).unwrap();
        assert_eq!(records.len(), 38);
        let sprint = BTreeMap::from([(
            ModifierKey {
                instance: 1,
                state: 1,
                slot: 0,
            },
            Modifier {
                index: 0,
                operation: Operation::Multiply,
                value: 1.5,
                priority: 0.0,
            },
        )]);
        let released = BTreeMap::new();
        let mut combined = Sha256::new();
        for (i, guid) in records.keys().enumerate() {
            let body = crate::accounts::parse_guid(guid).unwrap();
            let spawn = if i % 3 == 2 {
                [45.5, 2.5, 34.75]
            } else {
                [54.56507, 1.0, 42.14788]
            };
            let category = [0x08, 0x10, 0x18][i % 3];
            let mut mover = Mover::new(
                *settings.for_body(Some(body)),
                Arc::clone(&world),
                spawn,
                -31010,
                category,
            )
            .unwrap();
            let first = 4000 + i as u32 * 500;
            let mut digest = Sha256::new();
            let mut visited = 0;
            for n in 0..304 {
                if (20..24).contains(&n) || (80..118).contains(&n) || (210..278).contains(&n) {
                    continue;
                }
                let jump = (24..29).contains(&n) || (138..153).contains(&n);
                let crouch = (45..75).contains(&n) || (121..136).contains(&n);
                let command = Command {
                    frame: first + n,
                    forward: [127, -127, 58, 0][(n as usize / 16) % 4],
                    right: [0, 0, -127, 111][(n as usize / 20) % 4],
                    yaw: (n as i32 / 12) * 4096 - 31010,
                    pitch: (n as i32 % 28) * 2500 - 32000,
                    buttons: (u32::from(jump) * JUMP) | (u32::from(crouch) * CROUCH),
                    ..Command::default()
                };
                mover
                    .step(scene.triangles(), command, 0.016, |mover, pose| {
                        visited += 1;
                        let record = movement_record(&mover.pose(), Some(pose.frame + 2)).unwrap();
                        digest.update((record.bit_len() as u32).to_le_bytes());
                        digest.update(record.as_bytes());
                        let active = if (12..35).contains(&((pose.frame - first) % 72)) {
                            &sprint
                        } else {
                            &released
                        };
                        mover.track_modifiers(pose.frame, active);
                    })
                    .unwrap();
            }
            assert_eq!(visited, 236);
            combined.update(body.to_le_bytes());
            combined.update(digest.finalize());
        }
        assert_eq!(
            format!("{:x}", combined.finalize()),
            "e2fcdb617ec2f5429ea6c54616de11ac20352e46801f0c9f51664818a13b44cf"
        );
    }
}
