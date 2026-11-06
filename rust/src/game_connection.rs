//! Match handoff admission and encrypted UDP connection state.

use std::{collections::HashMap, net::SocketAddr, time::Duration};

use crate::game_link::{
    ACK, FIN, HEADER_SIZE, KIND_CONTROL, KIND_FRAME, LinkCipher, MAX_DATAGRAM, Packet,
    ReceiveWindow, SYN, peek_connection,
};
use crate::match_handoff::Handoff;

pub const HANDOFF_TIME: Duration = Duration::from_secs(60);
pub const SILENCE_TIME: Duration = Duration::from_secs(10);
pub const LEAVE_GRACE: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Rejected {
    NotGameDatagram,
    UnknownConnection,
    HandoffExpired,
    AuthenticationFailed,
    ExpectedSyn,
    StaleSequence,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransmitError {
    UnknownPeer,
    PayloadTooLarge,
    EncryptionFailed,
}

#[derive(Debug, Eq, PartialEq)]
pub struct Outgoing {
    pub address: SocketAddr,
    pub sequence: u32,
    pub datagram: Vec<u8>,
}

#[derive(Debug, Eq, PartialEq)]
pub struct Received {
    pub connection: u32,
    pub seat: u64,
    pub admitted: bool,
    pub rebound: bool,
    pub ack: Option<(u32, u64)>,
    pub control: Option<Result<Outgoing, TransmitError>>,
    pub closed: bool,
    pub opened: bool,
    /// Handle this after the ACK and open notification. The caller also checks
    /// whether those events retired the player before dispatching the payload.
    pub payload: Option<Vec<u8>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Timeout {
    Handoff,
    Silence,
    LeaveGrace,
    Drain,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Expired {
    pub connection: u32,
    pub seat: u64,
    pub reason: Timeout,
}

struct Peer {
    address: SocketAddr,
    window: ReceiveWindow,
    next_sequence: u32,
    open: bool,
    heard: Duration,
    leaving: Option<Duration>,
}

enum State {
    Waiting { deadline: Duration },
    Active(Peer),
}

struct Connection {
    seat: u64,
    cipher: LinkCipher,
    state: State,
}

#[derive(Default)]
pub struct GameConnections {
    connections: HashMap<u32, Connection>,
}

impl GameConnections {
    /// Reserve a seat using the same directional keys as its lobby message.
    pub fn reserve_handoff(&mut self, handoff: &Handoff, seat: u64, now: Duration) -> bool {
        let Some(deadline) = now.checked_add(HANDOFF_TIME) else {
            return false;
        };
        self.reserve(
            handoff.conn,
            seat,
            LinkCipher::new(handoff.key_in, handoff.key_out),
            deadline,
        )
    }

    /// The deadline and receive timestamp must use the same clock.
    pub fn reserve(
        &mut self,
        connection: u32,
        seat: u64,
        cipher: LinkCipher,
        deadline: Duration,
    ) -> bool {
        if !(1..=0x0fff_ffff).contains(&connection) {
            return false;
        }
        let std::collections::hash_map::Entry::Vacant(entry) = self.connections.entry(connection)
        else {
            return false;
        };
        entry.insert(Connection {
            seat,
            cipher,
            state: State::Waiting { deadline },
        });
        true
    }

    /// Reject any subsequent SYN and release the seat at the next timeout sweep.
    pub fn cancel_handoff(&mut self, connection: u32) -> bool {
        match self.connections.get_mut(&connection) {
            Some(Connection {
                state: State::Waiting { deadline },
                ..
            }) => {
                *deadline = Duration::ZERO;
                true
            }
            _ => false,
        }
    }

    pub fn handoff_pending(&self, connection: u32, now: Duration) -> bool {
        self.connections.get(&connection).is_some_and(
            |entry| matches!(entry.state, State::Waiting { deadline } if now < deadline),
        )
    }

    pub fn receive(
        &mut self,
        datagram: &[u8],
        address: SocketAddr,
        now: Duration,
    ) -> Result<Received, Rejected> {
        let connection = peek_connection(datagram).ok_or(Rejected::NotGameDatagram)?;
        let entry = self
            .connections
            .get_mut(&connection)
            .ok_or(Rejected::UnknownConnection)?;
        let admitted = match entry.state {
            State::Waiting { deadline } if now >= deadline => return Err(Rejected::HandoffExpired),
            State::Waiting { .. } => true,
            State::Active(_) => false,
        };
        let packet = entry
            .cipher
            .open(datagram)
            .ok_or(Rejected::AuthenticationFailed)?;
        if admitted {
            if packet.flags & SYN == 0 {
                return Err(Rejected::ExpectedSyn);
            }
            // The Python listener allocates a peer at an authenticated SYN,
            // then checks its sequence against an initially empty window.
            entry.state = State::Active(Peer {
                address,
                window: ReceiveWindow::default(),
                next_sequence: 0,
                open: false,
                heard: now,
                leaving: None,
            });
        }
        let State::Active(peer) = &mut entry.state else {
            unreachable!("only active peers receive authenticated datagrams");
        };
        let fresh = peer.window.accept(packet.seq);
        if !fresh {
            // A stale initial SYN still took its seat in the reference, but
            // produced no acknowledgement and did not reach the player.
            return if admitted {
                Ok(Received {
                    connection,
                    seat: entry.seat,
                    admitted: true,
                    rebound: false,
                    ack: None,
                    control: None,
                    closed: false,
                    opened: false,
                    payload: None,
                })
            } else {
                Err(Rejected::StaleSequence)
            };
        }
        let rebound = peer.address != address;
        peer.address = address;
        peer.heard = now;

        let closed = packet.flags & FIN != 0;
        let control = if closed {
            Some(FIN | ACK)
        } else if packet.flags & SYN != 0 {
            Some(SYN | ACK)
        } else {
            None
        };
        let control = control.map(|flags| {
            peer.seal(
                &entry.cipher,
                Packet {
                    conn: connection,
                    kind: KIND_CONTROL,
                    flags,
                    ..Packet::default()
                },
            )
        });
        let opened = !closed && !peer.open && packet.flags & ACK != 0;
        peer.open |= opened;
        let payload = (!closed
            && peer.leaving.is_none()
            && packet.kind == KIND_FRAME
            && !packet.payload.is_empty())
        .then_some(packet.payload);
        let received = Received {
            connection,
            seat: entry.seat,
            admitted,
            rebound,
            ack: Some((packet.ack, packet.ack_bits)),
            control,
            closed,
            opened,
            payload,
        };
        if closed {
            self.connections.remove(&connection);
        }
        Ok(received)
    }

    pub fn transmit(&mut self, packet: Packet) -> Result<Outgoing, TransmitError> {
        let Some(Connection {
            cipher,
            state: State::Active(peer),
            ..
        }) = self.connections.get_mut(&packet.conn)
        else {
            return Err(TransmitError::UnknownPeer);
        };
        peer.seal(cipher, packet)
    }

    /// Begin the leave grace period once, preserving its original deadline.
    pub fn begin_leaving(&mut self, connection: u32, now: Duration) -> bool {
        let Some(Connection {
            state: State::Active(peer),
            ..
        }) = self.connections.get_mut(&connection)
        else {
            return false;
        };
        if peer.leaving.is_some() {
            return false;
        }
        peer.leaving = Some(now);
        true
    }

    pub fn expire(&mut self, now: Duration, force: bool) -> Vec<Expired> {
        let mut expired = Vec::new();
        self.connections.retain(|&connection, entry| {
            let reason = match &entry.state {
                _ if force => Timeout::Drain,
                State::Waiting { deadline } if now >= *deadline => Timeout::Handoff,
                State::Active(peer) if now.saturating_sub(peer.heard) > SILENCE_TIME => {
                    Timeout::Silence
                }
                State::Active(peer)
                    if peer
                        .leaving
                        .is_some_and(|leaving| now.saturating_sub(leaving) >= LEAVE_GRACE) =>
                {
                    Timeout::LeaveGrace
                }
                _ => return true,
            };
            expired.push(Expired {
                connection,
                seat: entry.seat,
                reason,
            });
            false
        });
        expired.sort_unstable_by_key(|entry| entry.connection);
        expired
    }
}

impl Peer {
    fn seal(&mut self, cipher: &LinkCipher, mut packet: Packet) -> Result<Outgoing, TransmitError> {
        if packet.payload.len() > MAX_DATAGRAM - HEADER_SIZE {
            return Err(TransmitError::PayloadTooLarge);
        }
        packet.seq = self.next_sequence;
        packet.ack = self.window.expected;
        packet.ack_bits = self.window.bits;
        self.next_sequence = self.next_sequence.wrapping_add(1);
        let datagram = cipher
            .seal(&packet)
            .map_err(|_| TransmitError::EncryptionFailed)?;
        Ok(Outgoing {
            address: self.address,
            sequence: packet.seq,
            datagram,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jam_codec::{Fields, Schemas, Value};
    use std::net::UdpSocket;

    const CONN: u32 = 27;
    const SEAT: u64 = 65;

    fn secs(value: u64) -> Duration {
        Duration::from_secs(value)
    }

    fn setup() -> (GameConnections, LinkCipher, SocketAddr, SocketAddr) {
        let mut game = GameConnections::default();
        assert!(game.reserve(
            CONN,
            SEAT,
            LinkCipher::new([11; 32], [22; 32]),
            HANDOFF_TIME,
        ));
        (
            game,
            LinkCipher::new([22; 32], [11; 32]),
            "127.0.0.1:2400".parse().unwrap(),
            "127.0.0.1:2401".parse().unwrap(),
        )
    }

    fn datagram(cipher: &LinkCipher, sequence: u32, flags: u8, payload: &[u8]) -> Vec<u8> {
        cipher
            .seal(&Packet {
                conn: CONN,
                kind: if payload.is_empty() {
                    KIND_CONTROL
                } else {
                    KIND_FRAME
                },
                seq: sequence,
                ack: 7,
                ack_bits: 13,
                flags,
                payload: payload.into(),
            })
            .unwrap()
    }

    #[test]
    fn authenticated_syn_opens_on_ack_and_fin_retires_after_reply() {
        let (mut game, client, address, _) = setup();
        let first = game
            .receive(&datagram(&client, 0, SYN, &[]), address, secs(1))
            .unwrap();
        assert!(first.admitted);
        assert!(!first.opened);
        assert_eq!(first.ack, Some((7, 13)));
        let response = client
            .open(&first.control.unwrap().unwrap().datagram)
            .unwrap();
        assert_eq!((response.conn, response.kind), (CONN, KIND_CONTROL));
        assert_eq!((response.seq, response.ack, response.ack_bits), (0, 1, 1));
        assert_eq!(response.flags, SYN | ACK);
        let opened = game
            .receive(&datagram(&client, 2, ACK, &[5]), address, secs(2))
            .unwrap();
        assert!(opened.opened);
        assert_eq!(opened.payload, Some(vec![5]));
        let closed = game
            .receive(
                &datagram(&client, 3, FIN | SYN | ACK, &[9]),
                address,
                secs(3),
            )
            .unwrap();
        assert!(closed.closed);
        assert!(!closed.opened);
        assert_eq!(closed.payload, None);
        let response = client
            .open(&closed.control.unwrap().unwrap().datagram)
            .unwrap();
        assert_eq!(response.flags, FIN | ACK);
        assert_eq!(
            (response.seq, response.ack, response.ack_bits),
            (1, 4, 0b1011)
        );
        assert_eq!(
            game.receive(&datagram(&client, 4, ACK, &[]), address, secs(4)),
            Err(Rejected::UnknownConnection)
        );
    }

    #[test]
    fn failed_authentication_missing_syn_and_expiry_do_not_consume_handoff() {
        let (mut game, client, address, _) = setup();
        let syn = datagram(&client, 1, SYN, &[]);
        let mut tampered = syn.clone();
        tampered[0] ^= 1;
        assert_eq!(
            game.receive(&tampered, address, secs(1)),
            Err(Rejected::AuthenticationFailed)
        );
        assert_eq!(
            game.receive(&datagram(&client, 0, ACK, &[]), address, secs(1)),
            Err(Rejected::ExpectedSyn)
        );
        assert_eq!(
            game.receive(&syn, address, HANDOFF_TIME),
            Err(Rejected::HandoffExpired)
        );
        assert_eq!(
            game.expire(HANDOFF_TIME, false),
            [Expired {
                connection: CONN,
                seat: SEAT,
                reason: Timeout::Handoff
            }]
        );
    }

    #[test]
    fn only_fresh_authenticated_packets_rebind_or_refresh_silence() {
        let (mut game, client, address, other_address) = setup();
        let syn = datagram(&client, 0, SYN, &[1]);
        let first = game.receive(&syn, address, secs(1)).unwrap();
        assert_eq!(first.payload, Some(vec![1]));
        assert_eq!(
            game.receive(&syn, other_address, secs(6)),
            Err(Rejected::StaleSequence)
        );
        let rebound = game
            .receive(&datagram(&client, 1, ACK, &[]), other_address, secs(7))
            .unwrap();
        assert!(rebound.rebound);
        let outgoing = game
            .transmit(Packet {
                conn: CONN,
                kind: KIND_FRAME,
                payload: vec![3],
                ..Packet::default()
            })
            .unwrap();
        assert_eq!(outgoing.address, other_address);
        let frame = client.open(&outgoing.datagram).unwrap();
        assert_eq!((frame.seq, frame.ack, frame.payload), (1, 2, vec![3]));
        assert_eq!(
            game.receive(&syn, other_address, secs(15)),
            Err(Rejected::StaleSequence)
        );
        assert!(game.expire(secs(17), false).is_empty());
        assert_eq!(game.expire(secs(18), false)[0].reason, Timeout::Silence);
    }

    #[test]
    fn leave_grace_keeps_acks_and_suppresses_payloads() {
        let (mut game, client, address, _) = setup();
        game.receive(&datagram(&client, 0, SYN, &[]), address, secs(1))
            .unwrap();
        assert!(game.begin_leaving(CONN, secs(2)));
        assert!(!game.begin_leaving(CONN, secs(3)));
        let received = game
            .receive(&datagram(&client, 1, ACK, &[2]), address, secs(4))
            .unwrap();
        assert_eq!(received.ack, Some((7, 13)));
        assert_eq!(received.payload, None);
        assert!(game.expire(secs(4), false).is_empty());
        assert_eq!(game.expire(secs(5), false)[0].reason, Timeout::LeaveGrace);
    }

    #[test]
    fn first_syn_allocates_even_if_its_sequence_is_outside_the_window() {
        let (mut game, client, address, _) = setup();
        let syn = datagram(&client, 1 << 31, SYN, &[]);
        let result = game.receive(&syn, address, secs(1)).unwrap();
        assert!(result.admitted);
        assert_eq!(result.ack, None);
        assert_eq!(result.control, None);
        assert!(!game.handoff_pending(CONN, secs(1)));
        let result = game
            .receive(&datagram(&client, 0, SYN, &[]), address, secs(2))
            .unwrap();
        assert!(!result.admitted);
        assert!(result.control.is_some());
    }

    #[test]
    fn explicit_cancel_is_rejected_before_the_next_timeout_sweep() {
        let (mut game, client, address, _) = setup();
        assert!(game.handoff_pending(CONN, Duration::ZERO));
        assert!(game.cancel_handoff(CONN));
        assert!(!game.handoff_pending(CONN, Duration::ZERO));
        let syn = datagram(&client, 0, SYN, &[]);
        assert_eq!(
            game.receive(&syn, address, Duration::ZERO),
            Err(Rejected::HandoffExpired)
        );
        assert_eq!(game.expire(Duration::ZERO, false)[0].seat, SEAT);
        assert!(!game.cancel_handoff(CONN));
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 handoff schema"]
    fn encoded_lobby_ticket_authenticates_a_loopback_udp_peer() {
        use crate::match_handoff::{HANDOFF, HANDOFF_MESSAGE};

        let runtime = std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let schemas = Schemas::load(runtime.join("data/schemas_174.json")).unwrap();
        let server_socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        let client_socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        for socket in [&server_socket, &client_socket] {
            socket.set_read_timeout(Some(secs(2))).unwrap();
        }
        let server_address = server_socket.local_addr().unwrap();
        let handoff = Handoff {
            conn: CONN,
            key_in: [11; 32],
            key_out: [22; 32],
            match_id: [SEAT, 1],
        };
        let fields = handoff.fields("127.0.0.1", server_address.port()).unwrap();
        let encoded = schemas.encode(HANDOFF, HANDOFF_MESSAGE, &fields).unwrap();
        let decoded = schemas
            .decode(HANDOFF, HANDOFF_MESSAGE, &encoded, true)
            .unwrap();
        let Value::Struct(seat) = &decoded[&0x80] else {
            panic!("ticket body");
        };
        let key = |fields: &Fields, offset| {
            let Value::Array(bytes) = &fields[&offset] else {
                panic!("ticket key");
            };
            std::array::from_fn(|index| match bytes[index] {
                Value::Unsigned(byte) => u8::try_from(byte).unwrap(),
                _ => panic!("key byte"),
            })
        };
        assert_eq!(seat[&0x28], Value::Unsigned(u64::from(CONN)));
        assert_eq!(
            seat[&0x2c],
            Value::Unsigned(u64::from(server_address.port()))
        );
        let client_cipher = LinkCipher::new(key(seat, 0xce), key(seat, 0xae));
        let mut game = GameConnections::default();
        assert!(game.reserve_handoff(&handoff, SEAT, secs(1)));
        client_socket
            .send_to(&datagram(&client_cipher, 0, SYN, &[]), server_address)
            .unwrap();
        let mut buffer = [0; MAX_DATAGRAM];
        let (length, address) = server_socket.recv_from(&mut buffer).unwrap();
        let received = game.receive(&buffer[..length], address, secs(2)).unwrap();
        assert_eq!(received.seat, SEAT);
        let reply = received.control.unwrap().unwrap();
        server_socket
            .send_to(&reply.datagram, reply.address)
            .unwrap();
        let (length, address) = client_socket.recv_from(&mut buffer).unwrap();
        assert_eq!(address, server_address);
        let response = client_cipher.open(&buffer[..length]).unwrap();
        assert_eq!((response.flags, response.ack), (SYN | ACK, 1));
    }
}
