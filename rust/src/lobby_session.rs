//! Accepted lobby connections and the login preparation shared with the session.
//!
//! This module assembles the currently ported initial messages for verification.
//! The complete login and post-login handlers still have to be integrated.

use std::{collections::BTreeMap, io, net::TcpStream, sync::Arc, time::Duration};

use crate::{
    accounts::{Account, Accounts, account_id_for, battle_tag},
    battle_tag_query,
    collection::{Collection, HERO_CATALOG, PROGRESSION},
    jam_codec::{Fields, Schemas, Value},
    jam_handshake::server_handshake,
    lobby_login::{self, INCOMPATIBLE_CLIENT, Identity, LOGIN_MESSAGE, Login, OUT_CONNECT},
    lobby_player_messages as player,
    lobby_presence::Presence,
    lobby_transport::{Event, LobbyMessage, LobbyTransport},
    retail_templates::{IN_CONNECT, LOBBY, RetailTemplates},
};

const HELLO_TIMEOUT: Duration = Duration::from_secs(30);
const DISCONNECT: u32 = 20503;
const PING: u32 = 0xdda5_83ef;
// Python builds these four messages per account instead of replaying the capture.
const UNPORTED_LOGIN_REPLACEMENTS: [(u32, u32); 4] = [
    (0xad1c_34ba, 58501), // Priority passes.
    (0x00a8_654e, 39802), // Arcade cards.
    (0x00a8_654e, 39810), // Arcade dates.
    (0x00a8_654e, 39804), // Arcade availability.
];

pub struct LobbySession<'a> {
    transport: LobbyTransport,
    schemas: &'a Schemas,
    local: bool,
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

pub struct LoginContent<'a> {
    pub menu_hero: Option<u64>,
    pub extra_skins: &'a [u64],
    pub wall_time: u64,
    pub server_time: u64,
}

impl<'a> LobbySession<'a> {
    /// Handle a launcher BattleTag query, or open the encrypted game connection.
    pub fn accept(
        mut stream: TcpStream,
        connection_id: u32,
        signing_key: &[u8; 64],
        accounts: &Accounts,
        schemas: &'a Schemas,
    ) -> io::Result<Option<Self>> {
        let local = stream.peer_addr()?.ip().is_loopback();
        stream.set_read_timeout(Some(HELLO_TIMEOUT))?;
        if battle_tag_query::answer(&mut stream, |name| resolve_battle_tag(accounts, name))? {
            return Ok(None);
        }
        let channel = server_handshake(&mut stream, connection_id, signing_key)?;
        let transport = LobbyTransport::new(stream, channel)?;
        Ok(Some(Self {
            transport,
            schemas,
            local,
        }))
    }

