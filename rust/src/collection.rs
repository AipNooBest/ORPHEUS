//! Lobby ownership, equipment and progression from the external retail catalog.

use std::{
    collections::{HashMap, HashSet},
    io,
    sync::Arc,
};

use indexmap::IndexMap;
use serde_json::{Map, Value as Json};

use crate::{
    accounts::{LootBox, Profile, parse_guid},
    catalog::ItemDb,
    jam_codec::{
        Fields, MAX_ARRAY_COUNT,
        Value::{self, Array, Bool, Signed, Struct, Unsigned},
    },
    lobby_transport::LobbyMessage,
    retail_templates::RetailTemplates,
    world::SkinTheme,
};

pub const HERO_CATALOG: u32 = 0x70e5_a247;
pub const PROGRESSION: u32 = 0x779a_8581;
const LOADOUT_SLOTS: [u32; 7] = [0x38, 0x40, 0x48, 0x50, 0x58, 0x70, 0x88];
const BORDER_BASE: u64 = 0x0250_0000_0000_0918;
const NO_UNLOCK_LEVEL: i64 = 65_535;
const BOX_SYNC_REASON: i64 = 12;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StoreEntry {
    pub unlock_level: i64,
    pub price: i64,
    pub event: i64,
}

impl StoreEntry {
    fn read(fields: &Fields) -> io::Result<Self> {
        Ok(Self {
            unlock_level: integer(fields, 0x10)?,
            price: integer(fields, 0x14)?,
            event: integer(fields, 0x16)?,
        })
    }
}

struct HeroCollection {
    defaults: Vec<u64>,
    loadout: Fields,
}

pub struct Collection {
    items: Arc<ItemDb>,
    hero_template: Fields,
    progression_template: Fields,
    hero_order: Vec<u64>,
    heroes: HashMap<u64, HeroCollection>,
    hero_of: IndexMap<u64, u64>,
    hero_items: HashMap<u64, Vec<u64>>,
    default_account: Vec<u64>,
    all_account: Vec<u64>,
    store: IndexMap<u64, StoreEntry>,
    borders: Vec<(i64, u64)>,
}

impl Collection {
    pub fn new(templates: &RetailTemplates, items: Arc<ItemDb>) -> io::Result<Self> {
        let mut hero_template = templates
            .first(HERO_CATALOG, 24900)
            .ok_or_else(|| invalid("missing hero catalog template"))?;
        let progression_template = templates
            .first(PROGRESSION, 24300)
            .ok_or_else(|| invalid("missing progression template"))?;
        let mut hero_order = Vec::new();
        let mut heroes = HashMap::new();
        for record in array(&hero_template, 0x80)? {
            let record = structure(record)?;
            let hero = guid(record, 0x30)?;
            let loadout = LOADOUT_SLOTS
                .into_iter()
                .map(|slot| Ok((slot, field(record, slot)?.clone())))
                .collect::<io::Result<Fields>>()?;
            hero_order.push(hero);
            heroes.insert(
                hero,
                HeroCollection {
                    defaults: owned_list(record, 0)?,
                    loadout,
                },
            );
        }

        let mut hero_of = IndexMap::new();
        let mut store = IndexMap::new();
        read_hero_stores(&items, &mut hero_template, &mut hero_of, &mut store)?;
        let mut hero_items = HashMap::<u64, Vec<u64>>::new();
        for (&guid, &hero) in &hero_of {
            hero_items.entry(hero).or_default().push(guid);
        }

        let default_account = owned_list(structure(field(&progression_template, 0x78)?)?, 0x30)?;
        let mut account_entries = IndexMap::new();
        for entry in array(structure(field(&progression_template, 0xf8)?)?, 0)? {
            let entry = structure(entry)?;
            account_entries.insert(guid(entry, 0)?, StoreEntry::read(entry)?);
        }
        let mut borders = Vec::new();
        let mut all_account = Vec::new();
        for (guid, entry) in account_entries {
            all_account.push(guid);
            store.entry(guid).or_insert(entry);
            if (0..NO_UNLOCK_LEVEL).contains(&entry.unlock_level)
                && items
                    .get(guid)
                    .is_some_and(|item| item.item_type == "PortraitFrame")
            {
                borders.push((entry.unlock_level, guid));
            }
        }
        borders.sort_unstable();
        all_account.extend(items.unlocks.values().filter_map(|item| {
            (matches!(item.item_type.as_str(), "Icon" | "Spray" | "PortraitFrame")
                && !hero_of.contains_key(&item.guid))
            .then_some(item.guid)
        }));

        Ok(Self {
            items,
            hero_template,
            progression_template,
            hero_order,
            heroes,
            hero_of,
            hero_items,
            default_account,
            all_account,
            store,
            borders,
        })
    }

