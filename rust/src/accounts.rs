//! Saved profiles and login-name account IDs for the 1.74 lobby.

use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicU8, Ordering},
    },
    time::UNIX_EPOCH,
};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::jam_codec::{Fields, Value as JamValue};

pub const DEFAULT_ICON: u64 = 0x0250_0000_0000_02f7;
pub const BOT_NAME: &str = "Bot";
const UNLOCKS_RESET_MARKER: &str = "unlocks_reset_v2";

// The type order determines the IDs in new profiles.
const BOX_TYPES: [(i32, &str, u64); 11] = [
    (0, "Standard", 50),
    (1, "Summer Games", 10),
    (2, "Halloween Terror", 10),
    (3, "Winter Wonderland", 10),
    (4, "Lunar New Year", 10),
    (5, "Archives", 10),
    (6, "Anniversary", 10),
    (7, "Golden", 10),
    (9, "Legendary Anniversary", 10),
    (10, "Wrecking Ball", 0),
    (12, "Legendary", 10),
];

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct LootBox {
    pub id: u64,
    #[serde(rename = "type")]
    pub box_type: i32,
    pub name: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(default)]
pub struct Profile {
    pub player_name: String,
    pub region: String,
    pub game_region: String,
    pub level: i64,
    pub credits: i64,
    pub comp_points: i64,
    pub league_tokens: i64,
    #[serde(with = "guid")]
    pub icon_guid: u64,
    #[serde(with = "guid")]
    pub frame_guid: u64,
    pub next_box_id: u64,
    pub loot_boxes: Vec<LootBox>,
    pub unlocked_items: Vec<Value>,
    pub unlock_all: bool,
    pub bot_chat: bool,
    pub friends: Vec<String>,
    pub friend_requests: Vec<String>,
    pub last_online: i64,
    pub lobby_hero: String,
    pub events: Vec<String>,
    pub greeted_events: Vec<String>,
    pub server_date: String,
    pub challenge: String,
    pub challenge_wins: i64,
    pub endorsement_level: i64,
    #[serde(deserialize_with = "map_or_empty")]
    pub ratings: Map<String, Value>,
    pub season: i64,
    #[serde(deserialize_with = "map_or_empty")]
    pub matches: Map<String, Value>,
    #[serde(deserialize_with = "map_or_empty")]
    pub wins: Map<String, Value>,
    pub sms_protect: bool,
    pub seasons_seen: Vec<i64>,
    #[serde(deserialize_with = "map_or_empty")]
    pub priority_passes: Map<String, Value>,
    #[serde(deserialize_with = "map_or_empty")]
    pub loadouts: Map<String, Value>,
    #[serde(deserialize_with = "map_or_empty")]
    pub settings: Map<String, Value>,
    #[serde(deserialize_with = "map_or_empty")]
    pub ux_states: Map<String, Value>,
    #[serde(deserialize_with = "map_or_empty")]
    pub stats: Map<String, Value>,
}

impl Default for Profile {
    fn default() -> Self {
        Self {
            player_name: "Researcher".into(),
            region: "US".into(),
            game_region: "americas".into(),
            level: 100,
            credits: 50_000,
            comp_points: 6_000,
            league_tokens: 1_000,
            icon_guid: DEFAULT_ICON,
            frame_guid: 0,
            next_box_id: 1,
            loot_boxes: Vec::new(),
            unlocked_items: Vec::new(),
            unlock_all: false,
            bot_chat: false,
            friends: Vec::new(),
            friend_requests: Vec::new(),
            last_online: 0,
            lobby_hero: "random".into(),
            events: vec!["goodbye".into()],
            greeted_events: Vec::new(),
            server_date: String::new(),
            challenge: String::new(),
            challenge_wins: 0,
            endorsement_level: 1,
            ratings: Map::new(),
            season: 32,
            matches: Map::new(),
            wins: Map::new(),
            sms_protect: true,
            seasons_seen: Vec::new(),
            priority_passes: Map::new(),
            loadouts: Map::new(),
            settings: Map::new(),
            ux_states: Map::new(),
            stats: Map::from_iter(
                [
                    "boxes_opened",
                    "legendaries_dropped",
                    "credits_earned_from_duplicates",
                ]
                .map(|key| (key.into(), Value::from(0))),
            ),
        }
    }
}

