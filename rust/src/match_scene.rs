//! Match entity baselines and the shared mode presentation frame.

use std::{collections::HashMap, io};

use crate::{
    bits::BitWriter,
    game_body::yaw_rotation,
    game_bots::Bot,
    game_content::GameMap,
    game_instance::FREE_FOR_ALL_TEAM,
    game_stats::GMP_STATS,
    script_graph::GraphStore,
    script_wire::{self, Instance, Layout, Variables},
    world::{
        Component, ComponentValue, Create, EntityUpdate, GAME_MODE_ENTITY, Operation, PLAYER_PATH,
        SkinTheme, WITH_PARTICIPANT, WITH_SCALE, WITH_TRANSFORM, health,
    },
};

const PLAYER_DEFINITION: u64 = 0x0400_0000_0000_000e;
const GAME_MODE_DEFINITION: u64 = 0x0400_0000_0000_16b2;
const PLAYER_FILTER: u64 = 0x0004_0001;
const TEAM_BIT: u64 = 0x0080_0000;
const MODE_UI: u32 = 0x0cb6;
const MODE_UI_STATE: usize = 21;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Possession {
    pub body: u32,
    pub hero: u64,
    pub skin: Option<u64>,
}

impl Possession {
    pub fn components(self) -> [Component; 2] {
        [
            fields(44, vec![Some(ComponentValue::Entity(self.body)); 3]),
            fields(
                75,
                vec![
                    Some(ComponentValue::Id(self.hero)),
                    self.skin.map(ComponentValue::Id),
                    None,
                    Some(ComponentValue::Id(self.hero)),
                ],
            ),
        ]
    }
}

pub struct PlayerAppearance<'a> {
    pub entity: u32,
    pub slot: i8,
    pub team: u8,
    pub card: &'a PlayerCard,
}

#[derive(Debug, Default)]
pub struct PlayerCard {
    pub account: [u64; 2],
    pub battle_tag: String,
    pub level: Option<u32>,
    pub portrait_frame: Option<u64>,
    pub ranked: Option<(i16, i16, i8)>,
    pub role: Option<i8>,
    pub skins: HashMap<u64, SkinTheme>,
}

pub fn player_create(
    map: &GameMap,
    player: &PlayerAppearance<'_>,
    possession: Option<Possession>,
    owner_board: Option<Component>,
) -> io::Result<EntityUpdate> {
    use ComponentValue::*;
    let card = player.card;
    let team = if map.free_for_all {
        FREE_FOR_ALL_TEAM
    } else {
        player.team
    };
    let mut components = vec![
        fields(26, vec![Some(Id(PLAYER_FILTER | team_bit(team)?))]),
        fields(
            29,
            vec![
                Some(DbId(card.account[0], card.account[1])),
                (!card.battle_tag.is_empty()).then(|| Text(card.battle_tag.clone())),
                card.level.map(U32),
                None,
                None,
                None,
                None,
                card.ranked.map(|rank| I16(rank.0)),
                card.ranked.map(|rank| I16(rank.1)),
                card.ranked.map(|rank| I8(rank.2)),
            ],
        ),
        fields(
            74,
            vec![
                card.portrait_frame.filter(|&guid| guid != 0).map(Id),
                None,
                Some(I8(player.slot)),
                None,
                None,
                card.role.filter(|&role| role != 0).map(I8),
                Some(U8(1)),
                Some(U8(1)),
            ],
        ),
    ];
    if let Some(possession) = possession {
        components.extend(possession.components());
    }
    if let Some(board) = owner_board {
        if board.index != GMP_STATS {
            return Err(invalid("the player board requires component 64"));
        }
        components.push(board);
    }
    let mut definition = Create::new(PLAYER_DEFINITION, components);
    definition.flags = WITH_PARTICIPANT | PLAYER_PATH | WITH_TRANSFORM | WITH_SCALE;
    Ok(create(player.entity, definition))
}

pub fn mode_create(mode: u64) -> EntityUpdate {
    create(
        GAME_MODE_ENTITY,
        Create::new(
            GAME_MODE_DEFINITION,
            vec![fields(114, vec![None, Some(ComponentValue::Id(mode))])],
        ),
    )
}

/// The match freezes each bot's pose and pools when it queues the create.
pub fn bot_create(bot: &Bot, vitals: Option<&crate::vitals::Vitals>) -> io::Result<EntityUpdate> {
    let pools = vitals.map_or_else(
        || health(bot.spawn.health as f32, bot.spawn.health as f32, 0.0, 0.0),
        crate::vitals::Vitals::component,
    );
    let team = fields(
        26,
        vec![Some(ComponentValue::Id(team_bit(bot.spawn.team)?))],
    );
    let mut definition = Create::new(bot.spawn.body, vec![pools, team]);
    definition.position = bot.position.map(|position| position as f32);
    definition.rotation = yaw_rotation(bot.yaw.round_ties_even());
    Ok(create(bot.entity, definition))
}

/// Build the initial world before the local player picks a hero.
pub fn initial_world(
    map: &GameMap,
    player: &PlayerAppearance<'_>,
    board: Component,
    bots: &[Bot],
) -> io::Result<Vec<EntityUpdate>> {
    let mut updates = Vec::with_capacity(map.placeables.len() + 2 + bots.len());
    updates.extend(map.placeables.iter().map(|&index| EntityUpdate {
        entity: 0x8000_0000 | index,
        operation: Some(Operation::Placeable(index)),
        ..EntityUpdate::default()
    }));
    updates.push(player_create(map, player, None, Some(board))?);
    updates.push(mode_create(map.mode_guid));
    for bot in bots {
        updates.push(bot_create(bot, None)?);
    }
    Ok(updates)
}

