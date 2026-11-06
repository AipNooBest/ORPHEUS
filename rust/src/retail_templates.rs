//! Ordered retail lobby templates and the per-account changes sent at login.
//!
//! The 1.68 template corpus, decoded against 1.74 schemas, remains an external
//! runtime input. This module ports the existing lobby replay and data format.

use std::{
    collections::{BTreeMap, HashSet},
    fs::File,
    io::{self, BufReader, Read},
    path::Path,
};

use serde::Deserialize;
use serde_json::{Map, Value as Json};

use crate::{
    accounts::{Profile, battle_tag},
    catalog::localize_fields,
    jam_codec::{Fields, Value},
    lobby_arcade::ARCADE,
    lobby_login::Identity,
    lobby_passes::PASSES,
    lobby_transport::LobbyMessage,
};

pub const RETAIL_ACCOUNT: u64 = 0x425a_e13f;
pub const REDACTED_NAME: &str = "XXXXXXXXXXXXXXX";
pub const CLIENT_BUILD: u32 = 104319;
pub const SUPPORTED_BUILDS_KEY: u64 = 0x0422_7e56;
pub const MENU_HERO_KEY: u64 = 0x8705_3c32;

const TEMPLATE_FORMAT: u32 = 1;
pub const CONFIG: u32 = 0x1732_ebf2;
pub const IN_CONNECT: u32 = 0xf5ff_548a;
pub const LOBBY: u32 = 0x1a98_79a4;
const CUSTOM_GAMES: u32 = 0x2128_6b49;
const FRIENDS: u32 = 0xf470_e2ab;
const OWL_LIVE: u32 = 0x713d_7589;
const REPLAYS: u32 = 0x5894_d085;
const STORE: u32 = 0x4bad_7a7e;

// Retain capture order, including groups whose protocol has no descriptive name.
pub const AT_LOGIN: [(u32, u32); 34] = [
    (LOBBY, 20814),
    (LOBBY, 20806),
    (LOBBY, 20812),
    (LOBBY, 20813),
    (LOBBY, 20821),
    (PASSES, 58501),
    (0xaa91_be18, 40900),
    (0xaa91_be18, 40914),
    (OWL_LIVE, 43200),
    (ARCADE, 39800),
    (ARCADE, 39801),
    (ARCADE, 39802),
    (ARCADE, 39803),
    (ARCADE, 39810),
    (ARCADE, 39806),
    (ARCADE, 39807),
    (ARCADE, 39804),
    (0x267f_de9e, 28001),
    (0x0dfe_efef, 38300),
    (CONFIG, 36600),
    (CONFIG, 36603),
    (CONFIG, 36602),
    (REPLAYS, 51804),
    (REPLAYS, 51807),
    (REPLAYS, 51810),
    (REPLAYS, 51817),
    (REPLAYS, 51812),
    (0x34bb_385d, 43301),
    (STORE, 26404),
    (STORE, 26400),
    (FRIENDS, 27104),
    (LOBBY, 20805),
    (LOBBY, 20820),
    (CUSTOM_GAMES, 23301),
];

#[derive(Debug)]
pub struct RetailTemplates {
    messages: Vec<LobbyMessage>,
}

#[derive(Deserialize)]
struct TemplateFile {
    format: u32,
    server: Vec<(String, u32, Map<String, Json>)>,
}

