//! Lobby-side seat delivery and match handoff for retail build 104319.
//!
//! Ports the handoff and queue-pop behavior from `ow174/lobby/matchmaker.py`
//! and the handoff data from `ow174/game/server.py`. The game server owns
//! authentication and expiry of its pending UDP seats.

use std::io;
use std::net::Ipv4Addr;
use std::time::{Duration, Instant};

use crate::jam_codec::{Fields, Value, id16};
use crate::lobby_transport::LobbyMessage;

pub const HANDOFF: u32 = 0x074d_ad18;
pub const HANDOFF_MESSAGE: u32 = 20600;
pub const FOUND_DELAY: Duration = Duration::from_secs(4);
const AWAY_DELAY: Duration = Duration::from_secs(1);
const CHAT_IN: u32 = 0x5f91_3f6b;
const MATCH_LOBBY: u32 = 0x1cfb_43cd;
const QUEUE: u32 = 0xb4f8_bc62;
const QUEUE_POPPED: u64 = 1;
const GAME_QUEUE: u64 = 4;
const GAME_FOUND: i64 = 4;
const GENERAL_CHANNEL: [u64; 2] = [0x8b0c, 0xcccc_0000_0f99_5be6];

fn chat_channel(id: [u64; 2], kind: u64) -> Fields {
    Fields::from([
        (0, Value::Struct(id16(id[0], id[1]))),
        (0x10, Value::Unsigned(kind)),
        (0x14, Value::Unsigned(0)),
    ])
}

pub fn general_channel() -> Fields {
    chat_channel(GENERAL_CHANNEL, 7)
}

pub fn match_channel(id: [u64; 2]) -> Fields {
    chat_channel(id, 6)
}

pub fn join_chat_channel(channel: Fields) -> LobbyMessage {
    (
        CHAT_IN,
        20402,
        Fields::from([(0x78, Value::Struct(channel))]),
    )
}

/// The match ID, connection ID and directional keys of a player's UDP seat.
#[derive(Clone)]
pub struct Handoff {
    pub conn: u32,
    /// The client encrypts with this key. The game server uses it to decrypt.
    pub key_in: [u8; 32],
    /// The game server encrypts with this key. The client uses it to decrypt.
    pub key_out: [u8; 32],
    pub match_id: [u64; 2],
}

impl Handoff {
    pub fn new(conn: u32, match_id: [u64; 2]) -> io::Result<Self> {
        let mut keys = [[0; 32]; 2];
        for key in &mut keys {
            getrandom::fill(key).map_err(|error| io::Error::other(error.to_string()))?;
        }
        let [key_in, key_out] = keys;
        Ok(Self {
            conn,
            key_in,
            key_out,
            match_id,
        })
    }

    /// Fields of 20600. Clearing +0x78 tells the client the inner struct is plain.
    pub fn fields(&self, host: &str, port: u16) -> io::Result<Fields> {
        if !host.is_ascii() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "the game host must be ASCII",
            ));
        }
        let mut address = [0; 64];
        let address_len = host.len().min(address.len() - 1);
        address[..address_len].copy_from_slice(&host.as_bytes()[..address_len]);
        let address = byte_array(&address);
        Ok(Fields::from([
            (0x78, Value::Bool(false)),
            (
                0x80,
                Value::Struct(Fields::from([
                    (0, Value::Struct(id16(self.match_id[0], self.match_id[1]))),
                    (0x28, Value::Unsigned(u64::from(self.conn))),
                    (0x2c, Value::Unsigned(u64::from(port))),
                    (0x2e, address.clone()),
                    (0x6e, address),
                    (0xae, byte_array(&self.key_in)),
                    (0xce, byte_array(&self.key_out)),
                    (0xee, Value::Bool(false)),
                ])),
            ),
        ]))
    }
}

fn byte_array(bytes: &[u8]) -> Value {
    Value::Array(
        bytes
            .iter()
            .map(|&byte| Value::Unsigned(u64::from(byte)))
            .collect(),
    )
}

