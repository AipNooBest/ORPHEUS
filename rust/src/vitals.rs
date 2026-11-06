//! A body's authoritative health, armour and shield pools.
//!
//! The damage and healing order follows the Python reference. Original-client
//! validation of the armour reduction and healing order remains open.

use crate::world::{ARMOUR_POOL, Component, HEALTH_POOL, SHIELDS_POOL, health_pools};

const ARMOUR_BLOCK: f64 = 5.0;
const DEATH_EPSILON: f64 = 1e-9;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Death {
    pub time: f64,
    pub frame: u32,
    pub killer: u32,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Damage {
    pub dealt: f64,
    pub killed: bool,
}

#[derive(Debug, PartialEq)]
pub struct Vitals {
    maximum: [f64; 3],
    current: [f64; 3],
    changed: [bool; 3],
    death: Option<Death>,
}

impl Vitals {
    pub fn new(health: f64, armour: f64, shields: f64) -> Self {
        let pools = [health, armour, shields];
        Self {
            maximum: pools,
            current: pools,
            changed: [false; 3],
            death: None,
        }
    }

    pub fn total(&self) -> f64 {
        self.current.iter().sum()
    }

    pub fn maximum(&self) -> f64 {
        self.maximum.iter().sum()
    }

    pub fn death(&self) -> Option<Death> {
        self.death
    }

    pub fn damage(&mut self, amount: f64, source: Death) -> Damage {
        if self.death.is_some() {
            return Damage::default();
        }
        let mut left = amount.max(0.0);
        let mut dealt = 0.0;
        for kind in [SHIELDS_POOL, ARMOUR_POOL, HEALTH_POOL] {
            if left <= 0.0 {
                break;
            }
            if kind == ARMOUR_POOL && self.current[kind] > 0.0 {
                left = (left - ARMOUR_BLOCK).max(left / 2.0);
            }
            let taken = left.min(self.current[kind]);
            if taken > 0.0 {
                self.current[kind] -= taken;
                self.changed[kind] = true;
                left -= taken;
                dealt += taken;
            }
        }
        let killed = dealt > 0.0 && self.current[HEALTH_POOL] <= DEATH_EPSILON;
        if killed {
            self.current[HEALTH_POOL] = 0.0;
            self.death = Some(source);
        }
        Damage { dealt, killed }
    }

    pub fn heal(&mut self, amount: f64) -> f64 {
        if self.death.is_some() {
            return 0.0;
        }
        let amount = amount.max(0.0);
        let mut left = amount;
        for kind in [HEALTH_POOL, ARMOUR_POOL, SHIELDS_POOL] {
            let given = left.min(self.maximum[kind] - self.current[kind]);
            if given > 0.0 {
                self.current[kind] += given;
                self.changed[kind] = true;
                left -= given;
            }
        }
        amount - left
    }

    pub fn component(&self) -> Component {
        self.encode([true; 3])
    }

    pub fn changed_component(&self) -> Option<Component> {
        self.changed
            .contains(&true)
            .then(|| self.encode(self.changed))
    }

    pub fn clear_changed(&mut self) {
        self.changed = [false; 3];
    }

    fn encode(&self, include: [bool; 3]) -> Component {
        health_pools(self.maximum, self.current, include)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::world::{ComponentValue, POOLS, health, pool};

    const HIT: Death = Death {
        time: 12.0,
        frame: 1042,
        killer: 9,
    };

    #[test]
    fn shield_armour_health_order_and_partial_updates_match_reference() {
        let mut vitals = Vitals::new(200.0, 50.0, 25.0);
        assert_eq!(vitals.component(), health(200.0, 200.0, 50.0, 25.0));
        assert_eq!(vitals.damage(30.0, HIT).dealt, 27.5);
        assert_eq!(vitals.current, [200.0, 47.5, 0.0]);
        let changed = vitals.changed_component().unwrap();
        assert_eq!(changed.fields[0], None);
        assert_eq!(
            changed.fields[POOLS],
            Some(ComponentValue::Blob20(pool(2, 1, 50.0, 47.5)))
        );
        assert_eq!(
            changed.fields[2 * POOLS],
            Some(ComponentValue::Blob20(pool(1, 2, 25.0, 0.0)))
        );
        vitals.clear_changed();
        assert!(vitals.changed_component().is_none());
        assert_eq!(vitals.damage(100.0, HIT).dealt, 95.0);
        assert_eq!(vitals.current, [152.5, 0.0, 0.0]);
        assert_eq!(vitals.heal(49.0), 49.0);
        assert_eq!(vitals.current, [200.0, 1.5, 0.0]);
        assert_eq!(vitals.heal(1000.0), 73.5);
        assert_eq!(vitals.total(), vitals.maximum());
    }

    #[test]
    fn lethal_transition_keeps_original_attribution_and_stops_healing() {
        let mut vitals = Vitals::new(10.0, 0.0, 0.0);
        assert_eq!(
            vitals.damage(10.0 - DEATH_EPSILON / 2.0, HIT),
            Damage {
                dealt: 10.0 - DEATH_EPSILON / 2.0,
                killed: true,
            }
        );
        assert_eq!(vitals.total(), 0.0);
        assert_eq!(vitals.death(), Some(HIT));
        assert_eq!(
            vitals.damage(100.0, Death { killer: 50, ..HIT }),
            Damage::default()
        );
        assert_eq!(vitals.heal(50.0), 0.0);
        assert_eq!(
            vitals.changed_component().unwrap().fields[0],
            Some(ComponentValue::Blob20(pool(1, 0, 10.0, 0.0)))
        );
    }
}
