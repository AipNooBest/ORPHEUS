//! Prepared owner statescript for a live body in build 104319.

use std::{collections::BTreeMap, io, sync::Arc};

use serde_json::Value as Json;

use crate::{
    accounts::parse_guid,
    commands::Command,
    game_content::PRACTICE_RANGE_MODE,
    game_heroes::{Hero, SOLDIER},
    game_instance::RETAIL_QUANTA,
    game_replication::OwnerBodyRuntime,
    mover::Mover,
    mover_modifiers::{Modifier, ModifierKey},
    script_codecs::CodecTable,
    script_graph::GraphStore,
    script_init::{Component, MAX_INIT_WORK, Root, Site, Snapshot, World},
    script_runtime::FRAME_MS,
    script_snapshot_wire::{SnapshotEncoder, View},
    script_wire::{Value, Variables},
    world::GAME_MODE_ENTITY,
};

#[derive(Clone, Copy)]
pub(crate) struct OwnerBodySpawn {
    pub entity: u32,
    pub frame: u32,
    pub position: [f64; 3],
    pub quanta: u32,
}

pub(crate) struct PlayerBodyFactory {
    graphs: Arc<GraphStore>,
    codecs: Arc<CodecTable>,
    mode: u64,
    hero: u64,
    roots: Vec<Root>,
    variables: Variables,
}

impl PlayerBodyFactory {
    pub(crate) fn prepare(
        graphs: Arc<GraphStore>,
        codecs: Arc<CodecTable>,
        mode: u64,
        hero: &Hero,
        health: f64,
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
        let variables = crate::script_init::player_entity_variables(body, health, health);
        let factory = Self {
            graphs,
            codecs,
            mode,
            hero: hero.guid,
            roots,
            variables,
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
        let mut world = BodyWorld {
            graphs: Arc::clone(&self.graphs),
            entity: spawn.entity,
            position: spawn.position,
            throttles: [0; 2],
            mode: self.mode,
            hero: self.hero,
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
    fn snapshot(&self) -> Option<&Snapshot> {
        self.component.snapshot()
    }

    fn encoder(&self) -> SnapshotEncoder<'_> {
        SnapshotEncoder::new(&self.world.graphs, &self.codecs)
    }

    fn advance(&mut self, command: Command, mover: &Mover) -> io::Result<()> {
        // The mover already supplies recoverable poses. A larger unposed gap
        // still needs the reference's separate script timer catch-up.
        if command.frame != self.frame.saturating_add(1) {
            return Err(unsupported("the owner VM has an unposed command gap"));
        }
        self.world.position = mover.pose().position;
        self.world.throttles = [command.right, command.forward];
        self.component
            .advance_command(command, &mut self.world)
            .map_err(unsupported)?;
        self.modifiers = self
            .component
            .movement_modifiers(&mut self.world)
            .map_err(unsupported)?;
        self.frame = command.frame;
        Ok(())
    }

    fn frame_ms(&self) -> u32 {
        FRAME_MS as u32
    }

    fn movement_modifiers(&self) -> Option<&BTreeMap<ModifierKey, Modifier>> {
        self.modifiers.as_ref()
    }
}

struct BodyWorld {
    graphs: Arc<GraphStore>,
    entity: u32,
    position: [f64; 3],
    throttles: [i8; 2],
    mode: u64,
    hero: u64,
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

    fn native(&mut self, _site: Site, config: &Json) -> Option<Value> {
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
            _ => None,
        }
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
        entity: 0xa000_0101,
        frame: 100,
        position: [1.0, 2.0, 3.0],
        quanta: RETAIL_QUANTA,
    };
    let factory =
        PlayerBodyFactory::prepare(graphs, codecs, PRACTICE_RANGE_MODE, &hero, 200.0, spawn)
            .unwrap();
    (factory, spawn)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        collision_index::TriangleIndex, contact_world::ContactWorld,
        game_instance::TOURNAMENT_QUANTA, mover_settings::MoverSettings,
    };

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
        assert_eq!(first.world.position(spawn.entity), spawn.position);
        assert_eq!(second.snapshot().unwrap().entity, spawn.entity + 1);

        let mut mover = Mover::new(
            MoverSettings::default(),
            Arc::new(ContactWorld::new(Vec::new()).unwrap()),
            spawn.position,
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
                |mover, command| first.advance(command, mover).unwrap(),
            )
            .unwrap();
        assert_eq!(first.world.position(spawn.entity), mover.pose().position);
        assert_eq!(first.component.now_ms(), 102 * FRAME_MS - 1);
        assert_eq!(second.component.now_ms(), 101 * FRAME_MS - 1);
        let gap = Command {
            frame: 104,
            ..command
        };
        assert!(first.advance(gap, &mover).is_err());
        assert_eq!(first.component.now_ms(), 102 * FRAME_MS - 1);
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 statescript catalog"]
    fn prepared_soldier_starts_the_reference_body_and_weapon_tree() {
        use crate::{game_clock::FIRST_TICK, game_content::GameMap};
        use std::path::PathBuf;

        let root = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let graphs = Arc::new(GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap());
        let body = &graphs.bodies()[&0x0400_0000_0000_03cf];
        let roots = crate::script_init::player_roots(body, &graphs).unwrap();
        let variables = crate::script_init::player_entity_variables(body, 200.0, 200.0);
        let mut world = BodyWorld {
            graphs: Arc::clone(&graphs),
            entity: 0xa000_0101,
            position: GameMap::practice_range().spawn_point(0, 0).unwrap().0,
            throttles: [0; 2],
            mode: PRACTICE_RANGE_MODE,
            hero: SOLDIER,
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
        // Python BodyScript.snapshot() after the same hero's startup.
        let expected = [
            (1, 0x0033, None),
            (2, 0x004b, None),
            (3, 0x0043, None),
            (4, 0x0251, None),
            (5, 0x0255, None),
            (6, 0x0257, None),
            (7, 0x0259, None),
            (8, 0x091b, None),
            (9, 0x08b6, None),
            (10, 0x0015, None),
            (11, 0x0254, None),
            (12, 0x01c7, Some((2, 0))),
            (13, 0x01cf, Some((2, 3))),
            (14, 0x02c5, Some((2, 5))),
            (15, 0x0b8b, Some((2, 10))),
            (16, 0x0c7d, Some((2, 18))),
            (17, 0x01ca, Some((2, 1))),
            (18, 0x0bf8, Some((9, 4))),
            (19, 0x0e8f, Some((13, 32))),
        ];
        let instances: Vec<_> = snapshot
            .instances
            .iter()
            .map(|(&id, instance)| (id, instance.graph, instance.parent))
            .collect();
        assert_eq!(instances, expected);
        assert_eq!(snapshot.instances[&11].variables[&28], Value::Integer(1));
    }
}
