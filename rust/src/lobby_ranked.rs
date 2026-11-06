//! Competitive season and rating projections from the external retail template.

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    io,
};

use serde_json::{Map, Value as Json};

use crate::{
    accounts::Profile,
    jam_codec::{
        Fields,
        Value::{self, Array, Bool, Float64, Signed, Struct, Unsigned},
    },
    lobby_clock::{DAY, from_stu_datetime, stu_datetime},
    lobby_transport::LobbyMessage,
    retail_templates::RetailTemplates,
};

pub const RANKED: u32 = 0x2372_a2e8;
pub const CARD_BASE: u64 = 0x0630_0000_0000_0000;
pub const ROLE_QUEUE: u64 = CARD_BASE | 0x1b3;
pub const OPEN_QUEUE: u64 = CARD_BASE | 0x1b4;
pub const COMPETITIVE_CTF: u64 = CARD_BASE | 0x1c3;
pub const LUCIO_CUP: u64 = CARD_BASE | 0x1ad;
pub const TANK: u64 = 0x0d80_0000_0000_0850;
pub const DAMAGE: u64 = 0x0d80_0000_0000_084e;
pub const SUPPORT: u64 = 0x0d80_0000_0000_0851;
pub const PC_PLATFORM: u64 = 15_959_616;
pub const PC_POOL: u64 = 16_025_156;

const STATE: u32 = 36300;
const CARD_RATINGS: u32 = 36302;
const CURRENT_SEASON: u64 = 32;
const FIRST_ROLE_SEASON: u64 = 18;
const SEASON_DATES: [u32; 4] = [0x20, 0x28, 0x30, 0x38];
const DAYS_INTO_SEASON: u64 = 7;
const TIER_FLOORS: [u16; 6] = [1500, 2000, 2500, 3000, 3500, 4000];
const PLACEMENT_MATCHES: u8 = 5;
const EVENT_CARDS: [u64; 2] = [COMPETITIVE_CTF, LUCIO_CUP];

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum Queue {
    Tank,
    Damage,
    Support,
    Open,
    Ctf,
    Lucio,
}

impl Queue {
    pub fn name(self) -> &'static str {
        match self {
            Self::Tank => "tank",
            Self::Damage => "damage",
            Self::Support => "support",
            Self::Open => "open",
            Self::Ctf => "ctf",
            Self::Lucio => "lucio",
        }
    }
}

pub type LeaderboardPlaces = BTreeMap<Queue, u16>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Season {
    pub number: u64,
    pub card: u64,
    pub open_card: Option<u64>,
}

impl Season {
    pub fn has_roles(self) -> bool {
        self.number >= FIRST_ROLE_SEASON
    }

    pub fn queue(self, card: u64, role: u64) -> Option<Queue> {
        match (card, role) {
            (COMPETITIVE_CTF, 0) => Some(Queue::Ctf),
            (LUCIO_CUP, 0) => Some(Queue::Lucio),
            (_, 0) if self.open_card == Some(card) => Some(Queue::Open),
            _ if self.card != card => None,
            (_, 0) if !self.has_roles() => Some(Queue::Open),
            _ if self.has_roles() => match role {
                TANK => Some(Queue::Tank),
                DAMAGE => Some(Queue::Damage),
                SUPPORT => Some(Queue::Support),
                _ => None,
            },
            _ => None,
        }
    }
}

/// Values used by the match team list. An unplaced player has all zeroes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MatchRank {
    pub skill_rating: u16,
    pub top_500_place: u16,
    pub tier: u8,
}

struct Rating {
    skill: u16,
    place: u16,
    placements_played: u8,
}

impl Rating {
    fn read(profile: &Profile, queue: Queue, places: &LeaderboardPlaces) -> io::Result<Self> {
        let skill = rating_of(profile, queue)?;
        let matches = matches_of(profile, queue)?;
        Ok(Self {
            skill,
            place: places.get(&queue).copied().unwrap_or(0),
            placements_played: matches.min(u16::from(PLACEMENT_MATCHES)) as u8,
        })
    }

