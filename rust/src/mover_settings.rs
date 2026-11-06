//! Character mover settings read from the 104319 runtime data.
//!
//! Defaults and body lookup follow ow174/game/mover.py.

use std::{collections::HashMap, fs, io, path::Path};

use indexmap::IndexMap;
use serde::Deserialize;

use crate::accounts::parse_guid;

pub const SOLDIER_BODY: u64 = 0x0400_0000_0000_03cf;

/// Values of a body's STUCharacterMoverComponent.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[cfg_attr(test, derive(serde::Serialize))]
#[serde(default)]
pub struct MoverSettings {
    pub stand_height: f64,
    pub stand_radius: f64,
    pub stand_pogo: f64,
    pub crouch_height: f64,
    pub crouch_radius: f64,
    pub crouch_pogo: f64,
    pub ground_circle_radius: f64,
    pub ground_circle_rays: u32,
    pub pogo_frequency: f64,
    pub pogo_damping: f64,
    /// Degrees.
    pub max_slope: f64,
    pub step_down: f64,
    pub run_forward: f64,
    pub run_backward: f64,
    pub run_strafe: f64,
    pub crouch_forward: f64,
    pub crouch_backward: f64,
    pub crouch_strafe: f64,
    /// Multiply by run speed to get acceleration in m/s².
    pub accel_ground: f64,
    pub accel_air: f64,
    pub accel_air_touching: f64,
    /// Multiply by run speed to get the base braking rate in m/s².
    pub decel_ground_base: f64,
    pub decel_ground_base_speed: f64,
    pub decel_ground_per_speed: f64,
    pub decel_air_base: f64,
    pub decel_air_base_speed: f64,
    pub decel_air_per_speed: f64,
    pub jump_speed: f64,
    pub gravity_scale: f64,
    pub terminal_velocity: f64,
    pub terminal_velocity_up: f64,
    pub ground_gravity_accel: f64,
    pub ground_gravity_decel: f64,
    pub character_collision: u32,
}

static DEFAULT_SETTINGS: MoverSettings = MoverSettings {
    stand_height: 2.0,
    stand_radius: 0.5,
    stand_pogo: 0.5,
    crouch_height: 1.25,
    crouch_radius: 0.5,
    crouch_pogo: 0.25,
    ground_circle_radius: 0.45,
    ground_circle_rays: 8,
    pogo_frequency: 10.0,
    pogo_damping: 1.0,
    max_slope: 45.0,
    step_down: 0.5,
    run_forward: 5.5,
    run_backward: 4.95,
    run_strafe: 5.5,
    crouch_forward: 3.0,
    crouch_backward: 3.0,
    crouch_strafe: 3.0,
    accel_ground: 35.0,
    accel_air: 3.5,
    accel_air_touching: 0.5,
    decel_ground_base: 15.0,
    decel_ground_base_speed: 6.2,
    decel_ground_per_speed: 2.0,
    decel_air_base: 0.0,
    decel_air_base_speed: 5.5,
    decel_air_per_speed: 0.35,
    jump_speed: 6.0,
    gravity_scale: 1.75,
    terminal_velocity: 30.0,
    terminal_velocity_up: 0.0,
    ground_gravity_accel: 0.0,
    ground_gravity_decel: 0.0,
    character_collision: 1,
};

impl Default for MoverSettings {
    fn default() -> Self {
        DEFAULT_SETTINGS
    }
}

#[derive(Debug, Default)]
pub struct MoverCatalog {
    by_body: HashMap<u64, MoverSettings>,
}

impl MoverCatalog {
    /// Read game_movers_174.json from the selected runtime.
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let path = path.as_ref();
        match path.metadata() {
            Ok(metadata) if !metadata.is_file() => Ok(Self::default()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Self::default()),
            Err(error) => Err(error),
            Ok(_) => Self::from_json(&fs::read_to_string(path)?),
        }
    }

    pub fn from_json(text: &str) -> io::Result<Self> {
        let raw: IndexMap<String, MoverSettings> = serde_json::from_str(text)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let by_body = raw
            .into_iter()
            .map(|(body, settings)| Ok((body_guid(&body)?, settings)))
            .collect::<io::Result<_>>()?;
        Ok(Self { by_body })
    }

    /// A missing body falls back to Soldier: 76, then to the built-in defaults.
    /// Missing fields in a present body use their own built-in field defaults.
    pub fn for_body(&self, body: Option<u64>) -> &MoverSettings {
        self.by_body
            .get(&body.unwrap_or_default())
            .or_else(|| self.by_body.get(&SOLDIER_BODY))
            .unwrap_or(&DEFAULT_SETTINGS)
    }
}

