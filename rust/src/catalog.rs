//! Lobby unlock, hero and account region metadata for retail 1.74.

use std::{collections::HashMap, fs, io, path::Path};

use indexmap::IndexMap;
use serde::{Deserialize, de::DeserializeOwned};

use crate::jam_codec::{Fields, Value};

pub const UNLOCK_BASE: u64 = 0x0250_0000_0000_0000;
pub const SKIN_THEME_BASE: u64 = 0x0a50_0000_0000_0000;
pub const HERO_BASE: u64 = 0x02e0_0000_0000_0000;
pub const TEAM_SKIN_TEXT: &str = "Unlocking includes both home and away skins";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Unlock {
    pub guid: u64,
    pub name: String,
    pub item_type: String,
    pub rarity: String,
    pub hero: Option<String>,
    pub source: String,
    pub box_type: Option<String>,
    pub categories: Vec<String>,
    pub available_in: String,
    pub skin_theme: u64,
    pub esports_team: String,
}

#[derive(Debug, Default)]
pub struct ItemDb {
    /// DataTool order matters for the existing loot selection and first-duplicate semantics.
    pub unlocks: IndexMap<u64, Unlock>,
    pub hero_names: IndexMap<u64, String>,
    pub hero_classes: IndexMap<u64, Option<String>>,
    team_skin_pairs: HashMap<u64, u64>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RawHero {
    name: Option<String>,
    #[serde(default = "missing_class")]
    class: Option<String>,
}

fn missing_class() -> Option<String> {
    Some(String::new())
}

type RawGroups = IndexMap<String, Option<Vec<RawEntry>>>;

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RawEntry {
    #[serde(rename = "GUID")]
    guid: Option<String>,
    name: Option<String>,
    #[serde(rename = "Type")]
    item_type: Option<String>,
    rarity: Option<String>,
    categories: Option<Vec<String>>,
    available_in: Option<String>,
    #[serde(rename = "SkinThemeGUID")]
    skin_theme: Option<String>,
    esports_team: Option<String>,
    unlocks: Option<Vec<RawEntry>>,
    loot_box_type: Option<String>,
}

fn read_optional<T: DeserializeOwned + Default>(path: &Path) -> io::Result<T> {
    match fs::read(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(T::default()),
        Err(error) => Err(io::Error::new(
            error.kind(),
            format!("{}: {error}", path.display()),
        )),
        Ok(bytes) => serde_json::from_slice::<Option<T>>(&bytes)
            .map(Option::unwrap_or_default)
            .map_err(|error| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("{}: {error}", path.display()),
                )
            }),
    }
}

fn guid_index(value: &str) -> Option<u64> {
    let index = value.split('.').next()?.trim();
    let index = index.strip_prefix('+').unwrap_or(index);
    let prefix = index
        .strip_prefix("0x")
        .or_else(|| index.strip_prefix("0X"));
    let index = prefix.map_or(index, |digits| digits.strip_prefix('_').unwrap_or(digits));
    if index.starts_with('+') || index.split('_').any(str::is_empty) {
        return None;
    }
    let index = index.replace('_', "");
    u64::from_str_radix(&index, 16).ok()
}

/// Convert a DataTool unlock GUID such as 4AEA.0A5 to its runtime form.
pub fn unlock_guid(value: &str) -> Option<u64> {
    let (_, kind) = value.split_once('.')?;
    if !kind.eq_ignore_ascii_case("0A5") {
        return None;
    }
    Some(UNLOCK_BASE | guid_index(value)?)
}

fn skin_theme_guid(value: Option<&str>) -> io::Result<u64> {
    match value {
        None | Some("") => Ok(0),
        Some(value) => guid_index(value)
            .map(|index| SKIN_THEME_BASE | index)
            .ok_or_else(|| invalid_guid("skin theme", value)),
    }
}

fn invalid_guid(kind: &str, value: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        format!("invalid {kind} GUID {value:?}"),
    )
}