    fn match_rank(&self) -> MatchRank {
        if self.placements_played < PLACEMENT_MATCHES {
            return MatchRank::default();
        }
        MatchRank {
            skill_rating: self.skill,
            top_500_place: self.place,
            tier: tier(self.skill),
        }
    }

    fn fields(&self) -> Fields {
        let rank = self.match_rank();
        let result = Struct(Fields::from([(0, Unsigned(1))]));
        Fields::from([
            (0x18, Unsigned(u64::from(rank.skill_rating))),
            (0x1a, Unsigned(u64::from(rank.skill_rating))),
            (0x1c, Unsigned(u64::from(rank.top_500_place))),
            (0x1e, Unsigned(u64::from(rank.top_500_place))),
            (0x20, Unsigned(u64::from(self.skill))),
            (0x22, Unsigned(u64::from(rank.tier))),
            (0x23, Unsigned(u64::from(rank.tier))),
            (
                0x28,
                Array(vec![result; usize::from(self.placements_played)]),
            ),
            (0x40, Bool(self.placements_played < PLACEMENT_MATCHES)),
        ])
    }
}

pub fn tier(rating: u16) -> u8 {
    TIER_FLOORS.partition_point(|&floor| rating >= floor) as u8
}

pub fn rating_of(profile: &Profile, queue: Queue) -> io::Result<u16> {
    Ok(profile_integer(&profile.ratings, queue, 2333)?.clamp(1, 5000) as u16)
}

pub fn matches_of(profile: &Profile, queue: Queue) -> io::Result<u16> {
    Ok(profile_integer(&profile.matches, queue, 25)?.clamp(0, 9999) as u16)
}

pub fn wins_of(profile: &Profile, queue: Queue) -> io::Result<u16> {
    Ok(
        profile_integer(&profile.wins, queue, 0)?.clamp(0, i64::from(matches_of(profile, queue)?))
            as u16,
    )
}

fn profile_integer(values: &Map<String, Json>, queue: Queue, default: i64) -> io::Result<i64> {
    let Some(value) = values.get(queue.name()) else {
        return Ok(default);
    };
    let integer = match value {
        Json::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64)),
        Json::Bool(value) => Some(i64::from(*value)),
        Json::String(value) => value.trim().parse().ok(),
        _ => None,
    };
    integer.ok_or_else(|| {
        invalid(format!(
            "invalid saved rating or match count for {}",
            queue.name()
        ))
    })
}

pub struct Ranked {
    template: Fields,
    seasons: BTreeMap<u64, Season>,
    card_templates: HashMap<u64, Fields>,
    running_templates: HashMap<u64, Fields>,
}

impl Ranked {
    pub fn new(templates: &RetailTemplates) -> io::Result<Self> {
        let template = templates
            .first(RANKED, STATE)
            .ok_or_else(|| invalid("missing ranked state template"))?;
        let history = array(&template, 0x98)?;
        let mut open_cards = HashMap::new();
        let mut seasons = BTreeMap::new();
        for entry in history {
            let entry = structure(entry)?;
            let parent = integer(entry, 0xf0)?;
            let card = integer(entry, 0x18)?;
            if parent != 0 {
                open_cards.entry(parent).or_insert(card);
            }
        }
        for entry in history {
            let entry = structure(entry)?;
            let number = integer(entry, 0x118)?;
            if number == 0 {
                continue;
            }
            let card = integer(entry, 0x18)?;
            let open_card = open_cards.get(&card).copied().filter(|&card| card != 0);
            seasons.insert(
                number,
                Season {
                    number,
                    card,
                    open_card,
                },
            );
        }
        if !seasons.contains_key(&CURRENT_SEASON) {
            return Err(invalid("ranked template lacks the current season"));
        }
        let card_templates = by_id(array(structure(field(&template, 0x80)?)?, 0)?, 0x18)?;
        for card in [ROLE_QUEUE, OPEN_QUEUE, COMPETITIVE_CTF] {
            if !card_templates.contains_key(&card) {
                return Err(invalid(format!(
                    "missing competitive rating template {card:#018x}"
                )));
            }
        }
        let running_templates = by_id(array(&template, 0xb0)?, 0)?;
        if !running_templates.contains_key(&ROLE_QUEUE) {
            return Err(invalid("missing running season template"));
        }
        Ok(Self {
            template,
            seasons,
            card_templates,
            running_templates,
        })
    }