fn body_guid(text: &str) -> io::Result<u64> {
    let trimmed = text.trim();
    let digits = trimmed.strip_prefix('+').unwrap_or(trimmed);
    let parsed = match digits.as_bytes() {
        [b'0', b'x' | b'X', ..] => parse_guid(digits),
        [b'_', ..] => None,
        _ => parse_guid(&format!("0x{digits}")),
    };
    parsed.ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("invalid mover body GUID {text:?}"),
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const TRAINING_BODY: u64 = 0x0400_0000_0000_1403;
    const FAR_RUNNER_BODY: u64 = 0x0400_0000_0000_14ed;
    const FRIENDLY_BODY: u64 = 0x0400_0000_0000_143e;

    #[test]
    fn body_fields_override_only_their_built_in_defaults() {
        let catalog = MoverCatalog::from_json(r#"{
            "0x04000000000003CF": {"name":"Fixture Soldier", "run_forward":5.75, "accel_ground":4},
            "0400000000001403": {"name":"Fixture training", "run_forward":4.5, "character_collision":0},
            "0X04000000000014ED": {"name":"Fixture runner", "run_forward":6, "ground_circle_rays":4}
        }"#).unwrap();
        let soldier = catalog.for_body(Some(SOLDIER_BODY));
        assert_eq!(soldier.run_forward, 5.75);
        assert_eq!(soldier.accel_ground, 4.0);
        assert!(std::ptr::eq(catalog.for_body(None), soldier));
        assert!(std::ptr::eq(catalog.for_body(Some(9)), soldier));
        let training = catalog.for_body(Some(TRAINING_BODY));
        assert_eq!(training.run_forward, 4.5);
        assert_eq!(training.accel_ground, MoverSettings::default().accel_ground);
        assert_eq!(training.character_collision, 0);
        let far_runner = catalog.for_body(Some(FAR_RUNNER_BODY));
        assert_eq!(far_runner.run_forward, 6.0);
        assert_eq!(far_runner.ground_circle_rays, 4);
    }

    #[test]
    fn missing_table_uses_defaults_and_malformed_table_is_an_error() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("game_movers_174.json");
        let missing = MoverCatalog::load(&path).unwrap();
        assert_eq!(missing.for_body(None), &MoverSettings::default());
        fs::write(&path, r#"{"0":{"stand_height":1.75}}"#).unwrap();
        let catalog = MoverCatalog::load(&path).unwrap();
        assert_eq!(catalog.for_body(None).stand_height, 1.75);
        assert_eq!(
            catalog.for_body(Some(TRAINING_BODY)),
            &MoverSettings::default()
        );
        for malformed in [
            r#"{"bad guid":{}}"#,
            r#"{"0x5":{"ground_circle_rays":4.5}}"#,
            "{",
        ] {
            fs::write(&path, malformed).unwrap();
            assert_eq!(
                MoverCatalog::load(&path).unwrap_err().kind(),
                io::ErrorKind::InvalidData
            );
        }
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 mover catalog"]
    fn actual_mover_components_match_the_python_reference() {
        use sha2::{Digest, Sha256};
        use std::collections::BTreeMap;
        use std::path::PathBuf;

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let catalog = MoverCatalog::load(root.join("data/game_movers_174.json")).unwrap();
        assert_eq!(catalog.by_body.len(), 38);
        let settings = |body| {
            let values = catalog.for_body(Some(body));
            (
                values.run_forward,
                values.accel_ground,
                values.decel_ground_base,
            )
        };
        assert_eq!(settings(TRAINING_BODY), (4.5, 1.0, 15.0));
        assert_eq!(settings(FAR_RUNNER_BODY), (6.0, 11.0, 6.5));
        assert_eq!(settings(FRIENDLY_BODY), (5.5, 1.0, 15.0));
        assert!(std::ptr::eq(
            catalog.for_body(None),
            catalog.for_body(Some(SOLDIER_BODY))
        ));

        let mut digest = Sha256::new();
        for (body, settings) in catalog.by_body.iter().collect::<BTreeMap<_, _>>() {
            digest.update(body.to_le_bytes());
            let object = serde_json::to_value(settings).unwrap();
            let fields = object
                .as_object()
                .unwrap()
                .iter()
                .collect::<BTreeMap<_, _>>();
            assert_eq!(fields.len(), 34);
            for (key, value) in fields {
                digest.update(key.as_bytes());
                digest.update([0]);
                if matches!(key.as_str(), "ground_circle_rays" | "character_collision") {
                    digest.update(
                        u32::try_from(value.as_u64().unwrap())
                            .unwrap()
                            .to_le_bytes(),
                    );
                } else {
                    digest.update(value.as_f64().unwrap().to_le_bytes());
                }
            }
        }
        // Python mover._movers(), encoded as little-endian f64 and u32 values.
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "cc067b2e1ab557fb6cd92f5ed485ccaf565c99f22394e01133a63d6464507991"
        );
    }
}
