//! Owner scoreboard fields and on-fire display values for component 64.
//!
//! Ported from ow174/game/stats.py and ow174/game/onfire.py. The retail fire
//! scoring rule and its threshold remain outside this component encoder.

use std::{collections::HashMap, fs, io, path::Path};

use indexmap::IndexMap;
use serde::Deserialize;

use crate::accounts::parse_guid;
use crate::world::{Component, ComponentValue, GMP_STATS_FIELDS};

pub const GMP_STATS: u8 = 64;
pub const GOLD: u8 = 1;
pub const SILVER: u8 = 2;
pub const BRONZE: u8 = 3;

pub const ELIMINATIONS: u64 = 0x0860_0000_0000_0025;
pub const OBJECTIVE_KILLS: u64 = 0x0860_0000_0000_0326;
pub const OBJECTIVE_TIME: u64 = 0x0860_0000_0000_0327;
pub const HERO_DAMAGE: u64 = 0x0860_0000_0000_04b8;
pub const HEALING: u64 = 0x0860_0000_0000_01d1;
pub const DEATHS: u64 = 0x0860_0000_0000_0029;

const MEDAL_SLOTS: usize = 8;
const HERO_SLOTS: usize = 6;
const MEDAL_STAT: usize = 5;
const HERO_STAT: usize = 13;
const MEDAL_VALUE: usize = 21;
const HERO_VALUE: usize = 29;
const MEDAL: usize = 37;
const SCORE: usize = 19;
const THRESHOLD: usize = 20;
const ON_FIRE: usize = 36;
const FULL: f32 = 1.28;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StatSlot {
    pub guid: u64,
    pub no_medal: bool,
}

#[derive(Debug, Default)]
pub struct PlayerStats {
    values: HashMap<u64, f64>,
}

impl PlayerStats {
    pub fn add(&mut self, stat: u64, amount: f64) {
        *self.values.entry(stat).or_default() += amount;
    }

    pub fn set(&mut self, stat: u64, value: f64) {
        self.values.insert(stat, value);
    }

    pub fn value(&self, stat: u64) -> f64 {
        self.values.get(&stat).copied().unwrap_or_default()
    }
}

pub type Medals = HashMap<u64, u8>;

#[derive(Debug, Default)]
pub struct StatsCatalog {
    player: StatLists,
    heroes: HashMap<u64, StatLists>,
    display: HashMap<u64, Option<u8>>,
}

impl StatsCatalog {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        Self::from_json(&fs::read_to_string(path)?)
    }

    pub fn from_json(text: &str) -> io::Result<Self> {
        let raw: RawCatalog = serde_json::from_str(text)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        Ok(Self {
            player: raw.player.try_into()?,
            heroes: raw
                .heroes
                .into_iter()
                .map(|(hero, lists)| Ok((guid(&hero)?, lists.try_into()?)))
                .collect::<io::Result<_>>()?,
            display: raw
                .stats
                .into_iter()
                .map(|(stat, display)| Ok((guid(&stat)?, display)))
                .collect::<io::Result<_>>()?,
        })
    }

    pub fn player_stats(&self, mode: u64) -> &[StatSlot] {
        self.player.select(mode)
    }

    pub fn hero_stats(&self, hero: u64, mode: u64) -> &[StatSlot] {
        self.heroes
            .get(&hero)
            .map_or(&[], |lists| lists.select(mode))
    }

    pub fn display_type(&self, stat: u64) -> Option<u8> {
        self.display.get(&stat).copied().flatten()
    }

    /// Medal placement among the caller's team, including all players in a
    /// free-for-all. Equal positive scores keep the input order.
    pub fn medals(&self, team: &[&PlayerStats], mode: u64) -> Vec<Medals> {
        let mut medals = vec![Medals::new(); team.len()];
        for slot in self.player_stats(mode).iter().filter(|slot| !slot.no_medal) {
            let mut ranked: Vec<_> = team
                .iter()
                .enumerate()
                .filter_map(|(index, player)| {
                    let value = player.value(slot.guid);
                    (value > 0.0).then_some((index, value))
                })
                .collect();
            ranked.sort_by(|left, right| right.1.total_cmp(&left.1));
            for (medal, (index, _)) in [GOLD, SILVER, BRONZE].into_iter().zip(ranked) {
                medals[index].insert(slot.guid, medal);
            }
        }
        medals
    }

    /// An owner's board. Unused slots carry zero so a hero or mode change
    /// clears the former entries. The on-fire fields are a separate update.
    pub fn component(
        &self,
        record: &PlayerStats,
        mode: u64,
        hero: Option<u64>,
        medals: Option<&Medals>,
    ) -> Component {
        let mut fields = vec![None; GMP_STATS_FIELDS];
        let player_stats = self.player_stats(mode);
        let hero_stats = hero
            .filter(|&hero| hero != 0)
            .map_or(&[][..], |hero| self.hero_stats(hero, mode));
        for (entries, slots, stat_offset, value_offset) in [
            (player_stats, MEDAL_SLOTS, MEDAL_STAT, MEDAL_VALUE),
            (hero_stats, HERO_SLOTS, HERO_STAT, HERO_VALUE),
        ] {
            for index in 0..slots {
                let stat = entries.get(index).map_or(0, |slot| slot.guid);
                let value = if stat == 0 {
                    0.0
                } else {
                    record.value(stat) as f32
                };
                fields[stat_offset + index] = Some(ComponentValue::U64(stat));
                fields[value_offset + index] = Some(ComponentValue::F32(value));
            }
        }
        for index in 0..MEDAL_SLOTS {
            let medal = player_stats
                .get(index)
                .filter(|slot| slot.guid != 0)
                .and_then(|slot| medals?.get(&slot.guid))
                .copied()
                .unwrap_or_default();
            fields[MEDAL + index] = Some(ComponentValue::U8(medal));
        }
        Component {
            index: GMP_STATS,
            fields,
        }
    }
}

