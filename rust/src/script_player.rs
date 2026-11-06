//! Prepared owner statescript for a live body in build 104319.

use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    sync::Arc,
};

use serde_json::Value as Json;

use crate::{
    accounts::parse_guid,
    commands::Command,
    game_combat::{PosedHitscan, eye_height},
    game_content::PRACTICE_RANGE_MODE,
    game_heroes::{Hero, SOLDIER},
    game_hitscan::HitDefinition,
    game_instance::RETAIL_QUANTA,
    game_pools::{BODY_GRAPH, FALLBACK_POOLS, HealthPools},
    game_replication::{BodyStep, OwnerBodyRuntime},
    mover::{Mover, sample_movement_flags},
    mover_modifiers::{Modifier, ModifierKey},
    script_codecs::CodecTable,
    script_expr::{EntityRelation, native_shape, to_float, valid_entity},
    script_graph::GraphStore,
    script_init::{Component, MAX_INIT_WORK, Root, Site, Snapshot, TargetTagEffect, World},
    script_runtime::FRAME_MS,
    script_snapshot_wire::{SnapshotEncoder, View},
    script_wire::{Value, Variables},
    vitals::{PoolKind, Vitals},
    world::{Component as EntityComponent, GAME_MODE_ENTITY, Movement},
};

#[derive(Clone, Copy)]
pub(crate) struct OwnerBodySpawn {
    pub controller: u32,
    pub entity: u32,
    pub body_filter: u64,
    pub frame: u32,
    pub pose: Movement,
    pub quanta: u32,
}

pub(crate) struct PlayerBodyFactory {
    graphs: Arc<GraphStore>,
    codecs: Arc<CodecTable>,
    mode: u64,
    hero: u64,
    roots: Vec<Root>,
    variables: Variables,
    pools: Option<HealthPools>,
    target_tags: Option<BTreeSet<u64>>,
    controller_on_fire: bool,
}

impl PlayerBodyFactory {
    pub(crate) fn prepare(
        graphs: Arc<GraphStore>,
        codecs: Arc<CodecTable>,
        mode: u64,
        hero: &Hero,
        pools: Option<HealthPools>,
        controller_on_fire: bool,
        probe: OwnerBodySpawn,
    ) -> io::Result<Self> {
        let body = graphs
            .bodies()
            .get(&hero.body)
            .filter(|body| body.hero.as_deref().and_then(parse_guid) == Some(hero.guid))
            .ok_or_else(|| {
                unsupported("the hero body definition is absent from the graph bundle")
            })?;
        let roots = crate::script_init::player_roots(body, &graphs).map_err(unsupported)?;
        let health = pools.unwrap_or(FALLBACK_POOLS).total();
        let variables = crate::script_init::player_entity_variables(body, health, health);
        let target_tags = body.target_tags.clone();
        let factory = Self {
            graphs,
            codecs,
            mode,
            hero: hero.guid,
            roots,
            variables,
            pools,
            target_tags,
            controller_on_fire,
        };
        let runtime = factory.start(probe)?;
        let encoder = runtime.encoder();
        let snapshot = runtime
            .snapshot()
            .expect("successful body startup has a snapshot");
        let projected = encoder
            .project(snapshot, View::Owner)
            .map_err(unsupported)?;
        encoder.full(&projected).map_err(unsupported)?;
        Ok(factory)
    }

    pub(crate) fn start(&self, spawn: OwnerBodySpawn) -> io::Result<PlayerBodyRuntime> {
        if spawn.quanta != RETAIL_QUANTA {
            return Err(unsupported("the owner VM requires the 16 ms retail clock"));
        }
        let has_authored_pools = self.roots.iter().any(|root| root.graph == BODY_GRAPH);
        let mut world = BodyWorld {
            graphs: Arc::clone(&self.graphs),
            controller: spawn.controller,
            entity: spawn.entity,
            body_filter: spawn.body_filter,
            position: spawn.pose.position,
            movement: spawn.pose,
            throttles: [0; 2],
            mode: self.mode,
            hero: self.hero,
            vitals: if has_authored_pools {
                Vitals::empty()
            } else {
                self.pools.unwrap_or(FALLBACK_POOLS).vitals()
            },
            pool_types_known: self.pools.is_some() || has_authored_pools,
            target_tags: self.target_tags.clone(),
            target_tag_effects: BTreeMap::new(),
            controller_on_fire: self.controller_on_fire,
        };
        let mut component = Component::start(
            Arc::clone(&self.graphs),
            spawn.entity,
            self.variables.clone(),
            self.roots.clone(),
            spawn.frame,
            &mut world,
            MAX_INIT_WORK,
        )
        .map_err(unsupported)?;
        component
            .advance_frame(spawn.frame, &mut world)
            .map_err(unsupported)?;
        world.vitals.clear_changed();
        let modifiers = component
            .movement_modifiers(&mut world)
            .map_err(unsupported)?;
        Ok(PlayerBodyRuntime {
            component,
            world,
            codecs: Arc::clone(&self.codecs),
            modifiers,
            frame: spawn.frame,
        })
    }
}

pub(crate) struct PlayerBodyRuntime {
    component: Component,
    world: BodyWorld,
    codecs: Arc<CodecTable>,
    modifiers: Option<BTreeMap<ModifierKey, Modifier>>,
    frame: u32,
}

impl OwnerBodyRuntime for PlayerBodyRuntime {
    fn vitals(&self) -> Option<&Vitals> {
        Some(&self.world.vitals)
    }

    fn take_health_update(&mut self) -> Option<EntityComponent> {
        let vitals = &mut self.world.vitals;
        if !vitals.has_changed() {
            return None;
        }
        vitals.clear_changed();
        Some(vitals.cumulative_component())
    }

    fn snapshot(&self) -> Option<&Snapshot> {
        self.component.snapshot()
    }