    pub fn heroes(&self) -> &[u64] {
        &self.hero_order
    }

    pub fn hero_for(&self, guid: u64) -> Option<u64> {
        self.hero_of.get(&guid).copied()
    }

    pub fn store(&self) -> &IndexMap<u64, StoreEntry> {
        &self.store
    }

    pub fn border_levels(&self) -> &[(i64, u64)] {
        &self.borders
    }

    pub fn portrait_frame(&self, profile: &Profile) -> u64 {
        if profile.frame_guid != 0 {
            return profile.frame_guid;
        }
        let earned = self
            .borders
            .partition_point(|&(level, _)| level <= profile.level);
        if earned == 0 {
            let levels_done = profile.level.max(1) - 1;
            let tier = (levels_done / 600).min(4) as u64;
            let stars = (levels_done % 600 / 100) as u64;
            let step = (levels_done % 100 / 10) as u64;
            return BORDER_BASE + tier * 60 + stars * 10 + step;
        }
        self.borders[earned - 1].1
    }

    pub fn owned_for_hero(&self, profile: &Profile, hero: u64) -> Vec<u64> {
        self.owned_hero(hero, profile.unlock_all, &sorted_unlocks(profile))
    }

    fn owned_hero(&self, hero: u64, unlock_all: bool, unlocked: &[u64]) -> Vec<u64> {
        let defaults = self
            .heroes
            .get(&hero)
            .map_or(&[][..], |collection| collection.defaults.as_slice());
        if unlock_all {
            let offered = self.hero_items.get(&hero).into_iter().flatten().copied();
            return append_new(defaults, offered);
        }
        let extra = unlocked
            .iter()
            .copied()
            .filter(|&guid| self.hero_for(guid) == Some(hero));
        append_new(defaults, extra)
    }

    pub fn owned_account(&self, profile: &Profile) -> Vec<u64> {
        if profile.unlock_all {
            return append_new(&self.default_account, self.all_account.iter().copied());
        }
        let extra = sorted_unlocks(profile)
            .into_iter()
            .filter(|&guid| self.hero_for(guid).is_none());
        append_new(&self.default_account, extra)
    }

    pub fn owns(&self, profile: &Profile, guid: u64) -> bool {
        self.hero_for(guid)
            .is_some_and(|hero| self.owned_for_hero(profile, hero).contains(&guid))
            || self.owned_account(profile).contains(&guid)
    }

    pub fn owned_set(&self, profile: &Profile) -> HashSet<u64> {
        let mut owned: HashSet<_> = self.owned_account(profile).into_iter().collect();
        let unlocked = sorted_unlocks(profile);
        for &hero in self.hero_items.keys() {
            owned.extend(self.owned_hero(hero, profile.unlock_all, &unlocked));
        }
        owned
    }

    pub fn loadout(&self, profile: &Profile, hero: u64) -> io::Result<Fields> {
        let mut slots = self
            .heroes
            .get(&hero)
            .map_or_else(Fields::new, |collection| collection.loadout.clone());
        if let Some(saved) = profile
            .loadouts
            .get(&guid_text(hero))
            .and_then(Json::as_object)
        {
            for (slot, value) in &mut slots {
                if let Some(change) = saved.get(&slot_key(*slot)) {
                    *value = saved_loadout(change)?;
                }
            }
        }
        Ok(slots)
    }

    pub fn equipped_skin(&self, profile: &Profile, hero: u64) -> io::Result<SkinTheme> {
        let loadout = self.loadout(profile, hero)?;
        let item = |slot| {
            let guid = unsigned(loadout.get(&slot)?).ok()?;
            self.items.get(guid)
        };
        Ok(SkinTheme {
            guid: item(0x38).map_or(0, |skin| skin.skin_theme),
            golden: item(0x50).is_some_and(|weapon| weapon.name == "GOLDEN"),
        })
    }

