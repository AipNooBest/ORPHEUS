//! Playable hero definitions for the 104319 match runtime.
//!
//! Lookup order and name normalization follow ow174/game/content.py.

use std::{collections::HashMap, fs, io, path::Path};

use indexmap::IndexMap;
use serde::Deserialize;

use crate::accounts::parse_guid;

pub const SOLDIER: u64 = 0x02e0_0000_0000_006e;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Hero {
    pub guid: u64,
    pub name: String,
    pub role: String,
    pub body: u64,
}

impl Hero {
    /// Role queue numbers carried by the game participant component.
    pub fn role_number(&self) -> Option<u8> {
        match self.role.as_str() {
            "Damage" => Some(1),
            "Tank" => Some(2),
            "Support" => Some(3),
            _ => None,
        }
    }
}

#[derive(Debug, Default)]
pub struct HeroCatalog {
    heroes: IndexMap<u64, Hero>,
    by_name: HashMap<String, u64>,
}

impl HeroCatalog {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        Self::from_json(&fs::read_to_string(path)?)
    }

    pub fn from_json(text: &str) -> io::Result<Self> {
        let raw: IndexMap<String, RawHero> = serde_json::from_str(text)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let mut heroes = IndexMap::with_capacity(raw.len());
        for (guid, row) in raw {
            let guid = required_guid(&guid)?;
            heroes.insert(
                guid,
                Hero {
                    guid,
                    name: row.name,
                    role: row.role,
                    body: required_guid(&row.body)?,
                },
            );
        }
        let by_name = heroes
            .values()
            .map(|hero| (plain(&hero.name), hero.guid))
            .collect();
        Ok(Self { heroes, by_name })
    }

    pub fn get(&self, guid: u64) -> Option<&Hero> {
        self.heroes.get(&guid)
    }

    pub fn heroes(&self) -> impl Iterator<Item = &Hero> + '_ {
        self.heroes.values()
    }

    /// Prefer an exact normalized name, then a unique name prefix, then a GUID.
    pub fn find_hero(&self, text: &str) -> Option<&Hero> {
        let wanted = plain(text);
        if wanted.is_empty() {
            return None;
        }
        if let Some(&guid) = self.by_name.get(&wanted) {
            return self.get(guid);
        }
        let mut starts = self
            .by_name
            .iter()
            .filter(|(name, _)| name.starts_with(&wanted));
        match (starts.next(), starts.next()) {
            (Some((_, &guid)), None) => self.get(guid),
            _ => self.get(hex_guid(text)?),
        }
    }
}

#[derive(Deserialize)]
struct RawHero {
    name: String,
    #[serde(rename = "class")]
    role: String,
    body: String,
}

fn plain(text: &str) -> String {
    text.to_lowercase()
        .chars()
        .filter_map(|ch| match ch {
            'ö' => Some('o'),
            'ú' => Some('u'),
            'a'..='z' | '0'..='9' => Some(ch),
            _ => None,
        })
        .collect()
}

fn required_guid(text: &str) -> io::Result<u64> {
    hex_guid(text).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid hero catalog GUID {text:?}"),
        )
    })
}

