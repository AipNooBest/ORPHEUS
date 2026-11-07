//! Accepted lobby connections and the login preparation shared with the session.
//!
//! This module assembles the currently ported initial messages for verification.
//! The complete login and post-login handlers still have to be integrated.

use std::{
    collections::BTreeMap,
    io,
    net::{SocketAddr, TcpStream},
    sync::Arc,
    time::{Duration, Instant},
};

use crate::{
    accounts::{Account, Accounts, account_id_for, battle_tag},
    battle_tag_query,
    collection::{Collection, HERO_CATALOG, PROGRESSION},
    jam_codec::{Fields, Schemas, Value},
    jam_handshake::handshake_io,
    lobby_arcade::Arcade,
    lobby_celebrations::Celebrations,
    lobby_clock::server_time,
    lobby_login::{self, INCOMPATIBLE_CLIENT, Identity, LOGIN_MESSAGE, Login, OUT_CONNECT},
    lobby_mode_rules::{self, MODE_RULES, RULES},
    lobby_party::{self, Membership, PartyMember},
    lobby_passes, lobby_player_messages as player,
    lobby_presence::Presence,
    lobby_ranked::{LeaderboardPlaces, Ranked},
    lobby_transport::{Event, LobbyMessage, LobbyTransport},
    retail_templates::{IN_CONNECT, LOBBY, RetailTemplates},
    service_tcp::PeerStream,
};

const HELLO_TIMEOUT: Duration = Duration::from_secs(30);
const DISCONNECT: u32 = 20503;
const PING: u32 = 0xdda5_83ef;

pub struct LobbySession<'a> {
    transport: LobbyTransport,
    schemas: &'a Schemas,
    local: bool,
    authenticated: bool,
}

pub enum LoginRequest {
    Prepared(PreparedLogin),
    Rejected { reason: u64 },
    Disconnected,
}

pub struct PreparedLogin {
    pub account: Arc<Account>,
    pub identity: Identity,
    pub tournament: bool,
}

#[derive(Clone, Copy)]
pub struct LoginContent<'a> {
    pub menu_hero: Option<u64>,
    pub extra_skins: &'a [u64],
    pub wall_time: u64,
    pub arcade: &'a Arcade,
    pub celebrations: Option<&'a Celebrations>,
    pub leaderboard_places: &'a LeaderboardPlaces,
    /// A confirmed single-member party; the caller sends other party rosters.
    pub solo_party: Option<SoloLoginParty>,
}

#[derive(Clone, Copy)]
pub struct SoloLoginParty {
    pub id: [u64; 2],
    pub entity: [u64; 2],
    pub menu_hero: u64,
    pub members_may_invite: bool,
}

impl<'a> LobbySession<'a> {
    /// Handle a launcher BattleTag query, or open the encrypted game connection.
    pub fn accept(
        stream: TcpStream,
        connection_id: u32,
        signing_key: &[u8; 64],
        accounts: &Accounts,
        schemas: &'a Schemas,
    ) -> io::Result<Option<Self>> {
        Self::accept_peer(
            PeerStream::new(stream, Arc::default())?,
            connection_id,
            signing_key,
            accounts,
            schemas,
        )
    }