impl RetailTemplates {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = path.as_ref();
        let file = File::open(path)?;
        Self::from_reader(BufReader::new(file))
            .map_err(|error| io::Error::new(error.kind(), format!("{}: {error}", path.display())))
    }

    pub fn from_reader(reader: impl Read) -> io::Result<Self> {
        let file: TemplateFile = serde_json::from_reader(reader).map_err(|error| {
            io::Error::new(
                error.io_error_kind().unwrap_or(io::ErrorKind::InvalidData),
                error,
            )
        })?;
        if file.format != TEMPLATE_FORMAT {
            return Err(invalid("unsupported retail template format"));
        }
        let messages = file
            .server
            .into_iter()
            .map(|(crc, id, value)| {
                let text = crc.trim();
                let text = text
                    .strip_prefix("0x")
                    .or_else(|| text.strip_prefix("0X"))
                    .unwrap_or(text);
                let crc = u32::from_str_radix(text, 16)
                    .map_err(|_| invalid(format!("invalid retail template CRC {crc:?}")))?;
                let fields = restore_fields(value)?;
                Ok((crc, id, fields))
            })
            .collect::<io::Result<_>>()?;
        Ok(Self { messages })
    }

    pub fn messages(&self) -> &[LobbyMessage] {
        &self.messages
    }

    /// Return an independent copy of the first matching template.
    pub fn first(&self, crc: u32, id: u32) -> Option<Fields> {
        self.messages
            .iter()
            .find(|(group, message, _)| *group == crc && *message == id)
            .map(|(_, _, fields)| fields.clone())
    }

    /// Return matching templates in their original order.
    pub fn all(&self, crc: u32, id: Option<u32>) -> Vec<(u32, Fields)> {
        self.messages
            .iter()
            .filter(|(group, message, _)| *group == crc && id.is_none_or(|id| id == *message))
            .map(|(_, message, fields)| (*message, fields.clone()))
            .collect()
    }

    /// Replay the static login messages. A built message replaces all recorded
    /// messages of its group and ID.
    pub fn at_login(
        &self,
        profile: &Profile,
        identity: Identity,
        menu_hero: Option<u64>,
        now: u64,
        built: &BTreeMap<(u32, u32), Fields>,
    ) -> io::Result<Vec<LobbyMessage>> {
        let tag = battle_tag(&profile.player_name, identity.account_lo);
        let mut messages = Vec::new();
        for (crc, id) in AT_LOGIN {
            if let Some(fields) = built.get(&(crc, id)) {
                messages.push((crc, id, fields.clone()));
                continue;
            }
            for (_, mut fields) in self.all(crc, Some(id)) {
                personalize_fields(&mut fields, &tag, identity.account_lo);
                let mut fields = localize_fields(&fields, &profile.region);
                refresh(crc, id, &mut fields, now, menu_hero)?;
                messages.push((crc, id, fields));
            }
        }
        Ok(messages)
    }

    pub fn menu_config(
        &self,
        profile: &Profile,
        identity: Identity,
        menu_hero: Option<u64>,
    ) -> io::Result<Fields> {
        let mut fields = self
            .first(CONFIG, 36600)
            .ok_or_else(|| invalid("missing menu config template"))?;
        let tag = battle_tag(&profile.player_name, identity.account_lo);
        personalize_fields(&mut fields, &tag, identity.account_lo);
        let mut fields = localize_fields(&fields, &profile.region);
        refresh_config(&mut fields, menu_hero)?;
        Ok(fields)
    }

    pub fn preload(&self, extra_skins: &[u64]) -> io::Result<Fields> {
        let mut fields = self
            .first(IN_CONNECT, 20505)
            .ok_or_else(|| invalid("missing preload template"))?;
        let Some(Value::Array(skins)) = fields.get_mut(&0x90) else {
            return Err(invalid("preload template lacks its skin list"));
        };
        let mut listed: HashSet<_> = skins.iter().filter_map(unsigned).collect();
        for &skin in extra_skins {
            if listed.insert(skin) {
                skins.push(Value::Unsigned(skin));
            }
        }
        Ok(fields)
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

pub(crate) fn restore_fields(map: Map<String, Json>) -> io::Result<Fields> {
    map.into_iter()
        .map(|(key, value)| {
            let offset = key
                .strip_prefix("+0x")
                .and_then(|text| u32::from_str_radix(text, 16).ok())
                .ok_or_else(|| invalid(format!("invalid retail template field offset {key:?}")))?;
            Ok((offset, restore_value(value)?))
        })
        .collect()
}

pub(crate) fn restore_value(value: Json) -> io::Result<Value> {
    match value {
        Json::Object(mut map) if map.len() == 1 && map.contains_key("$bytes") => {
            let Some(Json::String(hex)) = map.remove("$bytes") else {
                return Err(invalid("retail template blob must be hexadecimal text"));
            };
            Ok(Value::Blob(decode_blob(&hex)?))
        }
        Json::Object(map) => Ok(Value::Struct(restore_fields(map)?)),
        Json::Array(values) => Ok(Value::Array(
            values
                .into_iter()
                .map(restore_value)
                .collect::<io::Result<_>>()?,
        )),
        Json::Bool(value) => Ok(Value::Bool(value)),
        Json::String(value) => Ok(Value::String(value)),
        Json::Number(value) => {
            if let Some(value) = value.as_u64() {
                Ok(Value::Unsigned(value))
            } else if let Some(value) = value.as_i64() {
                Ok(Value::Signed(value))
            } else {
                value
                    .as_f64()
                    .map(Value::Float64)
                    .ok_or_else(|| invalid("invalid template number"))
            }
        }
        Json::Null => Err(invalid("unexpected null in a retail template field")),
    }
}

fn decode_blob(hex: &str) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::with_capacity(hex.len() / 2);
    let malformed = || invalid("invalid hex in a retail template blob");
    for part in hex.split_ascii_whitespace() {
        let pairs = part.as_bytes().chunks_exact(2);
        if !pairs.remainder().is_empty() {
            return Err(malformed());
        }
        for pair in pairs {
            let high = char::from(pair[0]).to_digit(16).ok_or_else(malformed)?;
            let low = char::from(pair[1]).to_digit(16).ok_or_else(malformed)?;
            bytes.push((high * 16 + low) as u8);
        }
    }
    Ok(bytes)
}

