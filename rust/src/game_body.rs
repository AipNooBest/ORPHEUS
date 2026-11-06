//! Frozen player-body creation records for the 104319 match.
//!
//! Owner and observer layouts follow Match._body_builder and Player.pose
//! in ow174/game/match.py.

use std::f64::consts::PI;

use crate::game_heroes::Hero;
use crate::game_pools::PoolCatalog;
use crate::vitals::Vitals;
use crate::world::{
    Component, ComponentValue, Create, EntityUpdate, LeaseBlock, Movement, Operation, SkinTheme,
};

pub const CROUCHED_MOVEMENT_FLAG: u32 = 0x2;

pub struct BodyCreate<'a> {
    pub entity: u32,
    pub hero: &'a Hero,
    /// The body filter bit, including the shared free-for-all bit if applicable.
    pub team_bit: u64,
    pub position: [f64; 3],
    pub yaw: i32,
    pub skin: SkinTheme,
    pub vitals: Option<&'a Vitals>,
}

pub enum BodyView<'a> {
    Owner {
        pose: Option<Movement>,
        /// Frozen from the current body script after its instance is assigned.
        leases: &'a [LeaseBlock],
    },
    Observer,
}

/// Initial owner movement uses its pose and crouch flag. The client's mover
/// continues from this state without an input history or animation counters.
pub fn owner_pose(source: Movement) -> Movement {
    Movement {
        position: source.position,
        yaw: source.yaw,
        pitch: source.pitch,
        velocity: source.velocity,
        flags: source.flags & CROUCHED_MOVEMENT_FLAG,
        ..Movement::default()
    }
}

pub fn yaw_rotation(yaw: f64) -> [f32; 4] {
    let half = yaw * PI / 65536.0;
    let (sin, cos) = half.sin_cos();
    [0.0, sin as f32, 0.0, cos as f32]
}