    pub fn season(&self, profile: &Profile) -> Season {
        let number = u64::try_from(profile.season).unwrap_or(0);
        self.seasons
            .get(&number)
            .copied()
            .unwrap_or(self.seasons[&CURRENT_SEASON])
    }

    pub fn rank(
        &self,
        profile: &Profile,
        card: u64,
        role: u64,
        places: &LeaderboardPlaces,
    ) -> io::Result<Option<MatchRank>> {
        let season = self.season(profile);
        if card == 0
            || (card != season.card
                && season.open_card != Some(card)
                && !EVENT_CARDS.contains(&card))
        {
            return Ok(None);
        }
        let rank = match season.queue(card, role) {
            Some(queue) => Rating::read(profile, queue, places)?.match_rank(),
            None => MatchRank::default(),
        };
        Ok(Some(rank))
    }

    /// Ratings grouped by card, for the career profile and competitive state.
    pub fn cards(
        &self,
        profile: &Profile,
        event_cards: &[u64],
        places: &LeaderboardPlaces,
    ) -> io::Result<Vec<Fields>> {
        let season = self.season(profile);
        let main = if season.has_roles() {
            ROLE_QUEUE
        } else {
            OPEN_QUEUE
        };
        let cards = [(season.card, main)]
            .into_iter()
            .chain(season.open_card.map(|card| (card, OPEN_QUEUE)))
            .chain(event_cards.iter().map(|&card| (card, COMPETITIVE_CTF)));
        let mut rated_cards = Vec::new();
        for (card_id, template_id) in cards {
            let mut card = self.card_templates[&template_id].clone();
            card.insert(0x18, Unsigned(card_id));
            let seen =
                i64::try_from(card_id).is_ok_and(|card| profile.seasons_seen.contains(&card));
            card.insert(0x2c, Bool(seen));
            for rating in array_mut(&mut card, 0)? {
                let rating = structure_mut(rating)?;
                let role = integer(rating, 0)?;
                rating.insert(0x10, Unsigned(PC_PLATFORM));
                rating.insert(0x14, Unsigned(PC_POOL));
                if let Some(queue) = season.queue(card_id, role) {
                    rating.extend(Rating::read(profile, queue, places)?.fields());
                }
            }
            rated_cards.push(card);
        }
        Ok(rated_cards)
    }

    /// Emit 36302 for each rated card before the complete 36300 state.
    pub fn login_messages(
        &self,
        profile: &Profile,
        event_cards: &[u64],
        places: &LeaderboardPlaces,
        now: u64,
    ) -> io::Result<Vec<LobbyMessage>> {
        let season = self.season(profile);
        let cards = self.cards(profile, event_cards, places)?;
        let state = self.state(season, event_cards, now, &cards)?;
        let mut messages: Vec<_> = cards
            .into_iter()
            .map(|card| (RANKED, CARD_RATINGS, Fields::from([(0x78, Struct(card))])))
            .collect();
        messages.push((RANKED, STATE, state));
        Ok(messages)
    }

    /// Every competitive event appears in the party and match-roster ratings,
    /// even when it is absent from the player's current Arcade.
    pub fn party_ratings(
        &self,
        profile: &Profile,
        places: &LeaderboardPlaces,
    ) -> io::Result<Vec<Value>> {
        let cards = self.cards(profile, &EVENT_CARDS, places)?;
        cards.iter().map(party_card).collect()
    }