/// The retail client's meter in percent. A zero threshold produces NaN.
pub fn meter(score: f32, threshold: f32) -> f32 {
    let top = threshold * FULL;
    if top == 0.0 {
        return f32::NAN;
    }
    let score = if top < score { top } else { score };
    (score / top) * 100.0
}

/// Only the owner's view includes score and threshold. All viewers may
/// receive the on-fire flag. Callers omit each field they are leaving alone.
pub fn on_fire_component(
    score: Option<f32>,
    threshold: Option<f32>,
    on_fire: Option<bool>,
) -> Component {
    let mut fields = vec![None; GMP_STATS_FIELDS];
    fields[SCORE] = score.map(ComponentValue::F32);
    fields[THRESHOLD] = threshold.map(ComponentValue::F32);
    fields[ON_FIRE] = on_fire.map(|flag| ComponentValue::U8(u8::from(flag)));
    Component {
        index: GMP_STATS,
        fields,
    }
}

/// The effective flag on a fresh player controller, before any stats updates.
pub(crate) fn initial_on_fire(component: &Component) -> Option<bool> {
    if component.index != GMP_STATS {
        return None;
    }
    match component.fields.get(ON_FIRE).and_then(Option::as_ref) {
        None => Some(false),
        Some(ComponentValue::U8(flag)) => Some(*flag != 0),
        _ => None,
    }
}

#[derive(Debug, Default)]
struct StatLists {
    default: Vec<StatSlot>,
    modes: HashMap<u64, Vec<StatSlot>>,
}

impl StatLists {
    fn select(&self, mode: u64) -> &[StatSlot] {
        self.modes.get(&mode).unwrap_or(&self.default)
    }
}

impl TryFrom<RawLists> for StatLists {
    type Error = io::Error;

    fn try_from(raw: RawLists) -> io::Result<Self> {
        Ok(Self {
            default: slots(raw.default)?,
            modes: raw
                .modes
                .into_iter()
                .map(|(mode, entries)| Ok((guid(&mode)?, slots(entries)?)))
                .collect::<io::Result<_>>()?,
        })
    }
}

#[derive(Deserialize)]
struct RawCatalog {
    player: RawLists,
    heroes: IndexMap<String, RawLists>,
    stats: IndexMap<String, Option<u8>>,
}