/// Capture the ordered components, skin and pose once so budget probes and
/// reliable retries serialize the same entity lifetime.
pub fn body_create(input: BodyCreate<'_>, view: BodyView<'_>, pools: &PoolCatalog) -> EntityUpdate {
    let health = input.vitals.map_or_else(
        || pools.body_pools(input.hero.guid).vitals().component(),
        Vitals::component,
    );
    let mut components = vec![
        health,
        Component {
            index: 26,
            fields: vec![Some(ComponentValue::Id(input.team_bit))],
        },
        Component {
            index: 123,
            fields: vec![Some(ComponentValue::Id(input.hero.guid))],
        },
    ];
    let movement = match view {
        BodyView::Owner { pose, leases } => {
            if !leases.is_empty() {
                components.push(Component {
                    index: 90,
                    fields: vec![Some(ComponentValue::Leases(leases.to_vec()))],
                });
            }
            pose
        }
        BodyView::Observer => None,
    };
    let mut definition = Create::new(input.hero.body, components);
    definition.position = input.position.map(|position| position as f32);
    definition.rotation = yaw_rotation(f64::from(input.yaw));
    definition.skin = input.skin;
    definition.movement = movement;
    EntityUpdate {
        entity: input.entity,
        operation: Some(Operation::Create(Box::new(definition))),
        movement,
        ..EntityUpdate::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::game_heroes::SOLDIER;
    use crate::vitals::Death;
    use crate::world::{WITH_SCALE, WITH_TRANSFORM, create, frame};
    use sha2::{Digest, Sha256};

    const BODY: u32 = 0xa000_0101;
    const REINHARDT: u64 = 0x02e0_0000_0000_0007;
    const THEME: u64 = 0x0a50_0000_0000_1234;
    const TEAM_BIT: u64 = 0x80_0000;
    const LEASE_SPAN: u32 = 1 << 22;

    fn leases() -> [LeaseBlock; 2] {
        [4, 5].map(|index| LeaseBlock {
            definition: 0x0400_0000_0000_1180,
            start: index * LEASE_SPAN,
            stop: (index + 1) * LEASE_SPAN,
            first_id: 0x10000,
            per_frame: 1,
        })
    }

    fn definition(update: &EntityUpdate) -> &Create {
        let Some(Operation::Create(definition)) = &update.operation else {
            panic!("expected body create");
        };
        definition
    }

    #[test]
    fn owner_create_captures_pose_golden_skin_and_lease_in_python_order() {
        let hero = Hero {
            guid: SOLDIER,
            name: "Soldier: 76".into(),
            role: "Damage".into(),
            body: 0x0400_0000_0000_03cf,
        };
        let source = Movement {
            position: [54.56640625, 1.125, 42.1474609375],
            yaw: -31010,
            pitch: -1234,
            velocity: [0.125, -0.5, 5.5],
            flags: CROUCHED_MOVEMENT_FLAG | 0x2000,
            throttles: [5, 100],
            input_frame: Some(900),
            input_throttles: [25, 127],
            air_ticks: 7,
            gravity: 1.75,
            frame: Some(899),
            crouch_frame: Some(850),
            spring: [125, 73],
            spring_speed: 2.0,
            fall: 0.25,
        };
        let pose = owner_pose(source);
        assert_eq!(
            pose,
            Movement {
                position: source.position,
                yaw: source.yaw,
                pitch: source.pitch,
                velocity: source.velocity,
                flags: CROUCHED_MOVEMENT_FLAG,
                ..Movement::default()
            }
        );
        let mut lease = leases();
        let update = body_create(
            BodyCreate {
                entity: BODY,
                hero: &hero,
                team_bit: TEAM_BIT << 1,
                position: [54.56507, 1.0, 42.14788],
                yaw: -31000,
                skin: SkinTheme {
                    guid: THEME,
                    golden: true,
                },
                vitals: None,
            },
            BodyView::Owner {
                pose: Some(pose),
                leases: &lease,
            },
            &PoolCatalog::default(),
        );
        assert_eq!(update.entity, BODY);
        assert_eq!(update.movement, Some(pose));
        let definition = definition(&update);
        assert_eq!(definition.flags, WITH_SCALE | WITH_TRANSFORM);
        assert_eq!(definition.frame_back, 1);
        assert_eq!(definition.movement, update.movement);
        assert_eq!(
            definition
                .components
                .iter()
                .map(|component| component.index)
                .collect::<Vec<_>>(),
            [51, 26, 123, 90]
        );
        assert_eq!(
            definition.components[3].fields[0],
            Some(ComponentValue::Leases(lease.to_vec()))
        );
        let record = create(3, definition).unwrap();
        assert_eq!(record.bit_len(), 1365);
        assert_eq!(
            format!("{:x}", Sha256::digest(record.as_bytes())),
            "a157b1187c3679a0e869d69ec935efa82d85d66b92ed5f3616fea8c93fe1ee46"
        );
        let packet = frame(1200, 900, &[], &[], &[&update]).unwrap();
        assert_eq!(
            format!("{:x}", Sha256::digest(&packet)),
            "44d49b099d2766c02592e1dd41acef3d53b1d41c10bd6c9edb173755d683dbab"
        );
        lease[0].start = 9;
        assert_eq!(create(3, definition).unwrap(), record);
        assert_ne!(
            definition.components[3].fields[0],
            Some(ComponentValue::Leases(lease.to_vec()))
        );
    }

    #[test]
    fn observer_create_freezes_damaged_pools_and_omits_pose_and_lease() {
        let hero = Hero {
            guid: REINHARDT,
            name: "Reinhardt".into(),
            role: "Tank".into(),
            body: 0x0400_0000_0000_0024,
        };
        let mut live = Vitals::new(300.0, 200.0, 0.0);
        let hit = Death {
            time: 3.0,
            frame: 1200,
            killer: BODY,
        };
        assert_eq!(live.damage(30.0, hit).dealt, 25.0);
        live.clear_changed();
        let update = body_create(
            BodyCreate {
                entity: 0xa000_0201,
                hero: &hero,
                team_bit: TEAM_BIT,
                position: [4.5, 2.0, -3.25],
                yaw: 16384,
                skin: SkinTheme {
                    guid: 0,
                    golden: true,
                },
                vitals: Some(&live),
            },
            BodyView::Observer,
            &PoolCatalog::default(),
        );
        assert_eq!(update.movement, None);
        let definition = definition(&update);
        assert_eq!(definition.movement, None);
        assert_eq!(
            definition
                .components
                .iter()
                .map(|component| component.index)
                .collect::<Vec<_>>(),
            [51, 26, 123]
        );
        let record = create(5, definition).unwrap();
        assert_eq!(record.bit_len(), 843);
        assert_eq!(
            format!("{:x}", Sha256::digest(record.as_bytes())),
            "79d53aa18f160aa2245a17da9ce7f2ef80ac04dae880f65830f66c9c4d4a1898"
        );
        assert_eq!(live.heal(25.0), 25.0);
        assert_eq!(create(5, definition).unwrap(), record);
        assert_ne!(live.component(), definition.components[0]);
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 hero and graph catalogs"]
    fn all_heroes_match_python_owner_and_observer_create_bytes() {
        use crate::game_heroes::HeroCatalog;
        use crate::script_graph::GraphStore;
        use std::path::{Path, PathBuf};

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let heroes = HeroCatalog::load(root.join("data/game_heroes_174.json")).unwrap();
        let graphs = GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap();
        let pools = PoolCatalog::from_graphs(&graphs).unwrap();
        let leases = leases();
        let mut digest = Sha256::new();
        assert_eq!(heroes.heroes().count(), 32);
        for (index, hero) in heroes.heroes().enumerate() {
            let step = i32::try_from(index).unwrap();
            let pose = Movement {
                position: [
                    f64::from(step) / 8.0 + 1.0,
                    1.0,
                    -4.5 + f64::from(step) / 16.0,
                ],
                yaw: -30000 + step * 1473,
                pitch: 700 - step * 9,
                velocity: [0.25, 0.0, -1.0],
                flags: if index % 3 == 0 {
                    CROUCHED_MOVEMENT_FLAG
                } else {
                    0
                },
                ..Movement::default()
            };
            for own in [false, true] {
                let origin = (index + usize::from(own)) % 8;
                let view = if own {
                    BodyView::Owner {
                        pose: Some(pose),
                        leases: &leases,
                    }
                } else {
                    BodyView::Observer
                };
                let body = body_create(
                    BodyCreate {
                        entity: BODY,
                        hero,
                        team_bit: TEAM_BIT << (index % 2),
                        position: pose.position,
                        yaw: pose.yaw,
                        skin: SkinTheme {
                            guid: THEME,
                            golden: index % 2 == 0,
                        },
                        vitals: None,
                    },
                    view,
                    &pools,
                );
                let record = create(origin, definition(&body)).unwrap();
                digest.update(hero.guid.to_le_bytes());
                digest.update([u8::from(own)]);
                digest.update(u32::try_from(origin).unwrap().to_le_bytes());
                digest.update(u32::try_from(record.bit_len()).unwrap().to_le_bytes());
                digest.update(record.as_bytes());
            }
        }
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "d307ad8c6f86b8e05c30a717afc32b29e2e15022d410f4e5b4572abd1a8f84ab"
        );
    }
}