impl ItemDb {
    /// Load the catalog JSON files from a prepared runtime's data directory.
    pub fn load(data_dir: &Path) -> io::Result<Self> {
        let heroes: IndexMap<String, RawHero> =
            read_optional(&data_dir.join("extracted_heroes.json"))?;
        let items: IndexMap<String, RawGroups> =
            read_optional(&data_dir.join("extracted_items.json"))?;
        let general: RawGroups = read_optional(&data_dir.join("extracted_general_unlocks.json"))?;

        let mut catalog = Self::default();
        for (guid, hero) in heroes {
            let Some(name) = hero.name.filter(|name| !name.is_empty()) else {
                continue;
            };
            let index = guid_index(&guid).ok_or_else(|| invalid_guid("hero", &guid))?;
            let id = HERO_BASE | index;
            catalog.hero_names.insert(id, name);
            catalog.hero_classes.insert(id, hero.class);
        }
        for (hero, groups) in items {
            catalog.add_groups(groups, Some(&hero))?;
        }
        catalog.add_groups(general, None)?;
        catalog.pair_team_skins();
        Ok(catalog)
    }

    fn add_groups(&mut self, groups: RawGroups, hero: Option<&str>) -> io::Result<()> {
        for (source, entries) in groups {
            for mut entry in entries.unwrap_or_default() {
                if let Some(unlocks) = entry.unlocks.take() {
                    for unlock in unlocks {
                        self.add(unlock, hero, &source, entry.loot_box_type.as_deref())?;
                    }
                } else {
                    self.add(entry, hero, &source, None)?;
                }
            }
        }
        Ok(())
    }

    fn add(
        &mut self,
        entry: RawEntry,
        hero: Option<&str>,
        source: &str,
        box_type: Option<&str>,
    ) -> io::Result<()> {
        let Some(guid) = entry.guid.as_deref().and_then(unlock_guid) else {
            return Ok(());
        };
        let skin_theme = skin_theme_guid(entry.skin_theme.as_deref())?;
        self.unlocks.entry(guid).or_insert_with(|| Unlock {
            guid,
            name: entry.name.unwrap_or_default(),
            item_type: entry.item_type.unwrap_or_default(),
            rarity: entry
                .rarity
                .filter(|rarity| !rarity.is_empty())
                .unwrap_or_else(|| "Common".into()),
            hero: hero.map(str::to_owned),
            source: source.into(),
            box_type: box_type.map(str::to_owned),
            categories: entry.categories.unwrap_or_default(),
            available_in: entry.available_in.unwrap_or_default(),
            skin_theme,
            esports_team: entry.esports_team.unwrap_or_default(),
        });
        Ok(())
    }

    fn pair_team_skins(&mut self) {
        let mut teams: HashMap<_, Vec<_>> = HashMap::new();
        for unlock in self.unlocks.values() {
            if unlock.available_in == TEAM_SKIN_TEXT {
                let year = unlock
                    .name
                    .rsplit_once(' ')
                    .map(|(_, year)| year)
                    .filter(|year| {
                        year.len() == 4 && year.bytes().all(|byte| byte.is_ascii_digit())
                    })
                    .unwrap_or_default();
                let key = (unlock.hero.as_deref(), unlock.esports_team.as_str(), year);
                teams.entry(key).or_default().push(unlock.guid);
            }
        }
        for guids in teams.values() {
            if let &[first, second] = guids.as_slice() {
                self.team_skin_pairs.insert(first, second);
                self.team_skin_pairs.insert(second, first);
            }
        }
    }

    pub fn team_skin_pair(&self, guid: u64) -> Option<u64> {
        self.team_skin_pairs.get(&guid).copied()
    }

    pub fn get(&self, guid: u64) -> Option<&Unlock> {
        self.unlocks.get(&guid)
    }

    pub fn describe(&self, guid: u64) -> String {
        match self.get(guid) {
            Some(unlock) => {
                let owner = unlock
                    .hero
                    .as_deref()
                    .filter(|name| !name.is_empty())
                    .map_or(String::new(), |name| format!("{name} "));
                format!(
                    "{owner}{} '{}' ({})",
                    unlock.item_type, unlock.name, unlock.rarity
                )
            }
            None => format!("0x{guid:016X}"),
        }
    }

