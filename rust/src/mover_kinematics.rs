//! Player command recovery and velocity before contact resolution.
//!
//! The PC path in `ow174/game/mover.py` runs friction, steering, the direction
//! cap and gravity in this order after its ground and stance transitions.

use std::f64::consts::TAU;

use crate::{
    commands::{Command, SUBFRAME, frame_distance},
    mover_modifiers::CAP_TINY,
    mover_settings::MoverSettings,
};

const EPSILON: f64 = 1.1754943508222875e-36;
const TINY: f64 = 1.1920929e-7;
pub const LAND_SPEED: f64 = 0.004;
pub const WORLD_GRAVITY: f64 = 10.0;
const TURN: f64 = TAU / 65536.0;
const PITCH_RADIANS: f32 = 0.000_095_873_8;
const PITCH_LIMIT: f32 = 1.553_343;
const PITCH_UNITS: f32 = 10_430.378;
const THROTTLE_UNIT: f32 = 1.0 / 127.0;
const HOLD_INPUT_FRAMES: u32 = 32;

/// Reconstruct short gaps before the newest command. Long gaps start at
/// the newest command. One-shot actions and subframe aim never repeat.
pub fn poses(previous: Option<Command>, newest: Command) -> Poses {
    let (previous, distance) = previous
        .map(|command| (command, frame_distance(newest.frame, command.frame)))
        .filter(|&(_, distance)| (2..=2 * HOLD_INPUT_FRAMES).contains(&distance))
        .unwrap_or((newest, 1));
    Poses {
        previous,
        newest,
        offsets: 1..distance + 1,
    }
}

/// Resumable sequence of reconstructed poses followed by the actual command.
pub struct Poses {
    previous: Command,
    newest: Command,
    offsets: std::ops::Range<u32>,
}

impl Poses {
    pub(crate) fn newest(&self) -> Command {
        self.newest
    }

    pub(crate) fn next_frame(&self) -> Option<u32> {
        let behind = self.offsets.len().checked_sub(1)? as u32;
        Some(self.newest.frame.wrapping_sub(behind))
    }
}

impl Iterator for Poses {
    type Item = Command;

    fn next(&mut self) -> Option<Command> {
        let offset = self.offsets.next()?;
        if self.offsets.is_empty() {
            return Some(self.newest);
        }
        let previous = self.previous;
        let source = if offset <= HOLD_INPUT_FRAMES {
            previous
        } else {
            Command {
                yaw: previous.yaw,
                pitch: previous.pitch,
                ..Command::default()
            }
        };
        Some(Command {
            frame: previous.frame.wrapping_add(offset),
            buttons: source.buttons & !SUBFRAME,
            action: 0,
            click: None,
            ..source
        })
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        self.offsets.size_hint()
    }
}

impl ExactSizeIterator for Poses {}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MotionInput {
    pub yaw: i32,
    pub pitch: i16,
    pub pitch_held: bool,
    pub throttles: [i8; 2],
    right: f64,
    forward: f64,
}

impl From<&Command> for MotionInput {
    fn from(command: &Command) -> Self {
        let (right, right_byte) = throttle(command.right);
        let (forward, forward_byte) = throttle(command.forward);
        let pitch = command.pitch as f32 * PITCH_RADIANS;
        let held = pitch.clamp(-PITCH_LIMIT, PITCH_LIMIT);
        let scaled = held * PITCH_UNITS;
        let rounded = if scaled < 0.0 {
            scaled - 0.5
        } else {
            scaled + 0.5
        };
        Self {
            yaw: command.yaw,
            pitch: rounded as i16,
            pitch_held: pitch != held,
            throttles: [right_byte, forward_byte],
            right,
            forward,
        }
    }
}