impl Profile {
    /// Add boxes without reusing an ID if an old profile's next ID fell behind.
    pub fn add_boxes(&mut self, box_type: i32, count: u64) -> io::Result<Vec<LootBox>> {
        let highest = self
            .loot_boxes
            .iter()
            .map(|item| item.id)
            .max()
            .unwrap_or(0);
        let first = self
            .next_box_id
            .max(highest.checked_add(1).ok_or_else(box_id_overflow)?);
        let end = first.checked_add(count).ok_or_else(box_id_overflow)?;
        let name = box_name(box_type);
        let added: Vec<_> = (first..end)
            .map(|id| LootBox {
                id,
                box_type,
                name: name.clone(),
            })
            .collect();
        self.loot_boxes.extend_from_slice(&added);
        self.next_box_id = end;
        Ok(added)
    }

    pub fn unlocked_guids(&self) -> HashSet<u64> {
        self.unlocked_items
            .iter()
            .filter_map(|value| match value {
                Value::String(text) => parse_guid(text),
                Value::Number(number) => number.as_u64().or_else(|| {
                    let value = number.as_f64()?;
                    (value >= 0.0 && value < u64::MAX as f64).then_some(value as u64)
                }),
                Value::Bool(value) => Some(u64::from(*value)),
                _ => None,
            })
            .collect()
    }
}

pub fn box_name(box_type: i32) -> String {
    BOX_TYPES
        .iter()
        .find(|(id, _, _)| *id == box_type)
        .map_or_else(|| format!("Type {box_type}"), |(_, name, _)| (*name).into())
}

fn box_id_overflow() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "profile exhausted the loot box ID range",
    )
}

pub fn new_profile() -> Profile {
    let mut profile = Profile::default();
    for (box_type, _, count) in BOX_TYPES {
        profile
            .add_boxes(box_type, count)
            .expect("starter box IDs fit");
    }
    profile
}

fn map_or_empty<'de, D>(deserializer: D) -> Result<Map<String, Value>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Ok(Option::deserialize(deserializer)?.unwrap_or_default())
}

mod guid {
    use serde::{Deserialize, Deserializer, Serializer, de::Error};
    use serde_json::Value;

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
        let input = Value::deserialize(deserializer)?;
        input
            .as_u64()
            .or_else(|| input.as_str().and_then(super::parse_guid))
            .ok_or_else(|| D::Error::custom("expected a GUID integer or text"))
    }

    pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&format!("0x{value:016X}"))
    }
}

pub(crate) fn saved_integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|value| value as i64)),
        Value::Bool(value) => Some(i64::from(*value)),
        Value::String(value) => value.trim().parse().ok(),
        _ => None,
    }
}

pub(crate) fn parse_guid(text: &str) -> Option<u64> {
    let text = trim_name(text);
    let text = text.strip_prefix('+').unwrap_or(text);
    let (digits, radix) = match text.as_bytes() {
        [b'0', b'x' | b'X', ..] => (&text[2..], 16),
        [b'0', b'o' | b'O', ..] => (&text[2..], 8),
        [b'0', b'b' | b'B', ..] => (&text[2..], 2),
        _ => (text, 10),
    };
    let digits = if radix == 10 {
        digits
    } else {
        digits.strip_prefix('_').unwrap_or(digits)
    };
    if digits.starts_with('+') || digits.split('_').any(str::is_empty) {
        return None;
    }
    let digits = digits.replace('_', "");
    if radix == 10 && digits.starts_with('0') && digits.bytes().any(|byte| byte != b'0') {
        return None;
    }
    u64::from_str_radix(&digits, radix).ok()
}

fn trim_name(name: &str) -> &str {
    name.trim_matches(|character: char| {
        character.is_whitespace() || matches!(character, '\u{001c}'..='\u{001f}')
    })
}

fn name_key(name: &str) -> String {
    trim_name(name).to_lowercase()
}

pub fn account_id_for(name: &str) -> u32 {
    0x1000_0000 | (crc32fast::hash(name_key(name).as_bytes()) & 0x0fff_ffff)
}

pub fn battle_tag(nickname: &str, account_lo: u32) -> String {
    format!("{}#{}", trim_name(nickname), 1000 + account_lo % 9000)
}

