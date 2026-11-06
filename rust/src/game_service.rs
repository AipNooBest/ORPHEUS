//! The native UDP match worker. It owns seats, connections and frame timing.
//!
//! Its current match implementation serves instance and loading messages.
//! World and controller entry wait for the simulation to supply their data.

use std::{
    collections::HashMap,
    io,
    net::SocketAddr,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use indexmap::IndexMap;
use mio::{Events, Interest, Poll, Token, Waker, net::UdpSocket};

use crate::{
    game_clock::{GameClock, Step, next_tick_after},
    game_connection::{GameConnections, MAX_CONNECTION_ID, Outgoing},
    game_egress::{FrameRecords, GameEgress},
    game_ingress::{GameIngress, MapReadiness, MapReadyStep, MessageKind, WorldReadiness, pong},
    game_instance::{Instance, Member, RETAIL_QUANTA, TOURNAMENT_QUANTA, ownership},
    game_link::{ACK, KIND_FRAME, Packet},
    game_messages::{Message, encode, locate},
    jam_codec::{Fields, Schemas, Value},
    match_handoff::Handoff,
    world::{MAX_PAYLOAD, frame},
};

const GAME_SOCKET: Token = Token(0);
const REQUESTS: Token = Token(1);
const RECEIVE_BATCH: usize = 64;

#[derive(Clone, Copy, Debug)]
pub struct MatchMap {
    pub guid: u64,
    pub mode: u64,
    pub team_sizes: [u8; 2],
    pub free_for_all: bool,
}

#[derive(Debug)]
pub struct MatchPlayer {
    pub name: String,
    pub team: u8,
    pub tournament: bool,
    pub ranked: bool,
}

pub struct GameService {
    address: SocketAddr,
    requests: mpsc::SyncSender<Request>,
    wake: Waker,
    stopping: Arc<AtomicBool>,
    worker: Option<JoinHandle<io::Result<()>>>,
}

type MatchId = [u64; 2];

enum Request {
    Open {
        id: MatchId,
        map: MatchMap,
        players: Vec<MatchPlayer>,
        reply: mpsc::SyncSender<io::Result<Vec<Handoff>>>,
    },
    Pending {
        connection: u32,
        reply: mpsc::SyncSender<bool>,
    },
    Cancel {
        connection: u32,
        reply: mpsc::SyncSender<bool>,
    },
}

impl GameService {
    pub fn bind(address: SocketAddr, schemas: Arc<Schemas>) -> io::Result<Self> {
        for id in [20300, 20301, 20302, 20304, 20306, 21601, 21602, 21610] {
            let (_, crc, _) = locate(id).map_err(invalid)?;
            schemas.fields(crc, id).map_err(invalid)?;
        }

        let mut socket = UdpSocket::bind(address)?;
        let address = socket.local_addr()?;
        let poll = Poll::new()?;
        poll.registry()
            .register(&mut socket, GAME_SOCKET, Interest::READABLE)?;
        let wake = Waker::new(poll.registry(), REQUESTS)?;
        let (requests, receiver) = mpsc::sync_channel(RECEIVE_BATCH);
        let stopping = Arc::new(AtomicBool::new(false));
        let worker_stopping = Arc::clone(&stopping);
        let worker = thread::Builder::new()
            .name("orpheus-game".into())
            .spawn(move || run(socket, poll, receiver, worker_stopping, schemas))?;
        Ok(Self {
            address,
            requests,
            wake,
            stopping,
            worker: Some(worker),
        })
    }

    pub fn local_addr(&self) -> SocketAddr {
        self.address
    }

    pub fn open_match(
        &self,
        id: [u64; 2],
        map: MatchMap,
        players: Vec<MatchPlayer>,
    ) -> io::Result<Vec<Handoff>> {
        self.request(|reply| Request::Open {
            id,
            map,
            players,
            reply,
        })?
    }

    pub fn handoff_pending(&self, connection: u32) -> io::Result<bool> {
        self.request(|reply| Request::Pending { connection, reply })
    }

    pub fn cancel_handoff(&self, connection: u32) -> io::Result<bool> {
        self.request(|reply| Request::Cancel { connection, reply })
    }

    fn request<T>(&self, request: impl FnOnce(mpsc::SyncSender<T>) -> Request) -> io::Result<T> {
        let (reply, receive) = mpsc::sync_channel(1);
        self.requests
            .try_send(request(reply))
            .map_err(|error| match error {
                mpsc::TrySendError::Full(_) => io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "game worker request queue is full",
                ),
                mpsc::TrySendError::Disconnected(_) => stopped(),
            })?;
        self.wake.wake()?;
        receive.recv().map_err(|_| stopped())
    }

    pub fn stop(&mut self) -> io::Result<()> {
        if let Some(worker) = self.worker.take() {
            self.stopping.store(true, Ordering::Release);
            let _ = self.wake.wake();
            worker
                .join()
                .map_err(|_| io::Error::other("game worker panicked"))??;
        }
        Ok(())
    }
}

