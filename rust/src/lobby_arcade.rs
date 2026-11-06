//! Arcade catalog, dated rotation and wire messages for retail build 104319.
//!
//! The catalog is supplied by the external runtime. Group ordering and the
//! dated rotation follow `ow174/content/arcade.py`.

use std::{
    collections::{BTreeMap, HashSet},
    fs::File,
    io::{self, BufReader},
    path::Path,
};

use serde::Deserialize;

use crate::{
    accounts::{Profile, parse_guid},
    jam_codec::{
        Fields,
        Value::{Array, Bool, Float32, Signed, Struct, Unsigned},
    },
    lobby_clock::DAY,
    lobby_passes::Pool,
    lobby_ranked::{CARD_BASE, Season},
    lobby_transport::LobbyMessage,
    retail_templates::RetailTemplates,
};

pub const ARCADE: u32 = 0x00a8_654e;
const SLOTS: usize = 7;
const CARDS: u32 = 39802;
const BUSY: u32 = 39804;
const WINDOWS: u32 = 39810;
const LUNAR: &[u32] = &[0x1a3, 0x25, 0x10c];
const COMPETITIVE_CTF: &[u32] = &[0x1c3];
const WINTER: &[u32] = &[0x98, 0x107, 0x4b, 0x5b, 0x1f];
const JUNKENSTEIN: &[u32] = &[
    0x3c, 0x3d, 0x4a, 0x3e, 0x43, 0x44, 0x49, 0x45, 0x16b, 0x16c, 0x16d, 0x16e, 0x16f, 0x170,
];
const LUCIOBALL: &[u32] = &[0x132, 0x35, 0x1ad];
const ARCHIVES: &[u32] = &[
    0x29, 0x24, 0x2a, 0x2b, 0x2c, 0x28, 0x2d, 0x2e, 0x58, 0x3a, 0x59, 0x5a, 0x66, 0x67, 0x68, 0x69,
    0x8a, 0x8b, 0x8c, 0x8d, 0x8e, 0x8f, 0x90, 0x91,
];
const CHALLENGES: &[u32] = &[
    0x119, 0x112, 0x1a9, 0x116, 0x127, 0x1aa, 0x111, 0x11b, 0x1ab,
];
const SEASONAL: &[&[u32]] = &[WINTER, JUNKENSTEIN, LUCIOBALL, ARCHIVES, LUNAR];
const EVENTS: &[(&str, &[&[u32]])] = &[
    ("lunar", &[LUNAR, COMPETITIVE_CTF]),
    ("winter", &[WINTER]),
    ("halloween", &[JUNKENSTEIN]),
    ("summer", &[LUCIOBALL]),
    ("archives", &[ARCHIVES, CHALLENGES]),
    ("anniversary", SEASONAL),
    ("anniversary_remix_1", SEASONAL),
    ("anniversary_remix_2", SEASONAL),
    ("malevento", &[&[0x1ae], &[0x1b0]]),
];
const FIXED_DAILY: [u32; 2] = [0x15c, 0x34];
const ROTATING: [u32; 12] = [
    0x1c, 0x64, 0xad, 0x1, 0x33, 0x1e, 0x3b, 0x1d, 0x30, 0xa6, 0x7a, 0x47,
];
const PERMANENT: [u32; 2] = [0xef, 0x2];

#[derive(Deserialize)]
struct CardSource {
    identifier: String,
    display: String,
    rewards: Vec<String>,
    flags: [bool; 7],
    #[serde(default)]
    roles: bool,
    #[serde(default)]
    passes: Option<String>,
}

struct Card {
    identifier: u64,
    display: u64,
    rewards: Vec<u64>,
    flags: [bool; 7],
    roles: bool,
    pool: Option<Pool>,
}

pub struct Arcade {
    cards: BTreeMap<u32, Card>,
    event_cards: HashSet<u32>,
    lists_template: Fields,
    busy_template: Vec<Fields>,
}

pub struct ArcadeState {
    pub messages: [LobbyMessage; 3],
    pub competitive_cards: Vec<u64>,
}