    pub(crate) fn accept_peer(
        mut stream: PeerStream,
        connection_id: u32,
        signing_key: &[u8; 64],
        accounts: &Accounts,
        schemas: &'a Schemas,
    ) -> io::Result<Option<Self>> {
        let local = stream.peer_addr()?.ip().is_loopback();
        let SocketAddr::V4(address) = stream.local_addr()? else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "the lobby handshake requires IPv4",
            ));
        };
        stream.set_deadline(Some(Instant::now() + HELLO_TIMEOUT));
        if battle_tag_query::answer_peer(&mut stream, |name| resolve_battle_tag(accounts, name))? {
            return Ok(None);
        }
        let channel = handshake_io(&mut stream, address, connection_id, signing_key)?;
        stream.set_deadline(Some(Instant::now() + HELLO_TIMEOUT));
        let transport = LobbyTransport::from_peer(stream, channel);
        Ok(Some(Self {
            transport,
            schemas,
            local,
            authenticated: false,
        }))
    }

    /// Wait for a valid login in the negotiated group. The account and session
    /// identity are available to the caller before any online state is changed.
    pub fn prepare_login(
        &mut self,
        accounts: &Accounts,
        local_default: impl FnOnce() -> io::Result<Arc<Account>>,
    ) -> io::Result<LoginRequest> {
        self.schemas
            .fields(OUT_CONNECT, LOGIN_MESSAGE)
            .map_err(invalid)?;
        let Some((_, _, fields)) =
            self.receive_matching(|crc, id| (crc, id) == (OUT_CONNECT, LOGIN_MESSAGE), || true)?
        else {
            return Ok(LoginRequest::Disconnected);
        };
        let (account, tournament) = match lobby_login::identify(&fields, self.local)? {
            Login::Named { name, tournament } => (accounts.get(&name)?, tournament),
            Login::LocalDefault => (local_default()?, false),
            Login::IncompatibleRemote => {
                return Ok(LoginRequest::Rejected {
                    reason: INCOMPATIBLE_CLIENT,
                });
            }
        };
        let identity = Identity::create(account.account_lo, self.transport.sequence)?;
        self.transport.set_deadline(None);
        self.authenticated = true;
        Ok(LoginRequest::Prepared(PreparedLogin {
            account,
            identity,
            tournament,
        }))
    }

    /// Receive the next valid client message after accepting the session login.
    /// Client messages use the single-byte group offset.
    pub fn receive_message(&mut self) -> io::Result<Option<LobbyMessage>> {
        self.receive_message_until(|| true)
    }

    pub(crate) fn receive_message_until(
        &mut self,
        keep_receiving: impl Fn() -> bool,
    ) -> io::Result<Option<LobbyMessage>> {
        if !self.authenticated {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "lobby session has not accepted a login",
            ));
        }
        self.receive_matching(|_, _| true, keep_receiving)
    }

    fn receive_matching(
        &mut self,
        accept: impl Fn(u32, u32) -> bool,
        keep_receiving: impl Fn() -> bool,
    ) -> io::Result<Option<LobbyMessage>> {
        while let Some(event) = self.transport.receive_until(&keep_receiving)? {
            let Event::Message { crc, offset, body } = event else {
                continue;
            };
            let Ok(base) = self.schemas.base(crc) else {
                continue;
            };
            let Some(id) = base.checked_add(u32::from(offset)) else {
                continue;
            };
            if !accept(crc, id) {
                continue;
            }
            let Ok(fields) = self.schemas.decode(crc, id, &body, true) else {
                continue;
            };
            return Ok(Some((crc, id, fields)));
        }
        Ok(None)
    }

    /// Send an outbound JAM message with the wire index announced by this client.
    /// Returns false if the client did not announce its group.
    pub fn send(&mut self, (crc, id, fields): &LobbyMessage) -> io::Result<bool> {
        let Some(wire) = self.transport.wire_for(*crc) else {
            return Ok(false);
        };
        let mut frame = self.schemas.header(*crc, wire, *id).map_err(invalid)?;
        frame.extend(self.schemas.encode(*crc, *id, fields).map_err(invalid)?);
        self.transport.send(&frame)?;
        Ok(true)
    }

    /// Send the disconnect reason. The caller manages the grace period and socket shutdown.
    pub fn send_disconnect(&mut self, reason: u64) -> io::Result<bool> {
        self.send(&(
            IN_CONNECT,
            DISCONNECT,
            Fields::from([(0x78, Value::Unsigned(reason)), (0x80, Value::Bool(false))]),
        ))
    }
}

