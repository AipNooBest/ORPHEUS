//! ClientOutInstance game messages and the known hero-select parameter layouts.

use crate::{
    game_heroes::{Hero, HeroCatalog},
    game_messages::Message,
    jam_codec::Value,
};

pub const CLIENT_GAME_MESSAGE: u32 = 21616;
pub const PICK_HERO: u64 = 0x0240_0000_0000_1361;
pub const OPEN_OR_CLOSE: u64 = 0x0240_0000_0000_1360;
const HERO_ASSET_TYPE: u16 = 0x02e0;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClientGameMessage<'a> {
    pub entity: u32,
    pub guid: u64,
    pub instance: i16,
    pub parameters: Parameters<'a>,
}

impl<'a> ClientGameMessage<'a> {
    /// Borrow the typed 21616 envelope from an already decoded game message.
    pub fn read(message: &'a Message) -> Option<Self> {
        if message.id != CLIENT_GAME_MESSAGE {
            return None;
        }
        let fields = &message.fields;
        let Value::Struct(entity) = fields.get(&0x78)? else {
            return None;
        };
        let Value::Unsigned(entity) = *entity.get(&0)? else {
            return None;
        };
        let Value::Blob(parameters) = fields.get(&0x80)? else {
            return None;
        };
        let Value::Unsigned(guid) = *fields.get(&0xa8)? else {
            return None;
        };
        let Value::Signed(instance) = *fields.get(&0xb0)? else {
            return None;
        };
        Some(Self {
            entity: u32::try_from(entity).ok()?,
            guid,
            instance: i16::try_from(instance).ok()?,
            parameters: Parameters::new(parameters),
        })
    }

