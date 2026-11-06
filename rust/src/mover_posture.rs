//! Character stance, take-off, landing and pogo spring state for 104319.

use crate::{
    commands::{CROUCH, Command, JUMP, frame_distance},
    mover_ground::{ProbeResult, capsule, pogo},
    mover_kinematics::{Ground, LAND_SPEED, WORLD_GRAVITY},
    mover_modifiers::{ActiveModifiers, CROUCH_INTENT, JUMP_INTENT, LAND_LIMIT},
    mover_settings::MoverSettings,
    world::POSITION_UNITS,
};

const JUMP_LATCH: u32 = 0x1;
pub(crate) const CROUCHED: u32 = 0x2;
pub(crate) const JUMPED: u32 = 0x4;
const FELL: u32 = 0x8;
const REAL_JUMP: u32 = 0x400;
pub(crate) const TOUCHING: u32 = 0x20000;
pub(crate) const NOT_GROUND_UNDER: u32 = 0x80000;
const TURN_HELD: u32 = 0x200000;
pub(crate) const IN_AIR: u32 = 0x80000c;

const SPRING_REST: f32 = 1.0 / 512.0;
const SPRING_STEPS: f32 = 32768.0;
const TINY: f32 = f32::EPSILON;
const CROUCH_AGAIN: f64 = 0.15;

pub fn snap(value: f64) -> f64 {
    (value * POSITION_UNITS + 0.5).floor() / POSITION_UNITS
}

pub struct Posture {
    pub flags: u32,
    pub air_ticks: u32,
    pub crouch_end: Option<u32>,
    landed: bool,
    pub fall: f32,
    spring: Spring,
}

struct Spring {
    low: f32,
    high: f32,
    primary: f32,
    secondary: f32,
    encoded: [u16; 2],
    speed: f32,
}

pub struct PostureStep<'a> {
    pub command: &'a Command,
    pub base: &'a MoverSettings,
    pub values: &'a MoverSettings,
    pub mods: &'a ActiveModifiers,
    pub probe: ProbeResult,
    pub dt: f64,
    pub pitch_held: bool,
}

impl Posture {
    pub fn new(base: &MoverSettings) -> Self {
        let mut spring = Spring {
            low: 0.0,
            high: 0.0,
            primary: 0.0,
            secondary: 0.0,
            encoded: [0; 2],
            speed: 0.0,
        };
        spring.set_limits(base);
        Self {
            flags: 0,
            air_ticks: 0,
            crouch_end: None,
            landed: false,
            fall: 0.0,
            spring,
        }
    }

    pub fn ground(&self) -> Ground {
        match self.flags {
            flags if flags & IN_AIR == 0 => Ground::Standing,
            flags if flags & TOUCHING != 0 => Ground::AirborneTouching,
            _ => Ground::Airborne,
        }
    }

    pub fn crouched(&self) -> bool {
        self.flags & CROUCHED != 0
    }

    pub fn spring_offset(&self) -> f32 {
        self.spring.primary
    }

    pub fn spring_speed(&self) -> f32 {
        self.spring.speed
    }

    pub fn spring_steps(&self) -> [u16; 2] {
        self.spring.encoded
    }

    /// Run after the ground probe and before resolving the requested motion.
    /// The resize callback commits the new capsule only if it fits.
    pub fn advance<E>(
        &mut self,
        step: PostureStep<'_>,
        feet: &mut [f64; 3],
        velocity: &mut [f64; 3],
        resize: impl FnOnce(f32, f32) -> Result<bool, E>,
    ) -> Result<(), E> {
        let was_crouched = self.crouched();
        let start = SpringAnchor {
            feet: feet[1],
            pogo: pogo(step.base, was_crouched),
            spring: self.spring.primary,
        };
        self.transition(&step, velocity);

        let frame = step.command.frame;
        let requested = step.command.buttons & CROUCH != 0
            && (was_crouched
                || self.crouch_end.is_none_or(|stood| {
                    f64::from(frame_distance(frame, stood)) * step.dt >= CROUCH_AGAIN
                }))
            && self.flags & IN_AIR == 0;
        let crouch = step
            .mods
            .intent(CROUCH_INTENT, f64::from(u8::from(requested)))
            > 0.5;
        if crouch != was_crouched {
            let (radius, segment) = capsule(step.base, crouch);
            if resize(radius, segment + radius)? {
                self.flags = (self.flags & !CROUCHED) | (u32::from(crouch) * CROUCHED);
                if !crouch {
                    self.crouch_end = Some(frame);
                }
                let difference = (step.base.crouch_pogo - step.base.stand_pogo) as f32;
                if difference < 0.0 {
                    let shift = if crouch { -difference } else { difference };
                    let minimum = -pogo(step.base, crouch);
                    self.spring.primary =
                        minimum.max(f64::from(self.spring.primary + shift)) as f32;
                    self.spring.secondary = self.spring.primary;
                }
            }
        }
        if self.flags & IN_AIR == 0 {
            if let Some(ground) = step.probe.ground {
                feet[1] = ground.y;
            }
        }
        self.spring.settle(
            start,
            feet[1],
            pogo(step.base, self.crouched()),
            step.values,
            step.dt as f32,
        );
        Ok(())
    }

