//! Post-login dispatch of the solo Practice Range create-game request.

use std::io;

use crate::{
    accounts::battle_tag,
    collection::Collection,
    game_content::GameMap,
    game_service::{GameService, MatchPlayer},
    jam_codec::{Fields, Value},
    lobby_session::PreparedLogin,
    lobby_transport::LobbyMessage,
    match_handoff::{Handoff, LobbySeat, enter_game, general_channel, match_channel},
    match_scene::PlayerCard,
    scene_assets::{PreparedScene, SceneAssets},
};

pub const GAME_REQUEST: u32 = 0xa6e5_3896;
pub const CREATE_GAME: u32 = 24000;

/// Chat and address data from the authenticated session's owner. The caller
/// supplies this only after confirming the account is playing alone.
pub struct SoloPracticeHandoff<'a> {
    pub match_id: [u64; 2],
    pub host: &'a str,
    pub general: Fields,
    pub match_channel: Fields,
}

impl<'a> SoloPracticeHandoff<'a> {
    pub fn new(match_id: [u64; 2], host: &'a str) -> Self {
        Self {
            match_id,
            host,
            general: general_channel(),
            match_channel: match_channel(match_id),
        }
    }
}

/// Practice uses a creation kind and flags, separate from a matchmaking card.
pub fn is_request((crc, id, fields): &LobbyMessage) -> bool {
    (*crc, *id) == (GAME_REQUEST, CREATE_GAME)
        && matches!(
            fields.get(&0x78),
            Some(Value::Unsigned(2) | Value::Signed(2))
        )
        && matches!(
            fields.get(&0xa8),
            Some(Value::Unsigned(4) | Value::Signed(4))
        )
}

/// Reserve a seat for a decoded post-login request and send its transfer in
/// order. The listener supplies the live login and an opener for this worker,
/// which may capture prepared world data in addition to the Practice Range map.
pub fn dispatch(
    request: &LobbyMessage,
    login: &PreparedLogin,
    game: &GameService,
    input: SoloPracticeHandoff<'_>,
    open_match: impl FnOnce(
        &GameService,
        [u64; 2],
        GameMap,
        Vec<MatchPlayer>,
    ) -> io::Result<Vec<Handoff>>,
    mut send: impl FnMut(&LobbyMessage) -> io::Result<bool>,
) -> io::Result<Option<LobbySeat>> {
    if !is_request(request) {
        return Ok(None);
    }
    let players = vec![MatchPlayer {
        name: login.account.name.clone(),
        team: 0,
        tournament: login.tournament,
        ranked: false,
    }];
    let tickets = open_match(game, input.match_id, GameMap::practice_range(), players)?;
    let [handoff] = match <[Handoff; 1]>::try_from(tickets) {
        Ok(tickets) => tickets,
        Err(tickets) => {
            let mut error =
                io::Error::other("the solo Practice Range requires exactly one UDP seat");
            for ticket in tickets {
                error = release(game, ticket.conn, error);
            }
            return Err(error);
        }
    };
    let result = (|| {
        if handoff.match_id != input.match_id {
            return Err(io::Error::other(
                "the reserved UDP seat is for another match",
            ));
        }
        let [leave, join, ticket] = enter_game(
            input.general,
            input.match_channel,
            &handoff,
            input.host,
            game.local_addr().port(),
        )?;
        send(&leave)?;
        send(&join)?;
        if !send(&ticket)? {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "the client did not announce the game handoff protocol",
            ));
        }
        Ok(())
    })();
    if let Err(error) = result {
        return Err(release(game, handoff.conn, error));
    }
    Ok(Some(LobbySeat {
        account_lo: login.account.account_lo,
        lobby_session: login.identity.session[0],
        handoff,
    }))
}

/// Build the player's world card from the same account and collection used at
/// login, then reserve the prepared map outside the game tick loop.
pub fn dispatch_prepared(
    request: &LobbyMessage,
    login: &PreparedLogin,
    game: &GameService,
    input: SoloPracticeHandoff<'_>,
    assets: &SceneAssets,
    collection: &Collection,
    send: impl FnMut(&LobbyMessage) -> io::Result<bool>,
) -> io::Result<Option<LobbySeat>> {
    dispatch_scene(
        request,
        login,
        game,
        input,
        (assets, collection),
        |map| assets.prepare(map),
        send,
    )
}