/// Select the configured public host for external clients and the lobby's
/// reached address for local clients, which may be behind the same router.
pub fn game_host(public: &str, reached: Ipv4Addr, peer: Ipv4Addr) -> String {
    // Match the reference's Python 3.12 ipaddress classification for its IPv4
    // lobby. Shared carrier NAT is neither private nor globally reachable.
    let non_global = matches!(peer.octets(),
        [0 | 10 | 127 | 240..=255, ..] | [100, 64..=127, ..] | [169, 254, ..] | [172, 16..=31, ..]
            | [192, 0, 0, 0..=7 | 170..=171] | [192, 0, 2, _] | [192, 168, ..]
            | [198, 18..=19, ..] | [198, 51, 100, _] | [203, 0, 113, _]);
    if public.is_empty() || non_global {
        reached.to_string()
    } else {
        public.to_owned()
    }
}

/// Stop the search timer before showing the "Game Found!" banner.
pub fn queue_popped(key: Fields) -> LobbyMessage {
    (
        QUEUE,
        44202,
        Fields::from([
            (0x78, Value::Struct(key)),
            (0xa8, Value::Unsigned(QUEUE_POPPED)),
        ]),
    )
}

fn match_lobby_id(id: [u64; 2]) -> Value {
    Value::Struct(Fields::from([(
        0,
        Value::Array(id.into_iter().map(Value::Unsigned).collect()),
    )]))
}

/// Show the banner, with optional competitive settings and roster in send order.
pub fn game_found(
    key: Fields,
    match_id: [u64; 2],
    versus: Option<(Fields, Fields)>,
) -> Vec<LobbyMessage> {
    let enter = Fields::from([
        (
            0x78,
            Value::Struct(Fields::from([
                (0, Value::Struct(key)),
                (0x30, match_lobby_id(match_id)),
                (0x60, Value::Unsigned(GAME_QUEUE)),
            ])),
        ),
        (0xe0, Value::Bool(false)),
        (0xe8, Value::Unsigned(0)),
    ]);
    let leave = Fields::from([
        (0x78, match_lobby_id(match_id)),
        (0x88, Value::Unsigned(0)),
        (0x90, match_lobby_id([0, 0])),
        (0xa0, Value::Signed(GAME_FOUND)),
    ]);
    let mut messages = vec![(MATCH_LOBBY, 53000, enter)];
    if let Some((settings, roster)) = versus {
        messages.extend([
            (MATCH_LOBBY, 53001, settings),
            (MATCH_LOBBY, 53003, leave),
            (MATCH_LOBBY, 53002, roster),
        ]);
    } else {
        messages.push((MATCH_LOBBY, 53003, leave));
    }
    messages
}

/// Leave General, join the match channel, then transfer to the game server.
pub fn enter_game(
    general: Fields,
    match_channel: Fields,
    handoff: &Handoff,
    host: &str,
    port: u16,
) -> io::Result<[LobbyMessage; 3]> {
    Ok([
        (
            CHAT_IN,
            20404,
            Fields::from([(0x78, Value::Struct(general))]),
        ),
        join_chat_channel(match_channel),
        (HANDOFF, HANDOFF_MESSAGE, handoff.fields(host, port)?),
    ])
}

/// A queue transfer must wait for the banner and for any old game to close.
/// The Practice Range request can transfer immediately.
pub fn handoff_delay(popped: bool, banner: Duration, away: bool) -> Duration {
    if !popped {
        Duration::ZERO
    } else if away {
        banner.max(AWAY_DELAY)
    } else {
        banner
    }
}

pub struct LobbySeat {
    pub account_lo: u32,
    /// Identity of the lobby session that reserved this seat, not the account ID.
    pub lobby_session: u64,
    pub handoff: Handoff,
}

pub enum SeatAction {
    Deliver(LobbySeat),
    Release(LobbySeat),
}

/// Seats whose banners are showing. The caller serializes changes with lobby state.
#[derive(Default)]
pub struct PendingHandoffs {
    seats: Vec<(Instant, LobbySeat)>,
}

impl PendingHandoffs {
    /// Stage a seat in deadline order, retaining insertion order for one batch.
    /// Return any superseded seat so its game-side reservation can be released.
    pub fn stage(&mut self, at: Instant, seat: LobbySeat) -> Option<LobbySeat> {
        let old = self
            .seats
            .iter()
            .position(|(_, pending)| pending.account_lo == seat.account_lo)
            .map(|index| self.seats.remove(index).1);
        let position = self.seats.partition_point(|(deadline, _)| *deadline <= at);
        self.seats.insert(position, (at, seat));
        old
    }