impl PreparedLogin {
    /// Build the verified subset of the initial login messages, in relative wire
    /// order. The caller must add the unported messages before serving retail.
    pub fn available_messages(
        &self,
        schemas: &Schemas,
        templates: &RetailTemplates,
        collection: &Collection,
        presence: &Presence,
        ranked: &Ranked,
        content: LoginContent<'_>,
    ) -> io::Result<Vec<LobbyMessage>> {
        let profile = self.account.profile();
        let identity = self.identity;
        let now = server_time(&profile.server_date, content.wall_time)?;
        let record = player::record(
            &profile,
            identity,
            collection.portrait_frame(&profile),
            content.wall_time,
        );
        let mut messages = vec![
            (IN_CONNECT, 20500, player::hello(&profile, identity)),
            (
                IN_CONNECT,
                20502,
                Fields::from([(0x78, Value::Struct(record))]),
            ),
        ];
        let preload = (IN_CONNECT, 20505, templates.preload(content.extra_skins)?);
        if let Some(celebrations) = content.celebrations {
            let [keys, records, progress] =
                celebrations.login_messages(&profile, collection.items(), now)?;
            messages.extend([keys, preload, records, progress]);
        } else {
            messages.push(preload);
        }
        messages.extend(player::endorsements(&profile, identity, content.wall_time));
        messages.push(presence.own(&profile, identity, content.wall_time)?);
        messages.extend([
            (PROGRESSION, 24300, collection.progression(&profile)?),
            (HERO_CATALOG, 24900, collection.hero_catalog(&profile)?),
        ]);
        let season = ranked.season(&profile);
        messages.push((
            MODE_RULES,
            RULES,
            lobby_mode_rules::mode_rules(
                templates,
                &profile,
                collection.heroes(),
                collection.items(),
                season,
            )?,
        ));
        let arcade = content.arcade.state(&profile, season, now)?;
        let mut built = BTreeMap::from([((LOBBY, 20812), player::ux_states(&profile)?)]);
        for (crc, id, fields) in arcade
            .messages
            .into_iter()
            .chain([lobby_passes::counts(&profile)?])
        {
            built.insert((crc, id), fields);
        }
        messages.extend(templates.at_login(&profile, identity, content.menu_hero, now, &built)?);
        messages.extend(ranked.login_messages(
            &profile,
            &arcade.competitive_cards,
            content.leaderboard_places,
            now,
        )?);
        messages.push((
            PING,
            35500,
            Fields::from([
                (0x78, Value::Unsigned(0)),
                (0x7c, Value::Unsigned(0)),
                (0x80, Value::Array(Vec::new())),
            ]),
        ));
        if let Some(party) = content.solo_party {
            let ratings = ranked.party_ratings(&profile, content.leaderboard_places)?;
            let member = PartyMember {
                profile: &profile,
                identity,
                menu_hero: party.menu_hero,
                ratings: &ratings,
                membership: Membership::Leader,
            };
            messages.push(lobby_party::state(
                collection,
                party.id,
                party.entity,
                &[member],
                party.members_may_invite,
                content.wall_time,
            )?);
        }
        messages.push((LOBBY, 20802, player::settings(schemas, &profile)?));
        Ok(messages)
    }
}

fn resolve_battle_tag(accounts: &Accounts, name: &str) -> String {
    let id = account_id_for(name);
    if let Ok(Some(account)) = accounts.by_id(id) {
        return account.battle_tag();
    }
    battle_tag(name, id)
}