impl Drop for GameService {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

struct Player {
    info: MatchPlayer,
    entity: u32,
    gone: bool,
}

impl Player {
    fn quanta(&self) -> u32 {
        if self.info.tournament {
            TOURNAMENT_QUANTA
        } else {
            RETAIL_QUANTA
        }
    }
}

struct Room {
    map: MatchMap,
    players: Vec<Player>,
    remaining: usize,
}

impl Room {
    fn message(&self, slot: usize, initial: bool) -> Message {
        let members: Vec<_> = self
            .players
            .iter()
            .map(|player| Member {
                entity: player.entity,
                name: &player.info.name,
                team: player.info.team,
                gone: player.gone,
                ranked: player.info.ranked,
            })
            .collect();
        let instance = Instance {
            map: self.map.guid,
            mode: self.map.mode,
            team_sizes: self.map.team_sizes,
            free_for_all: self.map.free_for_all,
            members: &members,
        };
        let player = &self.players[slot];
        if initial {
            instance.load(player.entity, player.quanta())
        } else {
            instance.update(player.entity)
        }
    }
}

#[derive(Default)]
struct Peer {
    ingress: GameIngress,
    egress: GameEgress<()>,
    loading: MapReadiness,
    ownership: Option<u32>,
    open: bool,
}

impl Peer {
    fn reliable(&mut self, schemas: &Schemas, message: Message) -> io::Result<u32> {
        let bytes = encode(schemas, message.id, &message.fields).map_err(invalid)?;
        self.egress.queue_reliable(bytes).map_err(invalid)
    }
}

struct State {
    schemas: Arc<Schemas>,
    clock: GameClock,
    links: GameConnections,
    rooms: HashMap<MatchId, Room>,
    seats: HashMap<u32, (MatchId, usize)>,
    peers: IndexMap<u32, Peer>,
    next_connection: u32,
}

impl State {
    fn new(schemas: Arc<Schemas>) -> Self {
        Self {
            schemas,
            clock: GameClock::default(),
            links: GameConnections::default(),
            rooms: HashMap::new(),
            seats: HashMap::new(),
            peers: IndexMap::new(),
            next_connection: 1,
        }
    }

