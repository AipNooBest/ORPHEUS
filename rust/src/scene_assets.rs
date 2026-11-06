//! Load the authorized data needed before the game worker can create a world.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File},
    io::{self, BufReader},
    path::{Path, PathBuf},
};

use serde::Deserialize;

use crate::{
    accounts::parse_guid,
    bits::BitWriter,
    collision_cache::{chunk_guid, read_movement_cache},
    collision_scene::CollisionScene,
    game_bots::{Bot, BotCatalog, BotWalk},
    game_content::GameMap,
    game_controller::Controller,
    game_stats::{PlayerStats, StatsCatalog},
    match_scene::{PlayerAppearance, initial_world, mode_ui},
    mover_settings::MoverCatalog,
    script_graph::GraphStore,
    script_wire::Variables,
    world::{Component, EntityUpdate},
};

const MOVER_CATEGORY: u16 = 0x18;

pub struct SceneAssets {
    collision: PathBuf,
    mode_props: BTreeMap<u64, BTreeSet<u64>>,
    graphs: GraphStore,
    bots: BotCatalog,
    movers: MoverCatalog,
    stats: StatsCatalog,
}

pub struct PreparedScene {
    map: GameMap,
    ground: CollisionScene,
    controller: Controller,
    mode_ui: BitWriter,
    initial_board: Component,
    bots: Vec<Bot>,
    scripted_bots: usize,
}

impl SceneAssets {
    pub fn load(runtime: impl AsRef<Path>) -> io::Result<Self> {
        let runtime = runtime.as_ref();
        let data = runtime.join("data");
        Ok(Self {
            collision: runtime.join("collision"),
            mode_props: read_mode_props(&data.join("collision_props_174.json"))?,
            graphs: GraphStore::load(data.join("game_graphs_174.json.gz"))?,
            bots: BotCatalog::load(data.join("ai_spawners_174.json"))?,
            movers: MoverCatalog::load(data.join("game_movers_174.json"))?,
            stats: StatsCatalog::load(data.join("game_stats_174.json"))?,
        })
    }

    pub fn graphs(&self) -> &GraphStore {
        &self.graphs
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
        let controller = Controller::load(&self.graphs, map.mode_guid)?;
        let mode_ui = mode_ui(&self.graphs, &Variables::new())?;
        let initial_board =
            self.stats
                .component(&PlayerStats::default(), map.mode_guid, None, None);
        Ok(PreparedScene {
            map,
            ground,
            controller,
            mode_ui,
            initial_board,
            bots,
            scripted_bots,
        })
    }
}

impl PreparedScene {
    pub fn map(&self) -> &GameMap {
        &self.map
    }

    pub fn scripted_bots(&self) -> usize {
        self.scripted_bots
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
