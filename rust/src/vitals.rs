//! A body's authoritative health, armour and shield pools.
//!
//! The damage and healing order follows the Python reference. Original-client
//! validation of the armour reduction and healing order remains open.

use crate::world::{
    ARMOUR_POOL, Component, HEALTH_POOL, POOLS, SHIELDS_POOL, pool, pool_component,
};

const ARMOUR_BLOCK: f64 = 5.0;
const DEATH_EPSILON: f64 = 1e-9;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(usize)]
pub enum PoolKind {
    Health = HEALTH_POOL,
    Armour = ARMOUR_POOL,
    Shields = SHIELDS_POOL,
}

const DAMAGE_ORDER: [PoolKind; 3] = [PoolKind::Shields, PoolKind::Armour, PoolKind::Health];

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
struct Pool {
    field: usize,
    id: u32,
    maximum: f64,
    current: f64,
}

impl Pool {
    fn kind(&self) -> usize {
        self.field / POOLS
    }

    fn wire(&self) -> [u8; 20] {
        pool(
            self.id,
            self.kind() as u32,
            self.maximum as f32,
            self.current as f32,
        )
    }
}

#[derive(Debug, PartialEq)]
pub struct Vitals {
    pools: Vec<Pool>,
    last_id: u32,
    used: u64,
    changed: u64,
    death: Option<Death>,
}

impl Vitals {
    pub fn new(health: f64, armour: f64, shields: f64) -> Self {
        Self::with_current_health(health, armour, shields, health)
    }

    pub(crate) fn with_current_health(
        health: f64,
        armour: f64,
        shields: f64,
        current: f64,
    ) -> Self {
        let mut vitals = Self::empty();
        let amounts = [health, armour, shields];
        for kind in DAMAGE_ORDER {
            let amount = amounts[kind as usize];
            if kind == PoolKind::Health || amount > 0.0 {
                let current = if kind == PoolKind::Health {
                    current
                } else {
                    amount
                };
                let _ = vitals.add_pool(kind, amount, current);
            }
        }
        vitals.clear_changed();
        vitals
    }

    pub(crate) fn empty() -> Self {
        Self {
            pools: Vec::new(),
            last_id: 0,
            used: 0,
            changed: 0,
            death: None,
        }
    }

    pub(crate) fn add_pool(&mut self, kind: PoolKind, maximum: f64, current: f64) -> Option<u32> {
        if !(0.0..=f64::from(f32::MAX)).contains(&maximum) || !(0.0..=maximum).contains(&current) {
            return None;
        }
        let first = kind as usize * POOLS;
        let mut field = first;
        let mut index = self.pools.partition_point(|pool| pool.field < first);
        while self
            .pools
            .get(index)
            .is_some_and(|pool| pool.field == field)
        {
            field += 1;
            index += 1;
        }
        if field >= first + POOLS {
            return None;
        }
        let id = self.last_id.checked_add(1)?;
        self.pools.insert(
            index,
            Pool {
                field,
                id,
                maximum,
                current,
            },
        );
        self.last_id = id;
        self.used |= 1 << field;
        self.changed |= 1 << field;
        Some(id)
    }

    pub(crate) fn pool_current(&self, id: u32) -> Option<f64> {
        self.pools
            .iter()
            .find(|pool| pool.id == id)
            .map(|pool| pool.current)
    }

    pub(crate) fn remove_pool(&mut self, id: u32) -> Option<f64> {
        let index = self.pools.iter().position(|pool| pool.id == id)?;
        let pool = self.pools.remove(index);
        self.changed |= 1 << pool.field;
        Some(pool.current)
    }

    pub fn total(&self) -> f64 {
        self.amount(None, false)
    }

    pub fn maximum(&self) -> f64 {
        self.amount(None, true)
    }

    pub(crate) fn amount(&self, kind: Option<PoolKind>, maximum: bool) -> f64 {
        self.pools
            .iter()
            .filter(|pool| kind.is_none_or(|kind| pool.kind() == kind as usize))
            .map(|pool| if maximum { pool.maximum } else { pool.current })
            .sum()
    }