    fn encoder(&self) -> SnapshotEncoder<'_> {
        SnapshotEncoder::new(&self.world.graphs, &self.codecs)
    }

    fn advance(&mut self, command: Command, mover: &Mover, step: BodyStep) -> io::Result<()> {
        if command.frame <= self.frame
            || (step != BodyStep::UnposedTimer && command.frame != self.frame.saturating_add(1))
        {
            return Err(unsupported(
                "the owner VM frame is outside the command sequence",
            ));
        }
        let pose = mover.pose();
        self.world.position = pose.position;
        // Command gaps and timer advances retain the last movement record.
        if step == BodyStep::Posed {
            self.world.movement = pose;
        }
        if step == BodyStep::UnposedTimer {
            self.component
                .advance_frame(command.frame, &mut self.world)
                .map_err(unsupported)?;
        } else {
            self.world.throttles = [command.right, command.forward];
            self.component
                .advance_command(command, &mut self.world)
                .map_err(unsupported)?;
            self.modifiers = self
                .component
                .movement_modifiers(&mut self.world)
                .map_err(unsupported)?;
        }
        self.frame = command.frame;
        Ok(())
    }

    fn frame_ms(&self) -> u32 {
        FRAME_MS as u32
    }

    fn movement_modifiers(&self) -> Option<&BTreeMap<ModifierKey, Modifier>> {
        self.modifiers.as_ref()
    }

    fn take_hitscan(&mut self, command: Option<Command>, mover: &Mover) -> Vec<PosedHitscan> {
        let shots = self.component.take_shots().unwrap_or_default();
        let Some(command) = command else {
            return Vec::new();
        };
        let [x, y, z] = mover.pose().position;
        let origin = [x, y + eye_height(self.world.hero, mover.crouched()), z];
        shots
            .into_iter()
            .filter_map(|shot| {
                let node = self.component.shot_node(&shot)?;
                let mut evaluate = |config: &Json| {
                    self.component
                        .evaluate_shot_config(&shot, config, &mut self.world)
                };
                let speed = node.field("m_projectileSpeed").unwrap_or(&Json::Null);
                if !speed.is_null() && to_float(&evaluate(speed).ok()?) != 0.0 {
                    return None;
                }
                let hit = HitDefinition::from_authored(node.field("m_6396149F")?, &mut evaluate)
                    .ok()??;
                if hit.amount <= 0.0 {
                    return None;
                }
                let reach = node.field("m_FEC435C6").unwrap_or(&Json::Null);
                let reach = if reach.is_null() {
                    0.0
                } else {
                    to_float(&evaluate(reach).ok()?)
                };
                Some(PosedHitscan {
                    source: shot.entity,
                    origin,
                    command,
                    shot_frame: shot.frame,
                    reach,
                    hit,
                })
            })
            .collect()
    }
}

struct BodyWorld {
    graphs: Arc<GraphStore>,
    controller: u32,
    entity: u32,
    body_filter: u64,
    position: [f64; 3],
    movement: Movement,
    throttles: [i8; 2],
    mode: u64,
    hero: u64,
    vitals: Vitals,
    pool_types_known: bool,
    target_tags: Option<BTreeSet<u64>>,
    target_tag_effects: BTreeMap<u64, TargetTagCounts>,
    controller_on_fire: bool,
}

#[derive(Default)]
struct TargetTagCounts {
    grants: u32,
    suppressions: u32,
}

impl World for BodyWorld {
    fn ruleset(&mut self, id: u32) -> Option<Value> {
        if (self.mode, self.hero) != (PRACTICE_RANGE_MODE, SOLDIER) {
            return None;
        }
        // Defaults read by the target startup, from ow174/game/script/rulesets.py.
        match id {
            0x2538 | 0x2819 | 0x32e8 => Some(Value::Float(1.0)),
            0x260f => Some(Value::Bool(true)),
            0x281a | 0x32e0 | 0x32e9 => Some(Value::Bool(false)),
            _ => None,
        }
    }

    fn entity_variable(&mut self, entity: u32, id: u32) -> Value {
        if entity != GAME_MODE_ENTITY {
            return Value::Null;
        }
        self.graphs
            .mode_variables(self.mode)
            .and_then(|variables| variables.get(&id))
            .copied()
            .map_or(Value::Null, Value::from)
    }

    fn position(&mut self, entity: u32) -> [f64; 3] {
        if entity == self.entity {
            self.position
        } else {
            [0.0; 3]
        }
    }

    fn movement_sample(&self, site: Site) -> Option<u16> {
        if !self.is_player_site(site) {
            return None;
        }
        sample_movement_flags(&self.movement)
    }

    fn body_scale(&mut self, site: Site, target: u32, override_value: &Value) -> Option<f64> {
        if target != self.entity || !self.is_player_site(site) {
            return None;
        }
        match override_value {
            Value::Null | Value::Integer(1) => Some(1.0),
            Value::Float(scale) if *scale == 1.0 => Some(1.0),
            _ => None,
        }
    }

    fn body_health(&self, site: Site, kind: Option<PoolKind>, maximum: bool) -> Option<f64> {
        if !self.is_player_site(site) || (kind.is_some() && !self.pool_types_known) {
            return None;
        }
        let amount = self.vitals.amount(kind, maximum);
        (amount.is_finite() && amount >= 0.0).then_some(amount)
    }

    fn add_health_pool(
        &mut self,
        site: Site,
        kind: PoolKind,
        maximum: f64,
        current: f64,
    ) -> Option<u32> {
        if !self.is_player_site(site) || !self.pool_types_known {
            return None;
        }
        self.vitals.add_pool(kind, maximum, current)
    }

    fn health_pool_current(&self, site: Site, pool: u32) -> Option<f64> {
        if !self.is_player_site(site) {
            return None;
        }
        self.vitals.pool_current(pool)
    }

    fn remove_health_pool(&mut self, site: Site, pool: u32) -> Option<f64> {
        if !self.is_player_site(site) {
            return None;
        }
        self.vitals.remove_pool(pool)
    }

    fn has_target_tag(&self, site: Site, entity: u32, tag: u64) -> Option<bool> {
        if !self.is_player_site(site) || entity != self.entity {
            return None;
        }
        if let Some(counts) = self.target_tag_effects.get(&tag) {
            return Some(counts.suppressions == 0);
        }
        Some(self.target_tags.as_ref()?.contains(&tag))
    }

    fn set_target_tag_effect(&mut self, site: Site, effect: TargetTagEffect, active: bool) -> bool {
        if !self.is_player_site(site) || effect.entity != self.entity {
            return false;
        }
        let counts = if active {
            self.target_tag_effects.entry(effect.tag).or_default()
        } else {
            let Some(counts) = self.target_tag_effects.get_mut(&effect.tag) else {
                return false;
            };
            counts
        };
        // The authored removal can mask existing grants. A later grant's
        // precedence over an already-active suppression is still unknown.
        if active && effect.present && counts.suppressions != 0 {
            return false;
        }
        let count = if effect.present {
            &mut counts.grants
        } else {
            &mut counts.suppressions
        };
        let next = if active {
            count.checked_add(1)
        } else {
            count.checked_sub(1)
        };
        let Some(next) = next else {
            return false;
        };
        *count = next;
        if counts.grants == 0 && counts.suppressions == 0 {
            self.target_tag_effects.remove(&effect.tag);
        }
        true
    }

    fn entity_filter_flags(&self, site: Site, entity: u32) -> Option<u64> {
        (self.is_player_site(site) && entity == self.entity).then_some(self.body_filter)
    }

    fn body_on_fire(&mut self, site: Site, entity: u32) -> Option<bool> {
        self.related_entity(site, EntityRelation::CurrentPossessor, entity)
            .map(|_| self.controller_on_fire)
    }

    fn related_entity(&mut self, site: Site, relation: EntityRelation, entity: u32) -> Option<u32> {
        if !self.is_player_site(site)
            || !valid_entity(self.controller)
            || self.controller == self.entity
        {
            return None;
        }
        // Both relations point to the same controller for this human body.
        match relation {
            EntityRelation::OwningController | EntityRelation::CurrentPossessor
                if entity == self.entity =>
            {
                Some(self.controller)
            }
            EntityRelation::PossessedBody if entity == self.controller => Some(self.entity),
            // The authenticated human body has no AI control entity in this world.
            EntityRelation::AiControlEntity if entity == self.entity => Some(0),
            _ => None,
        }
    }

