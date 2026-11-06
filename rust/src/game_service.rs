//! The native UDP match worker. It owns seats, connections and frame timing.
//!
//! A prepared solo Practice Range scene can advance from loading into its
//! initial world. Maps that need the unported bot VM retain that requirement.

use std::{
    collections::{HashMap, HashSet},
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
    game_bots::BOT_RECORD_DELAY,
    game_clock::{FIRST_TICK, GameClock, SEND_EVERY, Step, next_tick_after},
    game_connection::{GameConnections, MAX_CONNECTION_ID, Outgoing},
    game_content::{GameMap, PRACTICE_RANGE_MODE},
    game_controller::ControllerFrame,
    game_egress::{DeliveryReport, FrameRecords, GameEgress, RecordSource},
    game_ingress::{GameIngress, MapReadiness, MapReadyStep, MessageKind, WorldReadiness, pong},
    game_instance::{Instance, Member, RETAIL_QUANTA, TOURNAMENT_QUANTA, ownership},
    game_link::{ACK, KIND_FRAME, Packet},
    game_messages::{Message, encode, locate},
    jam_codec::{Fields, Schemas, Value},
    match_handoff::Handoff,
    match_scene::{PlayerAppearance, PlayerCard},
    scene_assets::PreparedScene,
    script_wire::chunk,
    world::{EntityUpdate, GAME_MODE_ENTITY, MAX_PAYLOAD, Operation, frame},
};

const GAME_SOCKET: Token = Token(0);
const REQUESTS: Token = Token(1);
const RECEIVE_BATCH: usize = 64;

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
    Open(Box<OpenRequest>),
    Pending {
        connection: u32,
        reply: mpsc::SyncSender<bool>,
    },
    Cancel {
        connection: u32,
        reply: mpsc::SyncSender<bool>,
    },
}

struct OpenRequest {
    id: MatchId,
    map: GameMap,
    players: Vec<MatchPlayer>,
    world: Option<WorldStart>,
    reply: mpsc::SyncSender<io::Result<Vec<Handoff>>>,
}

struct WorldStart {
    scene: Box<PreparedScene>,
    card: PlayerCard,
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
        map: GameMap,
        players: Vec<MatchPlayer>,
    ) -> io::Result<Vec<Handoff>> {
        self.request(|reply| {
            Request::Open(Box::new(OpenRequest {
                id,
                map,
                players,
                world: None,
                reply,
            }))
        })?
    }

    /// Open the currently ported world bootstrap. The graph-backed NPC
    /// behavior from the enriched bundle is a separate simulation prerequisite.
    pub fn open_prepared_practice(
        &self,
        id: [u64; 2],
        scene: PreparedScene,
        player: MatchPlayer,
        card: PlayerCard,
    ) -> io::Result<Handoff> {
        if scene.map().mode_guid != PRACTICE_RANGE_MODE || scene.scripted_bots() != 0 {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "the native world bootstrap requires the solo Practice Range without bot VM graphs",
            ));
        }
        let viewer = PlayerAppearance {
            entity: player_entity(0),
            slot: 0,
            team: player.team,
            card: &card,
        };
        let creates = scene.initial_world(&viewer)?;
        let controller = initial_controller(&scene, FIRST_TICK)?;
        let scripts = [
            script_record(viewer.entity, &controller)?,
            script_record(GAME_MODE_ENTITY, scene.mode_ui())?,
        ];
        for record in creates.iter().chain(&scripts) {
            let packet = frame(u32::MAX, u32::MAX, &[], &[], &[record]).map_err(invalid)?;
            if packet.len() > MAX_PAYLOAD {
                return Err(invalid("a world baseline exceeds the game datagram budget"));
            }
        }
        let map = scene.map().clone();
        let mut handoffs = self.request(|reply| {
            Request::Open(Box::new(OpenRequest {
                id,
                map,
                players: vec![player],
                world: Some(WorldStart {
                    scene: Box::new(scene),
                    card,
                }),
                reply,
            }))
        })??;
        handoffs
            .pop()
            .ok_or_else(|| invalid("a solo world requires a seat"))
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
    map: GameMap,
    players: Vec<Player>,
    remaining: usize,
    world: Option<WorldStart>,
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
            map: &self.map,
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
    egress: GameEgress<Arc<EntityUpdate>>,
    loading: MapReadiness,
    ownership: Option<u32>,
    open: bool,
    world: WorldDelivery,
}

