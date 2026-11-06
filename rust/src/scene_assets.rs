//! Load the authorized data needed before the game worker can create a world.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File},
    io::{self, BufReader},
    path::{Path, PathBuf},
    sync::{Arc, OnceLock},
};

use serde::Deserialize;

use crate::{
    accounts::parse_guid,
    bits::BitWriter,
    collision_cache::{chunk_guid, read_movement_cache},
    collision_index::TriangleIndex,
    collision_scene::CollisionScene,
    contact_world::ContactWorld,
    game_bots::{Bot, BotCatalog, BotWalk},
    game_clock::FIRST_TICK,
    game_content::{GameMap, PRACTICE_RANGE_MAP, PRACTICE_RANGE_MODE},
    game_controller::Controller,
    game_heroes::{Hero, HeroCatalog, SOLDIER},
    game_instance::RETAIL_QUANTA,
    game_pools::PoolCatalog,
    game_replication::OwnerBodyRuntime,
    game_stats::{PlayerStats, StatsCatalog},
    match_scene::{PlayerAppearance, initial_world, mode_ui},
    mover::Mover,
    mover_posture::snap,
    mover_settings::MoverCatalog,
    script_codecs::CodecTable,
    script_graph::GraphStore,
    script_player::{OwnerBodySpawn, PlayerBodyFactory},
    script_wire::Variables,
    world::{Component, EntityUpdate},
};

const MOVER_CATEGORY: u16 = 0x18;

type OwnerBodyFactory =
    dyn Fn(&Hero, OwnerBodySpawn) -> io::Result<Option<Box<dyn OwnerBodyRuntime>>> + Send + Sync;

pub struct SceneAssets {
    collision: PathBuf,
    mode_props: BTreeMap<u64, BTreeSet<u64>>,
    graphs: Arc<GraphStore>,
    codecs: Option<Arc<CodecTable>>,
    practice_owner_body: OnceLock<Option<Arc<OwnerBodyFactory>>>,
    bots: BotCatalog,
    movers: Arc<MoverCatalog>,
    stats: StatsCatalog,
    heroes: Arc<HeroCatalog>,
    pools: Arc<PoolCatalog>,
}

pub struct PreparedScene {
    map: GameMap,
    ground: CollisionScene,
    contacts: Option<Arc<ContactWorld>>,
    controller: Controller,
    mode_ui: BitWriter,
    initial_board: Component,
    bots: Vec<Bot>,
    scripted_bots: usize,
    movers: Arc<MoverCatalog>,
    heroes: Arc<HeroCatalog>,
    pools: Arc<PoolCatalog>,
    pub(crate) owner_body_factory: Option<Arc<OwnerBodyFactory>>,
}

