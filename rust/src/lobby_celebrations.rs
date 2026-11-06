//! Login event records and their externally supplied content keys.
//!
//! Mirrors `ow174/content/celebrations.py` and `ow174/catalog/events.py`.
//! Resource bytes stay in the separate runtime catalog and are sent on the
//! lobby link. Errors do not include the contents of that catalog.

use std::{
    collections::{BTreeMap, HashSet},
    fs::File,
    io::{self, BufReader, Read},
    path::Path,
};

use indexmap::IndexMap;
use serde::Deserialize;

use crate::{
    accounts::{Profile, parse_guid},
    catalog::{ItemDb, UNLOCK_BASE},
    jam_codec::{
        Fields,
        Value::{self, Array, Float64, Signed, Struct, Unsigned},
    },
    lobby_clock::{DAY, stu_datetime},
    lobby_transport::LobbyMessage,
    retail_templates::{IN_CONNECT, RetailTemplates},
};

pub const EVENTS: u32 = 0x8b77_c823;
pub const CONTENT_KEYS: u32 = 20504;
pub const CELEBRATIONS: u32 = 38900;
pub const CHALLENGE_PROGRESS: u32 = 38902;

const CELEBRATION_BASE: u64 = 0x0430_0000_0000_0000;
const CELEBRATION_TYPE_BASE: u64 = 0x0d80_0000_0000_0000;
const RESOURCE_KEY_BASE: u64 = 0x0f10_0000_0000_0000;
const MAP_BASE: u64 = 0x0800_0000_0000_0000;
const CHALLENGE_WINS_STAT: u64 = 0x0860_0000_0000_073f;
const CHALLENGE_TIER_WINS: u64 = 9;
const KEY_LENGTH: usize = 16;

#[derive(Clone, Copy, Eq, PartialEq)]
struct Event {
    celebration: u16,
    key: u16,
    kind: u16,
    box_type: i64,
    rewards: &'static [u16],
    // Splash presets and raw IDs with the same celebration are separate entries.
    splash: bool,
}

impl Event {
    const fn new(celebration: u16) -> Self {
        Self {
            celebration,
            key: 0,
            kind: 0,
            box_type: -1,
            rewards: &[],
            splash: false,
        }
    }

    const fn keyed(celebration: u16, key: u16) -> Self {
        Self {
            key,
            ..Self::new(celebration)
        }
    }

    const fn seasonal(
        celebration: u16,
        key: u16,
        kind: u16,
        box_type: i64,
        rewards: &'static [u16],
    ) -> Self {
        Self {
            kind,
            box_type,
            rewards,
            ..Self::keyed(celebration, key)
        }
    }

    fn guid(self) -> u64 {
        CELEBRATION_BASE | u64::from(self.celebration)
    }
}

fn preset(name: &str) -> Option<Event> {
    Some(match name {
        "goodbye" => Event::keyed(0x128, 0x1a3),
        "lunar" => Event::seasonal(0x105, 0x188, 0x237c, 4, &[0x0d5a, 0x4f50]),
        "halloween" => Event::seasonal(0xfb, 0x181, 0x212e, 2, &[0x0c23]),
        "winter" => Event::seasonal(0xff, 0x185, 0x2168, 3, &[0x0d0b]),
        "anniversary" => Event::seasonal(0x118, 0x198, 0x2428, 6, &[0x0ec3]),
        "anniversary_remix_1" => Event::seasonal(0x10e, 0x196, 0x2428, 6, &[0x0ec3]),
        "anniversary_remix_2" => Event::seasonal(0x112, 0x197, 0x2428, 6, &[0x0ec3]),
        "summer" => Event::seasonal(0xeb, 0x178, 0x212f, 1, &[0x0b2f]),
        "archives" => Event::seasonal(0xa8, 0x169, 0x2427, 5, &[0x0ec2]),
        "reaper" => Event {
            rewards: &[0x4dc7, 0x4ef3, 0x4fcf],
            ..Event::keyed(0x104, 0x189)
        },
        "cassidy" => Event::keyed(0xf7, 0x17f),
        "malevento" => Event::new(0xfa),
        "owl" => Event::new(0x125),
        "watchpoint" => Event::new(0x126),
        "ow2_credits" => Event {
            splash: true,
            ..Event::new(0x123)
        },
        _ => return None,
    })
}

