//! Main-menu model and party portrait selection for the lobby session.

use std::io;

use crate::{
    accounts::{Profile, parse_guid},
    catalog::{HERO_BASE, ItemDb, SKIN_THEME_BASE},
};

const PVE_NPCS: &[(&str, u64)] = &[
    ("null sector eradicator", 0x173),
    ("null sector slicer", 0x178),
    ("null sector nulltrooper", 0x179),
    ("null sector detonator", 0x17c),
    ("talon trooper", 0x1ac),
    ("talon sniper", 0x1b8),
    ("talon heavy assault", 0x1ba),
    ("talon assassin", 0x1bb),
    ("talon enforcer", 0x1ce),
    ("junkenstein zomnic", 0x175),
    ("junkenstein shock-tire", 0x176),
    ("junkenstein zombardier", 0x177),
    ("b.o.b.", 0x21d),
];

// These Anniversary menu themes are absent from the inherited preload.
const PRELOAD_MENU_THEMES: [u64; 8] = [
    0x49e1, 0x48a9, 0x49cf, 0x49e4, 0x49d8, 0x49d9, 0x49d1, 0x49a8,
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MenuSelection {
    pub party_hero: u64,
    /// None leaves the active celebration's default model in place.
    pub menu_guid: Option<u64>,
}

/// One random choice per session until the player changes their selection.
#[derive(Default)]
pub struct MenuHero {
    choice: String,
    random_pick: Option<u64>,
}

impl MenuHero {
    pub fn select(
        &mut self,
        profile: &Profile,
        heroes: &[u64],
        items: &ItemDb,
    ) -> io::Result<MenuSelection> {
        self.select_with(profile, heroes, items, random_index)
    }

    fn select_with(
        &mut self,
        profile: &Profile,
        heroes: &[u64],
        items: &ItemDb,
        roll: impl FnOnce(usize) -> io::Result<usize>,
    ) -> io::Result<MenuSelection> {
        let choice = if profile.lobby_hero.is_empty() {
            "random"
        } else {
            profile.lobby_hero.trim()
        };
        if self.choice != choice {
            self.choice = choice.into();
            self.random_pick = None;
        }
        let name = choice.to_lowercase();
        if name == "none" {
            return Ok(MenuSelection {
                party_hero: 0,
                menu_guid: None,
            });
        }
        if let Some(hero) = explicit_hero(choice, heroes, items) {
            return Ok(MenuSelection {
                party_hero: hero,
                menu_guid: Some(hero),
            });
        }
        let party_hero = if let Some(rolled) = self.random_pick {
            rolled
        } else {
            if heroes.is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "empty menu hero catalog",
                ));
            }
            let hero = heroes[roll(heroes.len())?];
            self.random_pick = Some(hero);
            hero
        };
        let npc = PVE_NPCS
            .iter()
            .find(|(label, _)| *label == name)
            .map(|(_, index)| HERO_BASE | index);
        let menu_guid = npc.or_else(|| (name != "random").then_some(party_hero));
        Ok(MenuSelection {
            party_hero,
            menu_guid,
        })
    }
}

/// A requested playable hero from the catalog, without any fallback.
pub(crate) fn explicit_hero(choice: &str, heroes: &[u64], items: &ItemDb) -> Option<u64> {
    let choice = choice.trim();
    if choice.is_empty()
        || choice.eq_ignore_ascii_case("random")
        || choice.eq_ignore_ascii_case("none")
    {
        return None;
    }
    let guid = choice
        .get(..2)
        .filter(|prefix| prefix.eq_ignore_ascii_case("0x"));
    items
        .hero_by_name(choice)
        .or_else(|| guid.and_then(|_| parse_guid(choice)))
        .filter(|hero| heroes.contains(hero))
}

pub fn extra_preload_themes(items: &ItemDb) -> impl Iterator<Item = u64> + '_ {
    PRELOAD_MENU_THEMES
        .into_iter()
        .map(|index| SKIN_THEME_BASE | index)
        .chain(
            items
                .unlocks
                .values()
                .map(|unlock| unlock.skin_theme)
                .filter(|&theme| theme != 0),
        )
}

fn random_index(len: usize) -> io::Result<usize> {
    let len = len as u64;
    let limit = u64::MAX - u64::MAX % len;
    loop {
        let mut entropy = [0; 8];
        getrandom::fill(&mut entropy).map_err(|error| io::Error::other(error.to_string()))?;
        let number = u64::from_le_bytes(entropy);
        if number < limit {
            return Ok((number % len) as usize);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn player_portrait_and_event_model_keep_their_separate_choices() {
        let mut items = ItemDb::default();
        let heroes = [HERO_BASE | 3, HERO_BASE | 7];
        items.hero_names.insert(heroes[0], "Tracer".into());
        let mut menu = MenuHero::default();
        let mut profile = Profile::default();
        assert_eq!(
            menu.select_with(&profile, &heroes, &items, |_| Ok(1))
                .unwrap(),
            MenuSelection {
                party_hero: heroes[1],
                menu_guid: None
            }
        );
        let no_roll = |_| panic!("this selection does not need fresh entropy");
        assert_eq!(
            menu.select_with(&profile, &heroes, &items, no_roll)
                .unwrap()
                .party_hero,
            heroes[1]
        );
        profile.lobby_hero = " tracer ".into();
        assert_eq!(
            menu.select_with(&profile, &heroes, &items, no_roll)
                .unwrap(),
            MenuSelection {
                party_hero: heroes[0],
                menu_guid: Some(heroes[0])
            }
        );
        profile.lobby_hero = "Talon Sniper".into();
        assert_eq!(
            menu.select_with(&profile, &heroes, &items, |_| Ok(1))
                .unwrap(),
            MenuSelection {
                party_hero: heroes[1],
                menu_guid: Some(HERO_BASE | 0x1b8)
            }
        );
        profile.lobby_hero = "none".into();
        assert_eq!(
            menu.select_with(&profile, &heroes, &items, no_roll)
                .unwrap(),
            MenuSelection {
                party_hero: 0,
                menu_guid: None
            }
        );
    }
}