pub(crate) fn dispatch_scene(
    request: &LobbyMessage,
    login: &PreparedLogin,
    game: &GameService,
    input: SoloPracticeHandoff<'_>,
    (assets, collection): (&SceneAssets, &Collection),
    prepare: impl FnOnce(GameMap) -> io::Result<PreparedScene>,
    send: impl FnMut(&LobbyMessage) -> io::Result<bool>,
) -> io::Result<Option<LobbySeat>> {
    dispatch(
        request,
        login,
        game,
        input,
        |game, id, map, mut players| {
            let player = players.pop().expect("solo Practice Range player");
            let card = {
                let profile = login.account.profile();
                PlayerCard {
                    account: login.identity.account_id(),
                    battle_tag: battle_tag(&profile.player_name, login.identity.account_lo),
                    level: Some(u32::try_from(profile.level).map_err(io::Error::other)?),
                    portrait_frame: Some(collection.portrait_frame(&profile)),
                    skins: assets
                        .heroes()
                        .heroes()
                        .map(|hero| {
                            collection
                                .equipped_skin(&profile, hero.guid)
                                .map(|skin| (hero.guid, skin))
                        })
                        .collect::<io::Result<_>>()?,
                    ..PlayerCard::default()
                }
            };
            game.open_prepared_practice(id, prepare(map)?, player, card)
                .map(|ticket| vec![ticket])
        },
        send,
    )
}