    pub fn hero_select<'catalog>(
        self,
        heroes: &'catalog HeroCatalog,
    ) -> Option<HeroSelectRequest<'catalog>> {
        match self.guid {
            PICK_HERO => Some(HeroSelectRequest::Pick(
                self.parameters
                    .find_asset(HERO_ASSET_TYPE, |guid| heroes.get(guid)),
            )),
            OPEN_OR_CLOSE => Some(HeroSelectRequest::OpenOrClose(
                self.parameters.bool_before_entities(),
            )),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HeroSelectRequest<'a> {
    /// A recognized pick without a known hero has no gameplay effect.
    Pick(Option<&'a Hero>),
    /// Without a readable bool, the client request toggles the current screen.
    OpenOrClose(Option<bool>),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Parameters<'a>(&'a [u8]);

impl<'a> Parameters<'a> {
    pub fn new(bytes: &'a [u8]) -> Self {
        Self(bytes)
    }

    pub fn as_bytes(self) -> &'a [u8] {
        self.0
    }

    /// Split the two little-endian entity words found at the end of the
    /// observed parameter blobs. Their meaning depends on the message.
    pub fn split_entity_tail(self) -> Option<(&'a [u8], [u32; 2])> {
        let (body, tail) = self.0.split_last_chunk::<8>()?;
        let &[a, b, c, d, e, f, g, h] = tail;
        Some((
            body,
            [
                u32::from_le_bytes([a, b, c, d]),
                u32::from_le_bytes([e, f, g, h]),
            ],
        ))
    }

    pub fn bool_before_entities(self) -> Option<bool> {
        let (body, _) = self.split_entity_tail()?;
        match body {
            [.., 1, 0] => Some(false),
            [.., 1, 1] => Some(true),
            _ => None,
        }
    }

    /// Find the first known unaligned asset of this type. This matches the
    /// reference pick reader without assuming the rest of the blob grammar.
    pub fn find_asset<T>(
        self,
        asset_type: u16,
        mut known: impl FnMut(u64) -> Option<T>,
    ) -> Option<T> {
        self.0.windows(8).find_map(|bytes| {
            let guid = u64::from_le_bytes(bytes.try_into().expect("eight-byte window"));
            if guid >> 48 == u64::from(asset_type) {
                known(guid)
            } else {
                None
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    use crate::{
        bits::BitReader,
        game_heroes::SOLDIER,
        game_messages::read_channel,
        jam_codec::{Fields, Schemas},
    };

    #[test]
    fn envelope_uses_the_client_message_id_and_fixed_width_fields() {
        let mut message = Message {
            id: CLIENT_GAME_MESSAGE,
            fields: Fields::from([
                (
                    0x78,
                    Value::Struct(Fields::from([(0, Value::Unsigned(0xa000_0100))])),
                ),
                (0x80, Value::Blob(vec![1, 2])),
                (0xa8, Value::Unsigned(OPEN_OR_CLOSE)),
                (0xb0, Value::Signed(-1)),
            ]),
        };
        let decoded = ClientGameMessage::read(&message).unwrap();
        assert_eq!(decoded.entity, 0xa000_0100);
        assert_eq!(decoded.guid, OPEN_OR_CLOSE);
        assert_eq!(decoded.instance, -1);
        assert_eq!(decoded.parameters.as_bytes(), [1, 2]);
        message.id -= 1;
        assert!(ClientGameMessage::read(&message).is_none());
        message.id += 1;
        message.fields.insert(0xb0, Value::Signed(32_768));
        assert!(ClientGameMessage::read(&message).is_none());
    }

    #[test]
    fn pick_scans_unaligned_asset_words_and_skips_unknown_candidates() {
        let heroes = HeroCatalog::from_json(
            r#"{"0x02E000000000006E":{"name":"Local fixture","class":"Damage","body":"0x0800000000000001"}}"#,
        )
        .unwrap();
        let mut bytes = vec![0xfa, 0xb0, 0x01];
        bytes.extend_from_slice(&0x02e0_0000_ffff_ffffu64.to_le_bytes());
        let pick = ClientGameMessage {
            entity: 1,
            guid: PICK_HERO,
            instance: 3,
            parameters: Parameters::new(&[]),
        };
        let unknown = ClientGameMessage {
            parameters: Parameters::new(&bytes),
            ..pick
        };
        assert_eq!(
            unknown.hero_select(&heroes),
            Some(HeroSelectRequest::Pick(None))
        );
        bytes.extend_from_slice(&SOLDIER.to_le_bytes());
        let known = ClientGameMessage {
            parameters: Parameters::new(&bytes),
            ..pick
        };
        assert_eq!(
            known.hero_select(&heroes),
            Some(HeroSelectRequest::Pick(Some(heroes.get(SOLDIER).unwrap())))
        );
    }

    #[test]
    fn only_the_bool_tag_before_the_complete_entity_tail_sets_the_screen() {
        let mut bytes = vec![0x05, 0x01, 0x01];
        bytes.extend_from_slice(&0xa000_0100u32.to_le_bytes());
        bytes.extend_from_slice(&0xa000_0200u32.to_le_bytes());
        let parameters = Parameters::new(&bytes);
        assert_eq!(parameters.as_bytes(), bytes);
        assert_eq!(
            parameters.split_entity_tail(),
            Some((&bytes[..3], [0xa000_0100, 0xa000_0200]))
        );
        assert_eq!(parameters.bool_before_entities(), Some(true));
        bytes[2] = 0;
        assert_eq!(Parameters::new(&bytes).bool_before_entities(), Some(false));
        bytes[2] = 2;
        assert_eq!(Parameters::new(&bytes).bool_before_entities(), None);
        bytes[1] = 2;
        bytes[2] = 1;
        assert_eq!(Parameters::new(&bytes).bool_before_entities(), None);
        assert_eq!(Parameters::new(&bytes[..7]).split_entity_tail(), None);
        assert_eq!(Parameters::new(&bytes[3..]).bool_before_entities(), None);
    }

    #[test]
    #[ignore = "requires authorized 104319 game schemas and hero catalog in ORPHEUS_RUNTIME_DIR"]
    fn reliable_21616_messages_match_the_python_reader_and_hero_select_requests() {
        let data = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let schemas = Schemas::load(data.join("game_schemas_174.json")).unwrap();
        let heroes = HeroCatalog::load(data.join("game_heroes_174.json")).unwrap();
        // Python game.messages.write_channel with synthetic entity IDs and the
        // known pick/open parameter bytes from tests/test_game_statescript.py.
        let bytes = concat!(
            "0b00070000000110000100a01d0000000000000001c15d400a0c01ff046e0000000000e002000100a0000200a061130000000040020300",
            "080000000110000100a00e00000001c3a2050101000100a0000200a060130000000040020300",
            "090000000110000100a00e00000001c3a2050100000100a0000200a060130000000040020300",
            "0a0000000110000100a00a0000000102000100a0000200a060130000000040020300",
            "0b0000000110000100a0000000006213000000004002ffff",
        )
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect::<Vec<_>>();
        let mut reader = BitReader::new(&bytes);
        let channel = read_channel(&mut reader, &schemas).unwrap();
        assert!(channel.unreliable.is_empty());
        assert_eq!(reader.position(), 1512);
        let expected = [
            Some(HeroSelectRequest::Pick(Some(heroes.get(SOLDIER).unwrap()))),
            Some(HeroSelectRequest::OpenOrClose(Some(true))),
            Some(HeroSelectRequest::OpenOrClose(Some(false))),
            Some(HeroSelectRequest::OpenOrClose(None)),
            None,
        ];
        assert_eq!(channel.reliable.len(), expected.len());
        for (index, (item, wanted)) in channel.reliable.iter().zip(expected).enumerate() {
            assert_eq!(item.sequence, 7 + index as u32);
            let message = ClientGameMessage::read(&item.message).unwrap();
            assert_eq!(message.entity, 0xa000_0100);
            assert_eq!(message.instance, if wanted.is_some() { 3 } else { -1 });
            assert_eq!(message.hero_select(&heroes), wanted);
            if wanted.is_some() {
                assert_eq!(
                    message.parameters.split_entity_tail().unwrap().1,
                    [0xa000_0100, 0xa000_0200],
                );
            }
        }
    }
}