fn active_events(names: &[String]) -> io::Result<Vec<Event>> {
    let mut events = Vec::new();
    for name in names {
        let name = name.trim().to_ascii_lowercase();
        let event = if name.starts_with("0x") {
            let raw = parse_guid(&name).ok_or_else(|| invalid("invalid raw celebration ID"))?;
            Some(Event::new(raw as u16))
        } else {
            preset(&name)
        };
        if let Some(event) = event.filter(|event| !events.contains(event)) {
            events.push(event);
        }
    }
    Ok(events)
}

struct Challenge {
    event: Event,
    rewards: [u64; 3],
}

fn challenge(name: &str, items: &ItemDb) -> Option<Challenge> {
    let (celebration, indexes) = match name {
        "Tracer's Comic Challenge" => (0x119, [0x4aea, 0x4aeb, 0x4aec]),
        "Symmetra's Restoration Challenge" => (0x11a, [0x4b10, 0x4b11, 0x4b08]),
        "Kanezaka Challenge" => (0x11b, [0x4bb2, 0x4bb1, 0x4aa4]),
        _ => return None,
    };
    let rewards = indexes.map(|index| UNLOCK_BASE | index);
    rewards
        .iter()
        .all(|&guid| items.get(guid).is_some())
        .then_some(Challenge {
            event: Event::keyed(celebration, 0x198),
            rewards,
        })
}

struct ResourceKey {
    name: u64,
    bytes: [u8; KEY_LENGTH],
}

#[derive(Deserialize)]
struct RawKey {
    name: Option<String>,
    key: String,
}

#[derive(Deserialize)]
struct RawMap {
    #[serde(rename = "CelebrationVariants")]
    variants: Option<Vec<RawMapVariant>>,
}

#[derive(Deserialize)]
struct RawMapVariant {
    #[serde(rename = "Virtual01C")]
    kind: String,
    #[serde(rename = "MapInfo")]
    map: VariantMap,
}

#[derive(Deserialize)]
struct VariantMap {
    #[serde(rename = "GUID")]
    guid: String,
}

type MapSwaps = BTreeMap<u64, Vec<(u64, u64)>>;

pub struct Celebrations {
    captured_names: Vec<u64>,
    captured_bytes: Vec<u8>,
    resource_keys: BTreeMap<u64, ResourceKey>,
    map_swaps: MapSwaps,
}

impl Celebrations {
    pub fn load(data_dir: &Path, templates: &RetailTemplates) -> io::Result<Self> {
        Self::from_readers(
            BufReader::new(File::open(data_dir.join("resource_keys_174.json"))?),
            BufReader::new(File::open(data_dir.join("extracted_maps.json"))?),
            templates,
        )
    }

    fn from_readers(
        keys: impl Read,
        maps: impl Read,
        templates: &RetailTemplates,
    ) -> io::Result<Self> {
        let template = templates
            .first(IN_CONNECT, CONTENT_KEYS)
            .ok_or_else(|| invalid("missing content-key template"))?;
        let (captured_names, captured_bytes) = template_prefix(&template)?;
        Ok(Self {
            captured_names,
            captured_bytes,
            resource_keys: read_keys(keys)?,
            map_swaps: read_map_swaps(maps)?,
        })
    }

    /// Build 20504, 38900, and 38902 in their relative login order. The
    /// caller places preload between the content keys and the event records.
    pub fn login_messages(
        &self,
        profile: &Profile,
        items: &ItemDb,
        now: u64,
    ) -> io::Result<[LobbyMessage; 3]> {
        let events = active_events(&profile.events)?;
        let challenge = challenge(&profile.challenge, items);
        let keys = self.content_keys(&events, challenge.as_ref());
        let mut records = events
            .iter()
            .map(|&event| self.record_fields(event, now).map(Struct))
            .collect::<io::Result<Vec<_>>>()?;
        let mut progress = Vec::new();
        if let Some(challenge) = challenge {
            let mut record = self.record_fields(challenge.event, now)?;
            let tiers = challenge
                .rewards
                .into_iter()
                .zip(1..)
                .map(|(guid, tier)| {
                    Struct(Fields::from([
                        (0, Array(vec![Unsigned(guid)])),
                        (0x18, Unsigned(CHALLENGE_TIER_WINS * tier)),
                    ]))
                })
                .collect();
            record.insert(0x18, Array(tiers));
            record.insert(0x78, Unsigned(CHALLENGE_WINS_STAT));
            records.push(Struct(record));
            progress.push(Struct(Fields::from([
                (0, Unsigned(challenge.event.guid())),
                (8, Float64(profile.challenge_wins as f64)),
            ])));
        }
        Ok([
            (IN_CONNECT, CONTENT_KEYS, keys),
            (EVENTS, CELEBRATIONS, Fields::from([(0x78, Array(records))])),
            (
                EVENTS,
                CHALLENGE_PROGRESS,
                Fields::from([(0x78, Array(progress))]),
            ),
        ])
    }