    fn transition(&mut self, step: &PostureStep<'_>, velocity: &mut [f64; 3]) {
        let mut flags = self.flags;
        self.landed = false;
        if flags & IN_AIR != 0 {
            let [x, y, z] = *velocity;
            let limit = step.mods.intent(LAND_LIMIT, LAND_SPEED);
            let landing = step.probe.ground.is_some_and(|ground| {
                let [nx, ny, nz] = ground.normal;
                (x * nx + y * ny) + z * nz < limit
            });
            if landing {
                if flags & (JUMPED | FELL) != 0 {
                    flags &= !(JUMPED | FELL);
                    self.air_ticks = 0;
                    self.landed = true;
                }
            } else {
                self.air_ticks = self.air_ticks.saturating_add(1);
            }
        } else if step.probe.ground.is_none() {
            self.air_ticks = 0;
            flags = (flags & !JUMPED) | FELL;
        }
        flags = (flags & !NOT_GROUND_UNDER)
            | (u32::from(step.probe.non_ground_under) * NOT_GROUND_UNDER)
            | (u32::from(step.pitch_held) * TURN_HELD);
        let button = step.command.buttons & JUMP != 0;
        let jump = step.mods.intent(JUMP_INTENT, f64::from(u8::from(button))) > 0.5;
        if jump {
            if flags & (IN_AIR | JUMP_LATCH) == 0 {
                velocity[1] = step.values.jump_speed;
                self.air_ticks = 0;
                flags = (flags & !(FELL | REAL_JUMP)) | JUMPED | REAL_JUMP | JUMP_LATCH;
            }
        } else if flags & IN_AIR == 0 {
            flags &= !JUMP_LATCH;
        }
        self.flags = flags;
    }

    pub fn apply_fall(&mut self, gravity_scale: f64, dt: f64) {
        self.fall += (-WORLD_GRAVITY * gravity_scale) as f32 * dt as f32;
    }

    /// Update the gravity portion after the contact solver projects velocity.
    pub fn finish_fall(&mut self, gravity_scale: f64, vertical: f64) {
        if self.fall.abs() <= TINY {
            return;
        }
        if self.landed {
            self.fall = 0.0;
        } else if self.flags & IN_AIR != 0 {
            let gravity = f64::from((-WORLD_GRAVITY * gravity_scale) as f32);
            if gravity * vertical < 0.0 {
                self.fall = 0.0;
            } else if vertical * 1.0_f64.copysign(f64::from(self.fall)) < f64::from(self.fall.abs())
            {
                self.fall = vertical as f32;
            }
        }
    }
}

struct SpringAnchor {
    feet: f64,
    pogo: f64,
    spring: f32,
}

impl Spring {
    fn set_limits(&mut self, base: &MoverSettings) {
        let stand = base.stand_height + base.stand_pogo;
        let crouch = base.crouch_height + base.crouch_pogo;
        self.low = -stand.max(crouch) as f32;
        self.high = (2.0 * base.step_down) as f32;
    }

    fn settle(
        &mut self,
        start: SpringAnchor,
        feet: f64,
        pogo: f64,
        values: &MoverSettings,
        dt: f32,
    ) {
        let lifted = (start.pogo + f64::from(start.spring)) as f32;
        let rest = (start.feet + f64::from(lifted)) as f32 - feet as f32;
        let offset = (f64::from(rest) - pogo) as f32;
        let (primary, secondary, speed) = if offset.abs() > SPRING_REST {
            let omega = (values.pogo_frequency * f64::from(std::f32::consts::TAU)) as f32;
            let h = omega * dt;
            let pull = ((omega * omega) * dt) * offset;
            let damping = (((values.pogo_damping + values.pogo_damping) as f32 + h) * h) + 1.0;
            let mut speed = (self.speed - pull) / damping;
            let mut spring = speed * dt + offset;
            let moved = (spring - self.primary).abs();
            if spring.abs() <= SPRING_REST {
                spring = 0.0;
                speed = if dt > TINY { -(offset / dt) } else { 0.0 };
            }
            let secondary = if moved > self.secondary.abs() {
                0.0
            } else if self.secondary > 0.0 {
                self.secondary - moved
            } else if self.secondary < 0.0 {
                self.secondary + moved
            } else {
                self.secondary
            };
            (spring, secondary, speed)
        } else {
            (0.0, 0.0, 0.0)
        };
        self.primary = primary.max(self.low).min(self.high);
        self.secondary = secondary.max(self.low).min(self.high);
        self.encode();
        self.speed = snap(f64::from(speed)) as f32;
    }

    fn encode(&mut self) {
        let span = self.high - self.low;
        if span <= TINY {
            self.primary = 0.0;
            self.secondary = 0.0;
            self.encoded = [0; 2];
            return;
        }
        let inverse = 1.0 / span;
        self.encoded = [self.primary, self.secondary].map(|offset| {
            let fraction = ((offset - self.low) * inverse).clamp(0.0, 1.0);
            (fraction * SPRING_STEPS + 0.5).floor() as u16
        });
        [self.primary, self.secondary] = self
            .encoded
            .map(|steps| span * (f32::from(steps) / SPRING_STEPS) + self.low);
    }
}
