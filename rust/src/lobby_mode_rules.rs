//! Career statistics and the mode-rules message sent at login.
//!
//! Ports `ow174/content/career.py`. The retail stat catalog remains in the
//! separately prepared templates. Only the two career stat lists are rebuilt.

use std::io;

use indexmap::IndexMap;

use crate::{
    accounts::{Profile, parse_guid},
    catalog::ItemDb,
    jam_codec::{
        Fields,
        Value::{self, Array, Float64, Struct, Unsigned},
    },
    lobby_ranked::{Queue, Season, wins_of},
    retail_templates::RetailTemplates,
};

pub const MODE_RULES: u32 = 0xb9dc_a497;
pub const RULES: u32 = 27202;

const TIME_PLAYED: u64 = 0x0860_0000_0000_0021;
const MATCHES_WON: u64 = 0x0860_0000_0000_0039;
const SEASON_KIND: u64 = 3;
const SEASON_FLAG: u64 = 0x20;
const ROLE_QUEUES: [Queue; 3] = [Queue::Tank, Queue::Damage, Queue::Support];

pub fn mode_rules(
    templates: &RetailTemplates,
    profile: &Profile,
    heroes: &[u64],
    items: &ItemDb,
    season: Season,
) -> io::Result<Fields> {
    let mut rules = templates
        .first(MODE_RULES, RULES)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing mode-rules template"))?;
    let stats = Array(career_stats(profile, heroes, items, season)?);
    rules.insert(0x78, stats.clone());
    rules.insert(0xa8, stats);
    Ok(rules)
}

/// The lifetime list followed by the selected season's two career categories.
/// The roster is the collection's ordered catalog, shared with other login messages.
pub fn career_stats(
    profile: &Profile,
    heroes: &[u64],
    items: &ItemDb,
    season: Season,
) -> io::Result<Vec<Value>> {
    if heroes.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "empty career hero catalog",
        ));
    }
    let main = picked_hero(&profile.lobby_hero, heroes, items).unwrap_or_else(|| {
        heroes[crc32fast::hash(profile.player_name.as_bytes()) as usize % heroes.len()]
    });
    let played: IndexMap<_, _> = std::iter::once(main)
        .chain(heroes.iter().copied().filter(|&hero| hero != main))
        .zip([500., 50., 40., 30., 20.])
        .map(|(hero, hours)| (hero, vec![stat(TIME_PLAYED, hours * 3600.)]))
        .collect();

    let mut won = IndexMap::new();
    for queue in ROLE_QUEUES {
        let wins = wins_of(profile, queue)?;
        if wins == 0 {
            continue;
        }
        if let Some(hero) = played.keys().chain(heroes).copied().find(|hero| {
            items
                .hero_classes
                .get(hero)
                .and_then(Option::as_deref)
                .is_some_and(|class| class.eq_ignore_ascii_case(queue.name()))
        }) {
            *won.entry(hero).or_insert(0) += wins;
        }
    }

    let mut all_modes = played;
    let mut seasonal = IndexMap::new();
    for (hero, wins) in won {
        let wins = stat(MATCHES_WON, f64::from(wins));
        all_modes.entry(hero).or_default().push(wins.clone());
        seasonal.insert(hero, vec![wins]);
    }
    let key = (season.number << 16) | SEASON_KIND;
    Ok(vec![
        category(all_modes, 0),
        category(seasonal.clone(), key),
        category(seasonal, key | SEASON_FLAG),
    ])
}

fn picked_hero(choice: &str, heroes: &[u64], items: &ItemDb) -> Option<u64> {
    let choice = choice.trim();
    if choice.is_empty()
        || choice.eq_ignore_ascii_case("random")
        || choice.eq_ignore_ascii_case("none")
    {
        return None;
    }
    items
        .hero_by_name(choice)
        .or_else(|| {
            choice
                .get(..2)
                .filter(|prefix| prefix.eq_ignore_ascii_case("0x"))?;
            parse_guid(choice)
        })
        .filter(|hero| heroes.contains(hero))
}

fn stat(guid: u64, value: f64) -> Value {
    Struct(Fields::from([(0, Unsigned(guid)), (8, Float64(value))]))
}

fn category(stats: IndexMap<u64, Vec<Value>>, key: u64) -> Value {
    let records = stats
        .into_iter()
        .map(|(hero, stats)| Struct(Fields::from([(0, Array(stats)), (0x18, Unsigned(hero))])))
        .collect();
    Struct(Fields::from([(0, Array(records)), (0x18, Unsigned(key))]))
}

#[cfg(test)]
mod tests {
    use std::{path::PathBuf, sync::Arc};

    use sha2::{Digest, Sha256};

    use super::*;
    use crate::{collection::Collection, jam_codec::Schemas, lobby_ranked::Ranked};

    const SEASON: Season = Season {
        number: 32,
        card: 1,
        open_card: None,
    };

    fn roster() -> ItemDb {
        let mut items = ItemDb::default();
        items.hero_names.insert(6, "Reaper".into());
        items.hero_names.insert(20, "Fictional NPC".into());
        for (hero, role) in [(2, "Damage"), (5, "SUPPORT"), (6, "Damage"), (7, "Tank")] {
            items.hero_classes.insert(hero, Some(role.into()));
        }
        items
    }