    fn content_keys(&self, events: &[Event], challenge: Option<&Challenge>) -> Fields {
        let mut names = self.captured_names.clone();
        let mut bytes = self.captured_bytes.clone();
        let mut seen: HashSet<_> = names.iter().copied().collect();
        let prioritized = events
            .iter()
            .map(|event| event.key)
            .chain(challenge.map(|challenge| challenge.event.key))
            .filter(|&key| key != 0)
            .filter_map(|key| self.resource_keys.get(&u64::from(key)));
        for key in prioritized.chain(self.resource_keys.values()) {
            if key.name != 0 && seen.insert(key.name) {
                names.push(key.name);
                bytes.extend(key.bytes);
            }
        }
        Fields::from([
            (0x78, Array(names.into_iter().map(Unsigned).collect())),
            (
                0x90,
                Array(
                    bytes
                        .into_iter()
                        .map(|byte| Unsigned(u64::from(byte)))
                        .collect(),
                ),
            ),
        ])
    }

    fn record_fields(&self, event: Event, now: u64) -> io::Result<Fields> {
        let key = u64::from(event.key);
        let (key_name, key_guid) = if key == 0 {
            (0, 0)
        } else {
            (
                self.resource_keys.get(&key).map_or(0, |key| key.name),
                RESOURCE_KEY_BASE | key,
            )
        };
        let kind = u64::from(event.kind);
        let kind_guid = if kind == 0 {
            0
        } else {
            CELEBRATION_TYPE_BASE | kind
        };
        let swaps = self
            .map_swaps
            .get(&kind)
            .into_iter()
            .flatten()
            .map(|&(base, variant)| {
                Struct(Fields::from([(0, Unsigned(base)), (8, Unsigned(variant))]))
            })
            .collect();
        let rewards = event
            .rewards
            .iter()
            .map(|&guid| Unsigned(UNLOCK_BASE | u64::from(guid)))
            .collect();
        let start = now
            .checked_sub(2 * DAY)
            .ok_or_else(|| invalid("invalid celebration start date"))?;
        let stop = now
            .checked_add(365 * DAY)
            .ok_or_else(|| invalid("invalid celebration end date"))?;
        Ok(Fields::from([
            (0, Array(swaps)),
            (0x18, Array(Vec::new())),
            (0x30, Unsigned(key_name)),
            (0x38, Unsigned(key_guid)),
            (0x40, Unsigned(event.guid())),
            (0x48, date(start)?),
            (0x50, date(stop)?),
            (0x58, Array(rewards)),
            (0x70, Unsigned(kind_guid)),
            (0x78, Unsigned(0)),
            (0x80, Array(Vec::new())),
            (0x98, Array(Vec::new())),
            (0xb0, Signed(event.box_type)),
        ]))
    }
}

fn date(seconds: u64) -> io::Result<Value> {
    Ok(Struct(Fields::from([(
        0,
        Unsigned(stu_datetime(seconds)?),
    )])))
}

fn read_keys(reader: impl Read) -> io::Result<BTreeMap<u64, ResourceKey>> {
    let malformed = || invalid("invalid external resource-key catalog");
    let entries: IndexMap<String, RawKey> =
        serde_json::from_reader(reader).map_err(|_| malformed())?;
    entries
        .into_iter()
        .map(|(index, entry)| {
            let index = hex_id(&index).ok_or_else(malformed)?;
            let name = match entry.name.as_deref() {
                Some(name) if !name.is_empty() => hex_id(name).ok_or_else(malformed)?,
                _ => 0,
            };
            let bytes = key_bytes(&entry.key).ok_or_else(malformed)?;
            Ok((index, ResourceKey { name, bytes }))
        })
        .collect()
}