    fn state(
        &self,
        season: Season,
        event_cards: &[u64],
        now: u64,
        cards: &[Fields],
    ) -> io::Result<Fields> {
        let mut value = self.template.clone();
        value.insert(0x78, Unsigned(season.card));
        structure_mut(field_mut(&mut value, 0x80)?)?
            .insert(0, Array(cards.iter().cloned().map(Struct).collect()));
        let later: HashSet<_> = self
            .seasons
            .values()
            .filter(|entry| entry.number > season.number)
            .flat_map(|entry| [Some(entry.card), entry.open_card].into_iter().flatten())
            .collect();
        let running: HashSet<_> = [Some(season.card), season.open_card]
            .into_iter()
            .flatten()
            .chain(event_cards.iter().copied())
            .filter(|&card| card != 0)
            .collect();
        let mut updated_history = Vec::new();
        let mut timers = Vec::new();
        for mut entry in std::mem::take(array_mut(&mut value, 0x98)?) {
            let entry_fields = structure_mut(&mut entry)?;
            let card = integer(entry_fields, 0x18)?;
            if later.contains(&card) {
                continue;
            }
            if running.contains(&card) {
                timers.push(Struct(self.shift_season(entry_fields, card, now)?));
            }
            updated_history.push(entry);
        }
        value.insert(0x98, Array(updated_history));
        value.insert(0xb0, Array(timers));
        let card_ids = array_mut(&mut value, 0xc8)?;
        let mut retained = Vec::with_capacity(card_ids.len());
        for card in std::mem::take(card_ids) {
            if !later.contains(&unsigned(&card)?) {
                retained.push(card);
            }
        }
        *card_ids = retained;
        Ok(value)
    }

    fn shift_season(&self, entry: &mut Fields, card: u64, now: u64) -> io::Result<Fields> {
        let start = season_time(entry, 0x20)?;
        let shift = i128::from(now) - i128::from(DAYS_INTO_SEASON * DAY) - i128::from(start);
        for key in SEASON_DATES {
            let time = i128::from(season_time(entry, key)?) + shift;
            let time =
                u64::try_from(time).map_err(|_| invalid("ranked season date out of range"))?;
            entry.insert(
                key,
                Struct(Fields::from([(0, Unsigned(stu_datetime(time)?))])),
            );
        }
        let mut timer = self
            .running_templates
            .get(&card)
            .unwrap_or(&self.running_templates[&ROLE_QUEUE])
            .clone();
        let remaining = i128::from(season_time(entry, 0x30)?) - i128::from(now);
        timer.insert(0, Unsigned(card));
        timer.insert(8, Float64(remaining as f64));
        Ok(timer)
    }
}

fn party_card(card: &Fields) -> io::Result<Value> {
    let mut ratings = Vec::new();
    for rating in array(card, 0)? {
        let rating = structure(rating)?;
        let mut role = [
            (0, 0),
            (8, 0x10),
            (0xc, 0x14),
            (0x10, 0x18),
            (0x12, 0x1c),
            (0x14, 0x20),
            (0x16, 0x22),
            (0x17, 0x40),
        ]
        .into_iter()
        .map(|(destination, source)| Ok((destination, field(rating, source)?.clone())))
        .collect::<io::Result<Fields>>()?;
        let placement = u16::try_from(integer(rating, 0x20)?)
            .map_err(|_| invalid("invalid ranked placement rating"))?;
        role.insert(0x18, Unsigned(u64::from(tier(placement))));
        ratings.push(Struct(role));
    }
    Ok(Struct(Fields::from([
        (0, Array(ratings)),
        (0x18, field(card, 0x18)?.clone()),
        (0x20, Unsigned(0)),
    ])))
}

fn season_time(entry: &Fields, offset: u32) -> io::Result<u64> {
    from_stu_datetime(integer(structure(field(entry, offset)?)?, 0)?)
}

fn by_id(entries: &[Value], offset: u32) -> io::Result<HashMap<u64, Fields>> {
    entries
        .iter()
        .map(|value| {
            let fields = structure(value)?;
            Ok((integer(fields, offset)?, fields.clone()))
        })
        .collect()
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn missing(offset: u32) -> io::Error {
    invalid(format!("missing ranked field +0x{offset:X}"))
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
        _ => Err(invalid("expected a ranked record")),
    }
}

fn structure_mut(value: &mut Value) -> io::Result<&mut Fields> {
    match value {
        Struct(fields) => Ok(fields),
        _ => Err(invalid("expected a ranked record")),
    }
}