    fn open_match(
        &mut self,
        id: MatchId,
        map: MatchMap,
        players: Vec<MatchPlayer>,
        now: Duration,
    ) -> io::Result<Vec<Handoff>> {
        let remaining = players.len();
        if self.clock.draining() {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "game clock is draining",
            ));
        }
        if self.rooms.contains_key(&id) {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "match already exists",
            ));
        }
        if remaining == 0 || remaining > usize::from(u8::MAX) {
            return Err(invalid("invalid match roster size"));
        }
        let room = Room {
            map,
            players: players
                .into_iter()
                .enumerate()
                .map(|(slot, info)| Player {
                    info,
                    entity: 0xa000_0000 | ((slot as u32 + 1) << 8),
                    gone: false,
                })
                .collect(),
            remaining,
        };
        let initial = room.message(0, true);
        let bytes = encode(&self.schemas, initial.id, &initial.fields).map_err(invalid)?;
        let payload = frame(u32::MAX, u32::MAX, &[(0, bytes)], &[], &[]).map_err(invalid)?;
        if payload.len() > MAX_PAYLOAD {
            return Err(invalid("match roster exceeds the game datagram budget"));
        }

        let mut tickets = Vec::with_capacity(remaining);
        for _ in 0..remaining {
            let connection = self.next_connection;
            self.next_connection = self.next_connection % MAX_CONNECTION_ID + 1;
            if self.seats.contains_key(&connection) {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "game seat ID is still in use",
                ));
            }
            tickets.push(Handoff::new(connection, id)?);
        }
        for (slot, ticket) in tickets.iter().enumerate() {
            let reserved = self
                .links
                .reserve_handoff(ticket, u64::from(ticket.conn), now);
            assert!(reserved, "seat and connection tables are in sync");
            self.seats.insert(ticket.conn, (id, slot));
        }
        self.rooms.insert(id, room);
        Ok(tickets)
    }

    fn handle_request(&mut self, request: Request, now: Duration) {
        match request {
            Request::Open {
                id,
                map,
                players,
                reply,
            } => {
                let result = self.open_match(id, map, players, now);
                if let Err(mpsc::SendError(Ok(tickets))) = reply.send(result) {
                    for ticket in tickets {
                        self.links.cancel_handoff(ticket.conn);
                    }
                }
            }
            Request::Pending { connection, reply } => {
                let _ = reply.send(self.links.handoff_pending(connection, now));
            }
            Request::Cancel { connection, reply } => {
                let _ = reply.send(self.links.cancel_handoff(connection));
            }
        }
    }

    fn retire(&mut self, connection: u32) {
        self.peers.shift_remove(&connection);
        if let Some((id, slot)) = self.seats.remove(&connection) {
            let room = self.rooms.get_mut(&id).expect("reserved match");
            room.players[slot].gone = true;
            room.remaining -= 1;
            if room.remaining == 0 {
                self.rooms.remove(&id);
            }
        }
    }

    fn receive(&mut self, socket: &UdpSocket, bytes: &[u8], address: SocketAddr, now: Duration) {
        let Ok(received) = self.links.receive(bytes, address, now) else {
            return;
        };
        let connection = received.connection;
        if received.admitted {
            self.peers.insert(connection, Peer::default());
        }
        let peer = self.peers.get_mut(&connection).expect("admitted peer");
        if let Some((ack, bits)) = received.ack {
            peer.egress.acknowledge(ack, bits);
        }
        if let Some(Ok(control)) = received.control {
            let _ = send(socket, control);
        }
        if received.closed {
            self.retire(connection);
            return;
        }
        peer.open |= received.opened;
        if self.clock.draining() {
            if received.admitted {
                self.send_home(connection, now);
            }
            return;
        }
        if received.opened {
            let (id, slot) = self.seats[&connection];
            let _ = peer.reliable(&self.schemas, self.rooms[&id].message(slot, true));
        }
        if let Some(payload) = received.payload {
            let Peer {
                ingress,
                egress,
                loading,
                ..
            } = peer;
            let _ = ingress.receive(&self.schemas, &payload, |message| {
                match MessageKind::from(message.id) {
                    MessageKind::Ping => {
                        let received = wall_nanoseconds();
                        if let Ok(pong) = pong(&message.fields, received, wall_nanoseconds()) {
                            if let Ok(bytes) = encode(&self.schemas, pong.id, &pong.fields) {
                                let _ = egress.queue_reliable(bytes);
                            }
                        }
                    }
                    MessageKind::MapLoaded => {
                        loading.mark_loaded(now);
                    }
                    _ => {}
                }
            });
        }
    }

    fn send_home(&mut self, connection: u32, now: Duration) {
        if self.links.begin_leaving(connection, now) {
            let peer = self.peers.get_mut(&connection).expect("admitted peer");
            let _ = peer.reliable(&self.schemas, leave_message());
        }
    }

    fn tick(&mut self, socket: &UdpSocket, now: Duration, missed: u64) {
        let step = self.clock.step(missed, !self.seats.is_empty());
        let (tick, send_frames, drain, force) = match step {
            Step::Simulate { frame, send_world } => (frame, send_world, false, false),
            Step::Drain {
                frame,
                entered,
                force,
                send_reliable,
            } => {
                if entered {
                    for (&connection, peer) in &mut self.peers {
                        if self.links.begin_leaving(connection, now) {
                            let _ = peer.reliable(&self.schemas, leave_message());
                        }
                    }
                }
                (frame, send_reliable, true, force)
            }
        };
        for expired in self.links.expire(now, force) {
            self.retire(expired.connection);
        }
        for (&connection, peer) in &mut self.peers {
            if !drain {
                let (id, slot) = self.seats[&connection];
                let room = &self.rooms[&id];
                let ready = WorldReadiness {
                    ownership_delivered: peer
                        .ownership
                        .is_some_and(|id| peer.egress.delivered_through(id)),
                    ..WorldReadiness::default()
                };
                while let Some(step) = peer.loading.take_due(now, ready) {
                    match step {
                        MapReadyStep::Ownership => {
                            peer.ownership = peer
                                .reliable(&self.schemas, ownership(room.players[slot].entity))
                                .ok();
                        }
                        MapReadyStep::Game => {
                            let _ = peer.reliable(&self.schemas, room.message(slot, false));
                        }
                        MapReadyStep::World | MapReadyStep::Controller => {
                            unreachable!("world entry requires the simulation")
                        }
                    }
                }
            }
            if send_frames && peer.open {
                let _ = peer.egress.send_frame(
                    FrameRecords {
                        tick,
                        queued: &[],
                        movements: &[],
                        owner_correction: false,
                        reliable_only: drain,
                    },
                    |_| tick,
                    |_| (),
                    |payload| {
                        let packet = Packet {
                            conn: connection,
                            kind: KIND_FRAME,
                            flags: ACK,
                            payload,
                            ..Packet::default()
                        };
                        let outgoing = self
                            .links
                            .transmit(packet)
                            .map_err(|error| io::Error::other(format!("game link: {error:?}")))?;
                        send(socket, outgoing)
                    },
                );
            }
        }
        self.clock.renew_if_idle(!self.seats.is_empty(), false);
    }
}