impl Arcade {
    pub fn load(path: impl AsRef<Path>, templates: &RetailTemplates) -> io::Result<Self> {
        let source: BTreeMap<String, CardSource> =
            serde_json::from_reader(BufReader::new(File::open(path)?)).map_err(invalid)?;
        let guid = |key: &str| {
            parse_guid(key).ok_or_else(|| invalid(format!("invalid arcade GUID {key}")))
        };
        let cards = source
            .into_iter()
            .map(|(key, card)| {
                let index = u32::try_from(guid(&key)?).map_err(invalid)?;
                let pool = card
                    .passes
                    .as_deref()
                    .map(guid)
                    .transpose()?
                    .and_then(Pool::from_guid);
                Ok((
                    index,
                    Card {
                        identifier: guid(&card.identifier)?,
                        display: guid(&card.display)?,
                        rewards: card
                            .rewards
                            .iter()
                            .map(|key| guid(key))
                            .collect::<io::Result<_>>()?,
                        flags: card.flags,
                        roles: card.roles,
                        pool,
                    },
                ))
            })
            .collect::<io::Result<BTreeMap<_, _>>>()?;
        let event_cards: HashSet<_> = EVENTS
            .iter()
            .flat_map(|(_, groups)| groups.iter())
            .flat_map(|group| group.iter().copied())
            .collect();
        for card in event_cards
            .iter()
            .copied()
            .chain(FIXED_DAILY)
            .chain(ROTATING)
            .chain(PERMANENT)
        {
            if !cards.contains_key(&card) {
                return Err(invalid(format!("missing arcade card {card:X}")));
            }
        }
        let lists_template = templates
            .first(ARCADE, CARDS)
            .ok_or_else(|| invalid("missing arcade card-list template"))?;
        let mut busy = templates
            .first(ARCADE, BUSY)
            .ok_or_else(|| invalid("missing arcade busyness template"))?;
        let Some(Array(busy)) = busy.remove(&0x78) else {
            return Err(invalid("invalid arcade busyness template"));
        };
        let busy_template = busy
            .into_iter()
            .map(|entry| {
                let Struct(entry) = entry else {
                    return Err(invalid("invalid arcade busyness record"));
                };
                if !matches!(entry.get(&0), Some(Unsigned(_))) {
                    return Err(invalid("invalid arcade busyness card GUID"));
                }
                Ok(entry)
            })
            .collect::<io::Result<_>>()?;
        Ok(Self {
            cards,
            event_cards,
            lists_template,
            busy_template,
        })
    }

    pub fn has_roles(&self, card: u64) -> bool {
        self.cards
            .get(&(card as u32))
            .is_some_and(|card| card.roles)
    }

    pub fn pass_pool(&self, card: u64) -> Option<Pool> {
        self.cards.get(&(card as u32)).and_then(|card| card.pool)
    }

    pub fn state(&self, profile: &Profile, season: Season, now: u64) -> io::Result<ArcadeState> {
        let cards = card_order(&profile.events, now);
        let competitive_cards = cards
            .iter()
            .filter_map(|&index| self.cards[&index].flags[0].then_some(full(index)))
            .collect();
        let windows = cards
            .iter()
            .map(|&index| {
                Struct(Fields::from([
                    (0, Unsigned(full(index))),
                    (8, Unsigned(now.saturating_sub(DAY))),
                    (0x10, Unsigned(now.saturating_add(30 * DAY))),
                ]))
            })
            .collect();
        Ok(ArcadeState {
            messages: [
                (ARCADE, CARDS, self.card_lists(&cards, season)?),
                (ARCADE, WINDOWS, Fields::from([(0x78, Array(windows))])),
                (ARCADE, BUSY, self.busy(&cards)),
            ],
            competitive_cards,
        })
    }