    pub fn equip(
        &self,
        profile: &mut Profile,
        hero: u64,
        guid: u64,
        position: i32,
    ) -> io::Result<bool> {
        let Some(item) = self.items.get(guid) else {
            return Ok(false);
        };
        if item.item_type == "Icon" {
            profile.icon_guid = guid;
            return Ok(true);
        }
        if hero == 0 {
            return Ok(false);
        }
        let (slot, list) = match item.item_type.as_str() {
            "Skin" => (0x38, false),
            "HighlightIntro" => (0x40, false),
            "VictoryPose" => (0x48, false),
            "WeaponSkin" => (0x50, false),
            "Spray" => (0x58, true),
            "VoiceLine" => (0x70, true),
            "Emote" => (0x88, true),
            _ => return Ok(false),
        };
        let value = if list {
            let position = position.max(0) as usize;
            if position >= MAX_ARRAY_COUNT {
                return Ok(false);
            }
            let loadout = self.loadout(profile, hero)?;
            let mut values = array(&loadout, slot)?
                .iter()
                .map(|value| Ok(Json::String(guid_text(unsigned(value)?))))
                .collect::<io::Result<Vec<_>>>()?;
            let item = Json::String(guid_text(guid));
            if position >= values.len() {
                let filler = values.last().cloned().unwrap_or_else(|| item.clone());
                values.resize(position + 1, filler);
            }
            values[position] = item;
            Json::Array(values)
        } else {
            Json::String(guid_text(guid))
        };
        let saved = profile
            .loadouts
            .entry(guid_text(hero))
            .or_insert_with(|| Json::Object(Map::new()))
            .as_object_mut()
            .ok_or_else(|| invalid("saved hero loadout is not an object"))?;
        saved.insert(slot_key(slot), value);
        Ok(true)
    }

    pub fn progression(&self, profile: &Profile) -> io::Result<Fields> {
        let mut value = self.progression_template.clone();
        let record = structure_mut(field_mut(&mut value, 0x78)?)?;
        record.extend([
            (0x00, box_list(&profile.loot_boxes)),
            (0x18, Array(Vec::new())),
            (0x30, ownership(self.owned_account(profile))),
            (0x48, Unsigned(0)),
            (0x50, Unsigned(2000)),
            (0x58, Unsigned(self.portrait_frame(profile))),
            (0x60, Unsigned(profile.icon_guid)),
            (0x68, Signed(profile.level)),
            (0x6c, Signed(profile.credits)),
            (0x70, Signed(profile.comp_points)),
            (0x74, Signed(profile.league_tokens)),
            (0x78, Bool(true)),
            (0x79, Bool(true)),
        ]);
        Ok(value)
    }

    pub fn hero_catalog(&self, profile: &Profile) -> io::Result<Fields> {
        let mut value = self.hero_template.clone();
        let unlocked = sorted_unlocks(profile);
        for record in array_mut(&mut value, 0x80)? {
            let record = structure_mut(record)?;
            let hero = guid(record, 0x30)?;
            record.insert(
                0,
                ownership(self.owned_hero(hero, profile.unlock_all, &unlocked)),
            );
            record.extend(self.loadout(profile, hero)?);
        }
        Ok(value)
    }

    pub fn unlock_granted(&self, guid: u64) -> LobbyMessage {
        match self.hero_for(guid) {
            Some(hero) if hero != 0 => (
                HERO_CATALOG,
                24901,
                Fields::from([
                    (0x78, Unsigned(hero)),
                    (0x80, Unsigned(guid)),
                    (0x88, Bool(true)),
                ]),
            ),
            _ => (
                PROGRESSION,
                24301,
                Fields::from([(0x78, Unsigned(guid)), (0x80, Bool(false))]),
            ),
        }
    }

    pub fn boxes_update(boxes: &[LootBox]) -> LobbyMessage {
        (
            PROGRESSION,
            24302,
            Fields::from([
                (0x78, box_list(boxes)),
                (0x90, Signed(BOX_SYNC_REASON)),
                (0x94, Bool(true)),
            ]),
        )
    }

    pub fn unlock_bought(&self, guid: u64) -> LobbyMessage {
        (
            PROGRESSION,
            24306,
            Fields::from([
                (0x78, Unsigned(guid)),
                (0x80, Unsigned(self.hero_for(guid).unwrap_or_default())),
            ]),
        )
    }
}