/// H=0 game mode graph with its client-only UI entry dependencies retained.
pub fn mode_ui(graphs: &GraphStore, variables: &Variables) -> io::Result<BitWriter> {
    let graph = graphs
        .graph(MODE_UI)
        .ok_or_else(|| invalid("missing game mode UI graph 0CB6"))?;
    let state = graph
        .state(MODE_UI_STATE)
        .filter(|node| node.class == "STU_9D7BF987")
        .ok_or_else(|| invalid("missing verified mode UI shared-variable state"))?;
    let slot = graph
        .remote_positions()
        .iter()
        .position(|&pos| pos == state.pos)
        .ok_or_else(|| invalid("mode UI shared-variable state has no remote slot"))?;
    let mut instance = Instance::new(1, Layout::try_from(graph).map_err(invalid)?, None);
    let mut active = BitWriter::new();
    // The verified 9D7BF987 remote-full layout has X/A bits and no payload.
    active.bits(3, 2).map_err(invalid)?;
    instance.states.insert(slot, active);
    script_wire::remote_full(&[instance], variables).map_err(invalid)
}

fn create(entity: u32, definition: Create) -> EntityUpdate {
    EntityUpdate {
        entity,
        operation: Some(Operation::Create(Box::new(definition))),
        ..EntityUpdate::default()
    }
}

fn fields(index: u8, fields: Vec<Option<ComponentValue>>) -> Component {
    Component { index, fields }
}

pub(crate) fn team_bit(team: u8) -> io::Result<u64> {
    if team > FREE_FOR_ALL_TEAM {
        return Err(invalid("invalid team for a player or bot"));
    }
    Ok(TEAM_BIT << team)
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        game_bots::{BOT_ENTITY, BotCatalog, BotWalk},
        game_stats::{PlayerStats, StatsCatalog},
        mover_settings::MoverCatalog,
        script_wire::Value,
        world::{FrameCandidates, MAX_PAYLOAD, frame, select_frame},
    };
    use sha2::{Digest, Sha256};

    fn runtime() -> std::path::PathBuf {
        std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap()).join("data")
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 spawn and stat catalogs"]
    fn matches_the_python_practice_range_create_frame_and_budget() {
        let data = runtime();
        let movers = MoverCatalog::load(data.join("game_movers_174.json")).unwrap();
        let bots = BotCatalog::load(data.join("ai_spawners_174.json")).unwrap();
        let stats = StatsCatalog::load(data.join("game_stats_174.json")).unwrap();
        let map = GameMap::practice_range();
        let bots = bots.make(map.map_guid, |body| {
            let mover = movers.for_body(Some(body));
            BotWalk::from_mover(
                mover.run_forward,
                mover.accel_ground,
                mover.decel_ground_base,
            )
        });
        let card = PlayerCard {
            account: [0x1234_5678, 0x0100_0000_0000_0000],
            battle_tag: "rust#2345".into(),
            level: Some(3000),
            portrait_frame: Some(0x0250_0000_0000_0f97),
            ranked: Some((3200, 120, 4)),
            role: Some(1),
            ..PlayerCard::default()
        };
        let player = PlayerAppearance {
            entity: 0xa000_0100,
            slot: 0,
            team: 1,
            card: &card,
        };
        let board = stats.component(&PlayerStats::default(), map.mode_guid, None, None);
        let updates = initial_world(&map, &player, board, &bots).unwrap();
        let expected: Vec<_> = [
            0x8000_0088,
            0x8000_022e,
            0x8000_0234,
            0x8000_0289,
            player.entity,
            GAME_MODE_ENTITY,
        ]
        .into_iter()
        .chain(BOT_ENTITY..BOT_ENTITY + 15)
        .collect();
        assert_eq!(
            updates
                .iter()
                .map(|update| update.entity)
                .collect::<Vec<_>>(),
            expected
        );
        let encoded = frame(1234, 1234, &[], &[], &updates.iter().collect::<Vec<_>>()).unwrap();
        assert_eq!(encoded.len(), 1674);
        assert_eq!(
            format!("{:x}", Sha256::digest(&encoded)),
            "b82987cb93e2e93851a16452fbb2230637b89edd70cf9f07b9a34c054689d66f",
        );
        let mut remaining = updates.as_slice();
        let mut transmitted = Vec::new();
        while !remaining.is_empty() {
            let selected = select_frame(
                FrameCandidates {
                    tick: 1234,
                    reliable: &[],
                    unreliable: &[],
                    queued: remaining,
                    movements: &[],
                    owner_correction: false,
                    reliable_only: false,
                },
                |_| 1234,
            )
            .unwrap();
            assert!(selected.payload.len() <= MAX_PAYLOAD && !selected.entities.is_empty());
            transmitted.extend(selected.entities.iter().map(|update| update.entity));
            remaining = &remaining[selected.entities.len()..];
        }
        assert_eq!(transmitted, expected);
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 graph bundle"]
    fn mode_ui_preserves_its_shared_variable_state_with_or_without_countdown() {
        let graphs = GraphStore::load(runtime().join("game_graphs_174.json.gz")).unwrap();
        let cases = [
            (Variables::new(), 38, "085b06000c"),
            (
                Variables::from([(15545, Value::Float(3.0))]),
                74,
                "085b0600cbe5a901c000",
            ),
        ];
        for (vars, length, expected) in cases {
            let frame = mode_ui(&graphs, &vars).unwrap();
            let actual: String = frame
                .as_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            assert_eq!((frame.bit_len(), actual.as_str()), (length, expected));
        }
    }
}