    pub fn death(&self) -> Option<Death> {
        self.death
    }

    pub fn damage(&mut self, amount: f64, source: Death) -> Damage {
        if self.death.is_some() || !amount.is_finite() || amount <= 0.0 {
            return Damage::default();
        }
        let mut left = amount;
        let mut dealt = 0.0;
        'damage: for kind in DAMAGE_ORDER {
            let mut reduce_armour = kind == PoolKind::Armour;
            for pool in self
                .pools
                .iter_mut()
                .filter(|pool| pool.kind() == kind as usize)
            {
                if left <= 0.0 {
                    break 'damage;
                }
                if pool.current == 0.0 {
                    continue;
                }
                if reduce_armour {
                    left = (left - ARMOUR_BLOCK).max(left / 2.0);
                    reduce_armour = false;
                }
                let taken = left.min(pool.current);
                if taken > 0.0 {
                    pool.current -= taken;
                    self.changed |= 1 << pool.field;
                    left -= taken;
                    dealt += taken;
                }
            }
        }
        let killed = dealt > 0.0 && self.amount(Some(PoolKind::Health), false) <= DEATH_EPSILON;
        if killed {
            for pool in self
                .pools
                .iter_mut()
                .take_while(|pool| pool.kind() == HEALTH_POOL)
            {
                if pool.current > 0.0 {
                    pool.current = 0.0;
                    self.changed |= 1 << pool.field;
                }
            }
            self.death = Some(source);
        }
        Damage { dealt, killed }
    }

    pub fn heal(&mut self, amount: f64) -> f64 {
        if self.death.is_some() || !amount.is_finite() || amount <= 0.0 {
            return 0.0;
        }
        let mut left = amount;
        for pool in &mut self.pools {
            if left <= 0.0 {
                break;
            }
            let given = left.min(pool.maximum - pool.current);
            if given > 0.0 {
                pool.current += given;
                self.changed |= 1 << pool.field;
                left -= given;
            }
        }
        amount - left
    }

    pub fn component(&self) -> Component {
        pool_component(self.pools.iter().map(|pool| (pool.field, pool.wire())))
    }

    /// Include previously occupied slots so a replacement update still clears them.
    pub(crate) fn cumulative_component(&self) -> Component {
        self.encode(self.used)
    }

    pub(crate) fn has_changed(&self) -> bool {
        self.changed != 0
    }

    pub fn changed_component(&self) -> Option<Component> {
        self.has_changed().then(|| self.encode(self.changed))
    }

    pub fn clear_changed(&mut self) {
        self.changed = 0;
    }

    fn encode(&self, mut fields: u64) -> Component {
        pool_component(std::iter::from_fn(|| {
            if fields == 0 {
                return None;
            }
            let field = fields.trailing_zeros() as usize;
            fields &= fields - 1;
            let bytes = self
                .pools
                .binary_search_by_key(&field, |pool| pool.field)
                .map_or([0; 20], |index| self.pools[index].wire());
            Some((field, bytes))
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::world::{ComponentValue, health};

    const HIT: Death = Death {
        time: 12.0,
        frame: 1042,
        killer: 9,
    };

    #[test]
    fn shield_armour_health_order_and_partial_updates_match_reference() {
        let mut vitals = Vitals::new(200.0, 50.0, 25.0);
        let currents = |vitals: &Vitals| {
            [PoolKind::Health, PoolKind::Armour, PoolKind::Shields]
                .map(|kind| vitals.amount(Some(kind), false))
        };
        assert_eq!(vitals.component(), health(200.0, 200.0, 50.0, 25.0));
        assert_eq!(vitals.damage(30.0, HIT).dealt, 27.5);
        assert_eq!(currents(&vitals), [200.0, 47.5, 0.0]);
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
        assert_eq!(currents(&vitals), [152.5, 0.0, 0.0]);
        assert_eq!(vitals.heal(49.0), 49.0);
        assert_eq!(currents(&vitals), [200.0, 1.5, 0.0]);
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

    #[test]
    fn pool_removal_survives_cumulative_delivery_and_reuse_gets_a_new_id() {
        let mut vitals = Vitals::new(200.0, 0.0, 0.0);
        let field = SHIELDS_POOL * POOLS;
        assert!(!vitals.has_changed());
        assert_eq!(vitals.cumulative_component(), vitals.component());
        let old = vitals.add_pool(PoolKind::Shields, 40.0, 40.0).unwrap();
        assert_eq!(old, 2);
        assert_eq!(vitals.damage(40.0, HIT).dealt, 40.0);
        assert_eq!(vitals.pool_current(old), Some(0.0));
        assert_eq!(vitals.amount(Some(PoolKind::Shields), true), 40.0);
        assert_eq!(
            vitals.changed_component().unwrap().fields[field],
            Some(ComponentValue::Blob20(pool(
                old,
                SHIELDS_POOL as u32,
                40.0,
                0.0
            )))
        );

        vitals.clear_changed();
        assert_eq!(vitals.remove_pool(old), Some(0.0));
        assert_eq!(vitals.pool_current(old), None);
        assert_eq!(vitals.component().fields[field], None);
        assert_eq!(
            vitals.changed_component().unwrap().fields[field],
            Some(ComponentValue::Blob20([0; 20]))
        );
        vitals.clear_changed();
        let cumulative = vitals.cumulative_component();
        assert_eq!(
            cumulative.fields[field],
            Some(ComponentValue::Blob20([0; 20]))
        );
        assert_eq!(cumulative.fields[field + 1], None);

        let fresh = vitals.add_pool(PoolKind::Shields, 20.0, 10.0).unwrap();
        assert_eq!(fresh, 3);
        assert_eq!(vitals.remove_pool(old), None);
        assert_eq!(vitals.pool_current(fresh), Some(10.0));
        assert_eq!(vitals.cumulative_component(), vitals.component());
    }

    #[test]
    fn pool_limits_reject_invalid_values_full_kinds_and_id_wrap() {
        let mut vitals = Vitals::empty();
        for (maximum, current) in [
            (f64::NAN, 0.0),
            (f64::INFINITY, 1.0),
            (f64::MAX, 1.0),
            (-1.0, 0.0),
            (1.0, f64::NEG_INFINITY),
            (1.0, f64::NAN),
            (1.0, 2.0),
        ] {
            assert_eq!(vitals.add_pool(PoolKind::Health, maximum, current), None);
        }
        assert!(!vitals.has_changed());
        for slot in 0..POOLS {
            assert_eq!(
                vitals.add_pool(PoolKind::Health, 10.0, 10.0),
                Some(slot as u32 + 1)
            );
        }
        assert_eq!(vitals.add_pool(PoolKind::Health, 10.0, 10.0), None);
        vitals.last_id = u32::MAX - 1;
        assert_eq!(
            vitals.add_pool(PoolKind::Armour, 10.0, 10.0),
            Some(u32::MAX)
        );
        assert_eq!(vitals.add_pool(PoolKind::Shields, 10.0, 10.0), None);
        assert_eq!(vitals.total(), 170.0);
        assert_eq!(
            Vitals::new(0.0, 0.0, 0.0).component(),
            health(0.0, 0.0, 0.0, 0.0)
        );
    }

    #[test]
    fn same_kind_pools_damage_and_heal_in_slot_order() {
        let mut vitals = Vitals::new(200.0, 10.0, 0.0);
        let extra_armour = vitals.add_pool(PoolKind::Armour, 30.0, 30.0).unwrap();
        assert_eq!(vitals.damage(f64::INFINITY, HIT), Damage::default());
        assert_eq!(vitals.heal(f64::NAN), 0.0);
        assert_eq!(vitals.damage(30.0, HIT).dealt, 25.0);
        assert_eq!(vitals.pool_current(1), Some(0.0));
        assert_eq!(vitals.pool_current(extra_armour), Some(15.0));
        assert_eq!(vitals.heal(11.0), 11.0);
        assert_eq!(vitals.pool_current(1), Some(10.0));
        assert_eq!(vitals.pool_current(extra_armour), Some(16.0));
    }
}