fn leave_message() -> Message {
    Message {
        id: 20304,
        fields: Fields::from([(0x78, Value::Unsigned(0)), (0x79, Value::Bool(false))]),
    }
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

fn stopped() -> io::Error {
    io::Error::new(io::ErrorKind::BrokenPipe, "game worker has stopped")
}

fn wall_nanoseconds() -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    u64::try_from(now.as_nanos()).unwrap_or(u64::MAX)
}

fn send(socket: &UdpSocket, outgoing: Outgoing) -> io::Result<u32> {
    let count = socket.send_to(&outgoing.datagram, outgoing.address)?;
    if count != outgoing.datagram.len() {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "partial game datagram",
        ));
    }
    Ok(outgoing.sequence)
}

fn run(
    socket: UdpSocket,
    mut poll: Poll,
    requests: mpsc::Receiver<Request>,
    stopping: Arc<AtomicBool>,
    schemas: Arc<Schemas>,
) -> io::Result<()> {
    let mut state = State::new(schemas);
    let start = Instant::now();
    let mut due = Duration::ZERO;
    let mut events = Events::with_capacity(2);
    // An oversized datagram must never be accepted as a truncated valid one.
    let mut buffer = [0; 65_536];
    let mut socket_ready = false;
    while !stopping.load(Ordering::Acquire) {
        let mut handled = 0;
        for request in requests.try_iter().take(RECEIVE_BATCH) {
            state.handle_request(request, start.elapsed());
            handled += 1;
        }
        if socket_ready {
            for _ in 0..RECEIVE_BATCH {
                match socket.recv_from(&mut buffer) {
                    Ok((count, address)) => {
                        state.receive(&socket, &buffer[..count], address, start.elapsed());
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        socket_ready = false;
                        break;
                    }
                    Err(error)
                        if matches!(
                            error.kind(),
                            io::ErrorKind::Interrupted
                                | io::ErrorKind::ConnectionReset
                                | io::ErrorKind::ConnectionRefused
                        ) =>
                    {
                        continue;
                    }
                    Err(error) => return Err(error),
                }
            }
        }
        let now = start.elapsed();
        if now >= due {
            let (next, missed) = next_tick_after(due, now)
                .ok_or_else(|| io::Error::other("game tick clock exhausted"))?;
            due = next;
            state.tick(&socket, now, missed);
        }
        let timeout = if socket_ready || handled == RECEIVE_BATCH {
            Duration::ZERO
        } else {
            due.saturating_sub(start.elapsed())
        };
        match poll.poll(&mut events, Some(timeout)) {
            Ok(()) => {
                socket_ready |= events.iter().any(|event| event.token() == GAME_SOCKET);
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        bits::{BitReader, BitWriter},
        game_link::{FIN, KIND_CONTROL, LinkCipher, MAX_DATAGRAM, ReceiveWindow, SYN},
        game_messages::{read_channel, write_channel},
    };
    use std::net::UdpSocket as ClientSocket;

    const MATCH: MatchId = [5, 8];
    const MAP: MatchMap = MatchMap {
        guid: 0x0800_0000_0000_0688,
        mode: 0x0230_0000_0000_0018,
        team_sizes: [1, 0],
        free_for_all: false,
    };

    fn local() -> SocketAddr {
        "127.0.0.1:0".parse().unwrap()
    }

    fn player() -> Vec<MatchPlayer> {
        vec![MatchPlayer {
            name: "Protocol fixture".into(),
            team: 0,
            tournament: false,
            ranked: false,
        }]
    }

    fn fixture() -> Schemas {
        Schemas::from_json(
            r#"{
                "C77F6403": {
                    "20300": [{"type":8,"off":1192,"size":8,"count":0,"array":false}],
                    "20301": [], "20302": [], "20304": [],
                    "20306": [
                        {"type":6,"off":120,"size":4,"count":0,"array":false},
                        {"type":8,"off":128,"size":8,"count":-1,"array":true}
                    ]
                },
                "A7FBBD0C": {
                    "21601": [], "21602": [],
                    "21610": [
                        {"type":6,"off":120,"size":4,"count":0,"array":false},
                        {"type":8,"off":128,"size":8,"count":-1,"array":true}
                    ]
                }
            }"#,
        )
        .unwrap()
    }

    struct Client {
        socket: ClientSocket,
        cipher: LinkCipher,
        connection: u32,
        next: u32,
        window: ReceiveWindow,
    }

    impl Client {
        fn new(address: SocketAddr, ticket: &Handoff) -> Self {
            let socket = ClientSocket::bind(local()).unwrap();
            socket.connect(address).unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            Self {
                socket,
                cipher: LinkCipher::new(ticket.key_out, ticket.key_in),
                connection: ticket.conn,
                next: 0,
                window: ReceiveWindow::default(),
            }
        }

        fn send(&mut self, flags: u8, payload: Vec<u8>) {
            let packet = Packet {
                conn: self.connection,
                kind: if payload.is_empty() {
                    KIND_CONTROL
                } else {
                    KIND_FRAME
                },
                seq: self.next,
                ack: self.window.expected,
                ack_bits: self.window.bits,
                flags,
                payload,
            };
            self.next += 1;
            self.socket
                .send(&self.cipher.seal(&packet).unwrap())
                .unwrap();
        }

        fn receive(&self) -> Packet {
            let mut bytes = [0; MAX_DATAGRAM];
            let count = self.socket.recv(&mut bytes).unwrap();
            let packet = self.cipher.open(&bytes[..count]).unwrap();
            assert_eq!(packet.conn, self.connection);
            packet
        }

        fn handshake(&mut self) {
            self.send(SYN, Vec::new());
            let response = self.receive();
            assert_eq!((response.kind, response.flags), (KIND_CONTROL, SYN | ACK));
            assert!(self.window.accept(response.seq));
            self.send(ACK, Vec::new());
        }
    }

    fn messages(schemas: &Schemas, packet: &Packet) -> Vec<crate::game_messages::ReliableMessage> {
        assert_eq!(packet.kind, KIND_FRAME);
        let mut reader = BitReader::new(&packet.payload);
        reader.var_a().unwrap();
        read_channel(&mut reader, schemas).unwrap().reliable
    }

    fn exercise_loading(schemas: Schemas) {
        let schemas = Arc::new(schemas);
        let mut server = GameService::bind(local(), Arc::clone(&schemas)).unwrap();
        let mut tickets = server.open_match(MATCH, MAP, player()).unwrap();
        let ticket = tickets.pop().unwrap();
        assert!(server.handoff_pending(ticket.conn).unwrap());
        let mut client = Client::new(server.local_addr(), &ticket);
        client.handshake();
        assert!(!server.handoff_pending(ticket.conn).unwrap());

        let first = client.receive();
        let initial = messages(&schemas, &first);
        assert_eq!((initial[0].sequence, initial[0].message.id), (0, 20300));
        assert_eq!(initial[0].message.fields[&0x4a8], Value::Unsigned(MAP.guid));
        // Drop the first instance datagram, then ACK the following heartbeat.
        let heartbeat = client.receive();
        assert!(messages(&schemas, &heartbeat).is_empty());
        assert!(client.window.accept(heartbeat.seq));
        client.send(ACK, Vec::new());
        let until = Instant::now() + Duration::from_secs(2);
        let retry = loop {
            let packet = client.receive();
            if !messages(&schemas, &packet).is_empty() {
                break packet;
            }
            assert!(Instant::now() < until, "waiting for retransmitted instance");
        };
        assert_eq!(messages(&schemas, &retry), initial);
        assert!(client.window.accept(retry.seq));

        let loaded = encode(&schemas, 21602, &Fields::new()).unwrap();
        let ping = encode(
            &schemas,
            21610,
            &Fields::from([
                (0x78, Value::Unsigned(17)),
                (0x80, Value::Array(vec![Value::Unsigned(25)])),
            ]),
        )
        .unwrap();
        let mut payload = BitWriter::new();
        write_channel(&mut payload, &[(0, loaded)], &[ping]).unwrap();
        let start = Instant::now();
        client.send(ACK, payload.into_bytes());
        let mut received = Vec::new();
        while received.len() < 3 && start.elapsed() < Duration::from_secs(2) {
            let packet = client.receive();
            assert!(client.window.accept(packet.seq));
            received.extend(messages(&schemas, &packet));
            client.send(ACK, Vec::new());
        }
        assert_eq!(
            received
                .iter()
                .map(|message| (message.sequence, message.message.id))
                .collect::<Vec<_>>(),
            [(1, 20306), (2, 20302), (3, 20301)]
        );
        assert!(start.elapsed() >= Duration::from_millis(400));
        assert_eq!(received[0].message.fields[&0x78], Value::Unsigned(17));
        let Value::Array(stamps) = &received[0].message.fields[&0x80] else {
            panic!("expected ping timestamps");
        };
        assert_eq!(stamps.len(), 3);
        assert_eq!(stamps[0], Value::Unsigned(25));

        client.send(FIN | ACK, Vec::new());
        let until = Instant::now() + Duration::from_secs(2);
        loop {
            let packet = client.receive();
            if packet.kind == KIND_CONTROL {
                assert_eq!(packet.flags, FIN | ACK);
                break;
            }
            assert!(Instant::now() < until, "waiting for FIN acknowledgement");
        }
        let address = server.local_addr();
        server.stop().unwrap();
        assert!(ClientSocket::bind(address).is_ok());
        assert_eq!(
            server
                .open_match(MATCH, MAP, player())
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::BrokenPipe
        );
    }

    #[test]
    fn worker_serves_a_match_seat_and_retries_before_map_entry() {
        exercise_loading(fixture());
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 game schemas"]
    fn worker_serves_loading_using_the_retail_schemas() {
        let root = std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let schemas = Schemas::load(root.join("data/game_schemas_174.json")).unwrap();
        exercise_loading(schemas);
    }

    #[test]
    fn canceled_seat_is_rejected_before_tick_and_released() {
        let mut server = GameService::bind(local(), Arc::new(fixture())).unwrap();
        let ticket = server
            .open_match(MATCH, MAP, player())
            .unwrap()
            .pop()
            .unwrap();
        assert!(server.handoff_pending(ticket.conn).unwrap());
        assert!(server.cancel_handoff(ticket.conn).unwrap());
        assert!(!server.handoff_pending(ticket.conn).unwrap());
        let mut client = Client::new(server.local_addr(), &ticket);
        client
            .socket
            .set_read_timeout(Some(Duration::from_millis(100)))
            .unwrap();
        client.send(SYN, Vec::new());
        let mut buffer = [0; MAX_DATAGRAM];
        let error = client.socket.recv(&mut buffer).unwrap_err();
        assert!(matches!(
            error.kind(),
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
        ));
        let retry = server
            .open_match(MATCH, MAP, player())
            .unwrap()
            .pop()
            .unwrap();
        assert_ne!(ticket.conn, retry.conn);
        server.stop().unwrap();
    }
}
