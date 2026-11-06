//! Frame-scoped movement modifiers from build 104319 statescript.
//!
//! The scalar slots and operation order follow `ow174/game/mover.py`.

use std::{cmp::Ordering, collections::BTreeMap};

use crate::{
    commands::{FRAME_HALF, frame_distance},
    mover_settings::MoverSettings,
};

pub const JUMP_INTENT: u8 = 20;
pub const CROUCH_INTENT: u8 = 21;
pub const LAND_LIMIT: u8 = 36;
pub(crate) const CAP_TINY: f64 = 1.1920929e-5;

const SPEED: usize = 0;
const ACCEL_GROUND: usize = 1;
const ACCEL_AIR: usize = 2;
const ACCEL_TOUCHING: usize = 3;
const DECEL_GROUND: usize = 4;
const DECEL_GROUND_BASE: usize = 5;
const DECEL_GROUND_SPEED: usize = 6;
const DECEL_AIR: usize = 7;
const DECEL_AIR_BASE: usize = 8;
const DECEL_AIR_SPEED: usize = 9;
const JUMP_SPEED: usize = 10;
const GRAVITY: usize = 11;
const TERMINAL: usize = 13;
const TERMINAL_UP: usize = 14;
const GROUND_ACCEL: usize = 15;
const GROUND_DECEL: usize = 16;
const COLLISION: usize = 22;
const STEP_DOWN: usize = 38;

