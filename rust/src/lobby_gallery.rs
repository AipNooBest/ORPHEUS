//! Retail gallery equip requests are sent on the lobby connection, even during a match.

use std::io;

use crate::{
    collection::{Collection, HERO_CATALOG},
    jam_codec::{Fields, Value},
    lobby_player_messages::record,
    lobby_session::PreparedLogin,
    lobby_transport::LobbyMessage,
    retail_templates::IN_CONNECT,
    world::SkinTheme,
};

pub(crate) const GALLERY_OUT: u32 = 0xb688_70b8;
pub(crate) const EQUIP: u32 = 24500;
pub(crate) const EQUIPPED: u32 = 24902;

pub(crate) struct EquipRequest {
    pub hero: u64,
    item: u64,
    slot: i32,
}

impl EquipRequest {
    pub(crate) fn read((crc, id, fields): &LobbyMessage) -> Option<Self> {
        if (*crc, *id) != (GALLERY_OUT, EQUIP) {
            return None;
        }
        let (Value::Unsigned(hero), Value::Unsigned(item), Value::Signed(slot)) =
            (fields.get(&0x78)?, fields.get(&0x80)?, fields.get(&0x88)?)
        else {
            return None;
        };
        Some(Self {
            hero: *hero,
            item: *item,
            slot: i32::try_from(*slot).ok()?,
        })
    }

    pub(crate) fn apply(
        &self,
        login: &PreparedLogin,
        collection: &Collection,
        wall_time: u64,
    ) -> io::Result<Option<Equipped>> {
        let Some(item) = collection.items().get(self.item) else {
            return Ok(None);
        };
        login.account.edit_profile(|profile| {
            if !collection.equip(profile, self.hero, self.item, self.slot)? {
                return Ok(None);
            }
            let confirmation = Fields::from([
                (0x78, Value::Unsigned(self.hero)),
                (0x80, Value::Unsigned(self.item)),
                (0x88, Value::Signed(i64::from(self.slot))),
            ]);
            let mut messages = vec![(HERO_CATALOG, EQUIPPED, confirmation)];
            if item.item_type == "Icon" {
                let card = record(
                    profile,
                    login.identity,
                    collection.portrait_frame(profile),
                    wall_time,
                );
                messages.push((
                    IN_CONNECT,
                    20502,
                    Fields::from([(0x78, Value::Struct(card))]),
                ));
            }
            let skin = match item.item_type.as_str() {
                "Skin" | "WeaponSkin" => Some(collection.equipped_skin(profile, self.hero)?),
                _ => None,
            };
            Ok(Some(Equipped { messages, skin }))
        })
    }
}

pub(crate) struct Equipped {
    pub messages: Vec<LobbyMessage>,
    pub skin: Option<SkinTheme>,
}