#[derive(Default)]
struct WorldDelivery {
    queued: Vec<Arc<EntityUpdate>>,
    created: HashSet<u32>,
    started: Option<u32>,
    presentation_due: bool,
    controller_queued: bool,
    mode_queued: bool,
}

impl WorldDelivery {
    fn acknowledge(&mut self, report: DeliveryReport<Arc<EntityUpdate>>) {
        if self.started.is_none() {
            return;
        }
        let mut retry = Vec::new();
        for packet in report.packets {
            if packet.arrived {
                for update in packet.records {
                    if matches!(update.operation, Some(Operation::Create(_))) {
                        self.created.insert(update.entity);
                    }
                }
            } else {
                retry.extend(packet.records);
            }
        }
        if !retry.is_empty() {
            retry.append(&mut self.queued);
            self.queued = retry;
        }
    }

    fn start(&mut self, world: &WorldStart, player: &Player, tick: u32) -> io::Result<()> {
        let viewer = PlayerAppearance {
            entity: player.entity,
            slot: 0,
            team: player.info.team,
            card: &world.card,
        };
        let records = world.scene.initial_world(&viewer)?;
        self.queued.extend(records.into_iter().map(Arc::new));
        self.started = Some(tick);
        Ok(())
    }

    fn present(&mut self, scene: &PreparedScene, entity: u32, tick: u32) -> io::Result<()> {
        if !self.controller_queued && self.created.contains(&entity) {
            let payload = initial_controller(scene, tick)?;
            self.queued.push(Arc::new(script_record(entity, &payload)?));
            self.controller_queued = true;
        }
        if !self.mode_queued && self.created.contains(&GAME_MODE_ENTITY) {
            self.queued
                .push(Arc::new(script_record(GAME_MODE_ENTITY, scene.mode_ui())?));
            self.mode_queued = true;
        }
        self.presentation_due = !(self.controller_queued && self.mode_queued);
        Ok(())
    }

    fn movement(&self, scene: &PreparedScene, tick: u32) -> Vec<EntityUpdate> {
        if !self
            .started
            .is_some_and(|started| tick.saturating_sub(started) >= BOT_RECORD_DELAY)
        {
            return Vec::new();
        }
        scene
            .bots()
            .iter()
            .filter(|bot| self.created.contains(&bot.entity) && bot.record_due(tick, SEND_EVERY))
            .map(|bot| EntityUpdate {
                entity: bot.entity,
                movement: Some(bot.movement(tick)),
                ..EntityUpdate::default()
            })
            .collect()
    }
}

fn initial_controller(scene: &PreparedScene, tick: u32) -> io::Result<crate::bits::BitWriter> {
    scene
        .controller()
        .controller_frame(
            tick,
            ControllerFrame {
                select_open: true,
                skins: true,
                hide_assemble: true,
                ..ControllerFrame::default()
            },
        )
        .map_err(invalid)
}

fn script_record(entity: u32, payload: &crate::bits::BitWriter) -> io::Result<EntityUpdate> {
    Ok(EntityUpdate {
        entity,
        chunk: Some(chunk(payload, 0, 1).map_err(invalid)?),
        ..EntityUpdate::default()
    })
}

fn player_entity(slot: usize) -> u32 {
    0xa000_0000 | ((slot as u32 + 1) << 8)
}

impl Peer {
    fn reliable(&mut self, schemas: &Schemas, message: Message) -> io::Result<u32> {
        let bytes = encode(schemas, message.id, &message.fields).map_err(invalid)?;
        self.egress.queue_reliable(bytes).map_err(invalid)
    }