    /// Wait for a valid login in the negotiated group. The account and session
    /// identity are available to the caller before any online state is changed.
    pub fn prepare_login(
        &mut self,
        accounts: &Accounts,
        local_default: impl FnOnce() -> io::Result<Arc<Account>>,
    ) -> io::Result<LoginRequest> {
        let header = self
            .schemas
            .header(OUT_CONNECT, 1, LOGIN_MESSAGE)
            .map_err(invalid)?;
        let login_offset = header[header.len() - 1];
        while let Some(event) = self.transport.receive()? {
            let Event::Message {
                crc: OUT_CONNECT,
                offset,
                body,
            } = event
            else {
                continue;
            };
            if offset != login_offset {
                continue;
            }
            let Ok(fields) = self.schemas.decode(OUT_CONNECT, LOGIN_MESSAGE, &body, true) else {
                continue;
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
            return Ok(LoginRequest::Prepared(PreparedLogin {
                account,
                identity,
                tournament,
            }));
        }
        Ok(LoginRequest::Disconnected)
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
        content: LoginContent<'_>,
    ) -> io::Result<Vec<LobbyMessage>> {
        let profile = self.account.profile();
        let identity = self.identity;
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
            (IN_CONNECT, 20505, templates.preload(content.extra_skins)?),
        ];
        messages.extend(player::endorsements(&profile, identity, content.wall_time));
        messages.push(presence.own(&profile, identity, content.wall_time)?);
        messages.extend([
            (PROGRESSION, 24300, collection.progression(&profile)?),
            (HERO_CATALOG, 24900, collection.hero_catalog(&profile)?),
        ]);
        let replay = templates.at_login(
            &profile,
            identity,
            content.menu_hero,
            content.server_time,
            &BTreeMap::from([((LOBBY, 20812), player::ux_states(&profile)?)]),
        )?;
        messages.extend(
            replay
                .into_iter()
                .filter(|&(crc, id, _)| !UNPORTED_LOGIN_REPLACEMENTS.contains(&(crc, id))),
        );
        messages.push((
            PING,
            35500,
            Fields::from([
                (0x78, Value::Unsigned(0)),
                (0x7c, Value::Unsigned(0)),
                (0x80, Value::Array(Vec::new())),
            ]),
        ));
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
    use sha2::{Digest, Sha256};

    use crate::{
        catalog::ItemDb,
        jam_cipher::Jam,
        jam_framing::{self, FrameReader},
        lobby_presence::FRIENDS,
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
    fn negotiates_pings_and_prepares_the_first_valid_login() {
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
                    }
                }"#,
            )
            .unwrap();
            let (stream, _) = listener.accept().unwrap();
            let mut session = LobbySession::accept(stream, 2, &[42; 64], &accounts, &schemas)
                .unwrap()
                .unwrap();
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
            assert!(session.send_disconnect(INCOMPATIBLE_CLIENT).unwrap());
        });

        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let (mut tx, rx) = handshake(&mut client);
        let mut announcement = vec![0, 0, 3, 0, 0, 0];
        for group in [IN_CONNECT, OUT_CONNECT, player::PERMISSIONS + 1] {
            announcement.extend_from_slice(&group.to_le_bytes());
        }
        let mut outgoing = Vec::new();
        for frame in [
            b"\x02\x01premature\0".as_slice(),
            &announcement,
            &jam_framing::PONG,
            b"\x03\x01wrong group\0",
            b"\x02\x00wrong message\0",
            b"\x02\x01unterminated",
            b"\x02\x01trailing byte\0X",
            b"\x02\x01 player \0",
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
    fn prepared_subset_matches_the_python_login_in_wire_order() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let schemas = Schemas::load(runtime.join("schemas_174.json")).unwrap();
        let templates = RetailTemplates::load(runtime.join("retail_templates.json")).unwrap();
        let items = Arc::new(ItemDb::load(&runtime).unwrap());
        let collection = Collection::new(&templates, items).unwrap();
        let presence = Presence::new(&templates).unwrap();
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
        // Python Content.login_messages and PlayerMessages.settings, filtered
        // to the same subset and encoded with a normalized wire index of one.
        for (level, unlock_all, frame, expected_digest) in [
            (
                100,
                false,
                0x0250_0000_0000_0921,
                "0e1a47287d6a6e592ca1d33746bdfd8fee7580ad22de2cbb0999bba851684a6b",
            ),
            (
                3000,
                true,
                0x0250_0000_0000_0f97,
                "98aab1f92b4e45cc16316747910aa6022a20b39682c7627080384f79b53255b8",
            ),
        ] {
            {
                let mut profile = prepared.account.profile();
                profile.level = level;
                profile.unlock_all = unlock_all;
            }
            let messages = prepared
                .available_messages(
                    &schemas,
                    &templates,
                    &collection,
                    &presence,
                    LoginContent {
                        menu_hero: None,
                        extra_skins: &[],
                        wall_time: 1_700_000_000,
                        server_time: 1_700_010_000,
                    },
                )
                .unwrap();
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
            assert_eq!(keys[keys.len() - 2..], [(PING, 35500), (LOBBY, 20802)]);
            assert!(keys[9..].contains(&(LOBBY, 20812)));
            let Value::Struct(card) = &messages[1].2[&0x78] else {
                panic!("missing player card");
            };
            let Value::Struct(progression) = &messages[7].2[&0x78] else {
                panic!("missing progression");
            };
            assert_eq!(card[&0x28], Value::Unsigned(frame));
            assert_eq!(progression[&0x58], card[&0x28]);
            assert_eq!(messages.len(), 46);
            let mut digest = Sha256::new();
            for (crc, id, fields) in messages {
                let mut wire = schemas.header(crc, 1, id).unwrap();
                wire.extend(schemas.encode(crc, id, &fields).unwrap());
                digest.update(crc.to_le_bytes());
                digest.update(id.to_le_bytes());
                digest.update(u32::try_from(wire.len()).unwrap().to_le_bytes());
                digest.update(wire);
            }
            assert_eq!(format!("{:x}", digest.finalize()), expected_digest);
        }
    }
}