/// Replace captured account fields, strings and embedded presence BattleTags.
pub fn personalize_fields(fields: &mut Fields, tag: &str, account_lo: u32) {
    for value in fields.values_mut() {
        personalize(value, tag, u64::from(account_lo));
    }
}

fn personalize(value: &mut Value, tag: &str, account_lo: u64) {
    match value {
        Value::Struct(fields) => {
            for value in fields.values_mut() {
                personalize(value, tag, account_lo);
            }
        }
        Value::Array(items) => {
            for item in items {
                personalize(item, tag, account_lo);
            }
        }
        Value::String(text) if text.starts_with(REDACTED_NAME) => *text = tag.into(),
        Value::Blob(blob) => personalize_blob(blob, tag.as_bytes()),
        _ if unsigned(value) == Some(RETAIL_ACCOUNT) => *value = Value::Unsigned(account_lo),
        _ => (),
    }
}

fn personalize_blob(blob: &mut Vec<u8>, tag: &[u8]) {
    let marker = REDACTED_NAME.as_bytes();
    let Some(start) = blob.windows(marker.len()).position(|text| text == marker) else {
        return;
    };
    // Presence stores this string with a one-byte protobuf length. Match the
    // established behavior of retaining only the first 127 bytes of the tag.
    if start < 2 || blob[start - 1] >= 0x80 {
        return;
    }
    let end = (start + usize::from(blob[start - 1])).min(blob.len());
    let tag = &tag[..tag.len().min(0x7f)];
    blob.splice(
        start - 1..end,
        std::iter::once(tag.len() as u8).chain(tag.iter().copied()),
    );
}

fn unsigned(value: &Value) -> Option<u64> {
    match value {
        Value::Unsigned(value) => Some(*value),
        Value::Signed(value) => u64::try_from(*value).ok(),
        _ => None,
    }
}

fn refresh(
    crc: u32,
    id: u32,
    fields: &mut Fields,
    now: u64,
    menu_hero: Option<u64>,
) -> io::Result<()> {
    match (crc, id) {
        (CONFIG, 36602) => {
            fields.insert(0x78, Value::Unsigned(now));
        }
        (CONFIG, 36600) => refresh_config(fields, menu_hero)?,
        _ => (),
    }
    Ok(())
}