    fn advance_loading(
        &mut self,
        schemas: &Schemas,
        room: &Room,
        slot: usize,
        now: Duration,
        tick: u32,
    ) -> io::Result<()> {
        let player = &room.players[slot];
        let ready = WorldReadiness {
            ownership_delivered: self
                .ownership
                .is_some_and(|id| self.egress.delivered_through(id)),
            collision_ready: room.world.is_some(),
            bot_scripts_ready: room
                .world
                .as_ref()
                .is_some_and(|world| world.scene.scripted_bots() == 0),
        };
        while let Some(step) = self.loading.take_due(now, ready) {
            match step {
                MapReadyStep::Ownership => {
                    self.ownership = Some(self.reliable(schemas, ownership(player.entity))?);
                }
                MapReadyStep::Game => {
                    self.reliable(schemas, room.message(slot, false))?;
                }
                MapReadyStep::World => {
                    let world = room.world.as_ref().expect("map-entry prerequisites");
                    self.world.start(world, player, tick)?;
                }
                MapReadyStep::Controller => self.world.presentation_due = true,
            }
        }
        if self.world.presentation_due {
            let world = room.world.as_ref().expect("world started");
            self.world.present(&world.scene, player.entity, tick)?;
        }
        Ok(())
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
        map: GameMap,
        players: Vec<MatchPlayer>,
        world: Option<WorldStart>,
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
                    entity: player_entity(slot),
                    gone: false,
                })
                .collect(),
            remaining,
            world,
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
            Request::Open(request) => {
                let OpenRequest {
                    id,
                    map,
                    players,
                    world,
                    reply,
                } = *request;
                let result = self.open_match(id, map, players, world, now);
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
            let report = peer.egress.acknowledge(ack, bits);
            if !self.clock.draining() {
                peer.world.acknowledge(report);
            }
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
                        peer.world.queued.clear();
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
        if !drain {
            for room in self.rooms.values_mut() {
                if let Some(world) = &mut room.world {
                    world.scene.step_bots(tick);
                }
            }
        }
        for (&connection, peer) in &mut self.peers {
            let (id, slot) = self.seats[&connection];
            let room = &self.rooms[&id];
            if !drain
                && peer
                    .advance_loading(&self.schemas, room, slot, now, tick)
                    .is_err()
            {
                peer.world = WorldDelivery::default();
                peer.loading = MapReadiness::default();
                if self.links.begin_leaving(connection, now) {
                    let _ = peer.reliable(&self.schemas, leave_message());
                }
            }
            if send_frames && peer.open {
                let movements = if drain {
                    Vec::new()
                } else {
                    room.world
                        .as_ref()
                        .map_or_else(Vec::new, |world| peer.world.movement(&world.scene, tick))
                };
                let queued = &peer.world.queued;
                let sent = peer.egress.send_frame(
                    FrameRecords {
                        tick,
                        queued,
                        movements: &movements,
                        owner_correction: false,
                        reliable_only: drain,
                    },
                    |_| tick,
                    |source| match source {
                        RecordSource::Queued(index) => Arc::clone(&queued[index]),
                        RecordSource::Movement(index) => Arc::new(movements[index].clone()),
                    },
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
                if let Ok(sent) = sent {
                    let count = sent
                        .records
                        .iter()
                        .take_while(|source| matches!(source, RecordSource::Queued(_)))
                        .count();
                    peer.world.queued.drain(..count);
                }
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
        game_content::PRACTICE_RANGE_MAP,
        game_link::{FIN, KIND_CONTROL, LinkCipher, MAX_DATAGRAM, ReceiveWindow, SYN},
        game_messages::{read_channel, write_channel},
    };
    use std::net::UdpSocket as ClientSocket;

    const MATCH: MatchId = [5, 8];

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
        let mut tickets = server
            .open_match(MATCH, GameMap::practice_range(), player())
            .unwrap();
        let ticket = tickets.pop().unwrap();
        assert!(server.handoff_pending(ticket.conn).unwrap());
        let mut client = Client::new(server.local_addr(), &ticket);
        client.handshake();
        assert!(!server.handoff_pending(ticket.conn).unwrap());

        let first = client.receive();
        let initial = messages(&schemas, &first);
        assert_eq!((initial[0].sequence, initial[0].message.id), (0, 20300));
        assert_eq!(
            initial[0].message.fields[&0x4a8],
            Value::Unsigned(PRACTICE_RANGE_MAP)
        );
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
                .open_match(MATCH, GameMap::practice_range(), player())
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
            .open_match(MATCH, GameMap::practice_range(), player())
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
            .open_match(MATCH, GameMap::practice_range(), player())
            .unwrap()
            .pop()
            .unwrap();
        assert_ne!(ticket.conn, retry.conn);
        server.stop().unwrap();
    }

    #[derive(Debug)]
    struct WorldRecord {
        entity: u32,
        create: bool,
        movement: bool,
        script: Option<Vec<u8>>,
    }

    fn world_records(schemas: &Schemas, packet: &Packet) -> Vec<WorldRecord> {
        let mut reader = BitReader::new(&packet.payload);
        reader.var_a().unwrap();
        read_channel(&mut reader, schemas).unwrap();
        reader.var_a().unwrap();
        assert_eq!(reader.bits(7).unwrap(), 0b010_0000);
        let count = reader.bits(7).unwrap();
        (0..count)
            .map(|_| {
                let mut record = WorldRecord {
                    entity: reader.entity_id().unwrap().unwrap(),
                    create: false,
                    movement: false,
                    script: None,
                };
                assert!(reader.bit().unwrap());
                if !reader.bit().unwrap() {
                    return record;
                }
                if reader.bit().unwrap() {
                    assert!(!reader.bit().unwrap());
                    assert_eq!(reader.w_u32().unwrap(), 0);
                    assert_eq!(reader.w_var().unwrap(), 1);
                    assert!(!reader.bit().unwrap());
                    let width = reader.w_var().unwrap() as usize;
                    record.script = Some(reader.raw_bits(width).unwrap());
                }
                if !reader.bit().unwrap() {
                    return record;
                }
                assert_eq!(reader.bits(2).unwrap(), 0);
                assert!(reader.bit().unwrap());
                record.movement = reader.bit().unwrap();
                if record.movement {
                    let length = reader.bits(12).unwrap() as usize;
                    reader.advance(length - 12).unwrap();
                }
                if reader.bit().unwrap() {
                    let operation = reader.bits(2).unwrap();
                    record.create = operation == 1;
                    if operation != 3 {
                        let length = reader.bits(14).unwrap() as usize;
                        reader.advance(length).unwrap();
                    }
                    assert!(!reader.bit().unwrap());
                }
                record
            })
            .collect()
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range scene"]
    fn prepared_world_waits_for_ownership_and_retries_baselines_before_scripts() {
        use crate::{
            accounts::Accounts,
            catalog::ItemDb,
            collection::Collection,
            game_bots::BOT_ENTITY,
            jam_codec::id16,
            lobby_login::Identity,
            lobby_practice::{CREATE_GAME, GAME_REQUEST, SoloPracticeHandoff, dispatch_prepared},
            lobby_session::PreparedLogin,
            match_handoff::{HANDOFF, HANDOFF_MESSAGE},
            retail_templates::RetailTemplates,
            scene_assets::SceneAssets,
        };

        const CONTROLLER: u32 = 0xa000_0100;
        let runtime = std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let data = runtime.join("data");
        let schemas = Arc::new(Schemas::load(data.join("game_schemas_174.json")).unwrap());
        let lobby = Schemas::load(data.join("schemas_174.json")).unwrap();
        let assets = SceneAssets::load(runtime).unwrap();
        let scripted_bots = assets.graphs().entity_body(0x0400_0000_0000_143e).is_some();
        let collection = Collection::new(
            &RetailTemplates::load(data.join("retail_templates.json")).unwrap(),
            Arc::new(ItemDb::load(&data).unwrap()),
        )
        .unwrap();
        let temporary = tempfile::tempdir().unwrap();
        let accounts = Accounts::new(
            temporary.path().join("profiles"),
            temporary.path().join("seed"),
        )
        .unwrap();
        let account = accounts.get("world fixture").unwrap();
        let login = PreparedLogin {
            identity: Identity::create(account.account_lo, 1).unwrap(),
            account,
            tournament: false,
        };
        let fields = lobby
            .decode(GAME_REQUEST, CREATE_GAME, b"\x02\x00\x04\x00\x00\x00", true)
            .unwrap();
        let request = (GAME_REQUEST, CREATE_GAME, fields);
        let mut server = GameService::bind(local(), Arc::clone(&schemas)).unwrap();
        let channel = |id: MatchId, kind| {
            Fields::from([
                (0, Value::Struct(id16(id[0], id[1]))),
                (0x10, Value::Unsigned(kind)),
            ])
        };
        let mut outgoing = Vec::new();
        let result = dispatch_prepared(
            &request,
            &login,
            &server,
            SoloPracticeHandoff {
                match_id: MATCH,
                host: "127.0.0.1",
                general: channel([7, 8], 7),
                match_channel: channel(MATCH, 6),
            },
            &assets,
            &collection,
            |(crc, id, fields)| {
                let bytes = lobby.encode(*crc, *id, fields).unwrap();
                outgoing.push((*crc, *id, lobby.decode(*crc, *id, &bytes, true).unwrap()));
                Ok(true)
            },
        );
        if scripted_bots {
            assert_eq!(result.err().unwrap().kind(), io::ErrorKind::Unsupported);
            assert!(outgoing.is_empty());
            return;
        }
        let ticket = result.unwrap().unwrap().handoff;
        assert_eq!(
            outgoing.iter().map(|(_, id, _)| *id).collect::<Vec<_>>(),
            [20404, 20402, HANDOFF_MESSAGE]
        );
        assert_eq!(outgoing[2].0, HANDOFF);
        let Value::Struct(wire) = &outgoing[2].2[&0x80] else {
            panic!("the decoded handoff has no seat body");
        };
        let key = |offset| {
            let Value::Array(bytes) = &wire[&offset] else {
                panic!("the decoded handoff has no direction key");
            };
            std::array::from_fn(|index| match bytes[index] {
                Value::Unsigned(byte) => u8::try_from(byte).unwrap(),
                _ => panic!("the handoff key contains an invalid byte"),
            })
        };
        assert_eq!(wire[&0x28], Value::Unsigned(u64::from(ticket.conn)));
        assert_eq!(
            wire[&0x2c],
            Value::Unsigned(u64::from(server.local_addr().port()))
        );
        let client_ticket = Handoff {
            key_in: key(0xae),
            key_out: key(0xce),
            ..ticket
        };
        let mut client = Client::new(server.local_addr(), &client_ticket);
        client.handshake();
        let load = client.receive();
        assert_eq!(messages(&schemas, &load)[0].message.id, 20300);
        assert!(client.window.accept(load.seq));
        let mut payload = BitWriter::new();
        let loaded = encode(&schemas, 21602, &Fields::new()).unwrap();
        write_channel(&mut payload, &[(0, loaded)], &[]).unwrap();
        let started = Instant::now();
        client.send(ACK, payload.into_bytes());

        let mut ownership = false;
        while started.elapsed() < Duration::from_millis(650) {
            let frame = client.receive();
            assert!(client.window.accept(frame.seq));
            assert!(world_records(&schemas, &frame).is_empty());
            ownership |= messages(&schemas, &frame)
                .iter()
                .any(|message| message.message.id == 20302);
        }
        assert!(ownership);
        client.send(ACK, Vec::new());

        let deadline = Instant::now() + Duration::from_secs(3);
        let lost = loop {
            let frame = client.receive();
            let records = world_records(&schemas, &frame);
            let creates: Vec<_> = records
                .iter()
                .filter(|record| record.create)
                .map(|record| record.entity)
                .collect();
            if !creates.is_empty() {
                assert!(creates.contains(&CONTROLLER) && creates.contains(&GAME_MODE_ENTITY));
                break creates; // Deliberately leave the first baseline out of the ACK window.
            }
            assert!(client.window.accept(frame.seq));
            client.send(ACK, Vec::new());
            assert!(Instant::now() < deadline, "waiting for first world creates");
        };
        let mut creates = HashSet::new();
        loop {
            let frame = client.receive();
            assert!(client.window.accept(frame.seq));
            let records = world_records(&schemas, &frame);
            assert!(records.iter().all(|record| record.script.is_none()));
            for record in records.iter().filter(|record| record.create) {
                assert!(creates.insert(record.entity));
            }
            if lost.iter().all(|id| creates.contains(id)) {
                break;
            }
            client.send(ACK, Vec::new());
            assert!(Instant::now() < deadline, "waiting for baseline retransmit");
        }
        assert_eq!(creates.len(), 21);

        // Even after the retransmission, the script stays behind its create ACK.
        let withheld = client.receive();
        assert!(client.window.accept(withheld.seq));
        assert!(
            world_records(&schemas, &withheld)
                .iter()
                .all(|record| record.script.is_none())
        );
        client.send(ACK, Vec::new());

        let mut mode = false;
        let mut controller = false;
        let mut moving_bot = false;
        while !(mode && controller && moving_bot) && Instant::now() < deadline {
            let frame = client.receive();
            assert!(client.window.accept(frame.seq));
            for record in world_records(&schemas, &frame) {
                assert!(!record.create);
                if record.movement {
                    assert!((BOT_ENTITY..BOT_ENTITY + 15).contains(&record.entity));
                    assert!(creates.contains(&record.entity));
                    moving_bot = true;
                }
                if let Some(script) = record.script {
                    if record.entity == GAME_MODE_ENTITY {
                        assert_eq!(script, [0x08, 0x5b, 0x06, 0x00, 0x0c]);
                        mode = true;
                    } else {
                        assert_eq!(record.entity, CONTROLLER);
                        let mut bits = BitReader::new(&script);
                        assert_eq!(bits.bits(3).unwrap(), 2);
                        assert!(bits.w_var().unwrap() >= crate::game_clock::FIRST_TICK);
                        controller = true;
                    }
                }
            }
            client.send(ACK, Vec::new());
        }
        assert!(mode && controller && moving_bot);
        server.stop().unwrap();
    }
}
