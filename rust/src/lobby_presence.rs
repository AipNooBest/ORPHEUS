//! Account and game-account presence for the lobby, personalized from external templates.

use std::{collections::HashSet, io};

use prost::encoding::{
    DecodeContext, WireType, decode_key, decode_varint, encode_key, encode_varint, skip_field,
};

use crate::{
    accounts::{Profile, battle_tag},
    jam_codec::{
        Fields,
        Value::{self, Array, Blob, Signed, Struct, Unsigned},
    },
    lobby_login::Identity,
    lobby_transport::LobbyMessage,
    retail_templates::{RETAIL_ACCOUNT, RetailTemplates, personalize_fields},
};

pub const FRIENDS: u32 = 0xf470_e2ab;
pub const STATUS_ONLINE: u8 = 1;
pub const STATUS_AWAY: u8 = 2;
pub const STATUS_BUSY: u8 = 3;
pub const STATUS_OFFLINE: u8 = 4;
const UPDATE: u32 = 27113;

const RETAIL_APP_ACCOUNT: u64 = 0x3a16_9def;
const RETAIL_PRO_ACCOUNT: u64 = 0x3a16_9e4c;
const RETAIL_FULL_NAME: &[u8] = b"ExampleUser";
const APP_ACCOUNT_BITS: u64 = 0x0a0a_0000;
const PRO_ACCOUNT_BITS: u64 = 0x0b0b_0000;

const PRESENCE_PROGRAM: u64 = 16_974;
const ACCOUNT_GROUP: u64 = 1;
const GAME_ACCOUNT_ONLINE: &[u8] = b"\x08\xce\x84\x01\x10\x02\x18\x01\x20\x00";
const BOOL_VARIANT: u8 = 0x10;
const INT_VARIANT: u8 = 0x18;
const MESSAGE_VARIANT: u8 = 0x3a;
const MICROSECONDS: u64 = 1_000_000;
const YEAR_2001: u64 = 978_307_200 * MICROSECONDS;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum View {
    Own,
    Friend,
}

pub struct Presence {
    templates: Vec<Fields>,
}

impl Presence {
    pub fn new(templates: &RetailTemplates) -> io::Result<Self> {
        let mut seen = HashSet::new();
        let mut records = Vec::new();
        for (_, mut message) in templates.all(FRIENDS, Some(UPDATE)) {
            let Some(Array(values)) = message.remove(&0x78) else {
                return Err(invalid("presence update has no record list"));
            };
            let Some(Struct(record)) = values.into_iter().next() else {
                return Err(invalid("presence update has no first record"));
            };
            let Some(Array(fields)) = record.get(&0x20) else {
                return Err(invalid("presence record has no field list"));
            };
            let id = record_id(&record)?;
            // The capture contains partial updates after the first full account record.
            if fields.len() > 3 && seen.insert(id) {
                records.push(record);
            }
        }
        Ok(Self { templates: records })
    }

    /// The account, app and game records visible to the caller. Seconds are
    /// passed in explicitly so a batch uses a single wall clock value.
    pub fn records(
        &self,
        profile: &Profile,
        account_lo: u32,
        status: u8,
        created: i64,
        now: u64,
        view: View,
    ) -> io::Result<Vec<Value>> {
        let timestamp = [profile.last_online, created]
            .into_iter()
            .find(|&time| time != 0)
            .map(u64::try_from)
            .transpose()
            .map_err(|_| invalid("presence timestamp precedes the Unix epoch"))?
            .unwrap_or(now)
            .checked_mul(MICROSECONDS)
            .ok_or_else(|| invalid("presence timestamp exceeds the wire range"))?;
        let account = u64::from(account_lo);
        let ids = [
            (RETAIL_ACCOUNT, account),
            (RETAIL_APP_ACCOUNT, account ^ APP_ACCOUNT_BITS),
            (RETAIL_PRO_ACCOUNT, account ^ PRO_ACCOUNT_BITS),
        ];
        let tag = battle_tag(&profile.player_name, account_lo);
        let online = view == View::Own || status != STATUS_OFFLINE;
        let flags = [
            (7, Some(status == STATUS_AWAY)),
            (11, Some(status == STATUS_BUSY)),
            (12, Some(view == View::Own && status == STATUS_OFFLINE)),
        ];
        let mut records = Vec::with_capacity(self.templates.len());
        for template in &self.templates {
            let original_id = record_id(template)?;
            let mut record = template.clone();
            personalize_fields(&mut record, &tag, account_lo);
            let Some(Struct(id)) = record.get_mut(&0) else {
                return Err(invalid("presence record has no account identifier"));
            };
            id.insert(0, Unsigned(replace_id(original_id, &ids)));
            let Some(Array(fields)) = record.get_mut(&0x20) else {
                return Err(invalid("presence record has no field list"));
            };
            personalize_record_fields(
                fields,
                &ids,
                &profile.player_name,
                timestamp,
                online,
                flags,
            )?;
            records.push(Struct(record));
        }
        Ok(records)
    }