fn key_bytes(hex: &str) -> Option<[u8; KEY_LENGTH]> {
    let mut bytes = Vec::with_capacity(KEY_LENGTH);
    for part in hex.split_ascii_whitespace() {
        let pairs = part.as_bytes().chunks_exact(2);
        if !pairs.remainder().is_empty() {
            return None;
        }
        for pair in pairs {
            let high = char::from(pair[0]).to_digit(16)?;
            let low = char::from(pair[1]).to_digit(16)?;
            bytes.push((high * 16 + low) as u8);
        }
    }
    bytes.try_into().ok()
}

fn read_map_swaps(reader: impl Read) -> io::Result<MapSwaps> {
    let maps: IndexMap<String, RawMap> =
        serde_json::from_reader(reader).map_err(|_| invalid("invalid celebration map catalog"))?;
    let mut swaps = MapSwaps::new();
    for (guid, map) in maps {
        let base = MAP_BASE | datatool_index(&guid)?;
        for variant in map.variants.into_iter().flatten() {
            let kind = datatool_index(&variant.kind)?;
            let variant = MAP_BASE | datatool_index(&variant.map.guid)?;
            swaps.entry(kind).or_default().push((base, variant));
        }
    }
    Ok(swaps)
}

fn datatool_index(guid: &str) -> io::Result<u64> {
    let index = guid.split_once('.').map_or(guid, |(index, _)| index);
    hex_id(index).ok_or_else(|| invalid("invalid DataTool celebration map GUID"))
}

fn hex_id(text: &str) -> Option<u64> {
    let text = text.trim();
    let text = text.strip_prefix('+').unwrap_or(text);
    if text.starts_with("0x") || text.starts_with("0X") {
        parse_guid(text)
    } else {
        parse_guid(&format!("0x{text}"))
    }
}