fn invalid(error: crate::jam_codec::CodecError) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        net::{Ipv4Addr, TcpListener},
        path::PathBuf,
        thread,
    };

    use hmac::{Hmac, Mac};
    use serde_json::json;
    use sha2::{Digest, Sha256};

    use crate::{
        catalog::ItemDb,
        jam_cipher::Jam,
        jam_framing::{self, FrameReader},
        lobby_practice::{GAME_REQUEST, is_request},
        lobby_presence::FRIENDS,
        lobby_ranked::{COMPETITIVE_CTF, Queue, RANKED},
    };

    #[test]
    fn answers_battle_tag_queries_without_creating_unknown_accounts() {
        let temporary = tempfile::tempdir().unwrap();
        let accounts = Accounts::new(
            temporary.path().join("profiles"),
            temporary.path().join("seed"),
        )
        .unwrap();
        let local = accounts.get("account").unwrap();
        local.profile().player_name = "Nickname".into();
        let expected = local.battle_tag();

        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let worker = thread::spawn(move || {
            let schemas = Schemas::from_json("{}").unwrap();
            for _ in 0..2 {
                let (stream, _) = listener.accept().unwrap();
                assert!(
                    LobbySession::accept(stream, 1, &[0; 64], &accounts, &schemas)
                        .unwrap()
                        .is_none()
                );
            }
            assert_eq!(accounts.all_saved().unwrap(), ["account"]);
        });
        assert_eq!(battle_tag_query::ask(address, "account").unwrap(), expected);
        assert_eq!(
            battle_tag_query::ask(address, "other").unwrap(),
            battle_tag("other", account_id_for("other"))
        );
        worker.join().unwrap();
    }

    #[test]
    fn negotiates_pings_and_accepts_practice_messages_after_login() {
        let temporary = tempfile::tempdir().unwrap();
        let accounts = Accounts::new(
            temporary.path().join("profiles"),
            temporary.path().join("seed"),
        )
        .unwrap();
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let worker = thread::spawn(move || {
            let schemas = Schemas::from_json(
                r#"{
                    "3C7E3468":{
                        "21799":[],
                        "21800":[{"type":12,"off":120,"count":0,"array":false}]
                    },
                    "F5FF548A":{
                        "20500":[{"type":12,"off":168,"count":0,"array":false}],
                        "20503":[{"type":8,"off":120,"count":0,"array":false},
                                 {"type":0,"off":128,"count":0,"array":false}]
                    },
                    "A6E53896":{"24000":[
                        {"type":2,"off":120,"count":0,"array":false},
                        {"type":12,"off":128,"count":0,"array":false},
                        {"type":7,"off":168,"count":0,"array":false}
                    ]
                    }
                }"#,
            )
            .unwrap();
            let (stream, _) = listener.accept().unwrap();
            let mut session = LobbySession::accept(stream, 2, &[42; 64], &accounts, &schemas)
                .unwrap()
                .unwrap();
            assert_eq!(
                session.receive_message().unwrap_err().kind(),
                io::ErrorKind::PermissionDenied
            );
            let LoginRequest::Prepared(prepared) = session
                .prepare_login(&accounts, || panic!("named login used the local default"))
                .unwrap()
            else {
                panic!("a valid login was not prepared")
            };
            assert!(prepared.tournament);
            assert_eq!(prepared.account.name, "player");
            assert_eq!(prepared.identity.account_lo, prepared.account.account_lo);
            assert_eq!(
                prepared.identity.session[0],
                u64::from(session.transport.sequence)
            );
            let profile = prepared.account.profile();
            assert_eq!(profile.last_online, 0);
            assert!(
                session
                    .send(&(
                        IN_CONNECT,
                        20500,
                        player::hello(&profile, prepared.identity),
                    ))
                    .unwrap()
            );
            assert!(
                !session
                    .send(&(player::PERMISSIONS, 55500, player::features()))
                    .unwrap()
            );
            let practice = session.receive_message().unwrap().unwrap();
            assert!(is_request(&practice));
            assert_eq!(practice.2[&0x80], Value::String(String::new()));
            assert!(session.send_disconnect(INCOMPATIBLE_CLIENT).unwrap());
        });

        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let (mut tx, rx) = handshake(&mut client);
        let mut announcement = vec![0, 0, 4, 0, 0, 0];
        for group in [
            IN_CONNECT,
            OUT_CONNECT,
            player::PERMISSIONS + 1,
            GAME_REQUEST,
        ] {
            announcement.extend_from_slice(&group.to_le_bytes());
        }
        let mut outgoing = Vec::new();
        for frame in [
            b"\x02\x01premature\0".as_slice(),
            &announcement,
            &jam_framing::PONG,
            b"\x04\0\x02early\0\x04\0\0\0",
            b"\x03\x01wrong group\0",
            b"\x02\x00wrong message\0",
            b"\x02\x01unterminated",
            b"\x02\x01trailing byte\0X",
            b"\x02\x01 player \0",
            b"\x04\0\x02\0\x04\0\0\0X",
            b"\x04\0\x02\0\x04\0\0\0",
        ] {
            jam_framing::send_frame(&mut outgoing, &mut tx, frame).unwrap();
        }
        client.write_all(&outgoing).unwrap();

        let mut data = Vec::new();
        client.read_to_end(&mut data).unwrap();
        let mut expected_hello = vec![1, 0];
        expected_hello.extend_from_slice(battle_tag("player", account_id_for("player")).as_bytes());
        expected_hello.push(0);
        let mut disconnect = vec![1, 3];
        disconnect.extend_from_slice(&INCOMPATIBLE_CLIENT.to_le_bytes());
        disconnect.push(0);
        assert_eq!(
            FrameReader::new(rx).feed(&data),
            [
                jam_framing::CONNECTED.to_vec(),
                jam_framing::PONG.to_vec(),
                expected_hello,
                disconnect,
            ]
        );
        worker.join().unwrap();
    }

    fn handshake(client: &mut TcpStream) -> (Jam, Jam) {
        fn proof(first: &[u8], second: &[u8]) -> [u8; 32] {
            let mut mac = Hmac::<Sha256>::new_from_slice(&[0; 32]).unwrap();
            mac.update(first);
            mac.update(second);
            mac.finalize().into_bytes().into()
        }

        client.write_all(b"HELLO PRO CLIENT\0").unwrap();
        let mut hello = [0; 17];
        client.read_exact(&mut hello).unwrap();
        assert_eq!(&hello, b"HELLO PRO SERVER\0");
        let nonce = [0x83; 40];
        client.write_all(&nonce).unwrap();
        let mut challenge = [0; 65];
        client.read_exact(&mut challenge).unwrap();
        let mut answer = [0; 40];
        let client_proof = proof(&nonce[8..], &challenge[33..]);
        answer[8..].copy_from_slice(&client_proof);
        client.write_all(&answer).unwrap();
        let mut state = [0; 324];
        client.read_exact(&mut state).unwrap();
        let server_proof = proof(&challenge[33..], &nonce[8..]);
        assert_eq!(&state[..32], &server_proof);
        let mut rx = Jam::new(&client_proof);
        rx.crypt_in_place(&mut state[32..]);
        (Jam::new(&server_proof), rx)
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn native_server_logs_in_saved_profile_and_loads_practice_over_udp() {
        use crate::{
            accounts::load_or_create_profile,
            bits::BitReader,
            game_content::PRACTICE_RANGE_MAP,
            game_heroes::SOLDIER,
            game_link::{ACK, KIND_FRAME, LinkCipher, MAX_DATAGRAM, Packet, SYN},
            game_messages::read_channel,
            lobby_gallery::{EQUIP, EQUIPPED, GALLERY_OUT},
            lobby_practice::CREATE_GAME,
            match_handoff::{HANDOFF, HANDOFF_MESSAGE},
            scene_assets::SceneAssets,
            server::{Server, ServerConfig},
        };
        use std::{fs, net::UdpSocket, time::Instant};

        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let state = tempfile::tempdir().unwrap();
        let mut config = ServerConfig::new(runtime.clone(), state.path().into());
        config.lobby_port = 0;
        config.game_port = 0;
        config.stall_port = 0;
        config.rpc_port = 0;
        if !SceneAssets::load(&runtime)
            .unwrap()
            .prepare(crate::game_content::GameMap::practice_range())
            .unwrap()
            .movement_ready()
        {
            assert!(
                matches!(Server::start(config), Err(error) if error.kind() == io::ErrorKind::Unsupported)
            );
            assert!(!state.path().join("profiles").exists());
            return;
        }
        let accounts = Accounts::new(
            state.path().join("profiles"),
            state.path().join("profile.json"),
        )
        .unwrap();
        accounts.get("earlier").unwrap();
        let rust = accounts.get("rust").unwrap();
        {
            let mut profile = rust.profile();
            profile.level = 3000;
            profile.endorsement_level = 5;
            profile.unlock_all = true;
        }
        rust.save().unwrap();
        fs::write(state.path().join("default_account.txt"), "RUST\n").unwrap();
        let schemas = Schemas::load(runtime.join("data/schemas_174.json")).unwrap();
        let game_schemas = Schemas::load(runtime.join("data/game_schemas_174.json")).unwrap();
        let collection = Collection::new(
            &RetailTemplates::load(runtime.join("data/retail_templates.json")).unwrap(),
            Arc::new(ItemDb::load(&runtime.join("data")).unwrap()),
        )
        .unwrap();
        let original_skin = collection.equipped_skin(&rust.profile(), SOLDIER).unwrap();
        let skin = collection
            .owned_for_hero(&rust.profile(), SOLDIER)
            .into_iter()
            .filter_map(|guid| collection.items().get(guid))
            .find(|item| {
                item.item_type == "Skin"
                    && item.skin_theme != 0
                    && item.skin_theme != original_skin.guid
            })
            .unwrap();
        let equip = Fields::from([
            (0x78, Value::Unsigned(SOLDIER)),
            (0x80, Value::Unsigned(skin.guid)),
            (0x88, Value::Signed(0)),
        ]);
        let mut server = Server::start(config).unwrap();
        for address in [server.stall_addr(), server.rpc_addr()] {
            assert!(address.ip().is_loopback());
            TcpStream::connect(address).unwrap();
        }
        let lobby = server.lobby_addr();
        let mut stalled = TcpStream::connect(lobby).unwrap();
        stalled.write_all(b"HELLO").unwrap();
        assert_eq!(
            battle_tag_query::ask(lobby, "rust").unwrap(),
            rust.battle_tag()
        );

        let mut tcp = TcpStream::connect(lobby).unwrap();
        tcp.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let (mut tx, rx) = handshake(&mut tcp);
        let groups = [
            IN_CONNECT,
            OUT_CONNECT,
            GAME_REQUEST,
            player::ENDORSEMENTS,
            PROGRESSION,
            HERO_CATALOG,
            HANDOFF,
            GALLERY_OUT,
        ];
        let mut announcement = vec![0, 0];
        announcement.extend_from_slice(&(groups.len() as u32).to_le_bytes());
        for group in groups {
            announcement.extend_from_slice(&group.to_le_bytes());
        }
        jam_framing::send_frame(&mut tcp, &mut tx, &announcement).unwrap();
        for (crc, wire, id, fields) in [
            (OUT_CONNECT, 2, LOGIN_MESSAGE, Fields::new()),
            (
                GAME_REQUEST,
                3,
                CREATE_GAME,
                Fields::from([
                    (0x78, Value::Unsigned(2)),
                    (0x80, Value::String(String::new())),
                    (0xa8, Value::Unsigned(4)),
                ]),
            ),
            (GALLERY_OUT, 8, EQUIP, equip.clone()),
        ] {
            let mut frame = schemas.header(crc, wire, id).unwrap();
            frame.extend(schemas.encode(crc, id, &fields).unwrap());
            jam_framing::send_frame(&mut tcp, &mut tx, &frame).unwrap();
        }

        let wanted = [
            (IN_CONNECT, 1, 20500),
            (player::ENDORSEMENTS, 4, 52002),
            (PROGRESSION, 5, 24300),
            (HERO_CATALOG, 6, 24900),
            (HANDOFF, 7, HANDOFF_MESSAGE),
            (HERO_CATALOG, 6, EQUIPPED),
        ];
        let mut received = std::collections::HashMap::new();
        let mut reader = FrameReader::new(rx);
        let mut buf = [0; 8192];
        let limit = Instant::now() + Duration::from_secs(5);
        while received.len() < wanted.len() {
            let count = tcp.read(&mut buf).unwrap();
            assert!(
                count != 0 && Instant::now() < limit,
                "incomplete native lobby login or handoff"
            );
            for frame in reader.feed(&buf[..count]) {
                for (crc, wire, id) in wanted {
                    if let Some(body) =
                        frame.strip_prefix(schemas.header(crc, wire, id).unwrap().as_slice())
                    {
                        received.insert(id, schemas.decode(crc, id, body, true).unwrap());
                    }
                }
            }
        }
        fn structure(value: &Value) -> &Fields {
            let Value::Struct(fields) = value else {
                panic!("expected structured protocol fields")
            };
            fields
        }
        fn owned_ids(value: &Value) -> impl Iterator<Item = u64> + '_ {
            let Value::Array(items) = value else {
                panic!("expected collection ownership array")
            };
            items.iter().map(|item| {
                let fields = structure(item);
                let Value::Unsigned(guid) = fields[&0] else {
                    panic!("missing cosmetic ID")
                };
                assert_eq!(fields[&8], Value::Bool(true));
                guid
            })
        }
        assert_eq!(received[&20500][&0xa8], Value::String(rust.battle_tag()));
        assert_eq!(structure(&received[&52002][&0x88])[&0x18], Value::Signed(5));
        let progression = structure(&received[&24300][&0x78]);
        assert_eq!(progression[&0x68], Value::Signed(3000));
        let Value::Array(heroes) = &received[&24900][&0x80] else {
            panic!("missing hero collection")
        };
        let cosmetics: std::collections::HashSet<_> = owned_ids(&progression[&0x30])
            .chain(
                heroes
                    .iter()
                    .flat_map(|hero| owned_ids(&structure(hero)[&0])),
            )
            .collect();
        assert_eq!(cosmetics.len(), 7122);
        assert_eq!(received[&EQUIPPED], equip);
        let saved = load_or_create_profile(rust.path.as_deref().unwrap()).unwrap();
        assert_eq!(
            collection.equipped_skin(&saved, SOLDIER).unwrap().guid,
            skin.skin_theme
        );

        let ticket = structure(&received[&HANDOFF_MESSAGE][&0x80]);
        assert_eq!(
            ticket[&0x2c],
            Value::Unsigned(u64::from(server.game_addr().port()))
        );
        let Value::Unsigned(connection) = ticket[&0x28] else {
            panic!("missing UDP connection")
        };
        let key = |offset| {
            let Value::Array(values) = &ticket[&offset] else {
                panic!("missing UDP key")
            };
            std::array::from_fn(|index| {
                let Value::Unsigned(byte) = values[index] else {
                    panic!("invalid UDP key byte")
                };
                u8::try_from(byte).unwrap()
            })
        };
        let cipher = LinkCipher::new(key(0xce), key(0xae));
        let udp = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let game = server.game_addr();
        udp.connect(game).unwrap();
        udp.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let mut packet = Packet {
            conn: u32::try_from(connection).unwrap(),
            flags: SYN,
            ..Packet::default()
        };
        udp.send(&cipher.seal(&packet).unwrap()).unwrap();
        let mut buf = [0; MAX_DATAGRAM];
        let count = udp.recv(&mut buf).unwrap();
        let syn_ack = cipher.open(&buf[..count]).unwrap();
        assert_eq!((syn_ack.conn, syn_ack.flags), (packet.conn, SYN | ACK));
        packet.seq += 1;
        packet.flags = ACK;
        packet.ack = syn_ack.seq.wrapping_add(1);
        udp.send(&cipher.seal(&packet).unwrap()).unwrap();
        let count = udp.recv(&mut buf).unwrap();
        let load = cipher.open(&buf[..count]).unwrap();
        assert_eq!(load.kind, KIND_FRAME);
        let mut bits = BitReader::new(&load.payload);
        bits.var_a().unwrap();
        let channel = read_channel(&mut bits, &game_schemas).unwrap();
        assert_eq!(channel.reliable[0].message.id, 20300);
        assert_eq!(
            channel.reliable[0].message.fields[&0x4a8],
            Value::Unsigned(PRACTICE_RANGE_MAP)
        );

        let closing = Instant::now();
        server.stop().unwrap();
        assert!(closing.elapsed() < Duration::from_secs(3));
        TcpListener::bind(lobby).unwrap();
        UdpSocket::bind(game).unwrap();
    }

    #[test]
    #[ignore = "requires an authorized 104319 runtime in ORPHEUS_RUNTIME_DIR"]
    fn prepared_subset_matches_the_python_login_in_wire_order() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let schemas = Schemas::load(runtime.join("schemas_174.json")).unwrap();
        let templates = RetailTemplates::load(runtime.join("retail_templates.json")).unwrap();
        let items = Arc::new(ItemDb::load(&runtime).unwrap());
        let reaper = items.hero_by_name("Reaper").unwrap();
        let collection = Collection::new(&templates, items).unwrap();
        let presence = Presence::new(&templates).unwrap();
        let ranked = Ranked::new(&templates).unwrap();
        let arcade = Arcade::load(runtime.join("arcade_cards_174.json"), &templates).unwrap();
        let temporary = tempfile::tempdir().unwrap();
        let accounts = Accounts::new(
            temporary.path().join("profiles"),
            temporary.path().join("seed"),
        )
        .unwrap();
        let account = accounts.get("player").unwrap();
        let prepared = PreparedLogin {
            identity: Identity::for_account(account.account_lo),
            account,
            tournament: false,
        };
        // Python login ordering replaces the early party from Content with the
        // session's actual party after PING. Compare both subsets.
        for (level, frame, date, menu_hero, event_cards, places, base, with_extras) in [
            (
                100,
                0x0250_0000_0000_0921,
                "2024-02-29",
                None,
                [].as_slice(),
                LeaderboardPlaces::new(),
                "c19b931eacf9c54cd61056f82694453fa93a1e716d0c7f01f1cb273480ac76ef",
                "092d0b645d812659197dc2f3e4ab5790ae1c7264060c33f2b6d4491d1f6a170b",
            ),
            (
                3000,
                0x0250_0000_0000_0f97,
                "2026-09-29",
                Some(reaper),
                [COMPETITIVE_CTF].as_slice(),
                LeaderboardPlaces::from([(Queue::Tank, 7)]),
                "7d7660da99e8973ce1ca8c0b2d73122f0822bead8a4d2fa7785876025b0251c7",
                "551492de79257b2112e0cdbcb3e2fc1ceffbc9493f4d55d18bce2863c558ace7",
            ),
        ] {
            let unlock_all = level == 3000;
            {
                let mut profile = prepared.account.profile();
                profile.level = level;
                profile.unlock_all = unlock_all;
                profile.server_date = date.into();
                profile.lobby_hero = if unlock_all { "Reaper" } else { "none" }.into();
                profile.events = vec![if unlock_all { "lunar" } else { "goodbye" }.into()];
                if unlock_all {
                    profile
                        .settings
                        .insert("+0x10D".into(), json!({"+0x0": false, "+0x5": 1}));
                }
            }
            let inputs = LoginContent {
                menu_hero,
                extra_skins: &[],
                wall_time: 1_700_000_000,
                arcade: &arcade,
                celebrations: None,
                leaderboard_places: &places,
                solo_party: None,
            };
            let exact_inputs = LoginContent {
                solo_party: Some(SoloLoginParty {
                    id: [0x1234, 0x2345],
                    entity: [0x0100_1d00_0000_0033, 0x4444],
                    menu_hero: menu_hero.unwrap_or_default(),
                    members_may_invite: !unlock_all,
                }),
                ..inputs
            };
            for (content, count, expected) in [
                (inputs, 46, base),
                (exact_inputs, 50 + event_cards.len(), with_extras),
            ] {
                let mut messages = prepared
                    .available_messages(
                        &schemas,
                        &templates,
                        &collection,
                        &presence,
                        &ranked,
                        content,
                    )
                    .unwrap();
                let arcade_and_passes = [
                    (lobby_passes::PASSES, 58501),
                    (crate::lobby_arcade::ARCADE, 39802),
                    (crate::lobby_arcade::ARCADE, 39810),
                    (crate::lobby_arcade::ARCADE, 39804),
                ];
                let actual: Vec<_> = messages
                    .iter()
                    .map(|&(crc, id, _)| (crc, id))
                    .filter(|key| arcade_and_passes.contains(key))
                    .collect();
                assert_eq!(actual, arcade_and_passes);
                let ranks = messages
                    .iter()
                    .filter(|message| message.0 == RANKED)
                    .count();
                assert_eq!(ranks, 3 + event_cards.len());
                assert_eq!(messages[9].0, MODE_RULES);
                assert_eq!(messages[9].1, RULES);
                assert_eq!(
                    messages.len(),
                    54 + event_cards.len() + usize::from(content.solo_party.is_some())
                );
                // Keep the earlier reference corpus for the original subset.
                // The new dynamic rows have their own complete wire comparison.
                messages.retain(|&(crc, id, _)| {
                    (crc, id) != (MODE_RULES, RULES)
                        && !arcade_and_passes.contains(&(crc, id))
                        && (content.solo_party.is_some() || crc != RANKED)
                });
                let keys: Vec<_> = messages.iter().map(|&(crc, id, _)| (crc, id)).collect();
                assert_eq!(
                    keys[..9],
                    [
                        (IN_CONNECT, 20500),
                        (IN_CONNECT, 20502),
                        (IN_CONNECT, 20505),
                        (player::ENDORSEMENTS, 52002),
                        (player::ENDORSEMENTS, 52005),
                        (player::ENDORSEMENTS, 52006),
                        (FRIENDS, 27113),
                        (PROGRESSION, 24300),
                        (HERO_CATALOG, 24900),
                    ]
                );
                if content.solo_party.is_some() {
                    assert_eq!(
                        keys[keys.len() - 4..],
                        [
                            (RANKED, 36300),
                            (PING, 35500),
                            (lobby_party::PARTY, 20700),
                            (LOBBY, 20802)
                        ]
                    );
                } else {
                    assert_eq!(keys[keys.len() - 2..], [(PING, 35500), (LOBBY, 20802)]);
                }
                assert!(keys[9..].contains(&(LOBBY, 20812)));
                let Value::Struct(card) = &messages[1].2[&0x78] else {
                    panic!("missing player card");
                };
                let Value::Struct(progression) = &messages[7].2[&0x78] else {
                    panic!("missing progression");
                };
                assert_eq!(card[&0x28], Value::Unsigned(frame));
                assert_eq!(progression[&0x58], card[&0x28]);
                assert_eq!(messages.len(), count);
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
        }
    }
}