fn throttle(byte: i8) -> (f64, i8) {
    let throttle = f32::from(byte.max(-127)) * THROTTLE_UNIT;
    (f64::from(throttle), (throttle * 127.0) as i8)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Ground {
    Standing,
    Airborne,
    AirborneTouching,
}

#[derive(Clone, Copy)]
struct Speeds {
    forward: f64,
    backward: f64,
    strafe: f64,
}

impl Speeds {
    fn new(settings: &MoverSettings, crouched: bool) -> Self {
        if crouched {
            Self {
                forward: settings.crouch_forward,
                backward: settings.crouch_backward,
                strafe: settings.crouch_strafe,
            }
        } else {
            Self {
                forward: settings.run_forward,
                backward: settings.run_backward,
                strafe: settings.run_strafe,
            }
        }
    }

    fn directional_cap(self, [dx, dz]: [f64; 2], length: f64, [fx, fz]: [f64; 2]) -> f64 {
        if length < CAP_TINY {
            return self.strafe;
        }
        let cosine = (fx * dx + fz * dz) / length;
        if cosine > 0.0 {
            self.strafe + cosine * (self.forward - self.strafe)
        } else {
            self.strafe - cosine * (self.backward - self.strafe)
        }
    }

    fn wish(self, input: MotionInput, [fx, fz]: [f64; 2]) -> (f64, f64, f64) {
        let ahead = if input.forward > 0.0 {
            self.forward
        } else {
            self.backward
        };
        let x = input.right * self.strafe * -fz + input.forward * ahead * fx;
        let z = input.right * self.strafe * fx + input.forward * ahead * fz;
        let speed = length(x, z);
        if speed <= 0.0 {
            return (0.0, 0.0, 0.0);
        }
        let (x, z) = (x / speed, z / speed);
        (x, z, speed.min(self.directional_cap([x, z], 1.0, [fx, fz])))
    }
}

pub fn top_speed(settings: &MoverSettings, crouched: bool) -> f64 {
    let speeds = Speeds::new(settings, crouched);
    speeds.forward.max(speeds.backward).max(speeds.strafe)
}

pub struct VelocityStep<'a> {
    pub base: &'a MoverSettings,
    pub settings: &'a MoverSettings,
    pub input: MotionInput,
    pub ground: Ground,
    pub crouched: bool,
    pub dt: f64,
}

impl VelocityStep<'_> {
    /// Advance the unconstrained velocity, in m/s. The caller resolves
    /// world contacts and quantizes the final motion after this step.
    pub fn advance(&self, previous: [f64; 3]) -> [f64; 3] {
        let angle = f64::from(self.input.yaw) * TURN;
        let axes = [angle.sin(), angle.cos()];
        let speeds = Speeds::new(self.settings, self.crouched);
        let velocity = self.friction(previous);
        let wish = speeds.wish(self.input, axes);
        let [mut x, mut y, mut z] = self.steer(velocity, wish);
        let squared = x * x + z * z;
        let speed = if squared > EPSILON {
            squared.sqrt()
        } else {
            0.0
        };
        let cap = speeds.directional_cap([x, z], speed, axes);
        let allowed = speed.min(cap.max(length(previous[0], previous[2])));
        if (speed - allowed).abs() > LAND_SPEED && squared >= EPSILON {
            x = x / speed * allowed;
            z = z / speed * allowed;
        }
        if self.ground != Ground::Standing {
            y -= WORLD_GRAVITY * self.settings.gravity_scale * self.dt;
        }
        [x, y, z]
    }

    fn friction(&self, [mut x, mut y, mut z]: [f64; 3]) -> [f64; 3] {
        let data = self.settings;
        let grounded = self.ground == Ground::Standing;
        let (base, base_speed, per_speed) = if grounded {
            (
                data.decel_ground_base,
                data.decel_ground_base_speed,
                data.decel_ground_per_speed,
            )
        } else {
            (
                data.decel_air_base,
                data.decel_air_base_speed,
                data.decel_air_per_speed,
            )
        };
        let speed = length(x, z);
        let rate = base * data.run_forward
            + (speed - base_speed).max(0.0) * per_speed * self.base.run_forward;
        let slow = speed.min(self.dt * rate);
        if speed * speed > EPSILON && slow > 0.0 {
            x -= x / speed * slow;
            z -= z / speed * slow;
        }
        if !grounded {
            let terminal = if y > 0.0 {
                data.terminal_velocity_up
            } else {
                data.terminal_velocity
            };
            if terminal > CAP_TINY {
                let drag = -WORLD_GRAVITY * data.gravity_scale * y.abs() / (terminal * terminal)
                    * y
                    * self.dt;
                let length = (x * x + y * y + z * z).sqrt();
                y += drag.abs().min(length).copysign(drag);
            }
        }
        [x, y, z]
    }

    fn steer(&self, [x, y, z]: [f64; 3], (wx, wz, wish): (f64, f64, f64)) -> [f64; 3] {
        let data = self.settings;
        let rate = match self.ground {
            Ground::Standing => data.accel_ground,
            Ground::Airborne => data.accel_air,
            Ground::AirborneTouching => data.accel_air_touching,
        } * data.run_forward;
        let squared = x * x + z * z;
        let speed = if squared >= EPSILON {
            squared.sqrt()
        } else {
            -1.0
        };
        let target = if wish > 0.0 { speed.max(wish) } else { wish };
        let (cx, cz) = if speed > 0.0 { (x, z) } else { (-x, -z) };
        let (dx, dz) = (target * wx - cx, target * wz - cz);
        let delta = length(dx, dz);
        if delta * delta <= EPSILON || target <= 0.0 {
            return [x, y, z];
        }
        let k = (rate * self.dt / delta).clamp(0.0, 1.0);
        let (mut nx, mut nz) = (cx + k * dx, cz + k * dz);
        if speed > TINY && wx * cx + wz * cz > 0.0 {
            let desired = speed + (rate * self.dt).min(target - speed);
            let norm = length(nx, nz);
            if norm > 0.0 {
                nx = nx / norm * desired;
                nz = nz / norm * desired;
            }
        }
        [x + (nx - cx), y, z + (nz - cz)]
    }
}