fn template_prefix(fields: &Fields) -> io::Result<(Vec<u64>, Vec<u8>)> {
    let malformed = || invalid("invalid content-key template");
    let Some(Array(names)) = fields.get(&0x78) else {
        return Err(malformed());
    };
    let names = names
        .iter()
        .map(|value| match value {
            Unsigned(name) => Ok(*name),
            _ => Err(malformed()),
        })
        .collect::<io::Result<Vec<_>>>()?;
    let bytes = match fields.get(&0x90) {
        Some(Value::Blob(bytes)) => bytes.clone(),
        Some(Array(bytes)) => bytes
            .iter()
            .map(|value| match value {
                Unsigned(byte) => u8::try_from(*byte).map_err(|_| malformed()),
                _ => Err(malformed()),
            })
            .collect::<io::Result<_>>()?,
        _ => return Err(malformed()),
    };
    if names.len().checked_mul(KEY_LENGTH) != Some(bytes.len()) {
        return Err(malformed());
    }
    Ok((names, bytes))
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{catalog::Unlock, jam_codec::Schemas, lobby_clock::server_time};

    const TRACER: &str = "Tracer's Comic Challenge";
    const TRACER_REWARDS: [u64; 3] = [
        UNLOCK_BASE | 0x4aea,
        UNLOCK_BASE | 0x4aeb,
        UNLOCK_BASE | 0x4aec,
    ];

    fn names(values: &[&str]) -> Vec<String> {
        values.iter().map(|&value| value.into()).collect()
    }

    fn fixture() -> Celebrations {
        let templates = RetailTemplates::from_reader(
            &br#"{
            "format":1,"server":[["0xF5FF548A",20504,{"+0x78":[9],
            "+0x90":{"$bytes":"09090909090909090909090909090909"},"+0xA8":[] }]]
        }"#[..],
        )
        .unwrap();
        let keys = &br#"{
            "198":{"name":"b2","key":"b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"},
            "1":{"name":"a1","key":"11111111111111111111111111111111"},
            "0x188":{"name":"a1","key":"a1a1a1a1a1a1a1a1 a1a1a1a1a1a1a1a1"},
            "4":{"name":"c3","key":"c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"},
            "181":{"name":null,"key":"81818181818181818181818181818181"},
            "3":{"name":"9","key":"33333333333333333333333333333333"}
        }"#[..];
        let maps = &br#"{
            "02A.0CE":{"CelebrationVariants":[{"Virtual01C":"237C.01C","MapInfo":{"GUID":"044.0CE"}}]},
            "01.0CE":{"CelebrationVariants":[{"Virtual01C":"237C.01C","MapInfo":{"GUID":"045.0CE"}}]},
            "03.0CE":{"CelebrationVariants":null},
            "04.0CE":{}
        }"#[..];
        Celebrations::from_readers(keys, maps, &templates).unwrap()
    }

    fn tracer_rewards() -> ItemDb {
        let mut items = ItemDb::default();
        for guid in TRACER_REWARDS {
            items.unlocks.insert(
                guid,
                Unlock {
                    guid,
                    name: String::new(),
                    item_type: String::new(),
                    rarity: String::new(),
                    hero: None,
                    source: String::new(),
                    box_type: None,
                    categories: Vec::new(),
                    available_in: String::new(),
                    skin_theme: 0,
                    esports_team: String::new(),
                },
            );
        }
        items
    }

    fn as_array(value: &Value) -> &[Value] {
        let Array(values) = value else {
            panic!("expected an array")
        };
        values
    }

    fn as_struct(value: &Value) -> &Fields {
        let Struct(fields) = value else {
            panic!("expected a struct")
        };
        fields
    }

    #[test]
    fn prioritizes_event_then_challenge_keys_before_numeric_catalog_order() {
        let celebrations = fixture();
        let now = server_time("2022-09-16", 0).unwrap();
        let profile = Profile {
            events: names(&[" LUNAR ", "lunar"]),
            challenge: TRACER.into(),
            ..Profile::default()
        };
        let items = tracer_rewards();
        let [(_, _, keys), ..] = celebrations.login_messages(&profile, &items, now).unwrap();
        assert_eq!(
            keys[&0x78],
            Array([9, 0xa1, 0xb2, 0xc3].map(Unsigned).to_vec())
        );
        assert_eq!(
            keys[&0x90],
            Array(
                [9, 0xa1, 0xb2, 0xc3]
                    .into_iter()
                    .flat_map(|byte| std::iter::repeat_n(Unsigned(byte), KEY_LENGTH))
                    .collect()
            )
        );
        assert_eq!(keys.len(), 2);

        let empty = Profile {
            events: Vec::new(),
            challenge: String::new(),
            ..Profile::default()
        };
        let [(_, _, fallback), ..] = celebrations.login_messages(&empty, &items, 0).unwrap();
        assert_eq!(
            fallback[&0x78],
            Array([9, 0xa1, 0xc3, 0xb2].map(Unsigned).to_vec())
        );
        assert_eq!(as_array(&fallback[&0x90])[KEY_LENGTH], Unsigned(0x11));
    }

    #[test]
    fn preserves_event_order_map_swaps_and_challenge_tiers() {
        let celebrations = fixture();
        let now = server_time("2022-09-16", 0).unwrap();
        let profile = Profile {
            events: names(&[
                "lunar",
                "0x105",
                "LUNAR",
                "owl",
                "0x125",
                "ow2_credits",
                "0x123",
                "halloween",
                "unknown",
            ]),
            challenge: TRACER.into(),
            challenge_wins: 17,
            ..Profile::default()
        };
        let mut items = tracer_rewards();
        let [_, (_, _, records), (_, _, progress)] =
            celebrations.login_messages(&profile, &items, now).unwrap();
        let records = as_array(&records[&0x78]);
        assert_eq!(
            records
                .iter()
                .map(|record| as_struct(record)[&0x40].clone())
                .collect::<Vec<_>>(),
            [0x105, 0x105, 0x125, 0x123, 0x123, 0xfb, 0x119]
                .map(|id| Unsigned(CELEBRATION_BASE | id))
        );
        let lunar = as_struct(&records[0]);
        assert_eq!(lunar[&0x30], Unsigned(0xa1));
        assert_eq!(lunar[&0x38], Unsigned(RESOURCE_KEY_BASE | 0x188));
        assert_eq!(lunar[&0x70], Unsigned(CELEBRATION_TYPE_BASE | 0x237c));
        assert_eq!(lunar[&0x48], date(now - 2 * DAY).unwrap());
        assert_eq!(lunar[&0x50], date(now + 365 * DAY).unwrap());
        assert_eq!(lunar[&0xb0], Signed(4));
        assert_eq!(
            lunar[&0],
            Array(
                [(0x2a, 0x44), (1, 0x45)]
                    .map(|(base, variant)| Struct(Fields::from([
                        (0, Unsigned(MAP_BASE | base)),
                        (8, Unsigned(MAP_BASE | variant)),
                    ])))
                    .to_vec()
            )
        );
        assert_eq!(as_struct(&records[1])[&0x30], Unsigned(0));
        assert_eq!(
            as_struct(&records[5])[&0x38],
            Unsigned(RESOURCE_KEY_BASE | 0x181)
        );
        let challenge = as_struct(&records[6]);
        assert_eq!(challenge[&0x78], Unsigned(CHALLENGE_WINS_STAT));
        assert_eq!(
            challenge[&0x18],
            Array(
                TRACER_REWARDS
                    .into_iter()
                    .zip([9, 18, 27])
                    .map(|(guid, wins)| Struct(Fields::from([
                        (0, Array(vec![Unsigned(guid)])),
                        (0x18, Unsigned(wins)),
                    ])))
                    .collect()
            )
        );
        assert_eq!(
            progress[&0x78],
            Array(vec![Struct(Fields::from([
                (0, Unsigned(CELEBRATION_BASE | 0x119)),
                (8, Float64(17.0)),
            ]))])
        );

        items.unlocks.swap_remove(&TRACER_REWARDS[2]);
        let [_, (_, _, records), (_, _, progress)] =
            celebrations.login_messages(&profile, &items, now).unwrap();
        assert_eq!(as_array(&records[&0x78]).len(), 6);
        assert!(as_array(&progress[&0x78]).is_empty());
    }

    #[test]
    fn rejects_malformed_keys_without_echoing_input() {
        let invalid = &br#"{"1":{"name":"1","key":"fictional malformed private input"}}"#[..];
        let error = read_keys(invalid).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(error.to_string(), "invalid external resource-key catalog");
        let wrong_type = &br#"{"1":{"name":17,"key":"fictional malformed private input"}}"#[..];
        assert_eq!(
            read_keys(wrong_type).err().unwrap().to_string(),
            error.to_string()
        );
    }

    #[test]
    #[ignore = "requires the external 104319 resource catalog and lobby templates"]
    fn login_messages_match_python_for_events_challenges_and_content_key_order() {
        let data = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap()).join("data");
        let templates = RetailTemplates::load(data.join("retail_templates.json")).unwrap();
        let celebrations = Celebrations::load(&data, &templates).unwrap();
        let items = ItemDb::load(&data).unwrap();
        let schemas = Schemas::load(data.join("schemas_174.json")).unwrap();
        let empty = Profile {
            events: Vec::new(),
            server_date: "2022-09-16".into(),
            ..Profile::default()
        };
        let all = Profile {
            events: names(&[
                " LUNAR ",
                "anniversary",
                "LUNAR",
                "0x105",
                "halloween",
                "winter",
                "anniversary_remix_1",
                "anniversary_remix_2",
                "summer",
                "archives",
                "reaper",
                "cassidy",
                "malevento",
                "owl",
                "watchpoint",
                "ow2_credits",
                "0x123",
                "goodbye",
                "unknown",
            ]),
            challenge: TRACER.into(),
            challenge_wins: 17,
            ..empty.clone()
        };
        let kana = Profile {
            events: names(&["owl", "0x125", "0x10000", "winter"]),
            challenge: "Kanezaka Challenge".into(),
            challenge_wins: 27,
            server_date: "2020-02-29".into(),
            ..empty.clone()
        };
        for (profile, lengths, expected) in [
            (
                empty,
                [4112, 4, 4],
                "0994d10d4eb80c4d575a303c262b441a66b4687f9e045ab8272d89f15fe4c59b",
            ),
            (
                all,
                [4112, 1742, 20],
                "bb44f0050624baca671315e0b92ebfc9914dbf86ec8cb9c90435223630d50088",
            ),
            (
                kana,
                [4112, 448, 20],
                "a744b4c7df0336d9995ed3f86b6b6c74fb028eb57bc4e4734a3df41f2b1990b4",
            ),
        ] {
            let now = server_time(&profile.server_date, 0).unwrap();
            let mut digest = Sha256::new();
            for ((crc, id, fields), length) in celebrations
                .login_messages(&profile, &items, now)
                .unwrap()
                .into_iter()
                .zip(lengths)
            {
                let body = schemas.encode(crc, id, &fields).unwrap();
                assert_eq!(body.len(), length);
                digest.update((length as u32).to_le_bytes());
                digest.update(body);
            }
            assert_eq!(format!("{:x}", digest.finalize()), expected);
        }
    }
}
