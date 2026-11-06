//! Match map selection and spawn transforms for the 104319 runtime catalog.
//!
//! Ported from the map portion of ow174/game/content.py.

use std::{collections::HashMap, fs, io, path::Path};

use indexmap::IndexMap;
use serde::Deserialize;

use crate::accounts::parse_guid;

pub const PRACTICE_RANGE_MAP: u64 = 0x0800_0000_0000_0688;
pub const PRACTICE_RANGE_MODE: u64 = 0x0230_0000_0000_0018;
pub const SPAWN_SPACING: f64 = 1.5;

const PRACTICE_RANGE_NAME: &str = "Practice Range";
// The retail loader waits for these modes to publish their own script state.
const SCRIPTED_LOADING_MODES: [u64; 5] = [
    0x0230_0000_0000_0008,
    0x0230_0000_0000_000f,
    0x0230_0000_0000_001a,
    0x0230_0000_0000_0025,
    0x0230_0000_0000_0043,
];

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
pub struct Spawn {
    pub team: Option<u8>,
    pub position: [f64; 3],
    #[serde(default)]
    pub yaw: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct GameMap {
    pub name: String,
    pub map_guid: u64,
    pub mode_guid: u64,
    pub placeables: Vec<u32>,
    pub spawns: Vec<Spawn>,
    pub mode_name: String,
    pub team_sizes: Vec<u8>,
    pub free_for_all: bool,
}

impl GameMap {
    /// The training route has an explicit map, mode, and first spawn.
    pub fn practice_range() -> Self {
        Self {
            name: PRACTICE_RANGE_NAME.into(),
            map_guid: PRACTICE_RANGE_MAP,
            mode_guid: PRACTICE_RANGE_MODE,
            placeables: vec![0x88, 0x22e, 0x234, 0x289],
            spawns: vec![Spawn {
                team: None,
                position: [54.56507, 1.0, 42.14788],
                yaw: -170.3434,
            }],
            mode_name: PRACTICE_RANGE_NAME.into(),
            team_sizes: vec![1, 0],
            free_for_all: false,
        }
    }