    fn native(&mut self, site: Site, config: &Json) -> Option<Value> {
        match config.get("$")?.as_str()? {
            "STU_1FF67CA6" => Some(Value::Entity(self.entity)),
            "STU_310D6529" => {
                const THROTTLE_UNIT: f32 = 1.0 / 127.0;
                let [right, forward] = self
                    .throttles
                    .map(|byte| f64::from(f32::from(byte) * THROTTLE_UNIT));
                Some(Value::Vector([right, forward, 0.0]))
            }
            "STU_37F20D24" => Some(Value::Asset(self.mode)),
            "STU_A3DEF368" if self.graphs.mode_variables(self.mode).is_some() => {
                Some(Value::Entity(GAME_MODE_ENTITY))
            }
            "STU_4A101787" => Some(Value::Bool(false)),
            // The normal human Practice Range reference takes the falsey
            // branch for this parameterless query. Its native meaning is open.
            "STU_A77AC2AC"
                if self.is_player_site(site)
                    && self.mode == PRACTICE_RANGE_MODE
                    && native_shape(config, "STU_A77AC2AC", None) =>
            {
                Some(Value::Null)
            }
            _ => None,
        }
    }
}

impl BodyWorld {
    fn is_player_site(&self, site: Site) -> bool {
        site.entity == self.entity && valid_entity(self.entity)
    }
}

fn unsupported(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::Unsupported, error.to_string())
}

