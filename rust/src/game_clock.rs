//! The fixed game tick and packet clock, separate from socket and simulation work.

use std::time::Duration;

pub const TICK_PERIOD: Duration = Duration::from_millis(16);
pub const SEND_EVERY: u32 = 3;
pub const FIRST_TICK: u32 = 1000;
pub const MAX_LEASE_FRAME: u32 = u32::MAX - 2 * (1 << 22);
const CLOCK_DRAIN_TICKS: u32 = (60_000u32 + 3_000).div_ceil(16) + SEND_EVERY;
pub const MAX_SIMULATION_TICK: u32 = MAX_LEASE_FRAME - CLOCK_DRAIN_TICKS;

/// Skip ticks strictly behind the monotonic clock after processing the current one.
pub fn next_tick_after(due: Duration, now: Duration) -> Option<(Duration, u64)> {
    let following = due.checked_add(TICK_PERIOD)?;
    if following >= now {
        return Some((following, 0));
    }
    let late = (now - following).as_nanos();
    let period = TICK_PERIOD.as_nanos();
    let missed = u64::try_from(late / period + 1).ok()?;
    let until_next = Duration::from_nanos((period - late % period) as u64);
    Some((now.checked_add(until_next)?, missed))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Step {
    Simulate {
        frame: u32,
        send_world: bool,
    },
    Drain {
        frame: u32,
        entered: bool,
        force: bool,
        send_reliable: bool,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct GameClock {
    frame: u32,
    drain_ticks_left: Option<u32>,
}

impl Default for GameClock {
    fn default() -> Self {
        Self {
            frame: FIRST_TICK,
            drain_ticks_left: None,
        }
    }
}

impl GameClock {
    pub fn frame(self) -> u32 {
        self.frame
    }

    pub fn draining(self) -> bool {
        self.drain_ticks_left.is_some()
    }

    pub fn step(&mut self, missed: u64, connections_pending: bool) -> Step {
        let candidate = u64::from(self.frame)
            .saturating_add(missed)
            .saturating_add(1);
        if self.drain_ticks_left.is_none() && candidate <= u64::from(MAX_SIMULATION_TICK) {
            let frame = candidate as u32;
            let send_world = frame / SEND_EVERY > self.frame / SEND_EVERY;
            self.frame = frame;
            return Step::Simulate { frame, send_world };
        }

        let entered = self.drain_ticks_left.is_none();
        let ticks = self.drain_ticks_left.get_or_insert_with(|| {
            CLOCK_DRAIN_TICKS.min(MAX_LEASE_FRAME.saturating_sub(self.frame))
        });
        let force = *ticks == 0;
        if !force && connections_pending {
            *ticks -= 1;
            self.frame += 1;
        }
        Step::Drain {
            frame: self.frame,
            entered,
            force,
            send_reliable: !force && self.frame % SEND_EVERY == 0,
        }
    }

    /// Reset only when the old connections and their lobby notifications are finished.
    pub fn renew_if_idle(&mut self, connections_pending: bool, reports_pending: bool) -> bool {
        if !self.draining() || connections_pending || reports_pending {
            return false;
        }
        *self = Self::default();
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_tick_alignment_across_a_stall() {
        let ms = Duration::from_millis;
        assert_eq!(
            next_tick_after(ms(10_000), ms(10_001)),
            Some((ms(10_016), 0))
        );
        assert_eq!(
            next_tick_after(ms(10_000), ms(10_016)),
            Some((ms(10_016), 0))
        );
        assert_eq!(
            next_tick_after(ms(10_000), ms(10_050)),
            Some((ms(10_064), 3))
        );
        assert_eq!(
            next_tick_after(ms(10_000), ms(10_048)),
            Some((ms(10_064), 3))
        );
    }

    #[test]
    fn sends_once_for_every_update_that_crosses_a_world_boundary() {
        let mut clock = GameClock {
            frame: 1001,
            ..Default::default()
        };
        let sent: Vec<_> = [0, 3, 2, 2, 0, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0]
            .into_iter()
            .filter_map(|missed| match clock.step(missed, true) {
                Step::Simulate {
                    frame,
                    send_world: true,
                } => Some(frame),
                _ => None,
            })
            .collect();
        assert_eq!(sent, [1002, 1006, 1009, 1012, 1014, 1022, 1023, 1026, 1029]);
    }

    #[test]
    fn drains_one_tick_at_a_time_and_waits_for_the_last_report() {
        let mut clock = GameClock {
            frame: MAX_LEASE_FRAME - 2,
            ..Default::default()
        };
        assert_eq!(
            clock.step(500, true),
            Step::Drain {
                frame: MAX_LEASE_FRAME - 1,
                entered: true,
                force: false,
                send_reliable: (MAX_LEASE_FRAME - 1) % SEND_EVERY == 0,
            }
        );
        assert_eq!(
            clock.step(0, true),
            Step::Drain {
                frame: MAX_LEASE_FRAME,
                entered: false,
                force: false,
                send_reliable: MAX_LEASE_FRAME % SEND_EVERY == 0,
            }
        );
        assert_eq!(
            clock.step(100, true),
            Step::Drain {
                frame: MAX_LEASE_FRAME,
                entered: false,
                force: true,
                send_reliable: false,
            }
        );
        assert!(!clock.renew_if_idle(true, false));
        assert!(!clock.renew_if_idle(false, true));
        assert!(clock.renew_if_idle(false, false));
        assert_eq!(clock, GameClock::default());
    }
}