fn json_error(error: serde_json::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

fn read_profile(path: &Path) -> io::Result<(Profile, bool)> {
    let data: Value = serde_json::from_slice(&fs::read(path)?).map_err(json_error)?;
    let migrate = data.get(UNLOCKS_RESET_MARKER) != Some(&Value::Bool(true));
    let mut profile: Profile = serde_json::from_value(data).map_err(json_error)?;
    if migrate {
        profile.unlocked_items.clear();
    }
    Ok((profile, migrate))
}

fn write_profile(profile: &Profile, path: &Path, replace: bool) -> io::Result<()> {
    #[derive(Serialize)]
    struct SavedProfile<'a> {
        #[serde(flatten)]
        profile: &'a Profile,
        unlocks_reset_v2: bool,
    }

    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let bytes = serde_json::to_vec_pretty(&SavedProfile {
        profile,
        unlocks_reset_v2: true,
    })
    .map_err(json_error)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(&bytes)?;
    temporary.as_file().sync_all()?;
    if replace {
        temporary.persist(path)
    } else {
        temporary.persist_noclobber(path)
    }
    .map(|_| ())
    .map_err(|error| error.error)
}

pub fn save_profile(profile: &Profile, path: &Path) -> io::Result<()> {
    write_profile(profile, path, true)
}

fn load_existing_profile(path: &Path) -> io::Result<Profile> {
    match read_profile(path) {
        Ok((profile, migrate)) => {
            if migrate {
                save_profile(&profile, path)?;
            }
            Ok(profile)
        }
        Err(error) if error.kind() == io::ErrorKind::InvalidData => {
            let profile = new_profile();
            save_profile(&profile, path)?;
            Ok(profile)
        }
        Err(error) => Err(error),
    }
}

pub fn load_or_create_profile(path: &Path) -> io::Result<Profile> {
    match load_existing_profile(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            create_profile(new_profile(), path)
        }
        result => result,
    }
}

fn create_profile(profile: Profile, path: &Path) -> io::Result<Profile> {
    match write_profile(&profile, path, false) {
        Ok(()) => Ok(profile),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => load_existing_profile(path),
        Err(error) => Err(error),
    }
}

pub struct Account {
    pub name: String,
    pub account_lo: u32,
    pub path: Option<PathBuf>,
    pub created: i64,
    profile: Mutex<Profile>,
    status: AtomicU8,
}

impl Account {
    fn new(name: String, profile: Profile, path: Option<PathBuf>, created: i64) -> Self {
        Self {
            account_lo: account_id_for(&name),
            name,
            path,
            created,
            profile: Mutex::new(profile),
            status: AtomicU8::new(1),
        }
    }

    pub fn profile(&self) -> MutexGuard<'_, Profile> {
        self.profile
            .lock()
            .expect("account profile lock was poisoned")
    }

    pub fn status(&self) -> u8 {
        self.status.load(Ordering::Relaxed)
    }

    pub fn set_status(&self, status: u8) {
        self.status.store(status, Ordering::Relaxed);
    }

    pub fn is_virtual(&self) -> bool {
        self.path.is_none()
    }

    pub fn battle_tag(&self) -> String {
        battle_tag(&self.profile().player_name, self.account_lo)
    }

    pub fn account_id_fields(&self) -> Fields {
        Fields::from([
            (0, JamValue::Unsigned(u64::from(self.account_lo))),
            (8, JamValue::Unsigned(0x0100_0000_0000_0000)),
        ])
    }

    pub fn save(&self) -> io::Result<()> {
        if let Some(path) = &self.path {
            save_profile(&self.profile(), path)?;
        }
        Ok(())
    }
}

pub struct Accounts {
    directory: PathBuf,
    template: PathBuf,
    by_name: Mutex<HashMap<String, Arc<Account>>>,
    pub bot: Arc<Account>,
}

impl Accounts {
    pub fn new(directory: impl Into<PathBuf>, template: impl Into<PathBuf>) -> io::Result<Self> {
        let directory = directory.into();
        fs::create_dir_all(&directory)?;
        let bot = Profile {
            player_name: BOT_NAME.into(),
            level: 250,
            endorsement_level: 5,
            ..Profile::default()
        };
        Ok(Self {
            directory,
            template: template.into(),
            by_name: Mutex::new(HashMap::new()),
            bot: Arc::new(Account::new(BOT_NAME.into(), bot, None, 0)),
        })
    }

