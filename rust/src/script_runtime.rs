//! Clock and pending-event ordering for authored statescripts.

use std::collections::{BTreeMap, VecDeque};

pub const FRAME_MS: u64 = crate::game_clock::TICK_PERIOD.as_millis() as u64;

pub(crate) fn frame_start(frame: u32) -> u64 {
    u64::from(frame) * FRAME_MS
}

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct QueueFull {
    pub limit: usize,
}

/// Each instance consumes events in timestamp order, with FIFO ties. The
/// component controls the order in which instances take a turn.
pub(crate) struct Timeline<E> {
    instances: BTreeMap<u16, BTreeMap<u64, VecDeque<E>>>,
    pending: usize,
    limit: usize,
}

impl<E> Timeline<E> {
    pub fn new(limit: usize) -> Self {
        Self {
            instances: BTreeMap::new(),
            pending: 0,
            limit,
        }
    }

    pub fn reserve(&self, count: usize) -> Result<(), QueueFull> {
        if count > self.limit - self.pending {
            Err(QueueFull { limit: self.limit })
        } else {
            Ok(())
        }
    }

    pub fn push(&mut self, instance: u16, due_ms: u64, event: E) -> Result<(), QueueFull> {
        self.reserve(1)?;
        self.instances
            .entry(instance)
            .or_default()
            .entry(due_ms)
            .or_default()
            .push_back(event);
        self.pending += 1;
        Ok(())
    }

    pub fn next_due(&self) -> Option<u64> {
        self.instances
            .values()
            .filter_map(|events| events.first_key_value().map(|(&due_ms, _)| due_ms))
            .min()
    }

    pub fn pop_due(&mut self, instance: u16, now_ms: u64) -> Option<E> {
        let events = self.instances.get_mut(&instance)?;
        let mut next = events.first_entry()?;
        if *next.key() > now_ms {
            return None;
        }
        let event = next.get_mut().pop_front().expect("populated timestamp");
        if next.get().is_empty() {
            next.remove();
        }
        if events.is_empty() {
            self.instances.remove(&instance);
        }
        self.pending -= 1;
        Some(event)
    }

    pub fn retain(&mut self, instance: u16, mut keep: impl FnMut(u64, &E) -> bool) {
        if let Some(events) = self.instances.get_mut(&instance) {
            events.retain(|&due_ms, queued| {
                let before = queued.len();
                queued.retain(|event| keep(due_ms, event));
                self.pending -= before - queued.len();
                !queued.is_empty()
            });
            if events.is_empty() {
                self.instances.remove(&instance);
            }
        }
    }

    pub fn remove(&mut self, instance: u16) {
        if let Some(events) = self.instances.remove(&instance) {
            self.pending -= events.values().map(VecDeque::len).sum::<usize>();
        }
    }

    pub fn iter(&self, instance: u16) -> impl Iterator<Item = (u64, &E)> {
        self.instances
            .get(&instance)
            .into_iter()
            .flatten()
            .flat_map(|(&due_ms, events)| events.iter().map(move |event| (due_ms, event)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_due_time_fifo_and_instance_turns() {
        let mut timeline = Timeline::new(6);
        for (instance, time, event) in [
            (2, 18, 'a'),
            (1, 17, 'b'),
            (2, 17, 'c'),
            (2, 17, 'd'),
            (1, 16, 'e'),
        ] {
            timeline.push(instance, time, event).unwrap();
        }
        assert_eq!(timeline.next_due(), Some(16));
        assert_eq!(timeline.pop_due(2, 16), None);
        assert_eq!(timeline.pop_due(1, 16), Some('e'));
        assert_eq!(timeline.pop_due(2, 17), Some('c'));
        timeline.push(2, 17, 'f').unwrap();
        assert_eq!(timeline.pop_due(2, 17), Some('d'));
        assert_eq!(timeline.pop_due(2, 17), Some('f'));
        assert_eq!(timeline.pop_due(2, 17), None);
        assert_eq!(timeline.pop_due(1, 17), Some('b'));
        assert_eq!(timeline.next_due(), Some(18));
        assert_eq!(timeline.pop_due(2, 18), Some('a'));
        assert_eq!(timeline.next_due(), None);
        assert_eq!(frame_start(u32::MAX), u64::from(u32::MAX) * 16);
    }

    #[test]
    fn bounded_pending_events_and_cancellation_reclaim_slots() {
        let mut timeline = Timeline::new(4);
        for (id, time, event) in [(7, 5, 42), (7, 6, 42), (7, 6, 43), (9, 5, 99)] {
            timeline.push(id, time, event).unwrap();
        }
        assert_eq!(timeline.push(9, 5, 100), Err(QueueFull { limit: 4 }));
        timeline.retain(7, |time, event| time != 5 || *event != 42);
        assert_eq!(
            timeline
                .iter(7)
                .map(|(time, event)| (time, *event))
                .collect::<Vec<_>>(),
            [(6, 42), (6, 43)]
        );
        assert_eq!(timeline.reserve(2), Err(QueueFull { limit: 4 }));
        timeline.remove(9);
        timeline.reserve(2).unwrap();
        timeline.push(7, 7, 44).unwrap();
        timeline.push(7, 8, 45).unwrap();
        assert_eq!(timeline.next_due(), Some(6));
        assert_eq!(timeline.pop_due(9, 8), None);
    }
}