fn hex_guid(text: &str) -> Option<u64> {
    let trimmed = text.trim();
    let digits = trimmed.strip_prefix('+').unwrap_or(trimmed);
    match digits.as_bytes() {
        [b'0', b'x' | b'X', ..] => parse_guid(digits),
        [b'_', ..] => None,
        _ => parse_guid(&format!("0x{digits}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FIXTURE: &str = r#"{
        "02E000000000006E": {"name":"Soldier: 76", "class":"Damage", "body":"0x04000000000003CF"},
        "0x3": {"name":"Torbjörn", "class":"Damage", "body":"0x13"},
        "0x2": {"name":"Lúcio", "class":"Support", "body":"0x12"},
        "0x4": {"name":"D.Va", "class":"Tank", "body":"0x14"},
        "0x6": {"name":"Target Alpha", "class":"Training", "body":"0x16"},
        "0x5": {"name":"Target Beta", "class":"Training", "body":"0x15"}
    }"#;

    #[test]
    fn normalized_names_and_unique_prefixes_precede_hex_lookup() {
        let catalog = HeroCatalog::from_json(FIXTURE).unwrap();
        let names = [
            "soldier",
            " Soldier: 76 ",
            "TORBJORN",
            "Torbjörn",
            "LUCIO",
            "D.Va",
            "dva",
        ];
        let guids = [SOLDIER, SOLDIER, 3, 3, 2, 4, 4];
        for (name, guid) in names.into_iter().zip(guids) {
            assert_eq!(catalog.find_hero(name).unwrap().guid, guid);
        }
        assert_eq!(
            catalog.find_hero("0x02E000000000006E"),
            catalog.get(SOLDIER)
        );
        assert_eq!(catalog.find_hero("02e000000000006e"), catalog.get(SOLDIER));
        assert!(catalog.find_hero("target").is_none());
        assert!(catalog.find_hero(" !!! ").is_none());
        assert!(catalog.find_hero("0x99").is_none());
        assert_eq!(catalog.get(2).unwrap().role_number(), Some(3));
        assert_eq!(catalog.get(4).unwrap().role_number(), Some(2));
        assert_eq!(catalog.get(SOLDIER).unwrap().role_number(), Some(1));
        assert_eq!(catalog.get(5).unwrap().role_number(), None);
        assert_eq!(
            catalog.heroes().map(|hero| hero.guid).collect::<Vec<_>>(),
            [SOLDIER, 3, 2, 4, 6, 5]
        );
    }

    #[test]
    fn names_that_normalize_to_the_same_key_use_the_last_entry() {
        let catalog = HeroCatalog::from_json(
            r#"{
            "0x1": {"name":"A.B", "class":"Tank", "body":"0x11"},
            "0x2": {"name":"AB", "class":"Damage", "body":"0x12"}
        }"#,
        )
        .unwrap();
        assert_eq!(catalog.find_hero("a.b").unwrap().guid, 2);
        assert_eq!(catalog.find_hero("a").unwrap().guid, 2);
        assert_eq!(catalog.get(1).unwrap().body, 0x11);
    }

    #[test]
    fn loads_an_external_file_and_rejects_a_malformed_body_guid() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("game_heroes_174.json");
        assert_eq!(
            HeroCatalog::load(&path).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        fs::write(&path, FIXTURE).unwrap();
        let catalog = HeroCatalog::load(&path).unwrap();
        assert_eq!(catalog.get(SOLDIER).unwrap().body, 0x0400_0000_0000_03cf);
        fs::write(
            &path,
            r#"{"0x1":{"name":"Broken","class":"Tank","body":"bad guid"}}"#,
        )
        .unwrap();
        assert_eq!(
            HeroCatalog::load(&path).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 hero catalog"]
    fn actual_hero_catalog_matches_the_ordered_python_reference() {
        use sha2::{Digest, Sha256};
        use std::path::PathBuf;

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let catalog = HeroCatalog::load(root.join("data/game_heroes_174.json")).unwrap();
        let mut digest = Sha256::new();
        let mut roles = [0; 3];
        for hero in catalog.heroes() {
            roles[usize::from(hero.role_number().unwrap() - 1)] += 1;
            assert_eq!(catalog.find_hero(&hero.name), Some(hero));
            assert_eq!(
                catalog.find_hero(&format!("0x{:016X}", hero.guid)),
                Some(hero)
            );
            digest.update(hero.guid.to_le_bytes());
            digest.update(hero.body.to_le_bytes());
            for text in [&hero.name, &hero.role] {
                digest.update(u32::try_from(text.len()).unwrap().to_le_bytes());
                digest.update(text.as_bytes());
            }
        }
        assert_eq!(roles, [17, 8, 7]);
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "db1613136543b5dcd128e2c9aa1f7037d9cd8171308895489b30d9c19f18a138"
        );
    }
}