fn length(x: f64, z: f64) -> f64 {
    (x * x + z * z).sqrt()
}

#[cfg(test)]
mod tests {
    use std::{collections::BTreeMap, fs, path::PathBuf};

    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{
        commands::{Click, FIRE},
        mover_modifiers::{
            CROUCH_INTENT, JUMP_INTENT, LAND_LIMIT, Modifier, ModifierKey, ModifierTimeline,
            Operation,
        },
        mover_settings::MoverCatalog,
    };

    #[test]
    fn small_input_gaps_repeat_held_inputs_then_neutral_and_keep_newest() {
        let last = Command {
            frame: u32::MAX - 30,
            forward: 127,
            right: -9,
            yaw: 32781,
            pitch: -15,
            buttons: FIRE | SUBFRAME,
            action: 3,
            extra: 70,
            click: Some(Click {
                subframe: 15,
                yaw: 33,
                pitch: 12,
            }),
        };
        let command = Command {
            frame: 33,
            action: 5,
            ..last
        };
        let recovered: Vec<_> = poses(Some(last), command).collect();
        assert_eq!(recovered.len(), 64);
        assert_eq!(recovered[29].frame, u32::MAX);
        assert_eq!(recovered[30].frame, 0);
        assert_eq!((recovered[31].buttons, recovered[31].extra), (FIRE, 70));
        assert_eq!((recovered[31].action, recovered[31].click), (0, None));
        assert_eq!(
            recovered[32],
            Command {
                frame: 2,
                yaw: last.yaw,
                pitch: last.pitch,
                ..Command::default()
            },
        );
        assert_eq!(recovered[63], command);
        let long_gap = Command {
            frame: 34,
            ..command
        };
        assert_eq!(poses(Some(last), long_gap).collect::<Vec<_>>(), [long_gap]);
    }

    #[test]
    fn clamps_pitch_and_stores_the_truncated_throttle() {
        let positive = MotionInput::from(&Command {
            pitch: 30000,
            right: 9,
            forward: -128,
            ..Command::default()
        });
        let negative = MotionInput::from(&Command {
            pitch: -30000,
            right: -9,
            forward: 127,
            ..Command::default()
        });
        assert_eq!((positive.pitch, negative.pitch), (16202, -16202));
        assert!(positive.pitch_held && negative.pitch_held);
        assert_eq!(positive.throttles, [8, -127]);
        assert_eq!(negative.throttles, [-8, 127]);
        assert_eq!(
            MotionInput::from(&Command {
                pitch: 89,
                ..Command::default()
            })
            .pitch,
            89
        );
    }