impl SceneAssets {
    pub fn load(runtime: impl AsRef<Path>) -> io::Result<Self> {
        let runtime = runtime.as_ref();
        let data = runtime.join("data");
        let graphs = Arc::new(GraphStore::load(data.join("game_graphs_174.json.gz"))?);
        let codecs = match CodecTable::load(data.join("statescript_codecs_174.json")) {
            Ok(table) => Some(Arc::new(table)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        let pools = Arc::new(PoolCatalog::from_graphs(&graphs)?);
        Ok(Self {
            collision: runtime.join("collision"),
            mode_props: read_mode_props(&data.join("collision_props_174.json"))?,
            graphs,
            codecs,
            practice_owner_body: OnceLock::new(),
            bots: BotCatalog::load(data.join("ai_spawners_174.json"))?,
            movers: Arc::new(MoverCatalog::load(data.join("game_movers_174.json"))?),
            stats: StatsCatalog::load(data.join("game_stats_174.json"))?,
            heroes: Arc::new(HeroCatalog::load(data.join("game_heroes_174.json"))?),
            pools,
        })
    }

    pub fn graphs(&self) -> &GraphStore {
        &self.graphs
    }

    pub fn heroes(&self) -> &HeroCatalog {
        &self.heroes
    }

    /// Load the map before handing it to the worker. The render layouts and
    /// graph requirements are checked without blocking a game tick.
    pub fn prepare(&self, map: GameMap) -> io::Result<PreparedScene> {
        let path = self
            .collision
            .join(format!("{:04X}.col", map.map_guid as u32));
        let cache = read_movement_cache(BufReader::new(File::open(path)?)).map_err(invalid)?;
        for (field, expected) in [("map", map.map_guid), ("chunk", chunk_guid(map.map_guid))] {
            if let Some(value) = cache.header.source.get(field) {
                if value.as_str().and_then(parse_guid) != Some(expected) {
                    return Err(invalid("movement cache identifies a different map"));
                }
            }
        }
        let empty = BTreeSet::new();
        let identifiers = self.mode_props.get(&map.mode_guid).unwrap_or(&empty);
        let ground = CollisionScene::movement_probe(&cache, identifiers).map_err(invalid)?;
        let bots = self.bots.make(map.map_guid, |body| {
            let settings = self.movers.for_body(Some(body));
            BotWalk::from_mover(
                settings.run_forward,
                settings.accel_ground,
                settings.decel_ground_base,
            )
        });
        let scripted_bots = bots
            .iter()
            .filter(|bot| self.graphs.entity_body(bot.spawn.body).is_some())
            .count();
        let contacts = (map.map_guid == PRACTICE_RANGE_MAP
            && map.mode_guid == PRACTICE_RANGE_MODE
            && scripted_bots == 0)
            .then(|| ContactWorld::from_movement_cache(cache, identifiers).map(Arc::new))
            .transpose()
            .map_err(invalid)?;
        let controller = Controller::load(&self.graphs, map.mode_guid)?;
        let mode_ui = mode_ui(&self.graphs, &Variables::new())?;
        let initial_board =
            self.stats
                .component(&PlayerStats::default(), map.mode_guid, None, None);
        let owner_body_factory = contacts.as_ref().and_then(|_| {
            self.practice_owner_body
                .get_or_init(|| self.prepare_owner_body(&GameMap::practice_range()))
                .clone()
        });
        Ok(PreparedScene {
            map,
            ground,
            contacts,
            controller,
            mode_ui,
            initial_board,
            bots,
            scripted_bots,
            movers: Arc::clone(&self.movers),
            heroes: Arc::clone(&self.heroes),
            pools: Arc::clone(&self.pools),
            owner_body_factory,
        })
    }

    fn prepare_owner_body(&self, map: &GameMap) -> Option<Arc<OwnerBodyFactory>> {
        let hero = self.heroes.get(SOLDIER)?;
        let (position, _) = map.spawn_point(0, 0)?;
        let prepared = self
            .codecs
            .as_ref()
            .ok_or_else(|| "the owner statescript codec table is unavailable".to_owned())
            .and_then(|codecs| {
                PlayerBodyFactory::prepare(
                    Arc::clone(&self.graphs),
                    Arc::clone(codecs),
                    map.mode_guid,
                    hero,
                    self.pools.body_pools(hero.guid).total(),
                    OwnerBodySpawn {
                        entity: 0xa000_0101,
                        frame: FIRST_TICK,
                        position: position.map(snap),
                        quanta: RETAIL_QUANTA,
                    },
                )
                .map_err(|error| error.to_string())
            });
        Some(Arc::new(move |hero, spawn| {
            if hero.guid != SOLDIER {
                return Ok(None);
            }
            let factory = prepared
                .as_ref()
                .map_err(|reason| io::Error::new(io::ErrorKind::Unsupported, reason.clone()))?;
            let runtime: Box<dyn OwnerBodyRuntime> = Box::new(factory.start(spawn)?);
            Ok(Some(runtime))
        }))
    }
}

impl PreparedScene {
    pub fn map(&self) -> &GameMap {
        &self.map
    }

    pub fn scripted_bots(&self) -> usize {
        self.scripted_bots
    }

    pub(crate) fn movement_ready(&self) -> bool {
        self.contacts.is_some()
    }

    pub(crate) fn movement_index(&self) -> &TriangleIndex {
        self.ground.triangles()
    }

    pub(crate) fn player_mover(
        &self,
        body: u64,
        team: u8,
        position: [f64; 3],
        yaw: i32,
    ) -> io::Result<Mover> {
        let contacts = self.contacts.as_ref().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::Unsupported,
                "the scene has no native player collision",
            )
        })?;
        let category = match (self.map.free_for_all, team) {
            (false, 0) => 0x08,
            (false, 1) => 0x10,
            _ => MOVER_CATEGORY,
        };
        Mover::new(
            *self.movers.for_body(Some(body)),
            Arc::clone(contacts),
            position,
            yaw,
            category,
        )
        .map_err(invalid)
    }

    pub(crate) fn owner_body_runtime(
        &self,
        hero: &Hero,
        spawn: OwnerBodySpawn,
    ) -> io::Result<Option<Box<dyn OwnerBodyRuntime>>> {
        self.owner_body_factory
            .as_ref()
            .map_or(Ok(None), |factory| factory(hero, spawn))
    }

    pub fn heroes(&self) -> &HeroCatalog {
        &self.heroes
    }

    pub fn pools(&self) -> &PoolCatalog {
        &self.pools
    }

    pub fn controller(&self) -> Controller {
        self.controller
    }

    pub fn mode_ui(&self) -> &BitWriter {
        &self.mode_ui
    }

    pub fn bots(&self) -> &[Bot] {
        &self.bots
    }

    pub(crate) fn bots_mut(&mut self) -> &mut [Bot] {
        &mut self.bots
    }

    pub(crate) fn bot_target(&self, bot: &Bot) -> crate::game_hitscan::TargetPose {
        let settings = self.movers.for_body(Some(bot.spawn.body));
        crate::game_hitscan::TargetPose {
            feet: bot.position,
            height: settings.stand_height,
            radius: settings.stand_radius,
            eye: crate::game_combat::eye_height(bot.spawn.hero, false),
        }
    }

    pub(crate) fn hitscan_wall(
        &self,
        origin: [f64; 3],
        direction: [f64; 3],
        reach: f64,
    ) -> Result<Option<f64>, crate::collision_index::IndexError> {
        self.ground
            .cast_ray(origin, direction, reach, MOVER_CATEGORY)
            .map(|hit| hit.map(|hit| hit.distance))
    }

    pub fn step_bots(&mut self, tick: u32) {
        let floor = self.ground.triangles();
        for bot in &mut self.bots {
            bot.step(tick, |x, z, top, bottom| {
                floor
                    .ground(x, z, top, bottom, MOVER_CATEGORY)
                    .map(|hit| hit.y)
            });
        }
    }

    pub fn initial_world(&self, viewer: &PlayerAppearance<'_>) -> io::Result<Vec<EntityUpdate>> {
        initial_world(&self.map, viewer, self.initial_board.clone(), &self.bots)
    }
}

