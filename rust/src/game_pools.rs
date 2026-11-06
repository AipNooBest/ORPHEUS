//! Initial health, armour, and shield pools for a playable hero body.
//!
//! Common body graph 0033 supplies the defaults. Each body's binding can
//! override them, following ow174/game/pools.py.

use std::{collections::HashMap, io};

use serde_json::Value;

use crate::accounts::parse_guid;
use crate::script_graph::{GraphStore, asset_guid};
use crate::vitals::Vitals;

const BODY_GRAPH: u32 = 0x0033;
const HEALTH_VAR: u64 = 199;
const ARMOUR_VAR: u64 = 1736;
const SHIELDS_VAR: u64 = 221;

pub const FALLBACK_POOLS: HealthPools = HealthPools {
    health: 200.0,
    armour: 0.0,
    shields: 0.0,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PoolKind {
    Health,
    Armour,
    Shields,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct HealthPools {
    pub health: f64,
    pub armour: f64,
    pub shields: f64,
}

impl HealthPools {
    pub fn total(self) -> f64 {
        self.health + self.armour + self.shields
    }

    pub fn vitals(self) -> Vitals {
        Vitals::new(self.health, self.armour, self.shields)
    }

    pub(crate) fn amount(self, kind: PoolKind, maximum: f64, current: Option<f64>) -> Option<f64> {
        let pools = [self.health, self.armour, self.shields];
        if pools.iter().any(|pool| !pool.is_finite() || *pool < 0.0)
            || !maximum.is_finite()
            || self.total() != maximum
        {
            return None;
        }
        let capacity = match kind {
            PoolKind::Health => self.health,
            PoolKind::Armour => self.armour,
            PoolKind::Shields => self.shields,
        };
        let Some(current) = current else {
            return Some(capacity);
        };
        if !(0.0..=maximum).contains(&current) {
            return None;
        }
        if current == 0.0 {
            return Some(0.0);
        }
        // An empty kind, full pools, or one nonzero kind has an unambiguous amount.
        if capacity == 0.0 || current == maximum {
            Some(capacity)
        } else if pools.iter().filter(|&&pool| pool > 0.0).count() == 1 {
            Some(current)
        } else {
            None
        }
    }
}

#[derive(Debug, Default)]
pub struct PoolCatalog {
    by_hero: HashMap<u64, Option<HealthPools>>,
}

impl PoolCatalog {
    /// Read each hero's first common graph binding once, before serving bodies.
    pub fn from_graphs(graphs: &GraphStore) -> io::Result<Self> {
        let Some(common) = graphs.graph(BODY_GRAPH) else {
            return Ok(Self::default());
        };
        let entries = common
            .fields
            .get("m_publicSchema")
            .and_then(|schema| schema.get("m_entries"))
            .ok_or_else(|| invalid("graph 0033 has no public schema entries"))?;
        let entries = match entries {
            Value::Array(entries) => entries.as_slice(),
            Value::Null => &[],
            _ => return Err(invalid("invalid public schema entries in graph 0033")),
        };
        let mut defaults = HealthPools {
            health: 0.0,
            armour: 0.0,
            shields: 0.0,
        };
        apply_numbers(&mut defaults, entries);

        let mut by_hero = HashMap::with_capacity(graphs.bodies().len());
        for body in graphs.bodies().values() {
            let Some(hero) = body.hero.as_deref().and_then(parse_guid) else {
                continue;
            };
            by_hero.entry(hero).or_insert_with(|| {
                let binding = body
                    .graphs
                    .iter()
                    .find(|binding| binding.graph == Some(BODY_GRAPH))?;
                let mut pools = defaults;
                if let Some(entries) = &binding.sync {
                    apply_numbers(&mut pools, entries);
                }
                Some(pools)
            });
        }
        Ok(Self { by_hero })
    }

    pub fn hero_pools(&self, hero: u64) -> Option<HealthPools> {
        self.by_hero.get(&hero).copied().flatten()
    }

    pub fn body_pools(&self, hero: u64) -> HealthPools {
        self.hero_pools(hero).unwrap_or(FALLBACK_POOLS)
    }
}

fn apply_numbers(pools: &mut HealthPools, entries: &[Value]) {
    for entry in entries {
        let Some(variable) = entry.get("m_0D09D2D9").and_then(asset_guid) else {
            continue;
        };
        let Some(value) = entry
            .get("m_value")
            .and_then(|outer| outer.get("m_value"))
            .and_then(Value::as_f64)
        else {
            continue;
        };
        match variable & 0xffff {
            HEALTH_VAR => pools.health = value,
            ARMOUR_VAR => pools.armour = value,
            SHIELDS_VAR => pools.shields = value,
            _ => {}
        }
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::game_heroes::{HeroCatalog, SOLDIER};
    use crate::world::update;
    use serde_json::json;

    fn entry(variable: u64, value: Value) -> Value {
        json!({
            "m_0D09D2D9": format!("0x{:016X}", 0x0d80_0000_0000_0000 | variable),
            "m_value": { "m_value": value }
        })
    }

    fn graph(fields: Value) -> Value {
        json!({
            "index": BODY_GRAPH,
            "header": {
                "m_nodesBitCount": 1, "m_statesBitCount": 1,
                "m_remoteSyncNodesBitCount": 1, "m_syncVarsBitCount": 1
            },
            "fields": fields, "items": 0, "containers": {}, "unknown_nodes": [],
            "nodes": [], "states": [], "entries": [], "remote": [], "sync_vars": []
        })
    }

    fn store(graphs: Vec<Value>, bodies: Value) -> GraphStore {
        let bundle = json!({"version": 1, "graphs": graphs, "bodies": bodies, "missing": []});
        GraphStore::from_json(&serde_json::to_vec(&bundle).unwrap()).unwrap()
    }

    #[test]
    fn common_defaults_and_first_body_binding_supply_numeric_pools() {
        let fields = json!({"m_publicSchema":{"m_entries":[
            entry(HEALTH_VAR, json!(1000)),
            entry(ARMOUR_VAR, json!(0)),
            entry(SHIELDS_VAR, json!(50.0))
        ]}});
        let body = |hero, graphs| {
            json!({
                "hero": hero, "name": "Fixture", "graphs": graphs, "manager": null, "weapons": []
            })
        };
        let binding = |graph, entries| {
            json!({
                "graph": graph, "m_75408640": 0, "m_1EB5A024": entries
            })
        };
        let bodies = json!({
            "0x10": body("0x1", vec![
                binding(BODY_GRAPH, json!([
                    entry(HEALTH_VAR, json!(250.0)),
                    entry(ARMOUR_VAR, json!(true)),
                    entry(HEALTH_VAR, json!("ignored")),
                    entry(SHIELDS_VAR, json!(0.0)),
                    entry(ARMOUR_VAR, json!(75.0)),
                    entry(999, json!(777))
                ])),
                binding(BODY_GRAPH, json!([entry(HEALTH_VAR, json!(500.0))]))
            ]),
            "0x11": body("0x2", vec![binding(BODY_GRAPH, Value::Null)]),
            "0x12": body("0x3", vec![binding(0x99, Value::Null)])
        });
        let graphs = store(vec![graph(fields)], bodies);
        let pools = PoolCatalog::from_graphs(&graphs).unwrap();
        let custom = pools.hero_pools(1).unwrap();
        assert_eq!(
            custom,
            HealthPools {
                health: 250.0,
                armour: 75.0,
                shields: 0.0
            }
        );
        assert_eq!(custom.total(), 325.0);
        assert_eq!(custom.vitals().maximum(), 325.0);
        assert_eq!(
            pools.hero_pools(2),
            Some(HealthPools {
                health: 1000.0,
                armour: 0.0,
                shields: 50.0
            })
        );
        assert_eq!(pools.hero_pools(3), None);
        assert_eq!(pools.body_pools(3), FALLBACK_POOLS);
        assert_eq!(pools.body_pools(4), FALLBACK_POOLS);
    }

    #[test]
    fn missing_common_graph_falls_back_but_invalid_schema_is_an_error() {
        let absent = store(Vec::new(), json!({}));
        assert_eq!(
            PoolCatalog::from_graphs(&absent)
                .unwrap()
                .body_pools(SOLDIER),
            FALLBACK_POOLS
        );
        let invalid = store(
            vec![graph(json!({"m_publicSchema":{"m_entries":42}}))],
            json!({}),
        );
        assert_eq!(
            PoolCatalog::from_graphs(&invalid).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 hero and graph catalogs"]
    fn all_hero_pools_match_python_defaults_overrides_and_component_bytes() {
        use sha2::{Digest, Sha256};
        use std::path::{Path, PathBuf};

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let catalog = HeroCatalog::load(root.join("data/game_heroes_174.json")).unwrap();
        let graphs = GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap();
        let pools = PoolCatalog::from_graphs(&graphs).unwrap();
        assert_eq!(pools.hero_pools(SOLDIER), Some(FALLBACK_POOLS));
        assert_eq!(
            pools.hero_pools(0x02e0_0000_0000_0007).unwrap().armour,
            200.0
        );
        assert_eq!(
            pools.hero_pools(0x02e0_0000_0000_0068).unwrap().shields,
            200.0
        );
        assert_eq!(catalog.heroes().count(), 32);
        let mut digest = Sha256::new();
        for (index, hero) in catalog.heroes().enumerate() {
            let values = pools.hero_pools(hero.guid).unwrap();
            let origin = index % 8;
            let record = update(origin, &[values.vitals().component()]).unwrap();
            digest.update(hero.guid.to_le_bytes());
            for value in [values.health, values.armour, values.shields] {
                digest.update(value.to_le_bytes());
            }
            digest.update(u32::try_from(origin).unwrap().to_le_bytes());
            digest.update(u32::try_from(record.bit_len()).unwrap().to_le_bytes());
            digest.update(record.as_bytes());
        }
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "5b60977de2bf545e1e4db4ec2087ddc30b3167b29b1fbc7a9cd7dfdb20733ac9"
        );
    }
}