fn refresh_config(fields: &mut Fields, menu_hero: Option<u64>) -> io::Result<()> {
    let Some(Value::Array(entries)) = fields.get_mut(&0x78) else {
        return Err(invalid("menu config template lacks its config entries"));
    };
    let build = CLIENT_BUILD.to_string();
    for entry in entries.iter_mut() {
        let Value::Struct(entry) = entry else {
            return Err(invalid("menu config template entry is not a struct"));
        };
        if entry.get(&0x0).and_then(unsigned) != Some(SUPPORTED_BUILDS_KEY) {
            continue;
        }
        let Some(Value::String(text)) = entry.get_mut(&0x8) else {
            return Err(invalid("menu config build entry lacks its text list"));
        };
        if !text.contains(&build) {
            text.truncate(text.trim_end_matches(']').len());
            text.push(',');
            text.push_str(&build);
            text.push(']');
        }
    }
    if let Some(menu_hero) = menu_hero {
        entries.push(Value::Struct(Fields::from([
            (0x0, Value::Unsigned(MENU_HERO_KEY)),
            (0x8, Value::String(format!("0x{menu_hero:016x}"))),
        ])));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jam_codec::Schemas;
    use sha2::{Digest, Sha256};

    fn fixtures() -> RetailTemplates {
        RetailTemplates::from_reader(
            br#"{"format":1,"description":"synthetic","server":[
            ["1A9879A4",20814,{"+0x78":"XXXXXXXXXXXXXXX#0000","+0x88":1113252159}],
            ["1A9879A4",20814,{"+0x78":{"$bytes":"0A 11 5858585858585858585858585858582378 1005"}}],
            ["F5FF548A",20505,{"+0x90":[42,43,42]}],
            ["1732EBF2",36602,{"+0x78":1}],
            ["1732EBF2",36600,{"+0x78":[{"+0x0":69369430,"+0x8":"[1,2]"}]}],
            ["1A9879A4",20812,{"+0x78":[3,4]}]
            ]}"#
            .as_slice(),
        )
        .unwrap()
    }

    #[test]
    fn reads_templates_in_order_and_restores_nested_blob_values() {
        let templates = fixtures();
        let rows = templates.all(LOBBY, None);
        assert_eq!(
            rows.iter().map(|(id, _)| *id).collect::<Vec<_>>(),
            [20814, 20814, 20812]
        );
        assert_eq!(templates.first(LOBBY, 20814), Some(rows[0].1.clone()));
        assert_eq!(
            rows[1].1[&0x78],
            Value::Blob(b"\x0a\x11XXXXXXXXXXXXXXX#x\x10\x05".to_vec())
        );
        assert!(templates.first(LOBBY, 42).is_none());
    }

    #[test]
    fn patches_identity_build_time_and_replaces_built_messages() {
        let templates = fixtures();
        let profile = Profile {
            player_name: "Local".into(),
            ..Profile::default()
        };
        let identity = Identity::for_account(42);
        let replacement = Fields::from([(0x78, Value::Bool(true))]);
        let messages = templates
            .at_login(
                &profile,
                identity,
                Some(0x02e0_0000_0000_0002),
                2,
                &BTreeMap::from([((LOBBY, 20812), replacement.clone())]),
            )
            .unwrap();

        assert_eq!(
            messages.iter().map(|(_, id, _)| *id).collect::<Vec<_>>(),
            [20814, 20814, 20812, 36600, 36602]
        );
        assert_eq!(messages[0].2[&0x78], Value::String("Local#1042".into()));
        assert_eq!(messages[0].2[&0x88], Value::Unsigned(42));
        assert_eq!(
            messages[1].2[&0x78],
            Value::Blob(b"\x0a\x0aLocal#1042\x10\x05".to_vec())
        );
        assert_eq!(messages[2].2, replacement);
        assert_eq!(messages[4].2[&0x78], Value::Unsigned(2));
        assert_eq!(
            templates
                .menu_config(&profile, identity, Some(0x02e0_0000_0000_0002))
                .unwrap(),
            messages[3].2
        );
        let Value::Array(entries) = &messages[3].2[&0x78] else {
            panic!("expected config entries")
        };
        let Value::Struct(build) = &entries[0] else {
            panic!("expected build entry")
        };
        let Value::Struct(hero) = &entries[1] else {
            panic!("expected menu hero entry")
        };
        assert_eq!(build[&0x8], Value::String("[1,2,104319]".into()));
        assert_eq!(hero[&0x8], Value::String("0x02e0000000000002".into()));
        assert_eq!(
            templates.first(LOBBY, 20814).unwrap()[&0x88],
            Value::Unsigned(RETAIL_ACCOUNT)
        );
    }

    #[test]
    fn preload_appends_only_unlisted_skins_in_input_order() {
        let templates = fixtures();
        let preload = templates.preload(&[43, 90, 90, 7]).unwrap();
        assert_eq!(
            preload[&0x90],
            Value::Array([42, 43, 42, 90, 7].map(Value::Unsigned).to_vec())
        );
        assert_eq!(
            templates.preload(&[]).unwrap()[&0x90],
            Value::Array([42, 43, 42].map(Value::Unsigned).to_vec())
        );
    }

    #[test]
    fn rejects_unsupported_templates_and_invalid_blob_hex() {
        for data in [
            br#"{"format":2,"server":[]}"#.as_slice(),
            br#"{"format":1,"server":[["CRC",1,{}]]}"#.as_slice(),
            br#"{"format":1,"server":[["0",1,{"+0x0":{"$bytes":"0 A"}}]]}"#.as_slice(),
        ] {
            assert_eq!(
                RetailTemplates::from_reader(data).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
        }
    }

    #[test]
    #[ignore = "requires the separately prepared lobby template corpus"]
    fn real_templates_and_localized_login_match_python_wire_hashes() {
        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let data = root.join("data");
        let templates = RetailTemplates::load(data.join("retail_templates.json")).unwrap();
        let schemas = Schemas::load(data.join("schemas_174.json")).unwrap();
        let digest = |messages: &[LobbyMessage]| {
            let mut hash = Sha256::new();
            for (crc, id, fields) in messages {
                let body = schemas.encode(*crc, *id, fields).unwrap();
                hash.update(crc.to_le_bytes());
                hash.update(id.to_le_bytes());
                hash.update(u32::try_from(body.len()).unwrap().to_le_bytes());
                hash.update(body);
            }
            format!("{:x}", hash.finalize())
        };
        assert_eq!(templates.messages().len(), 70);
        assert_eq!(
            digest(templates.messages()),
            "a9fbb7a561ce18fc680ed98b47e2a0d61c0cd507533f799e84a6d39cf9a07121"
        );
        let profile = Profile {
            player_name: "Parity".into(),
            region: "EU".into(),
            ..Profile::default()
        };
        let login = templates
            .at_login(
                &profile,
                Identity::for_account(0x1234_5678),
                Some(0x02e0_0000_0000_0002),
                1_800_000_000,
                &BTreeMap::new(),
            )
            .unwrap();
        assert_eq!(login.len(), 39);
        assert_eq!(
            digest(&login),
            "3e1e6556203dc02579ae9c655879c6b7a711c1967146c579014e2a4e56f714f5"
        );
    }
}