fn release(game: &GameService, connection: u32, error: io::Error) -> io::Error {
    match game.cancel_handoff(connection) {
        Ok(_) => error,
        Err(release) => io::Error::new(
            error.kind(),
            format!("{error} (could not release the Practice Range seat: {release})"),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{net::UdpSocket, path::PathBuf, sync::Arc, time::Duration};

    use crate::{
        accounts::Accounts,
        game_content::{PRACTICE_RANGE_MAP, PRACTICE_RANGE_MODE},
        game_link::{ACK, KIND_CONTROL, LinkCipher, MAX_DATAGRAM, Packet, SYN},
        jam_codec::Schemas,
        lobby_login::Identity,
        match_handoff::{HANDOFF, HANDOFF_MESSAGE},
    };

    const REQUEST: &[u8] = b"\x02\x00\x04\x00\x00\x00";

    #[test]
    fn checks_the_practice_kind_flags_and_message_identity() {
        let schemas = Schemas::from_json(
            r#"{"A6E53896":{"24000":[
                {"type":2,"off":120,"count":0,"array":false},
                {"type":12,"off":128,"count":0,"array":false},
                {"type":7,"off":168,"count":0,"array":false}
            ]}}"#,
        )
        .unwrap();
        let fields = schemas
            .decode(GAME_REQUEST, CREATE_GAME, REQUEST, true)
            .unwrap();
        assert!(is_request(&(GAME_REQUEST, CREATE_GAME, fields.clone())));
        assert!(!is_request(&(
            GAME_REQUEST,
            CREATE_GAME + 1,
            fields.clone()
        )));
        assert!(!is_request(&(
            GAME_REQUEST + 1,
            CREATE_GAME,
            fields.clone()
        )));
        let mut wrong_flags = fields.clone();
        wrong_flags.insert(0xa8, Value::Signed(12));
        assert!(!is_request(&(GAME_REQUEST, CREATE_GAME, wrong_flags)));
        let mut wrong_kind = fields;
        wrong_kind.insert(0x78, Value::Unsigned(1));
        assert!(!is_request(&(GAME_REQUEST, CREATE_GAME, wrong_kind)));
    }

    fn input(match_id: [u64; 2]) -> SoloPracticeHandoff<'static> {
        SoloPracticeHandoff::new(match_id, "127.0.0.1")
    }

    fn require_unsigned(fields: &Fields, offset: u32) -> u64 {
        let Value::Unsigned(value) = fields[&offset] else {
            panic!("missing unsigned field {offset:#x}");
        };
        value
    }

    fn require_bytes<const N: usize>(fields: &Fields, offset: u32) -> [u8; N] {
        let Value::Array(values) = &fields[&offset] else {
            panic!("missing byte array field {offset:#x}");
        };
        values
            .iter()
            .map(|value| match value {
                Value::Unsigned(byte) => u8::try_from(*byte).unwrap(),
                _ => panic!("expected a byte"),
            })
            .collect::<Vec<_>>()
            .try_into()
            .unwrap()
    }

    #[test]
    #[ignore = "requires authorized 104319 lobby and game schemas in ORPHEUS_RUNTIME_DIR"]
    fn practice_request_hands_off_an_authenticated_udp_seat_and_releases_failed_sends() {
        let data = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let lobby = Schemas::load(data.join("schemas_174.json")).unwrap();
        let game_schemas = Arc::new(Schemas::load(data.join("game_schemas_174.json")).unwrap());
        let mut game = GameService::bind("127.0.0.1:0".parse().unwrap(), game_schemas).unwrap();
        let fields = lobby
            .decode(GAME_REQUEST, CREATE_GAME, REQUEST, true)
            .unwrap();
        let request = (GAME_REQUEST, CREATE_GAME, fields);
        let temporary = tempfile::tempdir().unwrap();
        let accounts = Accounts::new(
            temporary.path().join("profiles"),
            temporary.path().join("seed"),
        )
        .unwrap();
        let account = accounts.get("fixture player").unwrap();
        let login = PreparedLogin {
            identity: Identity::create(account.account_lo, 0x1234).unwrap(),
            account,
            tournament: true,
        };
        let unrelated = (GAME_REQUEST, CREATE_GAME + 1, request.2.clone());
        assert!(
            dispatch(
                &unrelated,
                &login,
                &game,
                input([4, 7]),
                |_, _, _, _| panic!("unrelated request opened a match"),
                |_| panic!("unrelated request sent a message"),
            )
            .unwrap()
            .is_none()
        );

        let match_id = [5, 8];
        let mut messages = Vec::new();
        let seat = dispatch(
            &request,
            &login,
            &game,
            input(match_id),
            |game, id, map, players| {
                assert_eq!(id, match_id);
                assert_eq!(map.map_guid, PRACTICE_RANGE_MAP);
                assert_eq!(map.mode_guid, PRACTICE_RANGE_MODE);
                assert_eq!(map.team_sizes, [1, 0]);
                assert_eq!(players.len(), 1);
                assert_eq!(players[0].name, "fixture player");
                assert!(players[0].tournament);
                assert!(!players[0].ranked);
                game.open_match(id, map, players)
            },
            |(group, id, fields)| {
                let wire = lobby.encode(*group, *id, fields).unwrap();
                let decoded = lobby.decode(*group, *id, &wire, true).unwrap();
                messages.push((*group, *id, decoded));
                Ok(true)
            },
        )
        .unwrap()
        .unwrap();
        assert_eq!(seat.account_lo, login.account.account_lo);
        assert_eq!(seat.lobby_session, 0x1234);
        let chat = 0x5f91_3f6b;
        assert_eq!(
            messages
                .iter()
                .map(|&(crc, id, _)| (crc, id))
                .collect::<Vec<_>>(),
            [(chat, 20404), (chat, 20402), (HANDOFF, HANDOFF_MESSAGE)],
        );
        let Value::Struct(ticket) = &messages[2].2[&0x80] else {
            panic!("handoff ticket missing");
        };
        let connection = u32::try_from(require_unsigned(ticket, 0x28)).unwrap();
        assert_eq!(connection, seat.handoff.conn);
        assert_eq!(
            require_unsigned(ticket, 0x2c),
            u64::from(game.local_addr().port())
        );
        let address = require_bytes::<64>(ticket, 0x2e);
        assert_eq!(&address[..10], b"127.0.0.1\0");
        assert_eq!(require_bytes::<64>(ticket, 0x6e), address);
        assert!(game.handoff_pending(connection).unwrap());
        let cipher = LinkCipher::new(require_bytes(ticket, 0xce), require_bytes(ticket, 0xae));
        let client = UdpSocket::bind("127.0.0.1:0").unwrap();
        client.connect(game.local_addr()).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let syn = Packet {
            conn: connection,
            kind: KIND_CONTROL,
            flags: SYN,
            ..Packet::default()
        };
        client.send(&cipher.seal(&syn).unwrap()).unwrap();
        let mut buffer = [0; MAX_DATAGRAM];
        let count = client.recv(&mut buffer).unwrap();
        let response = cipher.open(&buffer[..count]).unwrap();
        assert_eq!((response.conn, response.flags), (connection, SYN | ACK));
        assert!(!game.handoff_pending(connection).unwrap());

        for (index, failed_id, kind) in [
            (0, 20402, io::ErrorKind::BrokenPipe),
            (1, HANDOFF_MESSAGE, io::ErrorKind::InvalidData),
        ] {
            let mut failed_seat = None;
            let result = dispatch(
                &request,
                &login,
                &game,
                input([6 + index, 8]),
                |game, id, map, players| {
                    let tickets = game.open_match(id, map, players)?;
                    failed_seat = Some(tickets[0].conn);
                    Ok(tickets)
                },
                |(_, id, _)| {
                    if *id == failed_id && kind == io::ErrorKind::BrokenPipe {
                        return Err(io::Error::new(kind, "test socket closed"));
                    }
                    Ok(*id != failed_id)
                },
            );
            let Err(error) = result else {
                panic!("failed handoff should release its seat");
            };
            assert_eq!(error.kind(), kind);
            assert!(!game.handoff_pending(failed_seat.unwrap()).unwrap());
        }
        game.stop().unwrap();
    }
}