    fn card_lists(&self, cards: &[u32], season: Season) -> io::Result<Fields> {
        let mut lists = self.lists_template.clone();
        let Some(Struct(fields)) = lists.get_mut(&0x78) else {
            return Err(invalid("invalid arcade card-list template"));
        };
        let Some(Array(competitive)) = fields.get_mut(&0xd0) else {
            return Err(invalid("missing competitive cards in arcade template"));
        };
        let mut competitive_rows = competitive.iter_mut();
        let Some(Struct(main)) = competitive_rows.next() else {
            return Err(invalid("missing main competitive card template"));
        };
        main.insert(0, Unsigned(season.card));
        main.insert(
            0x88,
            Array(season.open_card.map(Unsigned).into_iter().collect()),
        );
        main.insert(0xb2, Bool(season.has_roles()));
        if let Some(open_card) = season.open_card {
            let Some(Struct(open)) = competitive_rows.next() else {
                return Err(invalid("missing open queue card template"));
            };
            let card = self
                .cards
                .get(&(open_card as u32))
                .ok_or_else(|| invalid(format!("missing open queue card {open_card:X}")))?;
            open.insert(0, Unsigned(open_card));
            open.insert(0x18, Unsigned(card.display));
            open.insert(0x80, Unsigned(season.card));
        }
        competitive.truncate(if season.open_card.is_some() { 2 } else { 1 });
        fields.insert(
            0,
            Array(cards.iter().map(|&index| self.card(index)).collect()),
        );
        Ok(lists)
    }

    fn card(&self, index: u32) -> crate::jam_codec::Value {
        let card = &self.cards[&index];
        let daily = FIXED_DAILY.contains(&index) || ROTATING.contains(&index);
        let mut fields = Fields::from([
            (0, Unsigned(full(index))),
            (0x8, Unsigned(0)),
            (0x10, Unsigned(card.identifier)),
            (0x18, Unsigned(card.display)),
            (
                0x20,
                Array(card.rewards.iter().copied().map(Unsigned).collect()),
            ),
            (0x38, Array(Vec::new())),
            (0x50, Array(Vec::new())),
            (0x68, Array(Vec::new())),
            (0x80, Unsigned(0)),
            (0x88, Array(Vec::new())),
            (0xa0, Signed(0)),
            (0xa4, Unsigned(if daily { 24 } else { 0 })),
        ]);
        let capture_flag = matches!(index, 0x1c | 0xef | 0x2);
        for (offset, flag) in (0xa8..).zip(
            [capture_flag]
                .into_iter()
                .chain(card.flags)
                .chain([false; 3]),
        ) {
            fields.insert(offset, Bool(flag));
        }
        Struct(fields)
    }

    fn busy(&self, cards: &[u32]) -> Fields {
        let mut known = HashSet::new();
        let mut entries = Vec::new();
        for item in &self.busy_template {
            let Unsigned(card) = item[&0] else {
                unreachable!("validated template")
            };
            let index = card as u32;
            if self.event_cards.contains(&index) && !cards.contains(&index) {
                continue;
            }
            known.insert(card);
            entries.push(Struct(item.clone()));
        }
        for &index in cards {
            let card = full(index);
            if known.insert(card) {
                entries.push(Struct(Fields::from([
                    (0, Unsigned(card)),
                    (8, Float32(0.1)),
                ])));
            }
        }
        Fields::from([(0x78, Array(entries))])
    }
}

fn card_order(events: &[String], now: u64) -> Vec<u32> {
    let mut groups = Vec::new();
    for event in events {
        if let Some((_, seasonal)) = EVENTS.iter().find(|(name, _)| *name == event) {
            for &group in *seasonal {
                if !groups.contains(&group) {
                    groups.push(group);
                }
            }
        }
    }
    let rotating = SLOTS.saturating_sub(groups.len() + FIXED_DAILY.len() + PERMANENT.len());
    let offset = ((now / DAY) % ROTATING.len() as u64) as usize;
    let room = SLOTS.saturating_sub(groups.len());
    groups
        .into_iter()
        .take(SLOTS)
        .flat_map(|group| group.iter().copied())
        .chain(
            FIXED_DAILY
                .into_iter()
                .chain((0..rotating).map(|i| ROTATING[(offset + i) % ROTATING.len()]))
                .chain(PERMANENT)
                .take(room),
        )
        .collect()
}