    /// A stale disconnect cannot cancel the seat of a newer lobby session.
    pub fn cancel(&mut self, account_lo: u32, lobby_session: u64) -> Option<LobbySeat> {
        let position = self.seats.iter().position(|(_, seat)| {
            seat.account_lo == account_lo && seat.lobby_session == lobby_session
        })?;
        Some(self.seats.remove(position).1)
    }

    /// After the banner, deliver only to a live lobby session with a pending UDP seat.
    pub fn advance(
        &mut self,
        now: Instant,
        mut online: impl FnMut(u32, u64) -> bool,
        mut pending: impl FnMut(u32) -> bool,
    ) -> Vec<SeatAction> {
        let end = self.seats.partition_point(|(deadline, _)| *deadline <= now);
        self.seats
            .drain(..end)
            .map(|(_, seat)| {
                if online(seat.account_lo, seat.lobby_session) && pending(seat.handoff.conn) {
                    SeatAction::Deliver(seat)
                } else {
                    SeatAction::Release(seat)
                }
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jam_codec::Schemas;

    fn handoff(conn: u32) -> Handoff {
        Handoff {
            conn,
            key_in: std::array::from_fn(|index| index as u8),
            key_out: std::array::from_fn(|index| (index + 32) as u8),
            match_id: [0x0123_4567_89ab_cdef, 0x1122_3344_5566_7788],
        }
    }

    #[test]
    fn handoff_body_preserves_addresses_match_and_directional_keys() {
        let schemas = Schemas::from_json(
            r#"{"074DAD18":{"20600":[
            {"type":0,"off":120,"count":0,"array":false},
            {"type":14,"off":128,"count":0,"array":false,"fields":[
                {"type":14,"off":0,"count":0,"array":false,"fields":[
                    {"type":8,"off":0,"count":0,"array":false},
                    {"type":8,"off":8,"count":0,"array":false}
                ]},
                {"type":8,"off":16,"count":0,"array":false},
                {"type":8,"off":24,"count":0,"array":false},
                {"type":8,"off":32,"count":0,"array":false},
                {"type":6,"off":40,"count":0,"array":false},
                {"type":4,"off":44,"count":0,"array":false},
                {"type":1,"off":46,"count":64,"array":false},
                {"type":1,"off":110,"count":64,"array":false},
                {"type":2,"off":174,"count":32,"array":false},
                {"type":2,"off":206,"count":32,"array":false},
                {"type":0,"off":238,"count":0,"array":false}
            ]}
        ]}}"#,
        )
        .unwrap();
        let seat = handoff(0x1122_3344);
        let host = "203.0.113.8";
        let mut expected = [0u8; 240];
        expected[1..9].copy_from_slice(&seat.match_id[0].to_le_bytes());
        expected[9..17].copy_from_slice(&seat.match_id[1].to_le_bytes());
        expected[41..45].copy_from_slice(&seat.conn.to_le_bytes());
        expected[45..47].copy_from_slice(&3730u16.to_le_bytes());
        expected[47..47 + host.len()].copy_from_slice(host.as_bytes());
        expected[111..111 + host.len()].copy_from_slice(host.as_bytes());
        expected[175..207].copy_from_slice(&seat.key_in);
        expected[207..239].copy_from_slice(&seat.key_out);
        let encode = |host| {
            schemas
                .encode(HANDOFF, HANDOFF_MESSAGE, &seat.fields(host, 3730).unwrap())
                .unwrap()
        };
        assert_eq!(encode(host), expected);

        let long_host = "a".repeat(70);
        let body = encode(&long_host);
        for address in [&body[47..111], &body[111..175]] {
            assert_eq!(&address[..63], &[b'a'; 63]);
            assert_eq!(address[63], 0);
        }
        assert_eq!(
            seat.fields("游戏.test", 3730).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn uses_the_reached_address_for_local_and_shared_carrier_addresses() {
        let reached = Ipv4Addr::new(192, 168, 1, 5);
        for peer in [
            Ipv4Addr::LOCALHOST,
            Ipv4Addr::new(192, 168, 1, 7),
            Ipv4Addr::new(100, 64, 0, 2),
        ] {
            assert_eq!(game_host("games.example", reached, peer), "192.168.1.5");
        }
        let peer = Ipv4Addr::new(85, 10, 20, 30);
        assert_eq!(game_host("games.example", reached, peer), "games.example");
        assert_eq!(game_host("", reached, peer), "192.168.1.5");
    }

    #[test]
    fn queue_banner_and_chat_precede_game_transfer() {
        let key = Fields::from([(0x20, Value::Unsigned(77))]);
        let seat = handoff(42);
        let (crc, message, fields) = queue_popped(key.clone());
        assert_eq!((crc, message), (QUEUE, 44202));
        assert_eq!(fields[&0x78], Value::Struct(key.clone()));
        assert_eq!(fields[&0xa8], Value::Unsigned(1));

        let regular = game_found(key.clone(), seat.match_id, None);
        assert_eq!(
            regular.iter().map(|(_, id, _)| *id).collect::<Vec<_>>(),
            [53000, 53003]
        );
        assert_eq!(regular[1].2[&0xa0], Value::Signed(4));
        assert_eq!(regular[1].2[&0x78], match_lobby_id(seat.match_id));
        let competitive = game_found(key, seat.match_id, Some((Fields::new(), Fields::new())));
        assert_eq!(
            competitive.iter().map(|(_, id, _)| *id).collect::<Vec<_>>(),
            [53000, 53001, 53003, 53002]
        );

        let entering = enter_game(Fields::new(), Fields::new(), &seat, "127.0.0.1", 3730).unwrap();
        assert_eq!(
            entering.map(|(_, id, _)| id),
            [20404, 20402, HANDOFF_MESSAGE]
        );
        assert_eq!(handoff_delay(false, FOUND_DELAY, true), Duration::ZERO);
        assert_eq!(handoff_delay(true, FOUND_DELAY, true), FOUND_DELAY);
        assert_eq!(
            handoff_delay(true, Duration::ZERO, true),
            Duration::from_secs(1)
        );
        assert_eq!(handoff_delay(true, Duration::ZERO, false), Duration::ZERO);
    }

    fn lobby_seat(account_lo: u32, lobby_session: u64, conn: u32) -> LobbySeat {
        LobbySeat {
            account_lo,
            lobby_session,
            handoff: handoff(conn),
        }
    }

    #[test]
    fn pending_seats_check_session_and_admission_once_in_batch_order() {
        let now = Instant::now();
        let deadline = now + FOUND_DELAY;
        let mut pending = PendingHandoffs::default();
        assert!(pending.stage(deadline, lobby_seat(1, 10, 40)).is_none());
        pending.stage(deadline, lobby_seat(2, 20, 41));
        pending.stage(deadline, lobby_seat(3, 30, 42));
        pending.stage(deadline, lobby_seat(4, 40, 43));
        let superseded = pending
            .stage(deadline + AWAY_DELAY, lobby_seat(4, 41, 44))
            .unwrap();
        assert_eq!(superseded.handoff.conn, 43);
        assert!(pending.cancel(4, 40).is_none());
        assert_eq!(pending.cancel(4, 41).unwrap().handoff.conn, 44);

        assert!(
            pending
                .advance(now, |_, _| panic!("early"), |_| panic!("early"))
                .is_empty()
        );
        let mut checked = Vec::new();
        let actions = pending.advance(
            deadline,
            |_, session| session != 20,
            |conn| {
                checked.push(conn);
                conn != 42
            },
        );
        assert_eq!(checked, [40, 42]);
        let delivered = actions
            .into_iter()
            .map(|action| match action {
                SeatAction::Deliver(seat) => (true, seat.handoff.conn),
                SeatAction::Release(seat) => (false, seat.handoff.conn),
            })
            .collect::<Vec<_>>();
        assert_eq!(delivered, [(true, 40), (false, 41), (false, 42)]);
        assert!(
            pending
                .advance(
                    deadline,
                    |_, _| panic!("sent twice"),
                    |_| panic!("sent twice")
                )
                .is_empty()
        );
    }
}