    pub fn hero_name(&self, guid: u64) -> String {
        self.hero_names
            .get(&guid)
            .cloned()
            .unwrap_or_else(|| format!("0x{guid:016X}"))
    }

    pub fn hero_by_name(&self, name: &str) -> Option<u64> {
        let wanted = name.trim().to_lowercase();
        self.hero_names
            .iter()
            .find(|(_, name)| name.to_lowercase() == wanted)
            .map(|(&id, _)| id)
    }
}

pub const RUB_CURRENCY: u64 = 643;
pub const RUB_COUNTRY: &str = "RUS";
pub const DEFAULT_REGION: &str = "US";
pub const DEFAULT_GAME_REGION: &str = "americas";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Region {
    pub code: &'static str,
    pub label: &'static str,
    pub country: &'static str,
    pub currency: u64,
    pub template: &'static str,
    pub decimal_comma: bool,
}

impl Region {
    pub fn display(&self, amount: f64) -> String {
        let text = format!("{amount:.2}");
        let text = if self.decimal_comma {
            text.replace('.', ",")
        } else {
            text
        };
        self.template.replace("{}", &text)
    }
}

pub const REGIONS: [Region; 4] = [
    Region {
        code: "US",
        label: "United States (USD)",
        country: "USA",
        currency: 840,
        template: "${}",
        decimal_comma: false,
    },
    Region {
        code: "EU",
        label: "Europe (EUR)",
        country: "DEU",
        currency: 978,
        template: "{} \u{20ac}",
        decimal_comma: true,
    },
    Region {
        code: "GB",
        label: "United Kingdom (GBP)",
        country: "GBR",
        currency: 826,
        template: "\u{a3}{}",
        decimal_comma: false,
    },
    Region {
        code: "RU",
        label: "Russia (RUB)",
        country: RUB_COUNTRY,
        currency: RUB_CURRENCY,
        template: "{} RUB",
        decimal_comma: true,
    },
];
pub const GAME_REGIONS: [(&str, u8); 3] = [("americas", 1), ("europe", 2), ("asia", 3)];

pub fn game_region_number(name: &str) -> u8 {
    GAME_REGIONS
        .iter()
        .find(|(region, _)| *region == name)
        .map_or(GAME_REGIONS[0].1, |(_, number)| *number)
}

pub fn region_of(code: &str) -> &'static Region {
    REGIONS
        .iter()
        .find(|region| region.code == code)
        .unwrap_or(&REGIONS[0])
}

// Captured whole RUB prices and the local bundle amounts in cents.
const TIERS: [(u16, u16, u16); 5] = [
    (119, 199, 179),
    (299, 499, 449),
    (599, 999, 899),
    (1199, 1999, 1799),
    (2399, 3999, 3599),
];

pub fn localize(value: &Value, code: &str) -> Value {
    let mut localized = value.clone();
    let region = region_of(code);
    if region.currency != RUB_CURRENCY {
        localize_value(&mut localized, region);
    }
    localized
}

pub fn localize_fields(fields: &Fields, code: &str) -> Fields {
    let mut localized = fields.clone();
    let region = region_of(code);
    if region.currency != RUB_CURRENCY {
        localize_struct(&mut localized, region);
    }
    localized
}

fn localize_value(value: &mut Value, region: &Region) {
    match value {
        Value::Struct(fields) => localize_struct(fields, region),
        Value::Array(items) => {
            for item in items {
                localize_value(item, region);
            }
        }
        Value::String(text) if text == RUB_COUNTRY => *text = region.country.into(),
        _ => (),
    }
}

fn localize_struct(fields: &mut Fields, region: &Region) {
    if let Some(cents) = price_cents(fields, region) {
        let amount = Value::Unsigned(u64::from(cents) * 100);
        let text = Value::String(region.display(f64::from(cents) / 100.0));
        fields.insert(0x00, amount.clone());
        fields.insert(0x08, amount);
        fields.insert(0x10, Value::Unsigned(region.currency));
        fields.insert(0x18, text.clone());
        fields.insert(0x40, text);
        return;
    }
    for item in fields.values_mut() {
        localize_value(item, region);
    }
    if is_rub(fields.get(&0x78)) && matches!(fields.get(&0x80), Some(Value::Array(_))) {
        fields.insert(0x78, Value::Unsigned(region.currency));
    }
}