    pub fn get(&self, name: &str) -> io::Result<Arc<Account>> {
        let name = trim_name(name);
        let key = name_key(name);
        let mut accounts = self
            .by_name
            .lock()
            .expect("account registry lock was poisoned");
        if let Some(account) = accounts.get(&key) {
            return Ok(Arc::clone(account));
        }
        let path = self.path_for(name)?;
        let profile = match load_existing_profile(&path) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let mut profile = match read_profile(&self.template) {
                    Ok((profile, _)) => profile,
                    Err(error)
                        if matches!(
                            error.kind(),
                            io::ErrorKind::NotFound | io::ErrorKind::InvalidData
                        ) =>
                    {
                        new_profile()
                    }
                    Err(error) => return Err(error),
                };
                profile.player_name = name.into();
                create_profile(profile, &path)?
            }
            result => result?,
        };
        let created = creation_seconds(&fs::metadata(&path)?);
        let account = Arc::new(Account::new(name.into(), profile, Some(path), created));
        accounts.insert(key, Arc::clone(&account));
        Ok(account)
    }

    pub fn all_saved(&self) -> io::Result<Vec<String>> {
        let mut names: HashSet<_> = self
            .saved_paths()?
            .filter_map(|path| path.file_stem()?.to_str().map(str::to_owned))
            .collect();
        names.extend(
            self.by_name
                .lock()
                .expect("account registry lock was poisoned")
                .values()
                .map(|account| account.name.clone()),
        );
        let mut names: Vec<_> = names.into_iter().collect();
        names.sort_by_cached_key(|name| (name.to_lowercase(), name.clone()));
        Ok(names)
    }

    pub fn by_battle_tag(&self, tag: &str) -> io::Result<Option<Arc<Account>>> {
        let wanted = name_key(tag);
        if !wanted.contains('#') {
            return Ok(None);
        }
        if self.bot.battle_tag().to_lowercase() == wanted {
            return Ok(Some(Arc::clone(&self.bot)));
        }
        for name in self.all_saved()? {
            let account = self.get(&name)?;
            if account.battle_tag().to_lowercase() == wanted {
                return Ok(Some(account));
            }
        }
        Ok(None)
    }

    pub fn by_id(&self, account_lo: u32) -> io::Result<Option<Arc<Account>>> {
        if account_lo == self.bot.account_lo {
            return Ok(Some(Arc::clone(&self.bot)));
        }
        if let Some(account) = self
            .by_name
            .lock()
            .expect("account registry lock was poisoned")
            .values()
            .find(|account| account.account_lo == account_lo)
        {
            return Ok(Some(Arc::clone(account)));
        }
        for name in self.all_saved()? {
            if account_id_for(&name) == account_lo {
                return self.get(&name).map(Some);
            }
        }
        Ok(None)
    }

    fn path_for(&self, name: &str) -> io::Result<PathBuf> {
        let safe: String = name
            .chars()
            .filter(|character| character.is_alphanumeric() || matches!(character, '-' | '_' | ' '))
            .collect();
        let safe = trim_name(&safe);
        let safe = if safe.is_empty() { "player" } else { safe };
        let filename = format!("{safe}.json");
        let path = self.directory.join(&filename);
        if path.try_exists()? {
            return Ok(path);
        }
        let key = filename.to_lowercase();
        // Find profiles created with a different letter case even on case-sensitive filesystems.
        Ok(self
            .saved_paths()?
            .find(|saved| {
                saved
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.to_lowercase() == key)
            })
            .unwrap_or(path))
    }

    fn saved_paths(&self) -> io::Result<impl Iterator<Item = PathBuf>> {
        Ok(fs::read_dir(&self.directory)?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.extension()
                    .is_some_and(|extension| extension.eq_ignore_ascii_case("json"))
                    && path.is_file()
            }))
    }
}