fn full(index: u32) -> u64 {
    CARD_BASE | u64::from(index)
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use serde_json::json;
    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{jam_codec::Schemas, lobby_passes, lobby_ranked::Ranked};

    #[test]
    fn rotation_counts_groups_instead_of_cards_and_deduplicates_events() {
        assert_eq!(
            card_order(&[], 0),
            [0x15c, 0x34, 0x1c, 0x64, 0xad, 0xef, 0x2]
        );
        assert_eq!(card_order(&[], DAY)[2..5], [0x64, 0xad, 0x1]);
        assert_eq!(
            card_order(&["lunar".into(), "lunar".into()], 0),
            [0x1a3, 0x25, 0x10c, 0x1c3, 0x15c, 0x34, 0x1c, 0xef, 0x2]
        );
        let anniversary = card_order(&["anniversary".into()], 0);
        assert_eq!(
            anniversary.len(),
            SEASONAL.iter().map(|group| group.len()).sum::<usize>() + 2
        );
        assert!(anniversary.ends_with(&FIXED_DAILY));
    }

    #[test]
    #[ignore = "requires an authorized 104319 Arcade catalog in ORPHEUS_RUNTIME_DIR"]
    fn arcade_and_pass_wire_bytes_match_python_across_events_seasons_and_dates() {
        let data = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap()).join("data");
        let schemas = Schemas::load(data.join("schemas_174.json")).unwrap();
        let templates = RetailTemplates::load(data.join("retail_templates.json")).unwrap();
        let ranked = Ranked::new(&templates).unwrap();
        let arcade = Arcade::load(data.join("arcade_cards_174.json"), &templates).unwrap();
        assert!(arcade.has_roles(CARD_BASE | 0xed));
        assert_eq!(arcade.pass_pool(CARD_BASE | 0xed), Some(Pool::QuickPlay));
        assert!(!arcade.has_roles(2));
        assert_eq!(arcade.pass_pool(2), None);
        let events: &[&[&str]] = &[
            &[],
            &["goodbye"],
            &["lunar"],
            &["winter"],
            &["halloween"],
            &["summer"],
            &["archives"],
            &["anniversary"],
            &["anniversary_remix_1"],
            &["anniversary_remix_2"],
            &["malevento"],
            &["lunar", "summer"],
            &["winter", "anniversary", "summer"],
            &["archives", "malevento", "lunar", "lunar"],
            &["unknown", "halloween", "winter"],
        ];
        let seasons = [1, 17, 18, 22, 23, 32];
        let mut digest = Sha256::new();
        for (group, event) in events.iter().enumerate() {
            for day in 0..12 {
                let n = group * 12 + day;
                let profile = Profile {
                    events: event.iter().map(|value| (*value).into()).collect(),
                    season: seasons[n % 6],
                    priority_passes: [
                        ("quick_play".into(), json!(((n * 7) % 41).to_string())),
                        ("competitive".into(), json!((n * 13) % 41)),
                    ]
                    .into_iter()
                    .collect(),
                    ..Profile::default()
                };
                let now = 1_700_000_000 + n as u64 * DAY + 29;
                let state = arcade
                    .state(&profile, ranked.season(&profile), now)
                    .unwrap();
                digest.update((state.competitive_cards.len() as u32).to_le_bytes());
                for card in state.competitive_cards {
                    digest.update(card.to_le_bytes());
                }
                for (crc, id, fields) in state
                    .messages
                    .into_iter()
                    .chain([lobby_passes::counts(&profile).unwrap()])
                {
                    let mut wire = schemas.header(crc, 1, id).unwrap();
                    wire.extend(schemas.encode(crc, id, &fields).unwrap());
                    digest.update(crc.to_le_bytes());
                    digest.update(id.to_le_bytes());
                    digest.update((wire.len() as u32).to_le_bytes());
                    digest.update(wire);
                }
            }
        }
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "e1dbf0e419aab360fa3f2fa3eb38600272b2676af3c544f10a52729b90f048a4"
        );
    }
}