    #[test]
    fn movement_preserves_momentum_and_uses_contact_specific_air_control() {
        let settings = MoverSettings::default();
        let step = VelocityStep {
            base: &settings,
            settings: &settings,
            input: MotionInput::from(&Command {
                forward: 127,
                ..Command::default()
            }),
            ground: Ground::Standing,
            crouched: false,
            dt: 0.016,
        };
        assert_eq!(step.advance([0.0; 3]), [0.0, 0.0, 3.08]);
        assert!((step.advance([0.0, 0.0, 12.0])[2] - 9.6592).abs() < 1e-12);
        let air = VelocityStep {
            ground: Ground::Airborne,
            ..step
        }
        .advance([0.0; 3]);
        let touching = VelocityStep {
            ground: Ground::AirborneTouching,
            ..step
        }
        .advance([0.0; 3]);
        assert_eq!((air[2], touching[2]), (0.308, 0.044));
        assert_eq!(air[1], -0.28);
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn movement_velocity_matches_python_across_authored_mover_settings() {
        let path = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap())
            .join("data/game_movers_174.json");
        let movers = MoverCatalog::load(&path).unwrap();
        let raw: BTreeMap<String, serde_json::Value> =
            serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(raw.len(), 38);
        let indexes = [
            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 20, 21, 22, 36, 38,
        ];
        let operations = [
            Operation::Add,
            Operation::Multiply,
            Operation::Set,
            Operation::Minimum,
            Operation::Maximum,
        ];
        let dts = [0.0, 0.016, 0.032, 0.001, 0.25, 0.064];
        let mut digest = Sha256::new();
        for (i, guid) in raw.keys().enumerate() {
            let guid = crate::accounts::parse_guid(guid).unwrap();
            digest.update(guid.to_le_bytes());
            let base = movers.for_body(Some(guid));
            for n in 0..192 {
                let seed = n + i;
                let mut timeline = ModifierTimeline::default();
                if n % 4 != 0 {
                    let changes = [
                        (1, 0, 0, Operation::Multiply, 1.5, 2.0),
                        (1, 1, 0, Operation::Add, -0.24, -1.0),
                        (1, 2, 14, Operation::Set, (n % 15) as f64 / 2.0, 0.0),
                        (
                            1,
                            3,
                            11,
                            Operation::Multiply,
                            [-1.0, 0.0, 1.0, 1.25][(n / 4) % 4],
                            0.0,
                        ),
                        (
                            2,
                            0,
                            indexes[seed % indexes.len()],
                            operations[(seed / indexes.len()) % 5],
                            (((n * 17 + i * 5) % 33) as f64 - 9.0) / 7.0,
                            0.0,
                        ),
                    ];
                    let mods = changes
                        .into_iter()
                        .map(|(instance, slot, index, operation, value, priority)| {
                            (
                                ModifierKey {
                                    instance,
                                    state: 1,
                                    slot,
                                },
                                Modifier {
                                    index,
                                    operation,
                                    value,
                                    priority,
                                },
                            )
                        })
                        .collect();
                    timeline.track(100, &mods);
                }
                let active = timeline.at(101);
                let settings = active.settings(base);
                let input = MotionInput::from(&Command {
                    yaw: ((i * 10013 + n * 6029) % 262144) as i32 - 131072,
                    pitch: ((n * 907) % 200000) as i32 - 100000,
                    right: (((n * 29 + i * 5) % 256) as i16 - 128) as i8,
                    forward: (((n * 13 + i * 11) % 256) as i16 - 128) as i8,
                    ..Command::default()
                });
                digest.update(input.pitch.to_le_bytes());
                digest.update([
                    u8::from(input.pitch_held),
                    input.throttles[0] as u8,
                    input.throttles[1] as u8,
                ]);
                for throttle in [input.right, input.forward] {
                    digest.update((throttle as f32).to_le_bytes());
                }
                for value in [
                    settings.run_forward,
                    settings.run_backward,
                    settings.run_strafe,
                    settings.crouch_forward,
                    settings.crouch_backward,
                    settings.crouch_strafe,
                    settings.accel_ground,
                    settings.accel_air,
                    settings.accel_air_touching,
                    settings.decel_ground_base,
                    settings.decel_ground_per_speed,
                    settings.decel_ground_base_speed,
                    settings.decel_air_base,
                    settings.decel_air_per_speed,
                    settings.decel_air_base_speed,
                    settings.jump_speed,
                    settings.gravity_scale,
                    settings.terminal_velocity,
                    settings.terminal_velocity_up,
                    settings.ground_gravity_accel,
                    settings.ground_gravity_decel,
                    settings.step_down,
                ] {
                    digest.update(value.to_le_bytes());
                }
                digest.update(settings.character_collision.to_le_bytes());
                for (index, value) in [
                    (JUMP_INTENT, 1.0),
                    (CROUCH_INTENT, 1.0),
                    (LAND_LIMIT, 0.004),
                ] {
                    digest.update(active.intent(index, value).to_le_bytes());
                }
                let ground = [Ground::Standing, Ground::Airborne, Ground::AirborneTouching][n % 3];
                let dt = dts[(n / 3) % dts.len()];
                let previous = match n % 17 {
                    0 => [0.0; 3],
                    1 => [1e-20, 0.125, -1e-19],
                    _ => [
                        (((n * 31 + i * 7) % 129) as f64 - 64.0) / 3.0,
                        (((n * 17 + i * 11) % 127) as f64 - 90.0) / 2.0,
                        (((n * 23 + i * 13) % 117) as f64 - 58.0) / 3.0,
                    ],
                };
                let step = VelocityStep {
                    base,
                    settings: &settings,
                    input,
                    ground,
                    crouched: seed % 2 == 0,
                    dt,
                };
                for axis in step.advance(previous) {
                    // Compare before the much coarser game grid but allow
                    // low-bit differences in the host's sin/cos.
                    digest.update(((axis * 16777216.0 + 0.5).floor() as i64).to_le_bytes());
                }
            }
        }
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "1cc8efee5023b28c77d6cfca201498c0c64e7706b0088fb4651cf6156d551347"
        );
    }
}
