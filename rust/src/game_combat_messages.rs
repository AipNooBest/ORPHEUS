//! Reliable combat notices shared by weapons and damage sources.
//!
//! The field layout follows ow174/game/combat.py for build 104319. Each
//! notice exposes the identity a recipient must know before it is queued.

use crate::{
    game_messages::{self, ChannelError},
    jam_codec::{Fields, Schemas, Value},
};

const HIT: u32 = 25001;
const DEATH: u32 = 25004;
const ELIMINATION: u32 = 25005;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum CombatNotice {
    Hit {
        target: u32,
        dealt: f64,
        critical: bool,
        killed: bool,
        shot_frame: u32,
    },
    Death {
        source: u32,
        killed_by: bool,
    },
    Elimination {
        killer: u32,
        victim: u32,
    },
}

impl CombatNotice {
    pub fn required_identity(self) -> Option<u32> {
        match self {
            Self::Hit { target, .. } => Some(target),
            Self::Death { source, killed_by } => killed_by.then_some(source),
            Self::Elimination { victim, .. } => Some(victim),
        }
    }

    pub fn encode(self, schemas: &Schemas) -> Result<Vec<u8>, ChannelError> {
        let entity = |id: u32| Value::Struct(Fields::from([(0, Value::Unsigned(u64::from(id)))]));
        let (id, fields) = match self {
            Self::Hit {
                target,
                dealt,
                critical,
                killed,
                shot_frame,
            } => (
                HIT,
                Fields::from([
                    (
                        0x78,
                        Value::Struct(Fields::from([
                            (0, entity(target)),
                            (
                                4,
                                Value::Unsigned(u64::from(dealt.round_ties_even().max(1.0) as u32)),
                            ),
                            (
                                8,
                                Value::Unsigned(u64::from(critical) | (u64::from(killed) << 1)),
                            ),
                        ])),
                    ),
                    (0x84, Value::Unsigned(u64::from(shot_frame))),
                ]),
            ),
            Self::Death { source, killed_by } => (
                DEATH,
                Fields::from([
                    (0x78, Value::Unsigned(2 * u64::from(killed_by))),
                    (0x7c, entity(source)),
                ]),
            ),
            Self::Elimination { killer, victim } => (
                ELIMINATION,
                Fields::from([
                    (0x78, Value::Unsigned(0)),
                    (0x80, entity(killer)),
                    (0x84, entity(victim)),
                    (0x88, Value::Unsigned(0)),
                    (0x89, Value::Bool(false)),
                ]),
            ),
        };
        game_messages::encode(schemas, id, &fields)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires the separately prepared 104319 game schemas"]
    fn combat_notice_messages_match_python() {
        const KILLER: u32 = 0xa000_0101;
        const TARGET: u32 = 0xa004_0010;
        let root = std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let schemas = Schemas::load(root.join("data/game_schemas_174.json")).unwrap();
        let cases: [(CombatNotice, Option<u32>, &[u8]); 4] = [
            (
                CombatNotice::Hit {
                    target: TARGET,
                    dealt: 12.5,
                    critical: true,
                    killed: true,
                    shot_frame: 1042,
                },
                Some(TARGET),
                b"\x02\x01\x10\x00\x04\xa0\x0c\x00\x00\x00\x03\x12\x04\x00\x00",
            ),
            (
                CombatNotice::Elimination {
                    killer: KILLER,
                    victim: TARGET,
                },
                Some(TARGET),
                b"\x02\x05\x00\x00\x00\x00\x00\x00\x00\x00\x01\x01\x00\xa0\x10\x00\x04\xa0\x00\x00",
            ),
            (
                CombatNotice::Death {
                    source: KILLER,
                    killed_by: true,
                },
                Some(KILLER),
                b"\x02\x04\x02\x01\x01\x00\xa0",
            ),
            (
                CombatNotice::Death {
                    source: TARGET,
                    killed_by: false,
                },
                None,
                b"\x02\x04\x00\x10\x00\x04\xa0",
            ),
        ];
        for (notice, identity, expected) in cases {
            assert_eq!(notice.required_identity(), identity);
            assert_eq!(notice.encode(&schemas).unwrap(), expected);
        }
    }
}