pub(crate) fn narrow(value: f64) -> f64 {
    f64::from(value as f32)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Operation {
    Add,
    Multiply,
    Set,
    Minimum,
    Maximum,
}

impl Operation {
    fn apply(self, value: f64, operand: f64) -> f64 {
        match self {
            Self::Add => narrow(value + operand),
            Self::Multiply => narrow(value * operand),
            Self::Set => operand,
            Self::Minimum if operand < value => operand,
            Self::Maximum if operand > value => operand,
            Self::Minimum | Self::Maximum => value,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Modifier {
    pub index: u8,
    pub operation: Operation,
    pub value: f64,
    pub priority: f64,
}

impl Modifier {
    fn apply(self, value: f64) -> f64 {
        self.operation.apply(value, self.value)
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct ModifierKey {
    pub instance: u16,
    pub state: u16,
    pub slot: usize,
}

struct Interval {
    modifier: Modifier,
    start: u32,
    end: Option<u32>,
}

#[derive(Default)]
pub struct ModifierTimeline {
    intervals: BTreeMap<ModifierKey, Interval>,
}

impl ModifierTimeline {
    /// Refresh the active states after a simulated command. Newly added or
    /// retired states take effect at the next frame. Retained states can
    /// refresh their value and priority without changing their start order.
    pub fn track(&mut self, frame: u32, active: &BTreeMap<ModifierKey, Modifier>) {
        let next = frame.wrapping_add(1);
        self.intervals.retain(|key, interval| {
            if interval.end.is_some_and(|end| reached(frame, end)) {
                return false;
            }
            if !active.contains_key(key) {
                interval.end.get_or_insert(next);
            }
            true
        });
        for (&key, &modifier) in active {
            let interval = self.intervals.entry(key).or_insert(Interval {
                modifier,
                start: next,
                end: None,
            });
            if interval.end.take().is_some() {
                interval.start = next;
                interval.modifier = modifier;
            } else {
                interval.modifier.value = modifier.value;
                interval.modifier.priority = modifier.priority;
            }
        }
    }

    pub fn at(&self, frame: u32) -> ActiveModifiers {
        let mut active: Vec<_> = self
            .intervals
            .values()
            .filter_map(|interval| {
                let age = frame_distance(frame, interval.start);
                (age < FRAME_HALF && interval.end.is_none_or(|end| !reached(frame, end)))
                    .then_some((age, interval.modifier))
            })
            .collect();
        // Stable sort keeps the iteration order of equal start and priority
        // values. The timeline already iterates by instance, state and slot.
        active.sort_by(|(left_age, left), (right_age, right)| {
            let priority = if left.priority == right.priority {
                Ordering::Equal
            } else {
                left.priority.total_cmp(&right.priority)
            };
            priority.then_with(|| right_age.cmp(left_age))
        });
        ActiveModifiers(active)
    }
}

fn reached(frame: u32, boundary: u32) -> bool {
    frame_distance(frame, boundary) < FRAME_HALF
}

#[derive(Default)]
pub struct ActiveModifiers(Vec<(u32, Modifier)>);

impl ActiveModifiers {
    pub fn intent(&self, index: u8, base: f64) -> f64 {
        self.0
            .iter()
            .map(|(_, modifier)| *modifier)
            .filter(|modifier| modifier.index == index)
            .fold(base, |value, modifier| modifier.apply(value))
    }

    pub fn settings(&self, base: &MoverSettings) -> MoverSettings {
        if self.0.is_empty() {
            return *base;
        }
        let mut slots = [None; STEP_DOWN + 1];
        slots[..10].fill(Some(1.0));
        for (index, value) in [
            (JUMP_SPEED, base.jump_speed),
            (GRAVITY, 1.0),
            (TERMINAL, base.terminal_velocity),
            (TERMINAL_UP, base.terminal_velocity_up),
            (GROUND_ACCEL, base.ground_gravity_accel),
            (GROUND_DECEL, base.ground_gravity_decel),
            (COLLISION, f64::from(base.character_collision)),
            (STEP_DOWN, 1.0),
        ] {
            slots[index] = Some(value);
        }
        for &(_, modifier) in &self.0 {
            if let Some(Some(value)) = slots.get_mut(usize::from(modifier.index)) {
                *value = modifier.apply(*value);
            }
        }
        let slot = |index: usize| slots[index].expect("initialized movement scalar");
        let nonnegative = |index| slot(index).max(0.0);
        let speed = nonnegative(SPEED);
        let ground_decel = nonnegative(DECEL_GROUND);
        let air_decel = nonnegative(DECEL_AIR);
        MoverSettings {
            run_forward: narrow(base.run_forward * speed),
            run_backward: narrow(base.run_backward * speed),
            run_strafe: narrow(base.run_strafe * speed),
            crouch_forward: narrow(base.crouch_forward * speed),
            crouch_backward: narrow(base.crouch_backward * speed),
            crouch_strafe: narrow(base.crouch_strafe * speed),
            accel_ground: narrow(base.accel_ground * nonnegative(ACCEL_GROUND)),
            accel_air: narrow(base.accel_air * nonnegative(ACCEL_AIR)),
            accel_air_touching: narrow(base.accel_air_touching * nonnegative(ACCEL_TOUCHING)),
            decel_ground_base: narrow(
                narrow(nonnegative(DECEL_GROUND_BASE) * ground_decel) * base.decel_ground_base,
            ),
            decel_ground_per_speed: narrow(ground_decel * base.decel_ground_per_speed),
            decel_ground_base_speed: narrow(
                narrow(speed * slot(DECEL_GROUND_SPEED)) * base.decel_ground_base_speed,
            ),
            decel_air_base: narrow(
                narrow(nonnegative(DECEL_AIR_BASE) * air_decel) * base.decel_air_base,
            ),
            decel_air_per_speed: narrow(air_decel * base.decel_air_per_speed),
            decel_air_base_speed: narrow(
                narrow(speed * slot(DECEL_AIR_SPEED)) * base.decel_air_base_speed,
            ),
            jump_speed: nonnegative(JUMP_SPEED),
            gravity_scale: narrow(slot(GRAVITY) * base.gravity_scale),
            terminal_velocity: nonnegative(TERMINAL),
            terminal_velocity_up: nonnegative(TERMINAL_UP),
            ground_gravity_accel: slot(GROUND_ACCEL),
            ground_gravity_decel: slot(GROUND_DECEL),
            character_collision: u32::from(slot(COLLISION) > CAP_TINY),
            step_down: narrow(base.step_down * nonnegative(STEP_DOWN)),
            ..*base
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn modifier(operation: Operation, value: f64, priority: f64) -> Modifier {
        Modifier {
            index: SPEED as u8,
            operation,
            value,
            priority,
        }
    }

    #[test]
    fn intervals_switch_on_the_next_frame_across_wrap() {
        let key = ModifierKey {
            instance: 1,
            state: 22,
            slot: 0,
        };
        let base = MoverSettings::default();
        let mut timeline = ModifierTimeline::default();
        let sprint = BTreeMap::from([(key, modifier(Operation::Multiply, 1.5, 0.0))]);
        timeline.track(u32::MAX, &sprint);
        assert_eq!(timeline.at(u32::MAX).settings(&base), base);
        assert_eq!(timeline.at(0).settings(&base).run_forward, 8.25);
        timeline.track(0, &BTreeMap::new());
        assert_eq!(timeline.at(0).settings(&base).run_forward, 8.25);
        assert_eq!(timeline.at(1).settings(&base), base);
        timeline.track(1, &BTreeMap::new());
        assert!(timeline.intervals.is_empty());
        timeline.track(2, &sprint);
        assert_eq!(timeline.at(2).settings(&base), base);
        assert_eq!(timeline.at(3).settings(&base).run_forward, 8.25);
    }

    #[test]
    fn application_orders_priority_then_first_frame_then_state_slot() {
        let key = |state| ModifierKey {
            instance: 1,
            state,
            slot: 0,
        };
        let mut timeline = ModifierTimeline::default();
        let mut active = BTreeMap::from([(key(9), modifier(Operation::Set, 3.0, 0.0))]);
        timeline.track(1, &active);
        active.insert(key(3), modifier(Operation::Multiply, 3.0, 0.0));
        active.insert(key(2), modifier(Operation::Add, 1.0, 0.0));
        active.insert(key(1), modifier(Operation::Minimum, 1.0, 1.0));
        timeline.track(2, &active);
        assert_eq!(timeline.at(2).intent(SPEED as u8, 1.0), 3.0);
        assert_eq!(timeline.at(3).intent(SPEED as u8, 1.0), 1.0);
        active.remove(&key(1));
        active.get_mut(&key(9)).unwrap().value = 2.0;
        timeline.track(3, &active);
        assert_eq!(timeline.at(4).intent(SPEED as u8, 1.0), 9.0);
    }
}