    /// Cycle through a team's points (or all points if that team has none).
    /// Players sharing a point alternate along its local right-hand axis.
    pub fn spawn_point(&self, team: u8, number: usize) -> Option<([f64; 3], f64)> {
        let for_team = |spawn: &&Spawn| spawn.team.is_none() || spawn.team == Some(team);
        let count = self.spawns.iter().filter(for_team).count();
        let row_size = if count == 0 { self.spawns.len() } else { count };
        let index = number.checked_rem(row_size)?;
        let spawn = self
            .spawns
            .iter()
            .filter(|spawn| count == 0 || for_team(spawn))
            .nth(index)?;
        let row = number / row_size;
        let side = row.div_ceil(2) as f64 * SPAWN_SPACING;
        let side = if row != 0 && row % 2 == 0 {
            -side
        } else {
            side
        };
        let (sin, cos) = spawn.yaw.to_radians().sin_cos();
        let [x, y, z] = spawn.position;
        Some(([x - cos * side, y, z + sin * side], spawn.yaw))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MapListing {
    pub guid: u64,
    pub name: String,
}

#[derive(Debug, Default)]
pub struct MapCatalog {
    entries: IndexMap<u64, MapEntry>,
}

impl MapCatalog {
    /// Read game_maps_174.json from the selected runtime. A missing catalog
    /// still leaves the built-in training route available.
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = path.as_ref();
        match path.metadata() {
            Ok(metadata) if !metadata.is_file() => Ok(Self::default()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(error),
            Ok(_) => Self::from_json(&fs::read_to_string(path)?),
        }
    }

    pub fn from_json(text: &str) -> io::Result<Self> {
        let raw: IndexMap<String, RawMap> = serde_json::from_str(text)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let mut entries = IndexMap::with_capacity(raw.len());
        for (guid, raw) in raw {
            let modes = raw
                .modes
                .iter()
                .map(|mode| hex_guid(mode))
                .collect::<io::Result<_>>()?;
            let placeables_by_mode = raw
                .placeables_by_mode
                .unwrap_or_default()
                .into_iter()
                .map(|(mode, indexes)| Ok((hex_guid(&mode)?, indexes)))
                .collect::<io::Result<_>>()?;
            let spawns = raw
                .spawns
                .unwrap_or_default()
                .into_iter()
                .map(|entry| SpawnEntry {
                    spawn: entry.spawn,
                    free_for_all: entry.ffa.unwrap_or_default(),
                    first_objective: entry
                        .objectives
                        .is_some_and(|objectives| objectives.iter().any(|(index, _)| *index == 0)),
                })
                .collect();
            entries.insert(
                hex_guid(&guid)?,
                MapEntry {
                    name: raw.name,
                    modes,
                    placeables: raw.placeables,
                    placeables_by_mode,
                    spawns,
                },
            );
        }
        Ok(Self { entries })
    }

    /// Catalog order is stable among entries with the same display name.
    pub fn map_catalog(&self) -> Vec<MapListing> {
        let mut maps = vec![MapListing {
            guid: PRACTICE_RANGE_MAP,
            name: PRACTICE_RANGE_NAME.into(),
        }];
        maps.extend(self.entries.iter().filter_map(|(&guid, entry)| {
            if guid == PRACTICE_RANGE_MAP {
                return None;
            }
            let name = match entry.name.as_deref() {
                Some(name) if !name.is_empty() => name.to_owned(),
                _ => format!("0x{guid:X}"),
            };
            Some(MapListing { guid, name })
        }));
        maps.sort_by(|left, right| left.name.cmp(&right.name));
        maps
    }

    pub fn map_name(&self, guid: u64) -> Option<&str> {
        if guid == PRACTICE_RANGE_MAP {
            return Some(PRACTICE_RANGE_NAME);
        }
        self.entries.get(&guid)?.name.as_deref()
    }

    pub fn game_map(
        &self,
        map_guid: u64,
        mode_guid: u64,
        mode_name: &str,
        free_for_all: bool,
        team_sizes: &[u8],
    ) -> Option<GameMap> {
        if map_guid == PRACTICE_RANGE_MAP {
            return Some(GameMap::practice_range());
        }
        let entry = self.entries.get(&map_guid)?;
        if SCRIPTED_LOADING_MODES.contains(&mode_guid) || !entry.modes.contains(&mode_guid) {
            return None;
        }
        let spawns = entry.first_round_spawns(free_for_all);
        if spawns.is_empty() {
            return None;
        }
        Some(GameMap {
            name: entry.name.clone()?,
            map_guid,
            mode_guid,
            placeables: entry
                .placeables_by_mode
                .get(&mode_guid)
                .unwrap_or(&entry.placeables)
                .clone(),
            spawns,
            mode_name: mode_name.into(),
            team_sizes: team_sizes.into(),
            free_for_all,
        })
    }
}

#[derive(Debug)]
struct MapEntry {
    name: Option<String>,
    modes: Vec<u64>,
    placeables: Vec<u32>,
    placeables_by_mode: HashMap<u64, Vec<u32>>,
    spawns: Vec<SpawnEntry>,
}

impl MapEntry {
    fn first_round_spawns(&self, free_for_all: bool) -> Vec<Spawn> {
        if free_for_all {
            return self
                .spawns
                .iter()
                .filter(|entry| entry.free_for_all || entry.spawn.team.is_none())
                .map(|entry| entry.spawn)
                .collect();
        }
        let mut chosen = Vec::new();
        for team in [0, 1] {
            let mine = self
                .spawns
                .iter()
                .filter(|entry| !entry.free_for_all && entry.spawn.team == Some(team));
            let has_first = mine.clone().any(|entry| entry.first_objective);
            chosen.extend(
                mine.filter(|entry| !has_first || entry.first_objective)
                    .map(|entry| entry.spawn),
            );
        }
        chosen
    }
}

#[derive(Debug)]
struct SpawnEntry {
    spawn: Spawn,
    free_for_all: bool,
    first_objective: bool,
}

#[derive(Deserialize)]
struct RawMap {
    name: Option<String>,
    #[serde(default)]
    modes: Vec<String>,
    #[serde(default)]
    placeables: Vec<u32>,
    placeables_by_mode: Option<IndexMap<String, Vec<u32>>>,
    spawns: Option<Vec<RawSpawn>>,
}

#[derive(Deserialize)]
struct RawSpawn {
    #[serde(flatten)]
    spawn: Spawn,
    ffa: Option<bool>,
    objectives: Option<Vec<(u32, String)>>,
}

fn hex_guid(text: &str) -> io::Result<u64> {
    let trimmed = text.trim();
    let digits = trimmed.strip_prefix('+').unwrap_or(trimmed);
    let parsed = match digits.as_bytes() {
        [b'0', b'x' | b'X', ..] => parse_guid(digits),
        [b'_', ..] => None,
        _ => parse_guid(&format!("0x{digits}")),
    };
    parsed.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid map or mode GUID {text:?}"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const ARENA: &str = r#"{
        "0x80": {
            "name": "Fixture Arena",
            "modes": ["20", "0x21", "0X22", "0x0230000000000025"],
            "placeables": [8, 4, 8],
            "placeables_by_mode": {"0x20": [], "21": [3, 1, 3]},
            "spawns": [
                {"team": 1, "position": [8, 1, 0], "objectives": [[1, "attack"]]},
                {"team": 0, "position": [1, 1, 0], "objectives": [[2, "defense"]]},
                {"team": 0, "position": [9, 1, 0], "objectives": [[0, "defense"]], "ffa": true},
                {"team": 0, "position": [2, 1, 0], "objectives": [[0, "defense"]], "yaw": 90},
                {"team": 1, "position": [3, 1, 0], "objectives": null},
                {"team": 0, "position": [4, 1, 0], "objectives": [[1, "defense"], [0, "defense"]]},
                {"team": null, "position": [5, 1, 0]},
                {"position": [6, 1, 0], "ffa": false},
                {"team": 1, "position": [7, 1, 0], "ffa": true}
            ]
        },
        "0x81": {"name": "No points", "modes": ["20"], "spawns": null}
    }"#;

    #[test]
    fn loads_external_catalog_and_keeps_the_practice_fallback() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("game_maps_174.json");
        let missing = MapCatalog::load(&path).unwrap();
        let practice = GameMap::practice_range();
        assert_eq!(missing.map_name(PRACTICE_RANGE_MAP), Some("Practice Range"));
        assert_eq!(
            missing.game_map(
                PRACTICE_RANGE_MAP,
                SCRIPTED_LOADING_MODES[0],
                "Ignored",
                true,
                &[9]
            ),
            Some(practice.clone())
        );
        assert_eq!(practice.placeables, [0x88, 0x22e, 0x234, 0x289]);
        assert_eq!(practice.team_sizes, [1, 0]);
        assert_eq!(
            practice.spawn_point(0, 0),
            Some(([54.56507, 1.0, 42.14788], -170.3434))
        );

        fs::write(&path, ARENA).unwrap();
        assert_eq!(
            MapCatalog::load(&path).unwrap().map_name(0x80),
            Some("Fixture Arena")
        );
        fs::write(&path, "{").unwrap();
        assert_eq!(
            MapCatalog::load(&path).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn dashboard_listing_sorts_stably_and_deduplicates_practice() {
        let catalog = MapCatalog::from_json(
            r#"{
            "0x9": {"name": "Same"},
            "0008": {"name": "Same"},
            "0x7": {},
            "0x0800000000000688": {"name": "Override"},
            "6": {"name": ""}
        }"#,
        )
        .unwrap();
        let listing = catalog.map_catalog();
        assert_eq!(
            listing
                .iter()
                .map(|entry| (entry.guid, entry.name.as_str()))
                .collect::<Vec<_>>(),
            [
                (6, "0x6"),
                (7, "0x7"),
                (PRACTICE_RANGE_MAP, "Practice Range"),
                (9, "Same"),
                (8, "Same")
            ]
        );
        assert_eq!(catalog.map_name(6), Some(""));
        assert_eq!(catalog.map_name(7), None);
        assert_eq!(catalog.map_name(50), None);
    }

    #[test]
    fn uses_mode_placeables_and_first_objective_in_team_order() {
        let catalog = MapCatalog::from_json(ARENA).unwrap();
        let arena = catalog
            .game_map(0x80, 0x20, "Team Fixture", false, &[6, 5])
            .unwrap();
        assert_eq!(arena.mode_name, "Team Fixture");
        assert_eq!(arena.team_sizes, [6, 5]);
        assert!(arena.placeables.is_empty());
        assert_eq!(
            arena
                .spawns
                .iter()
                .map(|spawn| (spawn.team, spawn.position[0]))
                .collect::<Vec<_>>(),
            [
                (Some(0), 2.0),
                (Some(0), 4.0),
                (Some(1), 8.0),
                (Some(1), 3.0)
            ]
        );
        let mode_placeables = catalog.game_map(0x80, 0x21, "", false, &[]).unwrap();
        assert_eq!(mode_placeables.placeables, [3, 1, 3]);
        let fallback = catalog.game_map(0x80, 0x22, "", false, &[]).unwrap();
        assert_eq!(fallback.placeables, [8, 4, 8]);
        assert!(catalog.game_map(0x80, 0x23, "", false, &[]).is_none());
        assert!(
            catalog
                .game_map(0x80, 0x0230_0000_0000_0025, "", false, &[])
                .is_none()
        );
        assert!(catalog.game_map(0x81, 0x20, "", false, &[]).is_none());
    }

    #[test]
    fn free_for_all_keeps_tagged_or_teamless_points_in_source_order() {
        let catalog = MapCatalog::from_json(ARENA).unwrap();
        let arena = catalog
            .game_map(0x80, 0x20, "FFA Fixture", true, &[8])
            .unwrap();
        assert!(arena.free_for_all);
        assert_eq!(arena.team_sizes, [8]);
        assert_eq!(
            arena
                .spawns
                .iter()
                .map(|spawn| (spawn.team, spawn.position[0]))
                .collect::<Vec<_>>(),
            [(Some(0), 9.0), (None, 5.0), (None, 6.0), (Some(1), 7.0)]
        );
        assert_eq!(arena.spawn_point(3, 0), Some(([5.0, 1.0, 0.0], 0.0)));
        assert_eq!(arena.spawn_point(0, 0), Some(([9.0, 1.0, 0.0], 0.0)));
    }

    #[test]
    fn repeated_spawns_offset_along_local_right_and_left() {
        let catalog = MapCatalog::from_json(ARENA).unwrap();
        let arena = catalog.game_map(0x80, 0x20, "", false, &[]).unwrap();
        for (number, expected_z) in [(0, 0.0), (2, 1.5), (4, -1.5), (6, 3.0), (8, -3.0)] {
            let ([x, y, z], yaw) = arena.spawn_point(0, number).unwrap();
            assert!((x - 2.0).abs() < 1.0e-12);
            assert_eq!(y, 1.0);
            assert_eq!(z, expected_z);
            assert_eq!(yaw, 90.0);
        }
        assert_eq!(arena.spawn_point(0, 3), Some(([2.5, 1.0, 0.0], 0.0)));
        assert_eq!(arena.spawn_point(2, 3), Some(([3.0, 1.0, 0.0], 0.0)));
        assert_eq!(arena.spawn_point(2, 7), Some(([1.5, 1.0, 0.0], 0.0)));
        let empty = GameMap {
            spawns: Vec::new(),
            ..arena
        };
        assert_eq!(empty.spawn_point(0, 0), None);
    }

    #[test]
    fn malformed_guids_are_invalid_data() {
        for input in [
            r#"{"not-hex":{}}"#,
            r#"{"0x1":{"modes":["not-hex"]}}"#,
            r#"{"0x1":{"placeables_by_mode":{"not-hex":[]}}}"#,
        ] {
            assert_eq!(
                MapCatalog::from_json(input).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
        }
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 map catalog"]
    fn map_catalog_and_mode_selections_match_the_python_corpus() {
        use sha2::{Digest, Sha256};
        use std::path::PathBuf;

        fn count(digest: &mut Sha256, value: usize) {
            digest.update(u32::try_from(value).unwrap().to_le_bytes());
        }

        fn text(digest: &mut Sha256, value: &str) {
            count(digest, value.len());
            digest.update(value.as_bytes());
        }

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let catalog = MapCatalog::load(root.join("data/game_maps_174.json")).unwrap();
        let listing = catalog.map_catalog();
        let mut digest = Sha256::new();
        count(&mut digest, listing.len());
        let mut cases = 0;
        let mut accepted = 0;
        for entry in &listing {
            digest.update(entry.guid.to_le_bytes());
            text(&mut digest, &entry.name);
            let name = catalog.map_name(entry.guid);
            digest.update([u8::from(name.is_some())]);
            if let Some(name) = name {
                text(&mut digest, name);
            }
            let modes = if entry.guid == PRACTICE_RANGE_MAP {
                &[PRACTICE_RANGE_MODE]
            } else {
                catalog.entries[&entry.guid].modes.as_slice()
            };
            count(&mut digest, modes.len());
            for &mode in modes {
                digest.update(mode.to_le_bytes());
                for ffa in [false, true] {
                    cases += 1;
                    let sizes: &[u8] = if ffa { &[8] } else { &[6, 6] };
                    let game = catalog.game_map(entry.guid, mode, "Fixture", ffa, sizes);
                    digest.update([u8::from(game.is_some())]);
                    let Some(game) = game else { continue };
                    accepted += 1;
                    digest.update(game.map_guid.to_le_bytes());
                    digest.update(game.mode_guid.to_le_bytes());
                    text(&mut digest, &game.name);
                    text(&mut digest, &game.mode_name);
                    digest.update([u8::from(game.free_for_all)]);
                    count(&mut digest, game.team_sizes.len());
                    digest.update(&game.team_sizes);
                    count(&mut digest, game.placeables.len());
                    for placeable in game.placeables {
                        digest.update(placeable.to_le_bytes());
                    }
                    count(&mut digest, game.spawns.len());
                    for spawn in game.spawns {
                        digest.update([spawn.team.unwrap_or(u8::MAX)]);
                        for value in spawn.position.into_iter().chain([spawn.yaw]) {
                            digest.update((value as f32).to_le_bytes());
                        }
                    }
                }
            }
        }
        assert_eq!((listing.len(), cases, accepted), (95, 698, 551));
        // Python game.content catalog and selection outputs, with geometry as LE f32.
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "414daebdec686669243271e7cadf40c5ca7f0cb2a0e41b7c3d434c7c82b17e4a"
        );
    }
}