    #[test]
    fn preserves_played_hero_order_and_adds_role_wins_after_time_played() {
        let items = roster();
        let heroes = [1, 2, 3, 4, 5, 6, 7];
        let mut profile = Profile {
            lobby_hero: " rEaPeR ".into(),
            ..Profile::default()
        };
        profile.wins.extend([
            ("tank".into(), 7.into()),
            ("damage".into(), 3.into()),
            ("support".into(), 4.into()),
        ]);
        profile.matches.insert("tank".into(), 5.into());
        profile.wins.insert("open".into(), 11.into());
        let got = career_stats(&profile, &heroes, &items, SEASON).unwrap();
        let all = IndexMap::from([
            (
                6,
                vec![stat(TIME_PLAYED, 1_800_000.), stat(MATCHES_WON, 3.)],
            ),
            (1, vec![stat(TIME_PLAYED, 180_000.)]),
            (2, vec![stat(TIME_PLAYED, 144_000.)]),
            (3, vec![stat(TIME_PLAYED, 108_000.)]),
            (4, vec![stat(TIME_PLAYED, 72_000.)]),
            (7, vec![stat(MATCHES_WON, 5.)]),
            (5, vec![stat(MATCHES_WON, 4.)]),
        ]);
        let seasonal = IndexMap::from([
            (7, vec![stat(MATCHES_WON, 5.)]),
            (6, vec![stat(MATCHES_WON, 3.)]),
            (5, vec![stat(MATCHES_WON, 4.)]),
        ]);
        assert_eq!(
            got,
            [
                category(all, 0),
                category(seasonal.clone(), 0x20_0003),
                category(seasonal, 0x20_0023)
            ]
        );
        profile.lobby_hero = "0X0000000000000006".into();
        assert_eq!(
            career_stats(&profile, &heroes, &items, SEASON).unwrap(),
            got
        );
    }

    #[test]
    fn clones_the_template_and_uses_a_stable_name_for_non_catalog_heroes() {
        let templates = RetailTemplates::from_reader(&br#"{"format":1,"server":[["0xB9DCA497",27202,{"+0x78":[],"+0x90":"kept","+0xA8":[]}]]}"#[..]).unwrap();
        let items = roster();
        let heroes = [1, 2, 3, 4, 5, 6, 7];
        let mut profile = Profile {
            player_name: "Fävorite 群".into(),
            lobby_hero: "random".into(),
            ..Profile::default()
        };
        let random = mode_rules(&templates, &profile, &heroes, &items, SEASON).unwrap();
        profile.lobby_hero = "Fictional NPC".into();
        let npc = mode_rules(&templates, &profile, &heroes, &items, SEASON).unwrap();
        assert_eq!(npc, random);
        assert_eq!(npc[&0x90], Value::String("kept".into()));
        assert_eq!(npc[&0x78], npc[&0xa8]);
        assert_eq!(
            templates.first(MODE_RULES, RULES).unwrap()[&0x78],
            Array(Vec::new())
        );
        assert!(career_stats(&profile, &[], &items, SEASON).is_err());
    }

    #[test]
    #[ignore = "requires the prepared 104319 retail templates and catalogs"]
    fn mode_rules_matches_three_python_104319_message_bodies() {
        let data = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap()).join("data");
        let templates = RetailTemplates::load(data.join("retail_templates.json")).unwrap();
        let items = Arc::new(ItemDb::load(&data).unwrap());
        let collection = Collection::new(&templates, Arc::clone(&items)).unwrap();
        let ranked = Ranked::new(&templates).unwrap();
        let schemas = Schemas::load(data.join("schemas_174.json")).unwrap();

        let default = Profile {
            player_name: "Mode fixture".into(),
            ..Profile::default()
        };
        let mut reaper = Profile {
            lobby_hero: " Reaper ".into(),
            season: 18,
            ..default.clone()
        };
        reaper.wins.extend([
            ("tank".into(), 5.into()),
            ("damage".into(), 13.into()),
            ("support".into(), 8.into()),
        ]);
        reaper.matches.insert("tank".into(), 4.into());
        let mut npc = Profile {
            player_name: "Fävorite 群".into(),
            lobby_hero: "B.O.B.".into(),
            season: 555,
            ..Profile::default()
        };
        npc.wins.insert("support".into(), 2.into());

        for (profile, length, digest) in [
            (
                default,
                66_272,
                "9f995f88d919b841a296a5f1b43c2cca5a53360e6ba83827168f4ee571676b4b",
            ),
            (
                reaper,
                66_728,
                "0ac88c4d182c2ab8bffb5759ffca83f61af266fa1e40bc3fe3f0f405a1306f56",
            ),
            (
                npc,
                66_416,
                "95009d091e3c450ff56124fa10f7b7a97b9d8cd452227712f75e04e80698c7fa",
            ),
        ] {
            let fields = mode_rules(
                &templates,
                &profile,
                collection.heroes(),
                &items,
                ranked.season(&profile),
            )
            .unwrap();
            let body = schemas.encode(MODE_RULES, RULES, &fields).unwrap();
            assert_eq!(body.len(), length);
            assert_eq!(format!("{:x}", Sha256::digest(body)), digest);
        }
    }
}
