//! Match entry and roster messages shared by team modes and free-for-all.

use crate::jam_codec::{
    Fields,
    Value::{Array, String, Struct, Unsigned},
};
use crate::{game_content::GameMap, game_messages::Message};

pub const RETAIL_QUANTA: u32 = 16_000;
pub const TOURNAMENT_QUANTA: u32 = 7_000;
const FREE_FOR_ALL_TEAM: u8 = 4;

pub struct Member<'a> {
    pub entity: u32,
    pub name: &'a str,
    pub team: u8,
    pub gone: bool,
    pub ranked: bool,
}

pub struct Instance<'a> {
    pub map: &'a GameMap,
    pub members: &'a [Member<'a>],
}

impl Instance<'_> {
    fn roster(&self, viewer: u32) -> Fields {
        let playing = || self.members.iter().filter(|member| !member.gone);
        let records = playing()
            .map(|member| Struct(Fields::from([(0x50, String(member.name.into()))])))
            .collect();
        let team = |number, size| {
            let slots = playing()
                .filter(|member| self.map.free_for_all || member.team == number)
                .map(|member| entity(member.entity))
                .collect();
            Struct(Fields::from([
                (0x18, Array(slots)),
                (0x60, Unsigned(u64::from(number))),
                (0x61, Unsigned(u64::from(size))),
            ]))
        };
        let capacity = |team| self.map.team_sizes.get(team).copied().unwrap_or(0);
        let teams = if self.map.free_for_all {
            vec![team(FREE_FOR_ALL_TEAM, capacity(0))]
        } else {
            vec![team(0, capacity(0)), team(1, capacity(1))]
        };
        let roster = Fields::from([
            (0, Struct(Fields::from([(0, Array(records))]))),
            (0xb0, Unsigned(self.map.mode_guid)),
            (0xc0, Array(teams)),
            (0xd8, Array(vec![entity(viewer)])),
        ]);
        Fields::from([(
            0,
            Struct(Fields::from([(0, Struct(roster)), (0x274, Unsigned(16))])),
        )])
    }

    pub fn load(&self, viewer: u32, quanta: u32) -> Message {
        // A competitive roster keeps its instance type after a ranked player leaves.
        let ranked = self.members.iter().any(|member| member.ranked);
        Message {
            id: 20300,
            fields: Fields::from([
                (0x78, Unsigned(if ranked { 9 } else { 1 })),
                (0x80, Struct(self.roster(viewer))),
                (0x4a8, Unsigned(self.map.map_guid)),
                (0x500, Unsigned(u64::from(quanta))),
                (
                    0x508,
                    Struct(Fields::from([(0, Unsigned(1)), (8, Unsigned(1))])),
                ),
            ]),
        }
    }

    pub fn update(&self, viewer: u32) -> Message {
        Message {
            id: 20301,
            fields: Fields::from([(0x78, Struct(self.roster(viewer)))]),
        }
    }
}

pub fn ownership(viewer: u32) -> Message {
    Message {
        id: 20302,
        fields: Fields::from([
            (0x78, entity(viewer)),
            (0x7c, crate::jam_codec::Value::Bool(false)),
            (0x80, Unsigned(0)),
        ]),
    }
}

fn entity(id: u32) -> crate::jam_codec::Value {
    Struct(Fields::from([(0, Unsigned(u64::from(id)))]))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn members() -> [Member<'static>; 3] {
        [
            Member {
                entity: 0x1000,
                name: "Blue",
                team: 0,
                gone: false,
                ranked: false,
            },
            Member {
                entity: 0x2000,
                name: "Retired",
                team: 1,
                gone: true,
                ranked: true,
            },
            Member {
                entity: 0x3000,
                name: "Red",
                team: 1,
                gone: false,
                ranked: false,
            },
        ]
    }

    #[test]
    fn instance_preserves_ranked_type_after_a_member_leaves() {
        let members = members();
        let map = GameMap::practice_range();
        let instance = Instance {
            map: &map,
            members: &members,
        };
        let initial = instance.load(members[0].entity, TOURNAMENT_QUANTA);
        assert_eq!(initial.fields[&0x78], Unsigned(9));
        assert_eq!(initial.fields[&0x500], Unsigned(7000));
        assert_eq!(
            instance.update(members[0].entity).fields[&0x78],
            initial.fields[&0x80]
        );
        let active_only = Instance {
            members: &members[..1],
            ..instance
        };
        assert_eq!(
            active_only.load(members[0].entity, RETAIL_QUANTA).fields[&0x78],
            Unsigned(1)
        );
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 game schemas"]
    fn map_load_and_roster_updates_match_python_for_teams_and_free_for_all() {
        use crate::{game_messages::encode, jam_codec::Schemas};
        use sha2::{Digest, Sha256};

        let root = std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let schemas = Schemas::load(root.join("data/game_schemas_174.json")).unwrap();
        let members = members();
        let practice_range = GameMap::practice_range();
        let free_for_all = GameMap {
            map_guid: 12,
            mode_guid: 34,
            team_sizes: vec![8],
            free_for_all: true,
            ..GameMap::practice_range()
        };
        let cases = [
            (
                Instance {
                    map: &practice_range,
                    members: &members,
                },
                [
                    (
                        513,
                        "96c8620fdd4430e82bdf363c9f1d07dd405581ad7ffd68bda00e13926a9374c3",
                    ),
                    (
                        438,
                        "8b2d19c3c9379c478ce04590af1633e8882ddde0fe26ced16112d1a5dcb62ee9",
                    ),
                ],
            ),
            (
                Instance {
                    map: &free_for_all,
                    members: &members,
                },
                [
                    (
                        494,
                        "4dbcdc55773344208c6cb95ea98d4d8efda57add102a373e4314b4547babe923",
                    ),
                    (
                        419,
                        "59330c72bf73f50b987fe7fe59cb4607e486f4582add1704cec5f035df24c0bf",
                    ),
                ],
            ),
        ];
        for (instance, expected) in cases {
            let messages = [
                instance.load(members[0].entity, RETAIL_QUANTA),
                instance.update(members[2].entity),
            ];
            for (message, (length, digest)) in messages.into_iter().zip(expected) {
                let bytes = encode(&schemas, message.id, &message.fields).unwrap();
                assert_eq!(bytes.len(), length);
                assert_eq!(format!("{:x}", Sha256::digest(bytes)), digest);
            }
        }
    }
}