fn creation_seconds(metadata: &fs::Metadata) -> i64 {
    match metadata.created() {
        Ok(created) => match created.duration_since(UNIX_EPOCH) {
            Ok(age) => age.as_secs() as i64,
            Err(error) => -(error.duration().as_secs() as i64),
        },
        #[cfg(unix)]
        Err(_) => std::os::unix::fs::MetadataExt::ctime(metadata),
        #[cfg(not(unix))]
        Err(_) => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn starter_boxes_and_ids_match_the_python_profile() {
        let mut profile = new_profile();
        assert_eq!(profile.loot_boxes.len(), 140);
        assert_eq!(profile.next_box_id, 141);
        assert_eq!(profile.loot_boxes[49].box_type, 0);
        assert_eq!(profile.loot_boxes[50].box_type, 1);
        assert_eq!(profile.loot_boxes[139].box_type, 12);
        profile.next_box_id = 3;
        let added = profile.add_boxes(99, 2).unwrap();
        assert_eq!(
            added[0],
            LootBox {
                id: 141,
                box_type: 99,
                name: "Type 99".into()
            }
        );
        assert_eq!(added[1].id, 142);
        assert_eq!(profile.next_box_id, 143);
    }

    #[test]
    fn reads_old_guid_encodings_and_migrates_unlocks_once() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("old.json");
        fs::write(
            &path,
            br#"{
            "icon_guid": "0x02500000000002F7",
            "frame_guid": 42,
            "ratings": null,
            "unlocked_items": ["0x10"],
            "settings": {"+0x78": {"list": [1, true, "0xA"]}},
            "unknown": 5
        }"#,
        )
        .unwrap();
        let mut profile = load_or_create_profile(&path).unwrap();
        assert_eq!(profile.icon_guid, DEFAULT_ICON);
        assert_eq!(profile.frame_guid, 42);
        assert!(profile.unlocked_items.is_empty());
        assert!(profile.ratings.is_empty());
        assert_eq!(profile.settings["+0x78"], json!({"list": [1, true, "0xA"]}));
        profile.unlocked_items = vec![json!("0x10"), json!(17), json!("bad")];
        save_profile(&profile, &path).unwrap();
        let on_disk: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(on_disk["frame_guid"], "0x000000000000002A");
        assert_eq!(on_disk[UNLOCKS_RESET_MARKER], true);
        assert!(!on_disk.as_object().unwrap().contains_key("unknown"));
        let again = load_or_create_profile(&path).unwrap();
        assert_eq!(again, profile);
        assert_eq!(again.unlocked_guids(), HashSet::from([16, 17]));
    }

    #[test]
    fn accounts_keep_nicknames_and_identity_across_restarts() {
        let directory = tempfile::tempdir().unwrap();
        let template = directory.path().join("template.json");
        save_profile(
            &Profile {
                credits: 123,
                ..Profile::default()
            },
            &template,
        )
        .unwrap();
        let profiles = directory.path().join("profiles");
        let registry = Accounts::new(&profiles, &template).unwrap();
        let account = registry.get("  Alpha  ").unwrap();
        assert_eq!(account.account_lo, 0x10e0_396a);
        assert_eq!(
            account.account_id_fields()[&0],
            JamValue::Unsigned(0x10e0_396a)
        );
        assert_eq!(account.profile().credits, 123);
        assert_eq!(account.profile().player_name, "Alpha");
        assert!(account.created > 0);
        assert!(Arc::ptr_eq(&account, &registry.get("alpha").unwrap()));
        account.profile().player_name = "NewNick".into();
        account.save().unwrap();

        let registry = Accounts::new(profiles, template).unwrap();
        let again = registry.get("alpha").unwrap();
        assert_eq!(again.profile().player_name, "NewNick");
        assert_eq!(again.account_lo, account.account_lo);
        let tag = again.battle_tag();
        assert!(Arc::ptr_eq(
            &again,
            &registry
                .by_battle_tag(&tag.to_uppercase())
                .unwrap()
                .unwrap()
        ));
        assert!(Arc::ptr_eq(
            &again,
            &registry.by_id(again.account_lo).unwrap().unwrap()
        ));
        assert!(registry.bot.is_virtual());
        assert_eq!(registry.bot.profile().level, 250);
    }

    #[test]
    fn a_login_cannot_escape_the_profiles_directory() {
        let directory = tempfile::tempdir().unwrap();
        let profiles = directory.path().join("profiles");
        let registry = Accounts::new(&profiles, directory.path().join("missing.json")).unwrap();
        let account = registry.get("../ A/B\\ ? ").unwrap();
        assert_eq!(
            account.path.as_deref(),
            Some(profiles.join("AB.json").as_path())
        );
        assert_eq!(account.profile().player_name, "../ A/B\\ ?");
        assert_eq!(registry.all_saved().unwrap().len(), 2);
        assert_eq!(account.profile().loot_boxes.len(), 140);
    }
}