#[cfg(test)]
pub(crate) fn fixture_body_factory() -> (PlayerBodyFactory, OwnerBodySpawn) {
    use serde_json::json;

    let body = 0x0400_0000_0000_0001;
    let hero = Hero {
        guid: SOLDIER,
        name: "Fixture".into(),
        role: "Damage".into(),
        body,
    };
    let graph = json!({
        "index":42,"items":0,"nodes":[],"states":[],"entries":[],"remote":[],
        "sync_vars":[],"containers":{},"unknown_nodes":[],"fields":{},
        "header":{"m_nodesBitCount":4,"m_statesBitCount":4,
            "m_remoteSyncNodesBitCount":4,"m_syncVarsBitCount":4}
    });
    let bundle = json!({"version":1,"missing":[],"graphs":[graph],"bodies":{
        format!("0x{body:016X}"): {"hero": format!("0x{SOLDIER:016X}"), "name":"Fixture",
            "graphs":[{"graph":42,"m_75408640":0}],"manager":42,"weapons":[null,42]}
    }});
    let graphs = Arc::new(GraphStore::from_json(&serde_json::to_vec(&bundle).unwrap()).unwrap());
    let codecs = Arc::new(CodecTable::from_json(r#"{"classes":{}}"#).unwrap());
    let spawn = OwnerBodySpawn {
        controller: 0xa000_0100,
        entity: 0xa000_0101,
        body_filter: crate::match_scene::team_bit(0).unwrap(),
        frame: 100,
        pose: Movement {
            position: [1.0, 2.0, 3.0],
            ..Movement::default()
        },
        quanta: RETAIL_QUANTA,
    };
    let factory = PlayerBodyFactory::prepare(
        graphs,
        codecs,
        PRACTICE_RANGE_MODE,
        &hero,
        None,
        false,
        spawn,
    )
    .unwrap();
    (factory, spawn)
}

#[cfg(test)]
pub(crate) fn fixture_hitscan_factory(damage: f64) -> PlayerBodyFactory {
    use serde_json::json;

    let graph = json!({
        "index":42,"items":3,"states":[1,2],"entries":[0],"remote":[],
        "sync_vars":[],"containers":{},"unknown_nodes":[],"fields":{"m_680A2CB2":[0]},
        "header":{"m_nodesBitCount":4,"m_statesBitCount":4,
            "m_remoteSyncNodesBitCount":4,"m_syncVarsBitCount":4},
        "nodes":[
            {"pos":0,"class":"STUStatescriptEntry","uid":0,"rid":0,"remote":-1,
                "state":null,"client_only":false,"server_only":false,"fields":{
                "m_outPlug":{"$":"Plug","links":[[1,"m_beginPlug"]],"m_FF3DAF1E":[]}}},
            {"pos":1,"class":"STUStatescriptStateLogicalButton","uid":1,"rid":1,"remote":-1,
                "state":0,"client_only":false,"server_only":true,"fields":{
                "m_2BBEEAB8":true,"m_logicalButton":20,
                "m_onGoingDownPlug":{"$":"Plug","links":[[2,"m_beginPlug"]],"m_FF3DAF1E":[]}}},
            {"pos":2,"class":"STUStatescriptStateWeaponVolley","uid":2,"rid":2,"remote":-1,
                "state":1,"client_only":false,"server_only":true,"fields":{
                "m_2BBEEAB8":true,"m_A17BE89B":2,"m_numShotsPerSecond":9,
                "m_FEC435C6":180,"m_6396149F":{"m_modifyHealth":{"m_amount":damage},"m_A872C70E":2}}}
        ]
    });
    let bundle = json!({"version":1,"missing":[],"graphs":[graph],"bodies":{}});
    let mut factory = fixture_body_factory().0;
    factory.graphs =
        Arc::new(GraphStore::from_json(&serde_json::to_vec(&bundle).unwrap()).unwrap());
    factory.roots = vec![Root::new(1, 42)];
    factory
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        collision_index::TriangleIndex, contact_world::ContactWorld,
        game_instance::TOURNAMENT_QUANTA, mover_settings::MoverSettings,
    };

    #[test]
    fn player_natives_follow_the_verified_owner_and_override_shapes() {
        use crate::script_expr::EvalError;
        use serde_json::json;

        let spawn = fixture_body_factory().1;
        let mut runtime = fixture_hitscan_factory(20.0).start(spawn).unwrap();
        let evaluate = |body: &mut PlayerBodyRuntime, config: &Json| {
            body.component
                .evaluate_state_config(1, 0, config, &mut body.world)
        };
        let owner = json!({"$":"STU_B5A0CAF0","m_EE729DCB":0});
        let size = json!({"$":"STU_FDBB0B67","m_entity":owner});
        assert_eq!(evaluate(&mut runtime, &size), Ok(Value::Float(1.0)));
        for (value, supported) in [
            (Value::Integer(1), true),
            (Value::Float(1.0), true),
            (Value::Bool(true), false),
            (Value::Float(1.25), false),
            (Value::Null, true),
        ] {
            runtime
                .component
                .set_entity_variable(31_210, value, &mut runtime.world)
                .unwrap();
            assert_eq!(
                evaluate(&mut runtime, &size),
                if supported {
                    Ok(Value::Float(1.0))
                } else {
                    Err(EvalError::UnsupportedConfig("STU_FDBB0B67".into()))
                }
            );
        }
        let dynamic = json!({"$":"STUConfigVarDynamic","m_identifier":400,"m_60DB8F99":true});
        let dynamic_size = json!({"$":"STU_FDBB0B67","m_entity":dynamic});
        for (entity, supported) in [(spawn.entity, true), (spawn.entity + 1, false), (0, false)] {
            runtime
                .component
                .set_entity_variable(400, Value::Entity(entity), &mut runtime.world)
                .unwrap();
            assert_eq!(
                evaluate(&mut runtime, &dynamic_size).ok(),
                supported.then_some(Value::Float(1.0))
            );
        }

        let ai = json!({"$":"STU_DB742151","m_entity":owner,"m_EE729DCB":0});
        assert_eq!(evaluate(&mut runtime, &ai), Ok(Value::Entity(0)));
        let path = json!({"$":"STU_D3EB8D87","m_00903303":ai,"m_EE729DCB":0});
        assert_eq!(evaluate(&mut runtime, &path), Ok(Value::Bool(false)));
        let other_path =
            json!({"$":"STU_D3EB8D87","m_00903303":{"$":"STU_DB742151","m_entity":dynamic}});
        assert_eq!(
            evaluate(&mut runtime, &other_path),
            Err(EvalError::UnsupportedConfig("STU_DB742151".into()))
        );
        let branch = json!({"$":"STU_A77AC2AC","m_EE729DCB":0});
        assert_eq!(evaluate(&mut runtime, &branch), Ok(Value::Null));
        runtime.world.mode = 0;
        assert_eq!(
            evaluate(&mut runtime, &branch),
            Err(EvalError::UnsupportedConfig("STU_A77AC2AC".into()))
        );
    }

    #[test]
    fn target_tag_effects_restore_authored_tags_and_remaining_grants() {
        let (mut factory, spawn) = fixture_body_factory();
        let authored = 0x0d60_0000_0000_0042;
        let tag = 0x0d60_0000_0000_0422;
        factory.target_tags = Some(BTreeSet::from([authored]));
        let world = &mut factory.start(spawn).unwrap().world;
        let site = Site {
            entity: spawn.entity,
            instance: 1,
            graph: 42,
            node: Some(1),
        };
        let read = |world: &BodyWorld, tag| world.has_target_tag(site, spawn.entity, tag);
        let grant = TargetTagEffect {
            entity: spawn.entity,
            tag,
            present: true,
        };
        let suppress = TargetTagEffect {
            present: false,
            ..grant
        };
        let mask_authored = TargetTagEffect {
            tag: authored,
            ..suppress
        };

        assert_eq!(read(world, authored), Some(true));
        assert!(world.set_target_tag_effect(site, mask_authored, true));
        assert_eq!(read(world, authored), Some(false));
        assert!(world.set_target_tag_effect(site, mask_authored, false));
        assert_eq!(read(world, authored), Some(true));

        assert_eq!(read(world, tag), Some(false));
        for _ in 0..2 {
            assert!(world.set_target_tag_effect(site, grant, true));
        }
        assert_eq!(read(world, tag), Some(true));
        for _ in 0..2 {
            assert!(world.set_target_tag_effect(site, suppress, true));
        }
        assert_eq!(read(world, tag), Some(false));
        assert!(!world.set_target_tag_effect(site, grant, true));
        assert!(world.set_target_tag_effect(site, grant, false));
        assert!(world.set_target_tag_effect(site, suppress, false));
        assert_eq!(read(world, tag), Some(false));
        assert!(world.set_target_tag_effect(site, suppress, false));
        assert_eq!(read(world, tag), Some(true));
        assert!(world.set_target_tag_effect(site, grant, false));
        assert_eq!(read(world, tag), Some(false));
        assert!(!world.set_target_tag_effect(site, grant, false));
    }

    #[test]
    fn target_tag_effects_keep_unknown_tags_and_replacement_independent() {
        let (factory, spawn) = fixture_body_factory();
        let tag = 0x0d60_0000_0000_0422;
        let site = Site {
            entity: spawn.entity,
            instance: 1,
            graph: 42,
            node: Some(1),
        };
        let read = |world: &BodyWorld| world.has_target_tag(site, spawn.entity, tag);
        let effect = TargetTagEffect {
            entity: spawn.entity,
            tag,
            present: true,
        };
        let mut runtime = factory.start(spawn).unwrap();
        assert_eq!(read(&runtime.world), None);
        let foreign = TargetTagEffect {
            entity: spawn.controller,
            ..effect
        };
        assert!(!runtime.world.set_target_tag_effect(site, foreign, true));
        assert!(runtime.world.set_target_tag_effect(site, effect, true));
        assert_eq!(read(&runtime.world), Some(true));
        assert_eq!(
            runtime.world.has_target_tag(site, spawn.entity, tag + 1),
            None
        );

        let replacement = factory
            .start(OwnerBodySpawn {
                frame: spawn.frame + 1,
                ..spawn
            })
            .unwrap();
        assert_eq!(read(&replacement.world), None);
        assert!(runtime.world.set_target_tag_effect(site, effect, false));
        assert_eq!(read(&runtime.world), None);
    }

    #[test]
    fn health_queries_read_the_authoritative_vitals() {
        use crate::script_expr::EvalError;
        use serde_json::json;

        let spawn = fixture_body_factory().1;
        let mut runtime = fixture_hitscan_factory(20.0).start(spawn).unwrap();
        let evaluate = |body: &mut PlayerBodyRuntime, config: &Json| {
            body.component
                .evaluate_state_config(1, 0, config, &mut body.world)
        };
        let owner = json!({"$": "STU_B5A0CAF0"});
        let current = json!({"$": "STU_32E6E4D8", "m_entity": owner});
        let maximum = json!({"$": "STU_7D7D2470", "m_entity": owner});
        assert_eq!(evaluate(&mut runtime, &current), Ok(Value::Float(200.0)));
        assert_eq!(evaluate(&mut runtime, &maximum), Ok(Value::Float(200.0)));
        runtime.world.vitals.damage(
            75.0,
            crate::vitals::Death {
                time: 0.0,
                frame: spawn.frame,
                killer: spawn.entity + 1,
            },
        );
        assert_eq!(evaluate(&mut runtime, &current), Ok(Value::Float(125.0)));
        assert_eq!(evaluate(&mut runtime, &maximum), Ok(Value::Float(200.0)));

        let target = json!({"$": "STUConfigVarDynamic", "m_identifier": 400, "m_60DB8F99": true});
        let current = json!({"$": "STU_32E6E4D8", "m_entity": target});
        for entity in [spawn.entity + 1, 0] {
            runtime
                .component
                .set_entity_variable(400, Value::Entity(entity), &mut runtime.world)
                .unwrap();
            assert_eq!(
                evaluate(&mut runtime, &current),
                Err(EvalError::UnsupportedConfig("STU_32E6E4D8".into()))
            );
        }
    }

    #[test]
    fn typed_health_queries_read_the_live_distribution_after_damage() {
        use crate::script_expr::EvalError;
        use serde_json::json;

        let (_, spawn) = fixture_body_factory();
        let mut factory = fixture_hitscan_factory(20.0);
        let mixed = HealthPools {
            health: 200.0,
            armour: 50.0,
            shields: 25.0,
        };
        let evaluate = |body: &mut PlayerBodyRuntime, config: &Json| {
            body.component
                .evaluate_state_config(1, 0, config, &mut body.world)
        };
        let query = |class, kind| {
            json!({
                "$":class, "m_entity":{"$":"STU_B5A0CAF0"}, "m_7BE02F89":null,
                "m_7E8B666C":{"$":"STU_297E1A30","m_7E8B666C":kind,"m_EE729DCB":0}
            })
        };
        let current = "STU_783B4CF0";
        let maximum = "STU_68883EAC";
        let unsupported = |class: &str| Err(EvalError::UnsupportedConfig(class.into()));

        let mut unknown = factory.start(spawn).unwrap();
        assert_eq!(
            evaluate(&mut unknown, &query(current, 0)),
            unsupported(current)
        );
        factory.pools = Some(FALLBACK_POOLS);
        let mut single = factory.start(spawn).unwrap();
        let hit = crate::vitals::Death {
            time: 0.0,
            frame: spawn.frame,
            killer: spawn.entity + 1,
        };
        single.world.vitals.damage(75.0, hit);
        for (kind, amount) in [(0, 125.0), (1, 0.0), (2, 0.0)] {
            assert_eq!(
                evaluate(&mut single, &query(current, kind)),
                Ok(Value::Float(amount))
            );
        }

        factory.pools = Some(mixed);
        for id in [253, 1732, 1737] {
            factory.variables.insert(id, Value::Float(mixed.total()));
        }
        let mut body = factory.start(spawn).unwrap();
        for (kind, amount) in [(0, 200.0), (1, 50.0), (2, 25.0)] {
            for class in [current, maximum] {
                assert_eq!(
                    evaluate(&mut body, &query(class, kind)),
                    Ok(Value::Float(amount))
                );
            }
        }
        assert!(body.take_health_update().is_none());
        assert_eq!(body.world.vitals.damage(30.0, hit).dealt, 27.5);
        for (kind, amount) in [(0, 200.0), (1, 47.5), (2, 0.0)] {
            assert_eq!(
                evaluate(&mut body, &query(current, kind)),
                Ok(Value::Float(amount))
            );
        }
        assert_eq!(
            evaluate(&mut body, &query(maximum, 2)),
            Ok(Value::Float(25.0))
        );
        assert_eq!(
            body.take_health_update(),
            Some(body.world.vitals.component())
        );
        assert!(body.take_health_update().is_none());
        assert!(body.world.vitals.damage(1000.0, hit).killed);
        for kind in [0, 1, 2] {
            assert_eq!(
                evaluate(&mut body, &query(current, kind)),
                Ok(Value::Float(0.0))
            );
        }
        let mut named = query(current, 0);
        named["m_7BE02F89"] = json!({"$":"STUConfigVarIdentifier","m_identifier":"0x01"});
        let mut foreign = query(current, 0);
        foreign["m_entity"] = json!({"$":"STU_F76C486C","m_D63CBEC2":{"$":"STU_B5A0CAF0"}});
        for config in [named, query(current, 3), foreign] {
            assert_eq!(evaluate(&mut single, &config), unsupported(current));
        }
    }

    #[test]
    fn entity_relations_resolve_only_the_active_player_pair() {
        use crate::script_expr::EvalError;
        use serde_json::json;

        let (_, mut spawn) = fixture_body_factory();
        let factory = fixture_hitscan_factory(20.0);
        spawn.controller = 0xa012_3400;
        let replacement = OwnerBodySpawn {
            entity: 0xa012_7856,
            ..spawn
        };
        let mut first = factory.start(spawn).unwrap();
        let mut second = factory.start(replacement).unwrap();
        let target = json!({"$":"STUConfigVarDynamic","m_identifier":400,"m_60DB8F99":true});
        let owner = json!({"$":"STU_F76C486C","m_D63CBEC2":target,"m_EE729DCB":0});
        let possessor = json!({"$":"STU_F7262B36","m_801B298F":target});
        let body = json!({"$":"STU_0AC15F78","m_entity":target});
        let invalid_owner = json!({"$":"STU_F76C486C","m_D63CBEC2":target,"m_EE729DCB":1});
        let evaluate = |runtime: &mut PlayerBodyRuntime, config: &Json, target: Value| {
            runtime
                .component
                .set_entity_variable(400, target, &mut runtime.world)
                .unwrap();
            runtime
                .component
                .evaluate_state_config(1, 0, config, &mut runtime.world)
        };
        let unsupported_owner = || Err(EvalError::UnsupportedConfig("STU_F76C486C".into()));
        let unsupported_body = || Err(EvalError::UnsupportedConfig("STU_0AC15F78".into()));
        assert_eq!(
            evaluate(&mut first, &owner, Value::Entity(spawn.entity)),
            Ok(Value::Entity(spawn.controller))
        );
        assert_eq!(
            evaluate(&mut first, &possessor, Value::Entity(spawn.entity)),
            Ok(Value::Entity(spawn.controller))
        );
        assert_eq!(
            evaluate(&mut first, &body, Value::Entity(spawn.controller)),
            Ok(Value::Entity(spawn.entity))
        );
        assert_eq!(
            evaluate(&mut second, &body, Value::Entity(spawn.controller)),
            Ok(Value::Entity(replacement.entity))
        );
        assert_eq!(
            evaluate(&mut second, &owner, Value::Entity(replacement.entity)),
            Ok(Value::Entity(spawn.controller))
        );
        assert_eq!(
            evaluate(&mut second, &possessor, Value::Entity(replacement.entity)),
            Ok(Value::Entity(spawn.controller))
        );
        for entity in [spawn.entity, spawn.controller, 0] {
            assert_eq!(
                evaluate(&mut second, &owner, Value::Entity(entity)),
                unsupported_owner()
            );
        }
        assert_eq!(
            evaluate(&mut second, &body, Value::Entity(replacement.entity)),
            unsupported_body()
        );
        assert_eq!(
            evaluate(&mut second, &possessor, Value::Entity(spawn.entity)),
            Err(EvalError::UnsupportedConfig("STU_F7262B36".into()))
        );
        assert_eq!(
            evaluate(
                &mut second,
                &body,
                Value::Integer(i64::from(spawn.controller))
            ),
            unsupported_body()
        );
        assert_eq!(
            evaluate(
                &mut second,
                &invalid_owner,
                Value::Entity(replacement.entity)
            ),
            unsupported_owner()
        );
    }

    #[test]
    fn verified_body_starts_fresh_instances_and_advances_only_its_own_retail_clock() {
        let (factory, spawn) = fixture_body_factory();
        assert!(
            factory
                .start(OwnerBodySpawn {
                    quanta: TOURNAMENT_QUANTA,
                    ..spawn
                })
                .is_err()
        );
        let mut first = factory.start(spawn).unwrap();
        let second = factory
            .start(OwnerBodySpawn {
                entity: spawn.entity + 1,
                ..spawn
            })
            .unwrap();
        let snapshot = first.snapshot().unwrap();
        assert_eq!(
            snapshot.instances.keys().copied().collect::<Vec<_>>(),
            [1, 2, 3]
        );
        assert_eq!(snapshot.instances[&3].variables[&28], Value::Integer(1));
        assert_eq!(snapshot.variables[&1732], Value::Float(200.0));
        assert_eq!(first.movement_modifiers().map(BTreeMap::len), Some(0));
        assert_eq!(first.world.position(spawn.entity), spawn.pose.position);
        assert_eq!(first.world.movement, spawn.pose);
        assert_eq!(second.snapshot().unwrap().entity, spawn.entity + 1);

        let site = Site {
            entity: spawn.entity,
            instance: 1,
            graph: 42,
            node: None,
        };
        assert_eq!(first.world.movement_sample(site), Some(2));
        assert_eq!(second.world.movement_sample(site), None);
        let mut mover = Mover::new(
            MoverSettings::default(),
            Arc::new(ContactWorld::new(Vec::new()).unwrap()),
            spawn.pose.position,
            0,
            0x18,
        )
        .unwrap();
        let command = Command {
            frame: 101,
            right: -63,
            forward: 64,
            ..Command::default()
        };
        mover
            .step(
                &TriangleIndex::new([]).unwrap(),
                command,
                0.016,
                |mover, command| first.advance(command, mover, BodyStep::Posed).unwrap(),
            )
            .unwrap();
        assert_eq!(first.world.position(spawn.entity), mover.pose().position);
        let posed = mover.pose();
        let sample = sample_movement_flags(&posed).expect("a regular posed movement record");
        assert_eq!(first.world.movement, posed);
        assert_eq!(first.world.movement_sample(site), Some(sample));
        assert_eq!(first.component.now_ms(), 102 * FRAME_MS - 1);
        assert_eq!(second.component.now_ms(), 101 * FRAME_MS - 1);
        let gap = Command {
            frame: 104,
            ..command
        };
        assert!(first.advance(gap, &mover, BodyStep::Posed).is_err());
        assert_eq!(first.component.now_ms(), 102 * FRAME_MS - 1);
        let unposed_mover = Mover::new(
            MoverSettings::default(),
            Arc::new(ContactWorld::new(Vec::new()).unwrap()),
            [4.0, 5.0, 6.0],
            0,
            0x18,
        )
        .unwrap();
        first
            .advance(
                Command {
                    frame: 102,
                    ..Command::default()
                },
                &unposed_mover,
                BodyStep::UnposedBoundary,
            )
            .unwrap();
        assert_eq!(first.world.movement, posed);
        first
            .advance(
                Command {
                    frame: 200,
                    ..Command::default()
                },
                &unposed_mover,
                BodyStep::UnposedTimer,
            )
            .unwrap();
        assert_eq!(first.world.movement, posed);
        first
            .advance(
                Command {
                    frame: 201,
                    ..Command::default()
                },
                &mover,
                BodyStep::UnposedBoundary,
            )
            .unwrap();
        assert_eq!(first.world.throttles, [0, 0]);
        assert_eq!(first.component.now_ms(), 202 * FRAME_MS - 1);
    }

    #[test]
    fn posed_volley_keeps_the_authored_shot_frame_and_drains_unposed_reports() {
        let factory = fixture_hitscan_factory(20.0);
        let (_, spawn) = fixture_body_factory();
        let mut runtime = factory.start(spawn).unwrap();
        let mover = Mover::new(
            MoverSettings::default(),
            Arc::new(ContactWorld::new(Vec::new()).unwrap()),
            spawn.pose.position,
            0,
            0x18,
        )
        .unwrap();
        let fire = Command {
            frame: 101,
            buttons: crate::commands::FIRE,
            ..Command::default()
        };
        runtime.advance(fire, &mover, BodyStep::Posed).unwrap();
        let command = Command {
            frame: 102,
            yaw: 12,
            pitch: 30,
            ..Command::default()
        };
        let shots = runtime.take_hitscan(Some(command), &mover);
        let [shot] = shots.as_slice() else {
            panic!("expected the opening shot")
        };
        assert_eq!(shot.source, spawn.entity);
        assert_eq!(shot.origin, [1.0, 3.55, 3.0]);
        assert_eq!(shot.shot_frame, 101);
        assert_eq!(shot.command, command);
        assert_eq!(
            (shot.reach, shot.hit.amount, shot.hit.critical),
            (180.0, 20.0, 2.0)
        );
        assert!(runtime.take_hitscan(Some(command), &mover).is_empty());

        runtime
            .advance(command, &mover, BodyStep::UnposedBoundary)
            .unwrap();
        assert_eq!(
            runtime.snapshot().unwrap().instances[&1].states[&0].kind,
            crate::script_init::StateKind::LogicalButton { down: false }
        );
        let timer = Command {
            frame: 107,
            ..Command::default()
        };
        runtime
            .advance(timer, &mover, BodyStep::UnposedTimer)
            .unwrap();
        assert!(runtime.take_hitscan(None, &mover).is_empty());
        assert!(runtime.take_hitscan(Some(timer), &mover).is_empty());
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 statescript catalog"]
    fn prepared_soldier_starts_the_reference_body_and_weapon_tree() {
        use crate::{
            game_clock::FIRST_TICK, game_content::GameMap, script_codecs::PayloadMode,
            script_init::StateKind,
        };
        use std::path::PathBuf;

        let root = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let graphs = Arc::new(GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap());
        let pools = crate::game_pools::PoolCatalog::from_graphs(&graphs)
            .unwrap()
            .hero_pools(SOLDIER)
            .unwrap();
        let body = &graphs.bodies()[&0x0400_0000_0000_03cf];
        let roots = crate::script_init::player_roots(body, &graphs).unwrap();
        let health = pools.total();
        let variables = crate::script_init::player_entity_variables(body, health, health);
        let mut world = BodyWorld {
            graphs: Arc::clone(&graphs),
            controller: 0xa000_0100,
            entity: 0xa000_0101,
            body_filter: crate::match_scene::team_bit(0).unwrap(),
            position: GameMap::practice_range().spawn_point(0, 0).unwrap().0,
            movement: Movement::default(),
            throttles: [0; 2],
            mode: PRACTICE_RANGE_MODE,
            hero: SOLDIER,
            vitals: Vitals::empty(),
            pool_types_known: true,
            target_tags: body.target_tags.clone(),
            target_tag_effects: BTreeMap::new(),
            controller_on_fire: false,
        };
        let mut component = Component::start(
            graphs,
            world.entity,
            variables,
            roots,
            FIRST_TICK,
            &mut world,
            MAX_INIT_WORK,
        )
        .unwrap();
        component.advance_frame(FIRST_TICK, &mut world).unwrap();
        let snapshot = component.snapshot().unwrap();
        for diagnostic in &snapshot.diagnostics {
            eprintln!(
                "{:04X}:{:?} {:?}",
                diagnostic.site.graph, diagnostic.site.node, diagnostic.issue
            );
        }
        // The frozen BodyScript replay allocates the same IDs with the team
        // mask. The exit retires 08B6 (9) and its unstarted child 0BF8 (25).
        let expected = [
            (1, 0x0033, None),
            (2, 0x004b, None),
            (3, 0x0043, None),
            (4, 0x0251, None),
            (5, 0x0255, None),
            (6, 0x0257, None),
            (7, 0x0259, None),
            (8, 0x091b, None),
            (10, 0x0015, None),
            (11, 0x0254, None),
            (12, 0x01c7, Some((2, 0))),
            (13, 0x01cf, Some((2, 3))),
            (14, 0x02c5, Some((2, 5))),
            (15, 0x0b8b, Some((2, 10))),
            (16, 0x0c7d, Some((2, 18))),
            (17, 0x01ca, Some((2, 1))),
            (18, 0x0ba4, Some((2, 11))),
            (19, 0x05ac, Some((2, 8))),
            (20, 0x02cf, Some((2, 6))),
            (21, 0x0b3b, Some((2, 9))),
            (22, 0x01d8, Some((2, 4))),
            (23, 0x0f82, Some((2, 21))),
            (24, 0x05ad, Some((2, 7))),
            (26, 0x0e8f, Some((13, 32))),
        ];
        let instances: Vec<_> = snapshot
            .instances
            .iter()
            .map(|(&id, instance)| (id, instance.graph, instance.parent))
            .collect();
        assert_eq!(instances, expected);
        let movement_graph = &snapshot.instances[&12];
        let movement = &movement_graph.states[&4];
        assert!(movement.active);
        assert_eq!(movement.kind, StateKind::MovementSample(2));
        assert_eq!(snapshot.variables[&545], Value::Bool(true));
        assert!(
            !snapshot
                .diagnostics
                .iter()
                .any(|item| item.site.graph == 0x01c7 && item.site.node == Some(7))
        );
        let body_pools = &snapshot.instances[&1];
        assert!(body_pools.states[&0].active);
        assert_eq!(
            body_pools.states[&0].kind,
            StateKind::HealthPool { pool: Some(1) }
        );
        assert_eq!(body_pools.variables[&520], Value::Float(200.0));
        assert_eq!(world.vitals.component(), pools.vitals().component());
        let target_tag = &snapshot.instances[&12].states[&31];
        let effect = TargetTagEffect {
            entity: world.entity,
            tag: 0x0d60_0000_0000_0422,
            present: true,
        };
        assert!(target_tag.active);
        assert_eq!(
            target_tag.kind,
            StateKind::TargetTag {
                effect: Some(effect)
            }
        );
        let site = Site {
            entity: world.entity,
            instance: 12,
            graph: 0x01c7,
            node: Some(96),
        };
        assert_eq!(
            world.has_target_tag(site, world.entity, effect.tag),
            Some(true)
        );
        assert!(
            !snapshot
                .diagnostics
                .iter()
                .any(|diagnostic| diagnostic.site == site)
        );
        let Value::Map(messages) = &snapshot.instances[&24].variables[&29859] else {
            panic!("the authored message table must be a map");
        };
        assert_eq!(messages.len(), 20);
        assert_eq!(
            messages[0],
            (
                Value::Asset(0x0a90_0000_0000_0171),
                Value::Asset(0x0240_0000_0000_05ad)
            )
        );
        assert_eq!(
            snapshot.instances[&1].variables[&9737],
            Value::Entity(world.entity)
        );
        assert!(
            !snapshot
                .diagnostics
                .iter()
                .any(|item| item.site.graph == 0x0033 && item.site.node == Some(51))
        );
        let watch = &snapshot.instances[&13].states[&21];
        assert!(watch.active);
        assert_eq!(watch.kind, crate::script_init::StateKind::Watch);
        assert!(
            !snapshot
                .diagnostics
                .iter()
                .any(|item| item.site.graph == 0x01cf && item.site.node == Some(41))
        );
        assert_eq!(snapshot.instances[&11].variables[&28], Value::Integer(1));
        for (instance, state) in [(10, 42), (10, 43), (11, 83), (13, 138), (13, 139), (14, 30)] {
            let controller = &snapshot.instances[&instance].states[&state];
            assert!(controller.active);
            assert_eq!(controller.kind, crate::script_init::StateKind::Plain);
        }
        let tips = &snapshot.instances[&4].states[&0];
        assert!(tips.active);
        assert_eq!(tips.kind, StateKind::Plain);
        let codecs = CodecTable::load(root.join("data/statescript_codecs_174.json")).unwrap();
        for mode in [
            PayloadMode::Remote { full: true },
            PayloadMode::Owner {
                full: true,
                frame_time: i64::from(FIRST_TICK) * FRAME_MS as i64,
                frame_ms: FRAME_MS as u32,
            },
        ] {
            for class in [
                "STU_CD46AF93",
                "STU_F5A99B2A",
                "STU_D9843704",
                "STU_E56BA8D8",
            ] {
                let codec = codecs.codec(class).unwrap();
                assert_eq!(codec.payload(&BTreeMap::new(), mode).unwrap().bit_len(), 0);
            }
        }
        for (instance, raw, position) in [(1, 13, 39), (2, 22, 32), (2, 23, 33), (10, 39, 73)] {
            let instance = &snapshot.instances[&instance];
            let node = world
                .graphs
                .graph(instance.graph)
                .unwrap()
                .state(usize::from(raw))
                .unwrap();
            assert_eq!((node.pos, node.class.as_str()), (position, "STU_CD46AF93"));
            let state = &instance.states[&raw];
            assert!(state.active);
            assert_eq!(state.kind, StateKind::Plain);
        }
        for (instance, raw) in [(1, 4), (10, 41)] {
            let child = &snapshot.instances[&instance].states[&raw];
            assert!(child.active);
            assert_eq!(child.kind, StateKind::BooleanSwitch(false));
        }
        let selection = &snapshot.instances[&18].states[&0];
        assert!(selection.active);
        assert_eq!(selection.kind, StateKind::BooleanSwitch(false));
        assert!(!snapshot.instances[&18].states.contains_key(&1));
        let on_fire = &snapshot.instances[&13].states[&17];
        assert!(on_fire.active);
        assert_eq!(on_fire.kind, StateKind::BooleanSwitch(false));
        assert!(!snapshot.diagnostics.iter().any(|diagnostic| {
            diagnostic.site.graph == 0x01cf && diagnostic.site.node == Some(31)
        }));
        for (graph, node) in [
            (0x0033, 1),
            (0x0033, 66),
            (0x004b, 19),
            (0x004b, 20),
            (0x08b6, 3),
            (0x08b6, 11),
            (0x0ba4, 10),
        ] {
            assert!(!snapshot.diagnostics.iter().any(|diagnostic| {
                diagnostic.site.graph == graph && diagnostic.site.node == Some(node)
            }));
        }
        let filter_diagnostic = snapshot
            .diagnostics
            .iter()
            .any(|diagnostic| diagnostic.site.graph == 0x004b && diagnostic.site.node == Some(21));
        assert_eq!(filter_diagnostic, world.target_tags.is_none());
        for raw in [12, 13] {
            let filter = &snapshot.instances[&2].states[&raw];
            assert!(filter.active);
            assert_eq!(filter.kind, StateKind::BooleanSwitch(true));
        }
        if world.target_tags.is_some() {
            let filter = &snapshot.instances[&2].states[&14];
            assert!(filter.active);
            assert_eq!(filter.kind, StateKind::BooleanSwitch(false));
        }

        let authored_graphs = Arc::clone(&world.graphs);
        let authored_field = |graph, position, field: &str| {
            authored_graphs
                .graph(graph)
                .unwrap()
                .node(position)
                .unwrap()
                .field(field)
                .unwrap()
                .clone()
        };
        let sprint_condition = authored_field(0x0255, 6, "m_condition");
        let watch_possessor = authored_field(0x01cf, 41, "m_B7A3CF78");
        let shield_refill = authored_field(0x0033, 66, "m_BD83C8E6");
        let health_present = authored_field(0x08b6, 2, "m_condition");
        let filter = authored_field(0x004b, 20, "m_condition");
        let mask = world.body_filter;
        for (flags, passes) in [(mask, true), (mask | 2, false)] {
            world.body_filter = flags;
            assert_eq!(
                component.evaluate_state_config(2, 13, &filter, &mut world),
                Ok(Value::Bool(passes))
            );
        }
        world.body_filter = mask;
        let on_fire = authored_field(0x01cf, 31, "m_condition");
        for value in [false, true] {
            world.controller_on_fire = value;
            assert_eq!(
                component.evaluate_state_config(13, 17, &on_fire, &mut world),
                Ok(Value::Bool(value))
            );
        }
        world.controller_on_fire = false;
        let entries = authored_field(0x05ad, 240, "m_value");
        let mut message = authored_field(0x05ad, 183, "m_gameMessage");
        // Supply one authored key as a literal rather than simulating its UI input.
        message["m_configVars"] = serde_json::json!([entries["m_configVars"][0]["m_key"].clone()]);
        message["m_expression"]["m_opcodes"] = serde_json::json!([8, 0, 14, 0, 17, 0]);
        assert_eq!(
            component.evaluate_state_config(24, 0, &message, &mut world),
            Ok(Value::Asset(0x0240_0000_0000_05ad))
        );
        if let Some(original_tags) = world.target_tags.clone() {
            let filter = authored_field(0x004b, 21, "m_condition");
            let tag_query = authored_field(0x0043, 45, "m_condition");
            let required = crate::script_graph::target_tag_guid(&tag_query["m_88B0050A"]).unwrap();
            assert!(!original_tags.contains(&required));
            for present in [false, true] {
                if present {
                    world.target_tags.as_mut().unwrap().insert(required);
                }
                assert_eq!(
                    component.evaluate_state_config(2, 14, &filter, &mut world),
                    Ok(Value::Bool(present))
                );
                assert_eq!(
                    component.evaluate_state_config(3, 0, &tag_query, &mut world),
                    Ok(Value::Bool(present))
                );
            }
            world.target_tags = Some(original_tags);
        }
        let rifle_conditions =
            [81, 83].map(|position| authored_field(0x0254, position, "m_condition"));
        // 01C7 st48 reads the same current and maximum health as the
        // health-ratio branches in 01CF st7/st9.
        let full_health = authored_field(0x01c7, 145, "m_condition");
        assert_eq!(
            component.evaluate_state_config(13, 21, &watch_possessor, &mut world),
            Ok(Value::Entity(world.controller))
        );
        assert_eq!(
            component.evaluate_state_config(1, 27, &shield_refill, &mut world),
            Ok(Value::Bool(false))
        );
        assert_eq!(
            component.evaluate_state_config(12, 48, &health_present, &mut world),
            Ok(Value::Bool(true))
        );
        assert_eq!(
            component.evaluate_state_config(12, 48, &full_health, &mut world),
            Ok(Value::Bool(true))
        );
        // 0255 st2 follows the throttle comparison for a human player.
        for (forward, enabled) in [(63, false), (64, true)] {
            world.throttles = [0, forward];
            assert_eq!(
                component.evaluate_state_config(5, 2, &sprint_condition, &mut world),
                Ok(Value::Bool(enabled))
            );
        }
        // Both 0254 rifle branches have the same parameterless human condition.
        for condition in rifle_conditions {
            assert_eq!(
                component.evaluate_state_config(11, 10, &condition, &mut world),
                Ok(Value::Null)
            );
        }
        world.vitals.damage(
            75.0,
            crate::vitals::Death {
                time: 0.0,
                frame: FIRST_TICK + 1,
                killer: world.entity + 1,
            },
        );
        component.advance_frame(FIRST_TICK + 1, &mut world).unwrap();
        let damaged = component.snapshot().unwrap();
        assert_eq!(damaged.variables[&253], Value::Float(125.0));
        assert_eq!(damaged.variables[&1732], Value::Float(125.0));
        assert_eq!(damaged.variables[&1737], Value::Float(200.0));
        assert_eq!(damaged.instances[&1].variables[&520], Value::Float(125.0));
        assert_eq!(
            component.evaluate_state_config(12, 48, &full_health, &mut world),
            Ok(Value::Bool(false))
        );
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 hero and graph catalogs"]
    fn authored_armour_and_shield_roots_reproduce_the_reference_body_pools() {
        use crate::game_clock::FIRST_TICK;
        use std::path::PathBuf;

        let root = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let heroes =
            crate::game_heroes::HeroCatalog::load(root.join("data/game_heroes_174.json")).unwrap();
        let graphs = Arc::new(GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap());
        let catalog = crate::game_pools::PoolCatalog::from_graphs(&graphs).unwrap();
        for (hero, secondary_state) in [(0x02e0_0000_0000_0007, 3), (0x02e0_0000_0000_0068, 1)] {
            let body = &graphs.bodies()[&heroes.get(hero).unwrap().body];
            let pools = catalog.hero_pools(hero).unwrap();
            let roots = crate::script_init::player_roots(body, &graphs)
                .unwrap()
                .into_iter()
                .filter(|root| root.graph == BODY_GRAPH)
                .collect();
            let health = pools.total();
            let mut world = BodyWorld {
                graphs: Arc::clone(&graphs),
                controller: 0xa000_0100,
                entity: 0xa000_0101,
                body_filter: crate::match_scene::team_bit(0).unwrap(),
                position: [0.0; 3],
                movement: Movement::default(),
                throttles: [0; 2],
                mode: PRACTICE_RANGE_MODE,
                hero,
                vitals: Vitals::empty(),
                pool_types_known: true,
                target_tags: body.target_tags.clone(),
                target_tag_effects: BTreeMap::new(),
                controller_on_fire: false,
            };
            let component = Component::start(
                Arc::clone(&graphs),
                world.entity,
                crate::script_init::player_entity_variables(body, health, health),
                roots,
                FIRST_TICK,
                &mut world,
                MAX_INIT_WORK,
            )
            .unwrap();
            let snapshot = component.snapshot().unwrap();
            assert!(!snapshot.diagnostics.iter().any(|item| {
                matches!(item.issue, crate::script_init::Issue::UnsupportedField(field) if field.starts_with("health pool"))
            }));
            let states = &snapshot.instances[&1].states;
            assert_eq!(
                states[&secondary_state].kind,
                crate::script_init::StateKind::HealthPool { pool: Some(1) }
            );
            assert_eq!(
                states[&0].kind,
                crate::script_init::StateKind::HealthPool { pool: Some(2) }
            );
            assert_eq!(world.vitals.component(), pools.vitals().component());
        }
    }
}