    pub fn own(&self, profile: &Profile, identity: Identity, now: u64) -> io::Result<LobbyMessage> {
        let records = self.records(
            profile,
            identity.account_lo,
            STATUS_ONLINE,
            0,
            now,
            View::Own,
        )?;
        Ok((FRIENDS, UPDATE, Fields::from([(0x78, Array(records))])))
    }
}

type Ids = [(u64, u64); 3];
type Flags = [(u64, Option<bool>); 3];

fn record_id(record: &Fields) -> io::Result<u64> {
    let Some(Struct(id)) = record.get(&0) else {
        return Err(invalid("presence record has no account identifier"));
    };
    match id.get(&0) {
        Some(Unsigned(value)) => Ok(*value),
        Some(Signed(value)) => u64::try_from(*value).map_err(|_| invalid("negative presence ID")),
        _ => Err(invalid("presence record has no account ID")),
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn personalize_record_fields(
    fields: &mut Vec<Value>,
    ids: &Ids,
    name: &str,
    timestamp: u64,
    online: bool,
    mut flags: Flags,
) -> io::Result<()> {
    let mut is_account = false;
    for entry in fields.iter_mut() {
        let Struct(field) = entry else {
            return Err(invalid("presence field is not a record"));
        };
        let Some(Blob(key)) = field.get_mut(&8) else {
            return Err(invalid("presence field is missing its key"));
        };
        rewrite_ids(key, ids);
        let is_game_online = key.as_slice() == GAME_ACCOUNT_ONLINE;
        let (group, id) = key_group_field(key);
        let Some(Blob(value)) = field.get_mut(&0x30) else {
            return Err(invalid("presence field is missing its value"));
        };
        rewrite_ids(value, ids);
        replace_short_string(value, RETAIL_FULL_NAME, name.as_bytes());
        replace_timestamp(value, timestamp);
        if !online && is_game_online {
            *value = bool_variant(false);
        }
        if group == Some(ACCOUNT_GROUP) {
            is_account = true;
            if let Some((_, setting)) = flags.iter_mut().find(|(field, _)| Some(*field) == id) {
                if let Some(enabled) = setting.take() {
                    *value = bool_variant(enabled);
                }
            }
        }
    }
    if is_account {
        for (field, setting) in flags {
            if let Some(enabled) = setting {
                fields.push(Struct(Fields::from([
                    (8, Blob(field_key(ACCOUNT_GROUP, field))),
                    (0x30, Blob(bool_variant(enabled))),
                ])));
            }
        }
    }
    Ok(())
}

fn bool_variant(value: bool) -> Vec<u8> {
    vec![BOOL_VARIANT, u8::from(value)]
}

fn field_key(group: u64, field: u64) -> Vec<u8> {
    let mut key = Vec::new();
    for (number, value) in [(1, PRESENCE_PROGRAM), (2, group), (3, field), (4, 0)] {
        encode_key(number, WireType::Varint, &mut key);
        encode_varint(value, &mut key);
    }
    key
}

fn key_group_field(mut blob: &[u8]) -> (Option<u64>, Option<u64>) {
    let mut group = None;
    let mut field = None;
    while !blob.is_empty() {
        let Ok((number, wire)) = decode_key(&mut blob) else {
            break;
        };
        if wire == WireType::Varint {
            let Ok(value) = decode_varint(&mut blob) else {
                break;
            };
            match number {
                2 => group = Some(value),
                3 => field = Some(value),
                _ => (),
            }
        } else if !skip_non_varint(wire, number, &mut blob) {
            break;
        }
    }
    (group, field)
}

fn skip_non_varint(wire: WireType, number: u32, blob: &mut &[u8]) -> bool {
    matches!(
        wire,
        WireType::LengthDelimited | WireType::SixtyFourBit | WireType::ThirtyTwoBit
    ) && skip_field(wire, number, blob, DecodeContext::default()).is_ok()
}

fn replace_timestamp(value: &mut Vec<u8>, timestamp: u64) {
    let Some(mut body) = value.strip_prefix(&[INT_VARIANT]) else {
        return;
    };
    let Ok(old) = decode_varint(&mut body) else {
        return;
    };
    if body.is_empty() && old >= YEAR_2001 {
        value.truncate(1);
        encode_varint(timestamp, value);
    }
}

fn replace_id(value: u64, ids: &Ids) -> u64 {
    ids.iter()
        .find_map(|&(old, new)| (old == value).then_some(new))
        .unwrap_or(value)
}

fn rewrite_ids(blob: &mut Vec<u8>, ids: &Ids) {
    for &(old, new) in ids {
        let old_bytes = old.to_le_bytes();
        let new_bytes = new.to_le_bytes();
        let mut tail = blob.as_mut_slice();
        while let Some(index) = tail
            .windows(old_bytes.len())
            .position(|bytes| bytes == old_bytes)
        {
            tail[index..index + old_bytes.len()].copy_from_slice(&new_bytes);
            tail = &mut tail[index + old_bytes.len()..];
        }
        replace_short_string(
            blob,
            format!("{old}#1").as_bytes(),
            format!("{new}#1").as_bytes(),
        );
    }
    if let Some(updated) = rewrite_variant_message(blob, ids) {
        *blob = updated;
    }
}

fn replace_short_string(blob: &mut Vec<u8>, old: &[u8], new: &[u8]) {
    let Some(index) = blob.windows(old.len()).position(|bytes| bytes == old) else {
        return;
    };
    if index < 2 || usize::from(blob[index - 1]) != old.len() {
        return;
    }
    let mut replacement = Vec::new();
    encode_varint(new.len() as u64, &mut replacement);
    replacement.extend_from_slice(new);
    blob.splice(index - 1..index + old.len(), replacement);
}

fn rewrite_variant_message(blob: &[u8], ids: &Ids) -> Option<Vec<u8>> {
    let mut body = blob.strip_prefix(&[MESSAGE_VARIANT])?;
    let size = decode_varint(&mut body).ok()?;
    if size != body.len() as u64 {
        return None;
    }
    let mut fields = Vec::new();
    let mut changed = false;
    while !body.is_empty() {
        let start = body;
        let (number, wire) = decode_key(&mut body).ok()?;
        if wire == WireType::Varint {
            let original = decode_varint(&mut body).ok()?;
            let value = replace_id(original, ids);
            changed |= value != original;
            encode_key(number, wire, &mut fields);
            encode_varint(value, &mut fields);
        } else if skip_non_varint(wire, number, &mut body) {
            fields.extend_from_slice(&start[..start.len() - body.len()]);
        } else {
            return None;
        }
    }
    if !changed {
        return None;
    }
    let mut message = vec![MESSAGE_VARIANT];
    encode_varint(fields.len() as u64, &mut message);
    message.extend(fields);
    Some(message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    use sha2::{Digest, Sha256};

    use crate::jam_codec::Schemas;

    const IDS: Ids = [(128, 2), (200, 3), (300, 4)];

    #[test]
    fn rewrites_fixed_width_strings_and_message_varints() {
        let mut fixed = vec![0x09];
        fixed.extend_from_slice(&128u64.to_le_bytes());
        rewrite_ids(&mut fixed, &IDS);
        assert_eq!(&fixed[1..], &2u64.to_le_bytes());

        let mut string = b"\x0a\x05128#1".to_vec();
        rewrite_ids(&mut string, &IDS);
        assert_eq!(string, b"\x0a\x032#1");

        let mut message = b"\x3a\x05\x08\x01\x18\x80\x01".to_vec();
        rewrite_ids(&mut message, &IDS);
        assert_eq!(message, b"\x3a\x04\x08\x01\x18\x02");

        let mut trailing = b"\x3a\x05\x08\x01\x18\x80\x01\x00".to_vec();
        rewrite_ids(&mut trailing, &IDS);
        assert_eq!(trailing, b"\x3a\x05\x08\x01\x18\x80\x01\x00");
    }

    #[test]
    fn own_account_flags_and_friend_game_account_status_have_distinct_fields() {
        let presence_field =
            |key, value| Struct(Fields::from([(8, Blob(key)), (0x30, Blob(value))]));
        let mut own = vec![presence_field(field_key(1, 12), bool_variant(false))];
        personalize_record_fields(
            &mut own,
            &IDS,
            "Local",
            1_700_000_000_000_000,
            true,
            [(7, Some(false)), (11, Some(false)), (12, Some(true))],
        )
        .unwrap();
        assert_eq!(
            own,
            [
                presence_field(field_key(1, 12), bool_variant(true)),
                presence_field(field_key(1, 7), bool_variant(false)),
                presence_field(field_key(1, 11), bool_variant(false)),
            ]
        );

        let mut friend = vec![presence_field(
            GAME_ACCOUNT_ONLINE.to_vec(),
            bool_variant(true),
        )];
        personalize_record_fields(
            &mut friend,
            &IDS,
            "Local",
            1_700_000_000_000_000,
            false,
            [(7, Some(false)), (11, Some(false)), (12, Some(false))],
        )
        .unwrap();
        assert_eq!(
            friend,
            [presence_field(
                GAME_ACCOUNT_ONLINE.to_vec(),
                bool_variant(false)
            )]
        );
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn own_and_friend_presence_match_python_wire_values() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let schemas = Schemas::load(runtime.join("schemas_174.json")).unwrap();
        let templates = RetailTemplates::load(runtime.join("retail_templates.json")).unwrap();
        let presence = Presence::new(&templates).unwrap();
        let profile = Profile {
            player_name: "Local Tester".into(),
            last_online: 1_700_000_000,
            ..Profile::default()
        };
        let never = Profile {
            last_online: 0,
            ..profile.clone()
        };
        let account = 0x1234_5678;
        let now = 1_700_020_000;
        let created = 1_690_000_000;
        let expected_online = "34a2b0939b236458b4a6fde8e2009719cb5e54494d0545639a7023a327938d9c";
        let (crc, id, own) = presence
            .own(&profile, Identity::for_account(account), now)
            .unwrap();
        assert_eq!((crc, id), (FRIENDS, UPDATE));
        let wire = schemas.encode(crc, id, &own).unwrap();
        assert_eq!(format!("{:x}", Sha256::digest(wire)), expected_online);

        // Independent Python Presence.records vectors for both views and all time fallbacks.
        for (profile, status, created, view, expected) in [
            (&profile, STATUS_ONLINE, created, View::Own, expected_online),
            (
                &profile,
                STATUS_AWAY,
                created,
                View::Own,
                "e9ed876b46b90b5277a42648f0fb476756475711fbb4b83b2183e23afc7ae099",
            ),
            (
                &profile,
                STATUS_BUSY,
                created,
                View::Friend,
                "7837f25c7de62b5bfbf110d08f83f5707bc8d53902a0ed96ccdf1a99519e7085",
            ),
            (
                &profile,
                STATUS_OFFLINE,
                created,
                View::Own,
                "c171c5d849ea586de7a5423d4f1805823f22f05298e2a59812183b428d0fb581",
            ),
            (
                &profile,
                STATUS_OFFLINE,
                created,
                View::Friend,
                "e0351ad021be99d04757abb038603671ad8d67e54b2672523716bdd3a3ac3b79",
            ),
            (
                &never,
                STATUS_OFFLINE,
                created,
                View::Friend,
                "eda7c96b352f678e8ca6813903084fb9e72017c4c2e0c803f08fd991fb2e4fc6",
            ),
            (
                &never,
                STATUS_ONLINE,
                0,
                View::Own,
                "ffbc1c0e8e9c8093c104ed1664674df00830fc69f39b2011e1122b62f860335e",
            ),
        ] {
            let records = presence
                .records(profile, account, status, created, now, view)
                .unwrap();
            assert_eq!(records.len(), 3);
            let wire = schemas
                .encode(FRIENDS, UPDATE, &Fields::from([(0x78, Array(records))]))
                .unwrap();
            assert_eq!(format!("{:x}", Sha256::digest(wire)), expected);
        }
    }
}