#[derive(Deserialize)]
struct RawLists {
    default: Vec<(String, RawNoMedal)>,
    modes: IndexMap<String, Vec<(String, RawNoMedal)>>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum RawNoMedal {
    Bool(bool),
    Int(i64),
}

fn slots(raw: Vec<(String, RawNoMedal)>) -> io::Result<Vec<StatSlot>> {
    raw.into_iter()
        .map(|(stat, no_medal)| {
            let no_medal = match no_medal {
                RawNoMedal::Bool(value) => value,
                RawNoMedal::Int(value) => value != 0,
            };
            Ok(StatSlot {
                guid: guid(&stat)?,
                no_medal,
            })
        })
        .collect()
}

fn guid(text: &str) -> io::Result<u64> {
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
            format!("invalid stat catalog GUID {text:?}"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::world::{WorldError, update};
    use sha2::{Digest, Sha256};

    const HERO: u64 = 0x90;
    const MODE: u64 = 2;
    const FIXTURE: &str = r#"{
        "player": {
            "default": [
                ["0x0860000000000025",0], ["08600000000004B8",false], ["0x0860000000000029",1]
            ],
            "modes": {"0x2":[["0x0860000000000327",false]]}
        },
        "heroes": {
            "0x90": {"default":[["0x08600000000001D1",0]], "modes":{"0x2":[]}}
        },
        "stats": {"0x0860000000000327":8, "0x0860000000000025":0, "0x0860000000000029":null}
    }"#;

    fn fixture() -> StatsCatalog {
        StatsCatalog::from_json(FIXTURE).unwrap()
    }

    #[test]
    fn reads_ordered_catalog_and_mode_lists_including_empty_overrides() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("game_stats_174.json");
        assert_eq!(
            StatsCatalog::load(&path).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        fs::write(&path, FIXTURE).unwrap();
        let catalog = StatsCatalog::load(&path).unwrap();
        let rows = |list: &[StatSlot]| {
            list.iter()
                .map(|slot| (slot.guid, slot.no_medal))
                .collect::<Vec<_>>()
        };
        assert_eq!(
            rows(catalog.player_stats(0)),
            [(ELIMINATIONS, false), (HERO_DAMAGE, false), (DEATHS, true)]
        );
        assert_eq!(rows(catalog.player_stats(MODE)), [(OBJECTIVE_TIME, false)]);
        assert_eq!(rows(catalog.hero_stats(HERO, 0)), [(HEALING, false)]);
        assert!(catalog.hero_stats(HERO, MODE).is_empty());
        assert!(catalog.hero_stats(3, 0).is_empty());
        assert_eq!(catalog.display_type(OBJECTIVE_TIME), Some(8));
        assert_eq!(catalog.display_type(ELIMINATIONS), Some(0));
        assert_eq!(catalog.display_type(DEATHS), None);
        fs::write(&path, "{").unwrap();
        assert_eq!(
            StatsCatalog::load(&path).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn medals_exclude_flagged_stats_and_keep_tied_players_in_order() {
        let catalog = fixture();
        let team: Vec<_> = [4.0, 9.0, 0.0, 9.0, 2.0, f64::NAN, -1.0]
            .into_iter()
            .map(|value| {
                let mut stats = PlayerStats::default();
                stats.set(ELIMINATIONS, value);
                stats.set(DEATHS, value);
                stats
            })
            .collect();
        let team: Vec<_> = team.iter().collect();
        let medals = catalog.medals(&team, 0);
        let kills: Vec<_> = medals
            .iter()
            .map(|entry| entry.get(&ELIMINATIONS).copied().unwrap_or_default())
            .collect();
        assert_eq!(kills, [BRONZE, GOLD, 0, SILVER, 0, 0, 0]);
        assert!(
            medals
                .iter()
                .all(|medal| !medal.contains_key(&DEATHS) && !medal.contains_key(&HERO_DAMAGE))
        );
    }

    #[test]
    fn owner_scoreboard_writes_full_slots_and_matches_python_at_bit_offset() {
        let catalog = fixture();
        let mut stats = PlayerStats::default();
        stats.add(ELIMINATIONS, 1.0);
        stats.add(ELIMINATIONS, 2.0);
        stats.set(HERO_DAMAGE, 1234.5);
        stats.set(DEATHS, 1.0);
        stats.set(HEALING, 80.125);
        let medals = Medals::from([(ELIMINATIONS, GOLD)]);
        let board = catalog.component(&stats, 0, Some(HERO), Some(&medals));
        let record = update(3, std::slice::from_ref(&board)).unwrap();
        assert_eq!(record.bit_len(), 1485);
        assert_eq!(
            format!("{:x}", Sha256::digest(record.as_bytes())),
            "87fc1849c25d7c6d89a8418a326436b2f4de432c535be54f299389579f027128"
        );
        assert_eq!(
            board.fields[MEDAL_STAT],
            Some(ComponentValue::U64(ELIMINATIONS))
        );
        assert_eq!(board.fields[MEDAL_VALUE], Some(ComponentValue::F32(3.0)));
        assert_eq!(board.fields[MEDAL], Some(ComponentValue::U8(GOLD)));
        assert_eq!(board.fields[HERO_STAT], Some(ComponentValue::U64(HEALING)));
        assert_eq!(board.fields[HERO_VALUE], Some(ComponentValue::F32(80.125)));
        assert_eq!(board.fields[SCORE], None);
        assert_eq!(board.fields[THRESHOLD], None);
        assert_eq!(board.fields[ON_FIRE], None);
        let before_pick = catalog.component(&stats, 0, None, None);
        for index in 0..HERO_SLOTS {
            assert_eq!(
                before_pick.fields[HERO_STAT + index],
                Some(ComponentValue::U64(0))
            );
            assert_eq!(
                before_pick.fields[HERO_VALUE + index],
                Some(ComponentValue::F32(0.0))
            );
        }
        let switch_mode = catalog.component(&stats, MODE, Some(HERO), None);
        assert_eq!(
            switch_mode.fields[MEDAL_STAT + 1],
            Some(ComponentValue::U64(0))
        );
        assert_eq!(switch_mode.fields[HERO_STAT], Some(ComponentValue::U64(0)));
    }

    #[test]
    fn on_fire_updates_match_python_and_leave_other_fields_unwritten() {
        assert_eq!(
            initial_on_fire(&on_fire_component(None, None, None)),
            Some(false)
        );
        assert_eq!(
            initial_on_fire(&on_fire_component(None, None, Some(true))),
            Some(true)
        );
        let owner = update(
            0,
            &[on_fire_component(Some(250.0), Some(500.0), Some(false))],
        )
        .unwrap();
        assert_eq!(
            owner.as_bytes(),
            [
                0x01, 0x40, 0x06, 0x00, 0x00, 0x18, 0x00, 0x10, 0x00, 0x00, 0x00, 0x7a, 0x43, 0x00,
                0x00, 0xfa, 0x43, 0x00,
            ]
        );
        let flag = update(0, &[on_fire_component(None, None, Some(true))]).unwrap();
        assert_eq!(flag.as_bytes(), [0x01, 0x40, 0x06, 0, 0, 0, 0, 0x10, 0, 1]);
        assert_eq!(meter(0.0, 1000.0), 0.0);
        assert_eq!(meter(640.0, 500.0), 100.0);
        assert_eq!(meter(900.0, 500.0), 100.0);
        assert_eq!(meter(320.0, 500.0), 50.0);
        assert!(meter(10.0, 0.0).is_nan());
        assert!(meter(f32::NAN, 500.0).is_nan());
    }

    #[test]
    fn stats_layout_accepts_the_unsigned_scalar_and_rejects_nested_fields() {
        let mut fields = vec![None; GMP_STATS_FIELDS];
        fields[35] = Some(ComponentValue::U16(0xabcd));
        let mut component = Component {
            index: GMP_STATS,
            fields,
        };
        let scalar = update(0, std::slice::from_ref(&component)).unwrap();
        assert_eq!(
            scalar.as_bytes(),
            [1, 0x40, 6, 0, 0, 0, 0, 8, 0, 0xcd, 0xab]
        );
        for field in 0..5 {
            component.fields[field] = Some(ComponentValue::U64(1));
            assert!(matches!(
                update(0, std::slice::from_ref(&component)),
                Err(WorldError::FieldType { component: GMP_STATS, field: bad }) if bad == field
            ));
            component.fields[field] = None;
        }
        component.fields.push(None);
        assert!(matches!(
            update(0, &[component]),
            Err(WorldError::FieldCount {
                component: GMP_STATS,
                ..
            })
        ));
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 stat catalog"]
    fn actual_scoreboard_components_match_the_python_wire_corpus() {
        use crate::game_content::PRACTICE_RANGE_MODE;
        use std::{collections::BTreeSet, path::PathBuf};

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let catalog = StatsCatalog::load(root.join("data/game_stats_174.json")).unwrap();
        let modes: BTreeSet<_> = std::iter::once(&catalog.player)
            .chain(catalog.heroes.values())
            .flat_map(|group| group.modes.keys().copied())
            .chain([0, PRACTICE_RANGE_MODE, 0x0230_0000_0000_0017])
            .collect();
        let mut heroes = catalog.heroes.keys().copied().collect::<Vec<_>>();
        heroes.sort_unstable();
        let mut records: [PlayerStats; 5] = std::array::from_fn(|_| PlayerStats::default());
        for &stat in catalog.display.keys() {
            let base = (stat & 0xf) as f64 / 8.0;
            for (index, (record, value)) in records
                .iter_mut()
                .zip([0.25, 1.25, 0.0, 1.25, 0.75])
                .enumerate()
            {
                record.set(stat, if index == 2 { 0.0 } else { base + value });
            }
        }
        let team: Vec<_> = records.iter().collect();
        let mut digest = Sha256::new();
        let mut case = 0;
        for mode in modes.iter().copied() {
            let medals = catalog.medals(&team, mode);
            for hero in std::iter::once(None).chain(heroes.iter().copied().map(Some)) {
                let index = case % records.len();
                let origin = case % 8;
                let board = catalog.component(&records[index], mode, hero, Some(&medals[index]));
                let packet = update(origin, &[board]).unwrap();
                digest.update(mode.to_le_bytes());
                digest.update(hero.unwrap_or_default().to_le_bytes());
                digest.update(u32::try_from(origin).unwrap().to_le_bytes());
                digest.update(u32::try_from(packet.bit_len()).unwrap().to_le_bytes());
                digest.update(packet.as_bytes());
                case += 1;
            }
        }
        assert_eq!((modes.len(), heroes.len(), case), (14, 32, 462));
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "50198309f54b9b975c225a13a2d6c0a5277faaa8615f715ad155e95e120b94de"
        );
    }
}