fn is_rub(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Unsigned(RUB_CURRENCY)))
        || matches!(value, Some(Value::Signed(currency)) if *currency == RUB_CURRENCY as i64)
}

fn price_cents(fields: &Fields, region: &Region) -> Option<u16> {
    if !is_rub(fields.get(&0x10)) {
        return None;
    }
    let Value::String(text) = fields.get(&0x18)? else {
        return None;
    };
    let head = text.split(' ').next()?.split(',').next()?;
    if !head.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let rub = head.parse::<u16>().ok()?;
    TIERS
        .iter()
        .find(|(tier, _, _)| *tier == rub)
        .map(|(_, usd, gbp)| if region.code == "GB" { *gbp } else { *usd })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_preserves_first_unlock_order_and_paired_seasons() {
        let data = tempfile::tempdir().unwrap();
        fs::write(
            data.path().join("extracted_heroes.json"),
            r#"{"00F0.075":{"Name":"Sparrow","Class":"Damage"},
            "0010.075":{"Name":"sparrow","Class":null},"0FFF.075":{"Name":null}}"#,
        )
        .unwrap();
        fs::write(
            data.path().join("extracted_items.json"),
            format!(
                r#"{{"Sparrow": {{
                    "Zulu": [{{"GUID":"F0.0A5","Name":"Home","Rarity":null,"Type":"Skin",
                    "SkinThemeGUID":"4924.0A6","Categories":["OWL"],"EsportsTeam":"Team",
                    "AvailableIn":"{TEAM_SKIN_TEXT}"}},{{"LootBoxType":"Standard","Unlocks":[
                        {{"GUID":"02.0a5","Name":"Away","EsportsTeam":"Team",
                        "AvailableIn":"{TEAM_SKIN_TEXT}"}},
                        {{"GUID":"BAD.075","Name":"Not an unlock"}},
                        {{"GUID":"03.0A5","Name":"Home 2018","EsportsTeam":"Team",
                        "AvailableIn":"{TEAM_SKIN_TEXT}"}},
                        {{"GUID":"04.0A5","Name":"Away 2018","EsportsTeam":"Team",
                        "AvailableIn":"{TEAM_SKIN_TEXT}"}}
                    ]}}],
                    "Alpha": [{{"GUID":"F0.0A5","Name":"Duplicate"}}],
                    "Unused": null
                }}}}"#,
            ),
        )
        .unwrap();
        fs::write(
            data.path().join("extracted_general_unlocks.json"),
            r#"{"Default":[{"GUID":"00F0.0A5","Name":"Repeated again"},
                           {"GUID":"01.0A5","Name":null,"Categories":null}]}"#,
        )
        .unwrap();

        let catalog = ItemDb::load(data.path()).unwrap();
        let ids: Vec<_> = catalog.unlocks.keys().map(|id| id ^ UNLOCK_BASE).collect();
        assert_eq!(ids, [0xf0, 2, 3, 4, 1]);
        assert_eq!(catalog.hero_by_name("  SPARROW "), Some(HERO_BASE | 0xf0));
        assert_eq!(catalog.hero_classes[&(HERO_BASE | 0x10)], None);
        let home = catalog.get(UNLOCK_BASE | 0xf0).unwrap();
        assert_eq!((&*home.name, &*home.rarity), ("Home", "Common"));
        assert_eq!(home.skin_theme, SKIN_THEME_BASE | 0x4924);
        assert_eq!(catalog.team_skin_pair(home.guid), Some(UNLOCK_BASE | 2));
        assert_eq!(
            catalog.team_skin_pair(UNLOCK_BASE | 3),
            Some(UNLOCK_BASE | 4)
        );
        assert_eq!(
            catalog.get(UNLOCK_BASE | 2).unwrap().box_type.as_deref(),
            Some("Standard")
        );
        assert_eq!(catalog.describe(home.guid), "Sparrow Skin 'Home' (Common)");
        assert_eq!(catalog.hero_name(1), "0x0000000000000001");
    }

    #[test]
    fn missing_metadata_is_empty_and_invalid_metadata_errors() {
        let data = tempfile::tempdir().unwrap();
        assert!(ItemDb::load(data.path()).unwrap().unlocks.is_empty());
        fs::write(data.path().join("extracted_items.json"), b"{").unwrap();
        let error = ItemDb::load(data.path()).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("extracted_items.json"));
    }

    #[test]
    fn validates_datatool_guid_kind_and_hex_digits() {
        assert_eq!(unlock_guid("4AEA.0a5"), Some(UNLOCK_BASE | 0x4aea));
        assert_eq!(unlock_guid("+0x_4A_EA.0A5"), Some(UNLOCK_BASE | 0x4aea));
        for invalid in ["4AEA", "4AEA.0A6", "4__AEA.0A5", "0x+4AEA.0A5"] {
            assert_eq!(unlock_guid(invalid), None, "{invalid}");
        }
    }

    fn price(text: &str) -> Value {
        Value::Struct(Fields::from([
            (0x00, Value::Unsigned(11_990_000)),
            (0x08, Value::Unsigned(11_990_000)),
            (0x10, Value::Unsigned(RUB_CURRENCY)),
            (0x18, Value::String(text.into())),
            (0x40, Value::String(text.into())),
        ]))
    }

    #[test]
    fn rewrites_nested_store_currency_prices_and_country_without_mutating_capture() {
        let amount = price("1199,00 RUB");
        let store = Fields::from([
            (0x78, Value::Unsigned(RUB_CURRENCY)),
            (0x80, Value::Array(vec![amount.clone()])),
            (0xf8, Value::String(RUB_COUNTRY.into())),
        ]);
        for (code, units, shown, country, currency) in [
            ("US", 199_900, "$19.99", "USA", 840),
            ("EU", 199_900, "19,99 \u{20ac}", "DEU", 978),
            ("GB", 179_900, "\u{a3}17.99", "GBR", 826),
        ] {
            let result = localize_fields(&store, code);
            assert_eq!(result[&0x78], Value::Unsigned(currency));
            assert_eq!(result[&0xf8], Value::String(country.into()));
            let Value::Array(items) = &result[&0x80] else {
                panic!("missing store list")
            };
            let Value::Struct(price) = &items[0] else {
                panic!("missing price")
            };
            assert_eq!(price[&0x10], Value::Unsigned(currency));
            assert_eq!(price[&0x00], Value::Unsigned(units));
            assert_eq!(price[&0x08], Value::Unsigned(units));
            assert_eq!(price[&0x18], Value::String(shown.into()));
            assert_eq!(price[&0x40], Value::String(shown.into()));
        }
        assert_eq!(store[&0x80], Value::Array(vec![amount]));
        assert_eq!(localize_fields(&store, "RU"), store);
        assert_eq!(
            localize_fields(&store, "nope"),
            localize_fields(&store, "US")
        );
        assert_eq!(game_region_number("asia"), 3);
        assert_eq!(game_region_number("elsewhere"), 1);
        let unknown = price("7,00 RUB");
        assert_eq!(localize(&unknown, "US"), unknown);
    }

    #[test]
    #[ignore = "requires the separately prepared DataTool catalogs"]
    fn matches_the_local_retail_catalog_counts_and_order() {
        let data = std::env::var_os("ORPHEUS_CATALOG_DATA")
            .expect("set ORPHEUS_CATALOG_DATA to the prepared data directory");
        let items = ItemDb::load(Path::new(&data)).unwrap();
        assert_eq!(items.unlocks.len(), 7131);
        assert_eq!(items.hero_names.len(), 55);
        assert_eq!(items.team_skin_pairs.len(), 1528);
        assert_eq!(items.unlocks.keys().next(), Some(&(UNLOCK_BASE | 0x761)));
        assert_eq!(items.unlocks.keys().last(), Some(&(UNLOCK_BASE | 0x5163)));
        let fusion = UNLOCK_BASE | 0x13c3;
        let away = items.team_skin_pair(fusion).unwrap();
        assert_eq!(items.get(away).unwrap().name, "Philadelphia Fusion Away");
        assert_eq!(items.team_skin_pair(away), Some(fusion));
    }
}