fn read_hero_stores(
    items: &ItemDb,
    template: &mut Fields,
    hero_of: &mut IndexMap<u64, u64>,
    store: &mut IndexMap<u64, StoreEntry>,
) -> io::Result<()> {
    let records = array_mut(template, 0x98)?;
    let mut indexes = HashMap::new();
    for (index, record) in records.iter().enumerate() {
        let record = structure(record)?;
        let hero = guid(record, 0x18)?;
        indexes.insert(hero, index);
        for entry in array(record, 0)? {
            let entry = structure(entry)?;
            let guid = guid(entry, 0)?;
            hero_of.insert(guid, hero);
            store.insert(guid, StoreEntry::read(entry)?);
        }
    }
    for item in items.unlocks.values() {
        if hero_of.contains_key(&item.guid) {
            continue;
        }
        let Some(hero) = item
            .hero
            .as_deref()
            .and_then(|name| items.hero_by_name(name))
        else {
            continue;
        };
        let Some(&index) = indexes.get(&hero) else {
            continue;
        };
        // Match the retail entries for unsold skins so they count in the gallery.
        let entry = StoreEntry {
            unlock_level: NO_UNLOCK_LEVEL,
            price: 0,
            event: -1,
        };
        array_mut(structure_mut(&mut records[index])?, 0)?.push(Struct(Fields::from([
            (0x00, Unsigned(item.guid)),
            (0x08, Unsigned(0)),
            (0x10, Signed(entry.unlock_level)),
            (0x14, Signed(entry.price)),
            (0x16, Signed(entry.event)),
            (0x17, Bool(false)),
        ])));
        hero_of.insert(item.guid, hero);
        store.insert(item.guid, entry);
    }
    Ok(())
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn missing(offset: u32) -> io::Error {
    invalid(format!("missing collection field +0x{offset:X}"))
}

fn field(fields: &Fields, offset: u32) -> io::Result<&Value> {
    fields.get(&offset).ok_or_else(|| missing(offset))
}

fn field_mut(fields: &mut Fields, offset: u32) -> io::Result<&mut Value> {
    fields.get_mut(&offset).ok_or_else(|| missing(offset))
}

fn structure(value: &Value) -> io::Result<&Fields> {
    match value {
        Struct(fields) => Ok(fields),
        _ => Err(invalid("expected collection record")),
    }
}

fn structure_mut(value: &mut Value) -> io::Result<&mut Fields> {
    match value {
        Struct(fields) => Ok(fields),
        _ => Err(invalid("expected collection record")),
    }
}

fn array(fields: &Fields, offset: u32) -> io::Result<&[Value]> {
    match field(fields, offset)? {
        Array(values) => Ok(values),
        _ => Err(invalid(format!(
            "expected collection array at +0x{offset:X}"
        ))),
    }
}

fn array_mut(fields: &mut Fields, offset: u32) -> io::Result<&mut Vec<Value>> {
    match field_mut(fields, offset)? {
        Array(values) => Ok(values),
        _ => Err(invalid(format!(
            "expected collection array at +0x{offset:X}"
        ))),
    }
}

fn unsigned(value: &Value) -> io::Result<u64> {
    match value {
        Unsigned(value) => Ok(*value),
        Signed(value) => u64::try_from(*value).map_err(|_| invalid("negative collection GUID")),
        _ => Err(invalid("expected collection GUID")),
    }
}

fn guid(fields: &Fields, offset: u32) -> io::Result<u64> {
    unsigned(field(fields, offset)?)
}

fn integer(fields: &Fields, offset: u32) -> io::Result<i64> {
    let error = || invalid(format!("expected collection integer at +0x{offset:X}"));
    match field(fields, offset)? {
        Signed(value) => Ok(*value),
        Unsigned(value) => i64::try_from(*value).map_err(|_| error()),
        _ => Err(error()),
    }
}

fn guid_text(guid: u64) -> String {
    format!("0x{guid:016X}")
}

fn slot_key(slot: u32) -> String {
    format!("+0x{slot:X}")
}

fn saved_loadout(value: &Json) -> io::Result<Value> {
    fn saved_guid(value: &Json) -> io::Result<Value> {
        value
            .as_str()
            .and_then(parse_guid)
            .map(Unsigned)
            .ok_or_else(|| invalid("expected saved loadout GUID text"))
    }
    match value {
        Json::Array(values) => values
            .iter()
            .map(saved_guid)
            .collect::<io::Result<_>>()
            .map(Array),
        value => saved_guid(value),
    }
}

fn sorted_unlocks(profile: &Profile) -> Vec<u64> {
    if profile.unlock_all {
        return Vec::new();
    }
    let mut unlocked: Vec<_> = profile.unlocked_guids().into_iter().collect();
    unlocked.sort_unstable();
    unlocked
}

fn append_new(defaults: &[u64], extra: impl IntoIterator<Item = u64>) -> Vec<u64> {
    let mut seen: HashSet<_> = defaults.iter().copied().collect();
    let mut owned = defaults.to_vec();
    owned.extend(extra.into_iter().filter(|&guid| seen.insert(guid)));
    owned
}

fn owned_list(fields: &Fields, offset: u32) -> io::Result<Vec<u64>> {
    array(fields, offset)?
        .iter()
        .map(|owned| guid(structure(owned)?, 0))
        .collect()
}

fn ownership(guids: Vec<u64>) -> Value {
    Array(
        guids
            .into_iter()
            .map(|guid| Struct(Fields::from([(0, Unsigned(guid)), (8, Bool(true))])))
            .collect(),
    )
}

fn box_list(boxes: &[LootBox]) -> Value {
    Array(
        boxes
            .iter()
            .map(|loot_box| {
                Struct(Fields::from([
                    (
                        0,
                        Struct(Fields::from([(
                            0,
                            Array(vec![Unsigned(loot_box.id), Unsigned(0)]),
                        )])),
                    ),
                    (0x10, Signed(i64::from(loot_box.box_type))),
                    (0x14, Unsigned(1)),
                ]))
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    use serde_json::json;
    use sha2::{Digest, Sha256};

    use crate::jam_codec::Schemas;

    const HERO: u64 = 0x10;
    const DEFAULT_SKIN: u64 = 0x20;
    const NEW_SKIN: u64 = 0x21;
    const HERO_SPRAY: u64 = 0x22;
    const ACCOUNT_ICON: u64 = 0x30;
    const LOW_BORDER: u64 = 0x31;
    const HIGH_BORDER: u64 = 0x32;

    fn synthetic_collection() -> Collection {
        let store_entry =
            |guid: u64, level: i64| json!({"+0x0": guid, "+0x10": level, "+0x14": 0, "+0x16": -1});
        let template = json!({
            "format": 1,
            "server": [
                ["70E5A247", 24900, {
                    "+0x80": [{"+0x0": [{"+0x0": DEFAULT_SKIN}], "+0x30": HERO,
                        "+0x38": DEFAULT_SKIN, "+0x40": 0, "+0x48": 0, "+0x50": 0,
                        "+0x58": [0x11, 0x12], "+0x70": [], "+0x88": []}],
                    "+0x98": [{"+0x18": HERO, "+0x0": [store_entry(DEFAULT_SKIN, 0)]}]
                }],
                ["779A8581", 24300, {
                    "+0x78": {"+0x30": [{"+0x0": ACCOUNT_ICON}]},
                    "+0xF8": {"+0x0": [store_entry(HIGH_BORDER, 200), store_entry(LOW_BORDER, 50)]}
                }]
            ]
        });
        let templates = RetailTemplates::from_reader(template.to_string().as_bytes()).unwrap();
        let mut items = ItemDb::default();
        items.hero_names.insert(HERO, "Local Hero".into());
        for (guid, item_type, hero) in [
            (NEW_SKIN, "Skin", true),
            (HERO_SPRAY, "Spray", true),
            (ACCOUNT_ICON, "Icon", false),
            (LOW_BORDER, "PortraitFrame", false),
            (HIGH_BORDER, "PortraitFrame", false),
        ] {
            items.unlocks.insert(
                guid,
                crate::catalog::Unlock {
                    guid,
                    name: guid_text(guid),
                    item_type: item_type.into(),
                    rarity: String::new(),
                    hero: hero.then(|| "Local Hero".into()),
                    source: String::new(),
                    box_type: None,
                    categories: Vec::new(),
                    available_in: String::new(),
                    skin_theme: 0,
                    esports_team: String::new(),
                },
            );
        }
        Collection::new(&templates, Arc::new(items)).unwrap()
    }

    #[test]
    fn equipped_skin_and_golden_weapon_come_from_the_same_loadout() {
        let mut collection = synthetic_collection();
        let items = Arc::get_mut(&mut collection.items).unwrap();
        let skin = items.unlocks.get_mut(&NEW_SKIN).unwrap();
        skin.skin_theme = 0x0a40_0000_0000_0210;
        let mut golden = skin.clone();
        golden.guid = 0x23;
        golden.name = "GOLDEN".into();
        golden.item_type = "WeaponSkin".into();
        golden.skin_theme = 0;
        items.unlocks.insert(golden.guid, golden);
        let mut profile = Profile::default();
        assert_eq!(
            collection.equipped_skin(&profile, HERO).unwrap(),
            SkinTheme::default()
        );
        assert!(collection.equip(&mut profile, HERO, NEW_SKIN, 0).unwrap());
        assert!(collection.equip(&mut profile, HERO, 0x23, 0).unwrap());
        assert_eq!(
            collection.equipped_skin(&profile, HERO).unwrap(),
            SkinTheme {
                guid: 0x0a40_0000_0000_0210,
                golden: true
            },
        );
        assert_eq!(
            collection.equipped_skin(&profile, 0).unwrap(),
            SkinTheme::default()
        );
    }

    #[test]
    fn new_hero_items_keep_catalog_order_and_stay_out_of_account_ownership() {
        let collection = synthetic_collection();
        let mut profile = Profile {
            unlocked_items: vec![json!("0x22"), json!("0x21"), json!("0x33")],
            ..Profile::default()
        };
        assert_eq!(collection.heroes(), [HERO]);
        assert_eq!(
            collection.owned_for_hero(&profile, HERO),
            [DEFAULT_SKIN, NEW_SKIN, HERO_SPRAY]
        );
        assert_eq!(collection.owned_account(&profile), [ACCOUNT_ICON, 0x33]);
        assert_eq!(collection.store()[&NEW_SKIN].event, -1);
        assert!(collection.owns(&profile, HERO_SPRAY));
        assert!(collection.owned_set(&profile).contains(&HERO_SPRAY));

        let (group, id, granted) = collection.unlock_granted(HERO_SPRAY);
        assert_eq!((group, id), (HERO_CATALOG, 24901));
        assert_eq!(granted[&0x78], Unsigned(HERO));
        let (group, id, bought) = collection.unlock_bought(NEW_SKIN);
        assert_eq!((group, id), (PROGRESSION, 24306));
        assert_eq!(bought[&0x78], Unsigned(NEW_SKIN));
        assert_eq!(bought[&0x80], Unsigned(HERO));

        profile.unlock_all = true;
        assert_eq!(
            collection.owned_account(&profile),
            [ACCOUNT_ICON, HIGH_BORDER, LOW_BORDER]
        );
        assert_eq!(
            collection.owned_for_hero(&profile, HERO),
            [DEFAULT_SKIN, NEW_SKIN, HERO_SPRAY]
        );
    }

    #[test]
    fn borders_follow_the_level_catalog_or_a_player_override() {
        let collection = synthetic_collection();
        let mut profile = Profile {
            level: -1,
            ..Profile::default()
        };
        assert_eq!(collection.portrait_frame(&profile), BORDER_BASE);
        assert_eq!(
            collection.border_levels(),
            [(50, LOW_BORDER), (200, HIGH_BORDER)]
        );
        profile.level = 50;
        assert_eq!(collection.portrait_frame(&profile), LOW_BORDER);
        profile.level = i64::MAX;
        assert_eq!(collection.portrait_frame(&profile), HIGH_BORDER);
        profile.frame_guid = LOW_BORDER;
        assert_eq!(collection.portrait_frame(&profile), LOW_BORDER);
    }

    #[test]
    fn equips_account_icons_and_retains_profile_loadout_text() {
        let collection = synthetic_collection();
        let mut profile = Profile::default();
        assert!(
            collection
                .equip(&mut profile, HERO, ACCOUNT_ICON, 0)
                .unwrap()
        );
        assert_eq!(profile.icon_guid, ACCOUNT_ICON);
        assert!(!collection.equip(&mut profile, 0, NEW_SKIN, 0).unwrap());
        assert!(collection.equip(&mut profile, HERO, NEW_SKIN, 0).unwrap());
        assert!(collection.equip(&mut profile, HERO, HERO_SPRAY, 3).unwrap());
        assert!(
            !collection
                .equip(&mut profile, HERO, HERO_SPRAY, i32::MAX)
                .unwrap()
        );
        assert_eq!(
            collection.loadout(&profile, HERO).unwrap()[&0x38],
            Unsigned(NEW_SKIN)
        );
        assert_eq!(
            collection.loadout(&profile, HERO).unwrap()[&0x58],
            Array(vec![
                Unsigned(0x11),
                Unsigned(0x12),
                Unsigned(0x12),
                Unsigned(HERO_SPRAY)
            ])
        );
        assert_eq!(
            profile.loadouts[&guid_text(HERO)]["+0x58"],
            json!([
                guid_text(0x11),
                guid_text(0x12),
                guid_text(0x12),
                guid_text(HERO_SPRAY)
            ])
        );
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn progression_and_gallery_wire_bytes_match_python_for_three_profiles() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let schemas = Schemas::load(runtime.join("schemas_174.json")).unwrap();
        let templates = RetailTemplates::load(runtime.join("retail_templates.json")).unwrap();
        let items = Arc::new(ItemDb::load(&runtime).unwrap());
        let reaper = items.hero_by_name("Reaper").unwrap();
        let collection = Collection::new(&templates, items).unwrap();
        assert_eq!(collection.heroes().len(), 32);
        assert_eq!(collection.store().len(), 7074);
        assert_eq!(collection.border_levels().len(), 300);

        let aurisa = 0x0250_0000_0000_1149;
        let pachimari = 0x0250_0000_0000_08e9;
        let luchador = 0x0250_0000_0000_4f53;
        let mut equipped = Profile {
            unlocked_items: [aurisa, pachimari, luchador]
                .map(|guid| Json::String(guid_text(guid)))
                .to_vec(),
            level: 247,
            credits: 347,
            ..Profile::default()
        };
        equipped.add_boxes(2, 2).unwrap();
        assert!(
            collection
                .equip(&mut equipped, reaper, luchador, 0)
                .unwrap()
        );

        // Python Collection messages for each synthetic profile, in the order below.
        for (profile, expected_frame, expected_owned, expected_digest) in [
            (
                Profile::default(),
                0x0250_0000_0000_0921,
                (60, 8, 320),
                "0c39de1d0f43716548d94e710f18275782a02d99d340028fa0fdb5ad5246522d",
            ),
            (
                equipped,
                0x0250_0000_0000_093b,
                (61, 9, 323),
                "a3ed977d2f823dcab3172de0755951b638f5829e7d7b96d2e7b5f1d861c97e04",
            ),
            (
                Profile {
                    unlock_all: true,
                    level: 3000, // The highest level the dashboard accepts.
                    ..Profile::default()
                },
                0x0250_0000_0000_0f97,
                (1119, 196, 7122),
                "3c5b07857cea218ef14237718e53e10022b9a93ef2ad2f37221049ad1abf6ced",
            ),
        ] {
            assert_eq!(collection.portrait_frame(&profile), expected_frame);
            assert_eq!(
                (
                    collection.owned_account(&profile).len(),
                    collection.owned_for_hero(&profile, reaper).len(),
                    collection.owned_set(&profile).len(),
                ),
                expected_owned
            );
            let messages = [
                (
                    PROGRESSION,
                    24300,
                    collection.progression(&profile).unwrap(),
                ),
                (
                    HERO_CATALOG,
                    24900,
                    collection.hero_catalog(&profile).unwrap(),
                ),
                collection.unlock_granted(aurisa),
                collection.unlock_granted(pachimari),
                collection.unlock_bought(luchador),
                Collection::boxes_update(&profile.loot_boxes),
            ];
            let mut digest = Sha256::new();
            for (crc, id, fields) in messages {
                let mut wire = schemas.header(crc, 1, id).unwrap();
                wire.extend(schemas.encode(crc, id, &fields).unwrap());
                digest.update(crc.to_le_bytes());
                digest.update(id.to_le_bytes());
                digest.update(u32::try_from(wire.len()).unwrap().to_le_bytes());
                digest.update(wire);
            }
            assert_eq!(format!("{:x}", digest.finalize()), expected_digest);
        }
    }
}