fn array(fields: &Fields, offset: u32) -> io::Result<&[Value]> {
    match field(fields, offset)? {
        Array(values) => Ok(values),
        _ => Err(invalid(format!("expected a ranked array at +0x{offset:X}"))),
    }
}

fn array_mut(fields: &mut Fields, offset: u32) -> io::Result<&mut Vec<Value>> {
    match field_mut(fields, offset)? {
        Array(values) => Ok(values),
        _ => Err(invalid(format!("expected a ranked array at +0x{offset:X}"))),
    }
}

fn unsigned(value: &Value) -> io::Result<u64> {
    match value {
        Unsigned(value) => Ok(*value),
        Signed(value) => {
            u64::try_from(*value).map_err(|_| invalid("expected unsigned ranked field"))
        }
        _ => Err(invalid("expected a ranked integer")),
    }
}

fn integer(fields: &Fields, offset: u32) -> io::Result<u64> {
    unsigned(field(fields, offset)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    use serde_json::json;
    use sha2::{Digest, Sha256};

    use crate::{jam_codec::Schemas, lobby_clock::server_time, lobby_party::PARTY};

    #[test]
    fn queue_selection_depends_on_season_role_and_event_card() {
        let role_season = Season {
            number: CURRENT_SEASON,
            card: ROLE_QUEUE,
            open_card: Some(OPEN_QUEUE),
        };
        assert_eq!(role_season.queue(ROLE_QUEUE, DAMAGE), Some(Queue::Damage));
        assert_eq!(role_season.queue(ROLE_QUEUE, 0), None);
        assert_eq!(role_season.queue(OPEN_QUEUE, SUPPORT), None);
        assert_eq!(role_season.queue(OPEN_QUEUE, 0), Some(Queue::Open));
        assert_eq!(role_season.queue(LUCIO_CUP, 0), Some(Queue::Lucio));
        assert_eq!(role_season.queue(COMPETITIVE_CTF, TANK), None);

        let early_season = Season {
            number: 10,
            card: CARD_BASE | 0xe6,
            open_card: None,
        };
        assert_eq!(early_season.queue(early_season.card, 0), Some(Queue::Open));
        assert_eq!(early_season.queue(early_season.card, SUPPORT), None);
        assert_eq!(early_season.queue(OPEN_QUEUE, 0), None);
    }

    #[test]
    fn placements_use_saved_skill_and_keep_match_rank_empty_until_fifth_match() {
        let mut profile = Profile::default();
        profile.ratings.insert("tank".into(), json!("4100"));
        profile.matches.insert("tank".into(), json!(4));
        profile.wins.insert("tank".into(), json!(10));
        let places = LeaderboardPlaces::from([(Queue::Tank, 7)]);
        assert_eq!(wins_of(&profile, Queue::Tank).unwrap(), 4);
        let rating = Rating::read(&profile, Queue::Tank, &places).unwrap();
        assert_eq!(rating.match_rank(), MatchRank::default());
        let fields = rating.fields();
        assert_eq!(fields[&0x20], Unsigned(4100));
        assert_eq!(fields[&0x40], Bool(true));
        assert_eq!(array(&fields, 0x28).unwrap().len(), 4);

        profile.matches.insert("tank".into(), json!(5));
        let rating = Rating::read(&profile, Queue::Tank, &places).unwrap();
        assert_eq!(
            rating.match_rank(),
            MatchRank {
                skill_rating: 4100,
                top_500_place: 7,
                tier: 6
            }
        );
        assert_eq!(rating.fields()[&0x40], Bool(false));
        profile.ratings.insert("tank".into(), json!(9000));
        assert_eq!(rating_of(&profile, Queue::Tank).unwrap(), 5000);
        profile.matches.insert("tank".into(), json!(-5));
        assert_eq!(matches_of(&profile, Queue::Tank).unwrap(), 0);
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn state_card_and_party_rating_messages_match_python_wire_vectors() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let schemas = Schemas::load(runtime.join("schemas_174.json")).unwrap();
        let templates = RetailTemplates::load(runtime.join("retail_templates.json")).unwrap();
        let ranked = Ranked::new(&templates).unwrap();
        let default = Profile::default();
        let season_25 = Profile {
            season: 25,
            ratings: Map::from_iter([
                ("tank".into(), json!(3100)),
                ("open".into(), json!(2500)),
                ("damage".into(), json!(2600)),
            ]),
            matches: Map::from_iter([("damage".into(), json!(2))]),
            seasons_seen: vec![(CARD_BASE | 0x121) as i64],
            ..Profile::default()
        };
        let season_10 = Profile {
            season: 10,
            ratings: Map::from_iter([("open".into(), json!(3300)), ("lucio".into(), json!(2800))]),
            server_date: "2026-09-29".into(),
            ..Profile::default()
        };
        let late_date = Profile {
            server_date: "2106-02-06".into(),
            ..Profile::default()
        };
        let now = 1_790_000_000;
        for (profile, event_cards, places, state_card, expected_len, expected) in [
            (
                &default,
                [COMPETITIVE_CTF].as_slice(),
                LeaderboardPlaces::new(),
                ROLE_QUEUE,
                5,
                "1b16680a48a2a8f5fd48e362b1feaa55ffeca0a6f3f2ca149f3cfe1efe62a60a",
            ),
            (
                &season_25,
                [COMPETITIVE_CTF].as_slice(),
                LeaderboardPlaces::from([(Queue::Tank, 7)]),
                CARD_BASE | 0x121,
                5,
                "70a380a23b9794786c427e45edaf6468537bb93e805a57eef18e38af410b0da4",
            ),
            (
                &season_10,
                [LUCIO_CUP].as_slice(),
                LeaderboardPlaces::from([(Queue::Open, 18)]),
                CARD_BASE | 0xe6,
                4,
                "be775ca73c870e2282b98d9e524c36cc856dc26f688aa093d02bd468c0a94688",
            ),
            (
                &late_date,
                [].as_slice(),
                LeaderboardPlaces::from([(Queue::Support, 42)]),
                ROLE_QUEUE,
                4,
                "60e8955c5105ed973e8c853563229b6e0f0b7f264edb372edd87193b5ec9d8f9",
            ),
        ] {
            let now = server_time(&profile.server_date, now).unwrap();
            let mut messages = ranked
                .login_messages(profile, event_cards, &places, now)
                .unwrap();
            let (crc, id, state) = messages.last().unwrap();
            assert_eq!((*crc, *id), (RANKED, STATE));
            assert_eq!(state[&0x78], Unsigned(state_card));
            let ratings = Array(ranked.party_ratings(profile, &places).unwrap());
            let member = Struct(Fields::from([(0x68, ratings)]));
            let party = Struct(Fields::from([(0, Array(vec![member]))]));
            messages.push((PARTY, 20700, Fields::from([(0x78, party)])));
            assert_eq!(messages.len(), expected_len);

            // Python Ranked.card_ratings, Ranked.state and a 20700 with only its
            // party member's rating field populated, normalized to wire index one.
            let mut digest = Sha256::new();
            for (crc, id, fields) in messages {
                let mut wire = schemas.header(crc, 1, id).unwrap();
                wire.extend(schemas.encode(crc, id, &fields).unwrap());
                digest.update(crc.to_le_bytes());
                digest.update(id.to_le_bytes());
                digest.update(u32::try_from(wire.len()).unwrap().to_le_bytes());
                digest.update(wire);
            }
            assert_eq!(format!("{:x}", digest.finalize()), expected);
        }

        let card = CARD_BASE | 0x121;
        let places = LeaderboardPlaces::from([(Queue::Tank, 7)]);
        assert_eq!(
            ranked.rank(&season_25, card, TANK, &places).unwrap(),
            Some(MatchRank {
                skill_rating: 3100,
                top_500_place: 7,
                tier: 4
            })
        );
        assert_eq!(
            ranked.rank(&season_25, card, DAMAGE, &places).unwrap(),
            Some(MatchRank::default())
        );
        assert_eq!(
            ranked.rank(&season_25, ROLE_QUEUE, TANK, &places).unwrap(),
            None
        );
    }
}