#[derive(Deserialize)]
struct PropCatalog {
    modes: BTreeMap<String, Vec<String>>,
}

fn read_mode_props(path: &Path) -> io::Result<BTreeMap<u64, BTreeSet<u64>>> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
        Err(error) => return Err(error),
    };
    let catalog: PropCatalog = serde_json::from_slice(&bytes).map_err(invalid)?;
    let guid = |value: &str| parse_guid(value).ok_or_else(|| invalid("invalid mode prop GUID"));
    catalog
        .modes
        .into_iter()
        .map(|(mode, props)| {
            Ok((
                guid(&mode)?,
                props
                    .iter()
                    .map(|value| guid(value))
                    .collect::<io::Result<_>>()?,
            ))
        })
        .collect()
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::world::movement_record;
    use sha2::{Digest, Sha256};

    #[test]
    #[ignore = "requires the baseline 104319 Practice Range pack without bot VM graphs"]
    fn stock_soldier_requires_more_vm_support_before_prepared_attach() {
        let runtime = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let assets = SceneAssets::load(runtime).unwrap();
        let scene = assets.prepare(GameMap::practice_range()).unwrap();
        assert!(scene.movement_ready());
        let hero = scene.heroes().get(SOLDIER).unwrap();
        let spawn = OwnerBodySpawn {
            entity: 0xa000_0101,
            frame: FIRST_TICK,
            position: scene.map().spawn_point(0, 0).unwrap().0.map(snap),
            quanta: RETAIL_QUANTA,
        };
        let Err(blocked) = scene.owner_body_runtime(hero, spawn) else {
            panic!("the stock owner VM was expected to require more graph support");
        };
        assert_eq!(blocked.kind(), io::ErrorKind::Unsupported);
        assert!(
            blocked.to_string().contains("initialization diagnostics"),
            "{blocked}"
        );
        eprintln!("prepared Soldier owner VM: {blocked}");
        let tracer = scene.heroes().find_hero("Tracer").unwrap();
        assert!(scene.owner_body_runtime(tracer, spawn).unwrap().is_none());
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range pack"]
    fn prepared_scene_matches_python_bot_motion_over_the_real_floor() {
        let runtime = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let assets = SceneAssets::load(runtime).unwrap();
        let mut scene = assets.prepare(GameMap::practice_range()).unwrap();
        assert_eq!(scene.bots().len(), 15);
        let scripted = if assets.graphs.graph_indexes().count() == 373 {
            0
        } else {
            15
        };
        assert_eq!(scene.scripted_bots(), scripted);
        let mut digest = Sha256::new();
        for tick in 1001..=1500 {
            scene.step_bots(tick);
            for bot in scene.bots() {
                let movement = movement_record(&bot.movement(tick), Some(tick)).unwrap();
                digest.update(bot.entity.to_le_bytes());
                digest.update((movement.bit_len() as u32).to_le_bytes());
                digest.update(movement.as_bytes());
                digest.update([u8::from(bot.record_due(tick, 3))]);
            }
        }
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "30a7004c18a623e62f54b084ae45a8ca67d771e672ce09296eda863e8b68dd9b",
        );
    }
}
