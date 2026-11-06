//! The native UDP match worker. It owns seats, connections and frame timing.
//!
//! A prepared solo Practice Range scene can advance from loading into its
//! initial world. Maps that need the unported bot VM retain that requirement.

use std::{
    collections::{HashMap, VecDeque},
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
    game_clock::{FIRST_TICK, GameClock, Step, next_tick_after},
    game_combat::{BotHit, BotReplacement, Combat, ShotContext},
    game_combat_messages::CombatNotice,
    game_connection::{GameConnections, MAX_CONNECTION_ID, Outgoing, SILENCE_TIME},
    game_content::{GameMap, PRACTICE_RANGE_MODE},
    game_controller::ControllerFrame,
    game_egress::GameEgress,
    game_ingress::{GameIngress, MapReadiness, MapReadyStep, MessageKind, WorldReadiness, pong},
    game_instance::{Instance, Member, RETAIL_QUANTA, TOURNAMENT_QUANTA, ownership},
    game_link::{ACK, KIND_FRAME, Packet},
    game_messages::{Message, encode, locate},
    game_replication::{Record, WorldDelivery},
    jam_codec::{Fields, Schemas, Value},
    match_handoff::Handoff,
    match_scene::{PlayerAppearance, PlayerCard},
    scene_assets::PreparedScene,
    script_wire::chunk,
    world::{EntityUpdate, GAME_MODE_ENTITY, MAX_PAYLOAD, frame},
};

const GAME_SOCKET: Token = Token(0);
const REQUESTS: Token = Token(1);
const RECEIVE_BATCH: usize = 64;
const INPUT_PER_PEER: usize = 16;
const INPUT_PER_TURN: usize = 64;
const MAX_PENDING_COMBAT_NOTICES: usize = 256;

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
    Seat {
        connection: u32,
        id: MatchId,
        release: bool,
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
    combat: Option<Combat>,
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
        if scene.map().mode_guid != PRACTICE_RANGE_MODE || !scene.movement_ready() {
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
                    combat: None,
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

    pub fn seat_exists(&self, handoff: &Handoff) -> io::Result<bool> {
        self.seat(handoff, false)
    }

    pub fn release_seat(&self, handoff: &Handoff) -> io::Result<bool> {
        self.seat(handoff, true)
    }

    fn seat(&self, handoff: &Handoff, release: bool) -> io::Result<bool> {
        self.request(|reply| Request::Seat {
            connection: handoff.conn,
            id: handoff.match_id,
            release,
            reply,
        })
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
    fn appearance(&self, slot: usize) -> Option<PlayerAppearance<'_>> {
        let player = &self.players[slot];
        Some(PlayerAppearance {
            entity: player.entity,
            slot: i8::try_from(slot).ok()?,
            team: player.info.team,
            card: &self.world.as_ref()?.card,
        })
    }

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

struct Peer {
    ingress: GameIngress,
    egress: GameEgress<Arc<Record>>,
    loading: MapReadiness,
    ownership: Option<u32>,
    open: bool,
    world: WorldDelivery,
    combat_ui: CombatUi,
}

#[derive(Default)]
struct CombatUi {
    notices: VecDeque<CombatNotice>,
    last_notice_by_entity: HashMap<u32, u32>,
    replacements: VecDeque<(Duration, BotReplacement)>,
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
    fn new(owner: u32) -> Self {
        Self {
            ingress: GameIngress::default(),
            egress: GameEgress::default(),
            loading: MapReadiness::default(),
            ownership: None,
            open: false,
            world: WorldDelivery::new(owner),
            combat_ui: CombatUi::default(),
        }
    }

    fn clear_world(&mut self) {
        self.world.clear();
        self.combat_ui = CombatUi::default();
    }

    fn bot_hit(&mut self, hit: BotHit) {
        self.world.queue_bot_health(hit.health);
        for notice in hit.notices.into_iter().flatten() {
            if self.combat_ui.notices.len() == MAX_PENDING_COMBAT_NOTICES {
                self.combat_ui.notices.pop_front();
            }
            self.combat_ui.notices.push_back(notice);
        }
    }

    fn flush_combat(&mut self, schemas: &Schemas, now: Duration) {
        for _ in 0..self.combat_ui.notices.len() {
            let notice = self
                .combat_ui
                .notices
                .pop_front()
                .expect("pending combat notice");
            let entity = notice.required_identity();
            if entity.is_some_and(|entity| !self.world.identity_ready(entity))
                || (notice.needs_mode_ui() && !self.world.mode_ready())
            {
                self.combat_ui.notices.push_back(notice);
                continue;
            }
            if let Ok(bytes) = notice.encode(schemas) {
                if let (Ok(ordinal), Some(entity)) = (self.egress.queue_reliable(bytes), entity) {
                    self.combat_ui.last_notice_by_entity.insert(entity, ordinal);
                }
            }
        }
        for _ in 0..self.combat_ui.replacements.len() {
            let (expires, replacement) = self
                .combat_ui
                .replacements
                .pop_front()
                .expect("pending bot replacement");
            let entity = replacement.retired;
            let has_notice = self
                .combat_ui
                .notices
                .iter()
                .any(|notice| notice.required_identity() == Some(entity));
            let waiting_for_reliable = self
                .combat_ui
                .last_notice_by_entity
                .get(&entity)
                .is_some_and(|&ordinal| !self.egress.delivered_through(ordinal));
            if now < expires
                && (has_notice || waiting_for_reliable || self.world.bot_health_pending(entity))
            {
                self.combat_ui
                    .replacements
                    .push_back((expires, replacement));
            } else {
                self.combat_ui
                    .notices
                    .retain(|notice| notice.required_identity() != Some(entity));
                self.combat_ui.last_notice_by_entity.remove(&entity);
                self.world.replace_bot(replacement);
            }
        }
    }

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
                    let viewer = room.appearance(slot).expect("prepared player");
                    self.world.start(&world.scene, &viewer, tick)?;
                }
                MapReadyStep::Controller => self.world.presentation_due = true,
            }
        }
        if self.world.presentation_due {
            let world = room.world.as_ref().expect("world started");
            self.world.present(&world.scene, tick)?;
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
    next_input_peer: usize,
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
            next_input_peer: 0,
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
            Request::Seat {
                connection,
                id,
                release,
                reply,
            } => {
                let exists = self
                    .seats
                    .get(&connection)
                    .is_some_and(|&(match_id, _)| match_id == id);
                if release && exists && !self.links.cancel_handoff(connection) {
                    self.send_home(connection, now);
                }
                let _ = reply.send(exists);
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
        let (id, slot) = self.seats[&connection];
        if received.admitted {
            self.peers
                .insert(connection, Peer::new(self.rooms[&id].players[slot].entity));
        }
        let peer = self.peers.get_mut(&connection).expect("admitted peer");
        if let Some((ack, bits)) = received.ack {
            let report = peer.egress.acknowledge(ack, bits);
            if !self.clock.draining() {
                peer.world.acknowledge(report, now);
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
        let room = &self.rooms[&id];
        if received.opened {
            let _ = peer.reliable(&self.schemas, room.message(slot, true));
        }
        if let Some(payload) = received.payload {
            let Peer {
                ingress,
                egress,
                loading,
                world,
                ..
            } = peer;
            let commands = ingress.receive(&self.schemas, &payload, |message| {
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
                    MessageKind::GameMessage if room.world.is_some() => {
                        world.queue_game_message(self.clock.frame(), now, message);
                    }
                    _ => {}
                }
            });
            if let (Ok(commands), Some(_)) = (commands, &room.world) {
                world.queue_commands(self.clock.frame(), &commands);
            }
        }
    }

    /// Bounded round-robin input work between receive batches and tick checks.
    /// A busy peer cannot consume the next peer's whole turn.
    fn advance_input(&mut self, now: Duration) -> bool {
        let count = self.peers.len();
        if count == 0 || self.clock.draining() {
            return false;
        }
        let start = self.next_input_peer % count;
        let tick = self.clock.frame();
        let mut remaining = INPUT_PER_TURN;
        for offset in 0..count {
            let index = (start + offset) % count;
            let (&connection, peer) = self.peers.get_index_mut(index).expect("active peer index");
            if !peer.world.has_input_work() {
                continue;
            }
            let (id, slot) = self.seats[&connection];
            let room = self.rooms.get_mut(&id).expect("prepared room with input");
            let world = room.world.as_ref().expect("prepared world with input");
            let viewer = room.appearance(slot).expect("prepared player");
            let context = ShotContext {
                team: viewer.team,
                quanta: room.players[slot].quanta(),
                tick,
                now,
            };
            let budget = remaining.min(INPUT_PER_PEER);
            let (used, leave) = peer
                .world
                .advance_input(&world.scene, &viewer, context.quanta, budget)
                .unwrap_or((budget, true));
            remaining -= used;
            self.next_input_peer = (index + 1) % count;
            let shots = peer.world.take_hitscan();
            if (!leave && peer.world.combat_body_active()) || !shots.is_empty() {
                let world = room.world.as_mut().expect("prepared world with combat");
                let combat = world
                    .combat
                    .get_or_insert_with(|| Combat::new(&world.scene));
                for shot in shots {
                    if let Ok(Some(hit)) =
                        combat.hitscan(&mut world.scene, shot, context, |entity| {
                            peer.world.identity_ready(entity)
                        })
                    {
                        peer.bot_hit(hit);
                    }
                }
            }
            if leave {
                peer.clear_world();
                peer.loading = MapReadiness::default();
                if self.links.begin_leaving(connection, now) {
                    let _ = peer.reliable(&self.schemas, leave_message());
                }
            }
            if remaining == 0 {
                break;
            }
        }
        self.peers.values().any(|peer| peer.world.has_input_work())
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
                        peer.clear_world();
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
        let mut spawned = HashMap::new();
        if !drain {
            for (&id, room) in &mut self.rooms {
                if let Some(world) = &mut room.world {
                    world.scene.step_bots(tick);
                    if let Some(combat) = &mut world.combat {
                        if let Ok(replacements) = combat.respawn_due(&mut world.scene, tick, now) {
                            if !replacements.is_empty() {
                                spawned.insert(id, replacements);
                            }
                        }
                        combat.record_poses(&world.scene, tick);
                    }
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
                peer.clear_world();
                peer.loading = MapReadiness::default();
                if self.links.begin_leaving(connection, now) {
                    let _ = peer.reliable(&self.schemas, leave_message());
                }
            }
            if let Some(replacements) = spawned.get(&id).filter(|_| peer.world.world_started()) {
                peer.combat_ui.replacements.extend(
                    replacements
                        .iter()
                        .cloned()
                        .map(|bot| (now.saturating_add(SILENCE_TIME), bot)),
                );
            }
            if send_frames && peer.open {
                if !drain {
                    peer.flush_combat(&self.schemas, now);
                }
                let movements = if drain {
                    Vec::new()
                } else {
                    room.world
                        .as_ref()
                        .map_or_else(Vec::new, |world| peer.world.movement(&world.scene, tick))
                };
                let _ =
                    peer.world
                        .send_frame(tick, movements, drain, &mut peer.egress, |payload| {
                            let packet = Packet {
                                conn: connection,
                                kind: KIND_FRAME,
                                flags: ACK,
                                payload,
                                ..Packet::default()
                            };
                            let outgoing = self.links.transmit(packet).map_err(|error| {
                                io::Error::other(format!("game link: {error:?}"))
                            })?;
                            send(socket, outgoing)
                        });
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
        let input_ready = state.advance_input(start.elapsed());
        let now = start.elapsed();
        if now >= due {
            let (next, missed) = next_tick_after(due, now)
                .ok_or_else(|| io::Error::other("game tick clock exhausted"))?;
            due = next;
            state.tick(&socket, now, missed);
        }
        let timeout = if socket_ready || input_ready || handled == RECEIVE_BATCH {
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
    use std::{collections::HashSet, net::UdpSocket as ClientSocket};

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
        operation: Option<u8>,
        movement: bool,
        script: Option<ReceivedChunk>,
    }

    #[derive(Debug, Eq, PartialEq)]
    struct ReceivedChunk {
        first: u32,
        span: u32,
        width: usize,
        data: Vec<u8>,
    }

    impl ReceivedChunk {
        fn bits(&self) -> BitReader<'_> {
            BitReader::with_range(&self.data, 0, self.width).unwrap()
        }
    }

    impl WorldRecord {
        fn is_create(&self) -> bool {
            self.operation == Some(1)
        }
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
                    operation: None,
                    movement: false,
                    script: None,
                };
                assert!(reader.bit().unwrap());
                if !reader.bit().unwrap() {
                    return record;
                }
                if reader.bit().unwrap() {
                    assert!(!reader.bit().unwrap());
                    let first = reader.w_u32().unwrap();
                    let span = reader.w_var().unwrap();
                    assert!(!reader.bit().unwrap());
                    let width = reader.w_var().unwrap() as usize;
                    record.script = Some(ReceivedChunk {
                        first,
                        span,
                        width,
                        data: reader.raw_bits(width).unwrap(),
                    });
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
                    let operation = reader.bits(2).unwrap() as u8;
                    record.operation = Some(operation);
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

    fn input_batch(first: u32, count: u8, yaw: i16) -> Vec<u8> {
        command_batch(first, count, yaw, 0, true)
    }

    fn command_batch(first: u32, count: u8, yaw: i16, buttons: u32, forward: bool) -> Vec<u8> {
        let mut input = BitWriter::new();
        input.bits(0, 24).unwrap(); // latency and key flags
        input.bits(u64::from(count), 8).unwrap();
        input.bits(u64::from(first), 32).unwrap();
        input.bit(forward); // new throttles
        if forward {
            input.bit(true); // compact throttle direction
            input.bit(true); // moving
            input.bits(2, 3).unwrap();
        }
        input.signed(i64::from(yaw), 16).unwrap();
        input.signed(0, 16).unwrap();
        input.bits(u64::from(buttons), 24).unwrap();
        input.bits(0, 8).unwrap(); // action
        input.bits(0, 16).unwrap(); // extra
        for _ in 1..count {
            input.bit(false); // next consecutive frame
            input.bit(false); // no changed fields
        }
        let mut payload = BitWriter::new();
        write_channel(&mut payload, &[], &[]).unwrap();
        payload.bit(true);
        payload.bits(input.bit_len() as u64, 32).unwrap();
        payload.raw_bits(input.as_bytes(), input.bit_len()).unwrap();
        payload.into_bytes()
    }

    fn input_ack(schemas: &Schemas, packet: &Packet) -> u32 {
        let mut reader = BitReader::new(&packet.payload);
        reader.var_a().unwrap();
        read_channel(&mut reader, schemas).unwrap();
        reader.var_a().unwrap()
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range scene"]
    fn busy_peers_resume_in_round_robin_order_after_the_global_input_budget() {
        use crate::{commands::Command, scene_assets::SceneAssets};

        let assets = SceneAssets::load(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap()).unwrap();
        let scene = assets.prepare(GameMap::practice_range()).unwrap();
        let mut state = State::new(Arc::new(fixture()));
        // Five synthetic peers share a prepared fixture solely to test the
        // worker scheduler. The actual admission route remains solo.
        let players: Vec<_> = (0..5)
            .map(|slot| Player {
                info: player().pop().unwrap(),
                entity: player_entity(slot),
                gone: false,
            })
            .collect();
        state.rooms.insert(
            MATCH,
            Room {
                map: scene.map().clone(),
                remaining: players.len(),
                players,
                world: Some(WorldStart {
                    scene: Box::new(scene),
                    card: PlayerCard::default(),
                    combat: None,
                }),
            },
        );
        let first = FIRST_TICK + 1;
        let commands: Vec<_> = (first..first + 32)
            .map(|frame| Command {
                frame,
                ..Command::default()
            })
            .collect();
        let room = &state.rooms[&MATCH];
        for slot in 0..room.players.len() {
            let connection = slot as u32 + 1;
            let viewer = room.appearance(slot).unwrap();
            let mut peer = Peer::new(viewer.entity);
            peer.world
                .start(&room.world.as_ref().unwrap().scene, &viewer, FIRST_TICK)
                .unwrap();
            peer.world.queue_commands(FIRST_TICK, &commands);
            state.seats.insert(connection, (MATCH, slot));
            state.peers.insert(connection, peer);
        }
        let acks = |state: &State| {
            state
                .peers
                .values()
                .map(|peer| peer.world.command_ack(FIRST_TICK))
                .collect::<Vec<_>>()
        };
        assert!(state.advance_input(Duration::ZERO));
        assert_eq!(
            acks(&state),
            [first + 15, first + 15, first + 15, first + 15, FIRST_TICK]
        );
        assert!(state.advance_input(Duration::ZERO));
        assert_eq!(
            acks(&state),
            [first + 31, first + 31, first + 31, first + 15, first + 15]
        );
        assert!(!state.advance_input(Duration::ZERO));
        assert_eq!(acks(&state), [first + 31; 5]);
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range scene"]
    fn prepared_world_waits_for_ownership_and_retries_baselines_before_scripts() {
        exercise_prepared_world(PreparedFixture::Stock);
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range scene without bot VM graphs"]
    fn prepared_worker_retries_a_concrete_owner_vm_on_encrypted_loopback() {
        exercise_prepared_world(PreparedFixture::Owner);
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range scene without bot VM graphs"]
    fn prepared_worker_delivers_a_volley_hit_and_replaces_the_bot_over_encrypted_udp() {
        exercise_prepared_world(PreparedFixture::Combat);
    }

    #[derive(Clone, Copy)]
    enum PreparedFixture {
        Stock,
        Owner,
        Combat,
    }

    fn exercise_prepared_world(fixture: PreparedFixture) {
        use crate::{
            accounts::Accounts,
            catalog::ItemDb,
            collection::Collection,
            game_bots::BOT_ENTITY,
            jam_codec::id16,
            lobby_login::Identity,
            lobby_practice::{CREATE_GAME, GAME_REQUEST, SoloPracticeHandoff, dispatch_scene},
            lobby_session::PreparedLogin,
            match_handoff::{HANDOFF, HANDOFF_MESSAGE},
            retail_templates::RetailTemplates,
            scene_assets::SceneAssets,
            script_player::{fixture_body_factory, fixture_hitscan_factory},
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
        let result = dispatch_scene(
            &request,
            &login,
            &server,
            SoloPracticeHandoff {
                match_id: MATCH,
                host: "127.0.0.1",
                general: channel([7, 8], 7),
                match_channel: channel(MATCH, 6),
            },
            (&assets, &collection),
            |map| {
                let mut scene = assets.prepare(map)?;
                if !matches!(fixture, PreparedFixture::Stock) {
                    assert!(!scripted_bots);
                    let factory = match fixture {
                        PreparedFixture::Combat => {
                            let [x, y, z] = scene.map().spawn_point(0, 0).unwrap().0;
                            let bot = scene
                                .bots_mut()
                                .iter_mut()
                                .find(|bot| bot.entity == BOT_ENTITY + 1)
                                .unwrap();
                            bot.position = [x - 4.0, y, z];
                            fixture_hitscan_factory(100.0)
                        }
                        _ => fixture_body_factory().0,
                    };
                    scene.owner_body_factory = Some(Arc::new(move |hero, spawn| {
                        if hero.guid != crate::game_heroes::SOLDIER {
                            return Ok(None);
                        }
                        Ok(Some(Box::new(factory.start(spawn)?)))
                    }));
                }
                Ok(scene)
            },
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
                .filter(|record| record.is_create())
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
            for record in records.iter().filter(|record| record.is_create()) {
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
                assert!(!record.is_create());
                if record.movement {
                    assert!((BOT_ENTITY..BOT_ENTITY + 15).contains(&record.entity));
                    assert!(creates.contains(&record.entity));
                    moving_bot = true;
                }
                if let Some(script) = record.script {
                    assert_eq!((script.first, script.span), (0, 1));
                    if record.entity == GAME_MODE_ENTITY {
                        assert_eq!(script.data, [0x08, 0x5b, 0x06, 0x00, 0x0c]);
                        mode = true;
                    } else {
                        assert_eq!(record.entity, CONTROLLER);
                        let mut bits = script.bits();
                        assert_eq!(bits.bits(3).unwrap(), 2);
                        assert!(bits.w_var().unwrap() >= crate::game_clock::FIRST_TICK);
                        controller = true;
                    }
                }
            }
            client.send(ACK, Vec::new());
        }
        assert!(mode && controller && moving_bot);
        match fixture {
            PreparedFixture::Owner => exercise_owner_vm(&mut client, &schemas),
            PreparedFixture::Combat => {
                exercise_combat(&mut client, &schemas, BOT_ENTITY + 1, BOT_ENTITY + 15)
            }
            PreparedFixture::Stock => exercise_hero_selection(&mut client, &schemas, &assets),
        }
        server.stop().unwrap();
    }

    fn send_game_message(
        client: &mut Client,
        schemas: &Schemas,
        ordinal: u32,
        guid: u64,
        parameters: Vec<u8>,
    ) {
        let fields = Fields::from([
            (
                0x78,
                Value::Struct(Fields::from([(
                    0,
                    Value::Unsigned(u64::from(player_entity(0))),
                )])),
            ),
            (0x80, Value::Blob(parameters)),
            (0xa8, Value::Unsigned(guid)),
            (0xb0, Value::Signed(3)),
        ]);
        let message = encode(
            schemas,
            crate::game_parameters::CLIENT_GAME_MESSAGE,
            &fields,
        )
        .unwrap();
        let mut payload = BitWriter::new();
        write_channel(&mut payload, &[(ordinal, message)], &[]).unwrap();
        client.send(ACK, payload.into_bytes());
    }

    fn exercise_combat(client: &mut Client, schemas: &Schemas, target: u32, replacement: u32) {
        use crate::{commands::FIRE, game_heroes::SOLDIER, game_parameters::PICK_HERO};

        let body = player_entity(0) | 1;
        send_game_message(
            client,
            schemas,
            1,
            PICK_HERO,
            SOLDIER.to_le_bytes().to_vec(),
        );
        let deadline = Instant::now() + Duration::from_secs(3);
        let frame = loop {
            let packet = client.receive();
            assert!(client.window.accept(packet.seq));
            client.send(ACK, Vec::new());
            if world_records(schemas, &packet)
                .iter()
                .any(|record| record.entity == body && record.is_create())
            {
                break BitReader::new(&packet.payload).var_a().unwrap() + 1;
            }
            assert!(Instant::now() < deadline, "waiting for weapon fixture body");
        };
        let commands = command_batch(frame, 1, -16384, FIRE, false);
        client.send(ACK, commands.clone());
        client.send(ACK, commands);

        let deadline = Instant::now() + Duration::from_secs(7);
        let mut lost = None;
        let mut delivered_hit = false;
        let mut delivered_kill = false;
        let mut delivered_health = false;
        let mut destroyed = false;
        let mut created = false;
        while !created {
            let packet = client.receive();
            let reliable = messages(schemas, &packet);
            let hit = reliable.iter().find(|item| item.message.id == 25001);
            if lost.is_none() {
                if let Some(hit) = hit {
                    lost = Some((hit.sequence, hit.message.fields.clone()));
                    continue; // let a following ACK reveal loss of the original hit
                }
            }
            for item in reliable {
                match item.message.id {
                    25001 => {
                        let (ordinal, fields) = lost.as_ref().unwrap();
                        assert_eq!((&item.sequence, &item.message.fields), (ordinal, fields));
                        assert_eq!(fields[&0x84], Value::Unsigned(u64::from(frame)));
                        let Value::Struct(impact) = &fields[&0x78] else {
                            panic!("hit body")
                        };
                        assert_eq!(impact[&4], Value::Unsigned(200));
                        assert_eq!(impact[&8], Value::Unsigned(3));
                        delivered_hit = true;
                    }
                    25005 => {
                        let Value::Struct(victim) = &item.message.fields[&0x84] else {
                            panic!("elimination body")
                        };
                        assert_eq!(victim[&0], Value::Unsigned(u64::from(target)));
                        delivered_kill = true;
                    }
                    _ => {}
                }
            }
            for record in world_records(schemas, &packet) {
                if record.entity == target {
                    if record.operation == Some(2) {
                        delivered_health = true;
                    }
                    if record.operation == Some(3) {
                        assert!(delivered_hit && delivered_kill && delivered_health);
                        destroyed = true;
                    }
                } else if record.entity == replacement && record.is_create() {
                    assert!(destroyed);
                    created = true;
                }
            }
            assert!(client.window.accept(packet.seq));
            client.send(ACK, Vec::new());
            assert!(
                Instant::now() < deadline,
                "waiting for combat recovery and bot respawn"
            );
        }
    }

    fn exercise_owner_vm(client: &mut Client, schemas: &Schemas) {
        use crate::{game_heroes::SOLDIER, game_parameters::PICK_HERO};

        let owner = player_entity(0);
        let body = owner | 1;
        send_game_message(
            client,
            schemas,
            1,
            PICK_HERO,
            SOLDIER.to_le_bytes().to_vec(),
        );
        let deadline = Instant::now() + Duration::from_secs(2);
        let start = loop {
            let packet = client.receive();
            let records = world_records(schemas, &packet);
            assert!(
                records
                    .iter()
                    .all(|record| record.entity != body || record.script.is_none())
            );
            assert!(client.window.accept(packet.seq));
            client.send(ACK, Vec::new());
            if records
                .iter()
                .any(|record| record.entity == body && record.is_create())
            {
                let controller = records
                    .iter()
                    .find(|record| record.entity == owner && record.script.is_some())
                    .unwrap();
                let mut bits = controller.script.as_ref().unwrap().bits();
                assert_eq!(bits.bits(3).unwrap(), 2);
                break bits.w_var().unwrap();
            }
            assert!(Instant::now() < deadline, "waiting for fixture body create");
        };
        let wait_chunk = |client: &mut Client| {
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                let packet = client.receive();
                if let Some(script) = world_records(schemas, &packet)
                    .into_iter()
                    .find(|record| record.entity == body && record.script.is_some())
                    .and_then(|record| record.script)
                {
                    return (packet.seq, script);
                }
                assert!(client.window.accept(packet.seq));
                client.send(ACK, Vec::new());
                assert!(Instant::now() < deadline, "waiting for fixture body script");
            }
        };
        let skip_chunk = |client: &mut Client, payload| {
            let packet = client.receive();
            assert!(
                world_records(schemas, &packet)
                    .iter()
                    .all(|record| record.entity != body || record.script.is_none())
            );
            assert!(client.window.accept(packet.seq));
            client.send(ACK, payload);
        };
        let accept_chunk = |client: &mut Client, sequence| {
            assert!(client.window.accept(sequence));
            client.send(ACK, Vec::new());
        };
        let check_delta = |chunk: &ReceivedChunk, corrected, command_frame| {
            let mut bits = chunk.bits();
            assert_eq!(bits.bits(3).unwrap(), if corrected { 6 } else { 2 });
            assert_eq!(bits.w_var().unwrap(), command_frame);
        };

        let (_, h0) = wait_chunk(client);
        assert_eq!((h0.first, h0.span), (0, 1));
        assert_eq!(h0.bits().bits(2).unwrap(), 0);
        skip_chunk(client, Vec::new());
        let (sequence, retry) = wait_chunk(client);
        assert_eq!(retry, h0);
        accept_chunk(client, sequence);

        let (_, c0) = wait_chunk(client);
        assert_eq!((c0.first, c0.span), (2, 0));
        check_delta(&c0, false, start);
        skip_chunk(client, input_batch(start + 1, 16, 0));
        let (sequence, retry) = wait_chunk(client);
        assert_eq!(retry, c0);
        accept_chunk(client, sequence);

        let (_, c1) = wait_chunk(client);
        assert_eq!((c1.first, c1.span), (3, 0));
        check_delta(&c1, true, start + 16);
        skip_chunk(client, input_batch(start + 17, 1, 0));
        let (sequence, cumulative) = wait_chunk(client);
        assert_eq!((cumulative.first, cumulative.span), (3, 1));
        check_delta(&cumulative, true, start + 17);
        accept_chunk(client, sequence);
    }

    fn exercise_hero_selection(
        client: &mut Client,
        schemas: &Schemas,
        assets: &crate::scene_assets::SceneAssets,
    ) {
        use crate::{
            game_heroes::SOLDIER,
            game_parameters::{OPEN_OR_CLOSE, PICK_HERO},
        };
        const OWNER: u32 = 0xa000_0100;
        const FIRST: u32 = OWNER | 1;
        const SECOND: u32 = OWNER | 2;

        let send = |client: &mut Client, ordinal, guid, parameters| {
            send_game_message(client, schemas, ordinal, guid, parameters);
        };
        let next_body_frame = |client: &mut Client, body| {
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                let packet = client.receive();
                let records = world_records(schemas, &packet);
                if records
                    .iter()
                    .any(|record| record.entity == body && record.is_create())
                {
                    break (packet, records);
                }
                assert!(client.window.accept(packet.seq));
                client.send(ACK, Vec::new());
                assert!(Instant::now() < deadline, "waiting for the selected body");
            }
        };
        let describe = |records: &[WorldRecord]| {
            records
                .iter()
                .filter(|record| matches!(record.entity, OWNER | FIRST | SECOND))
                .map(|record| {
                    (
                        record.entity,
                        record.operation,
                        record.script.as_ref().map(|script| script.span),
                    )
                })
                .collect::<Vec<_>>()
        };
        let controller =
            crate::game_controller::Controller::load(assets.graphs(), PRACTICE_RANGE_MODE).unwrap();
        let verify_controller = |records: &[WorldRecord], hero, open| {
            let script = records
                .iter()
                .find(|record| record.entity == OWNER && record.script.is_some())
                .and_then(|record| record.script.as_ref())
                .expect("hero selection controller frame");
            assert_eq!(script.first, 0);
            let mut reader = script.bits();
            assert_eq!(reader.bits(3).unwrap(), 2);
            let cmfd = reader.w_var().unwrap();
            let expected = controller
                .controller_frame(
                    cmfd,
                    ControllerFrame {
                        select_open: open,
                        skins: open,
                        hide_assemble: true,
                        hero: Some(hero),
                        ..ControllerFrame::default()
                    },
                )
                .unwrap();
            assert_eq!(script.width, expected.bit_len());
            assert_eq!(script.data, expected.as_bytes());
            cmfd
        };

        send(client, 1, PICK_HERO, SOLDIER.to_le_bytes().to_vec());
        let (_lost, records) = next_body_frame(client, FIRST);
        let expected = [
            (FIRST, Some(1), None),
            (OWNER, Some(2), None),
            (OWNER, None, Some(2)),
        ];
        assert_eq!(describe(&records), expected);
        assert!(
            records
                .iter()
                .any(|record| record.entity == FIRST && record.movement)
        );
        let first_cmfd = verify_controller(&records, SOLDIER, false);

        // A later datagram ACK identifies the first body batch as lost.
        let following = client.receive();
        assert!(client.window.accept(following.seq));
        assert!(describe(&world_records(schemas, &following)).is_empty());
        client.send(ACK, Vec::new());
        let (retry, records) = next_body_frame(client, FIRST);
        assert_eq!(describe(&records), expected);
        assert_eq!(verify_controller(&records, SOLDIER, false), first_cmfd);
        assert!(client.window.accept(retry.seq));

        let tick = BitReader::new(&retry.payload).var_a().unwrap();
        let first_input = tick + 32;
        let mut newest = first_input + 4;
        let await_input = |client: &mut Client, expected| {
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                let packet = client.receive();
                assert!(client.window.accept(packet.seq));
                if input_ack(schemas, &packet) == expected {
                    assert!(
                        world_records(schemas, &packet)
                            .iter()
                            .all(|record| record.entity != FIRST || !record.movement)
                    );
                    return;
                }
                client.send(ACK, Vec::new());
                assert!(
                    Instant::now() < deadline,
                    "waiting for command acknowledgement"
                );
            }
        };
        client.send(ACK, input_batch(first_input, 5, -31010));
        await_input(client, newest);
        client.send(ACK, input_batch(newest + 2_000, 1, -31010));
        for _ in 0..2 {
            let packet = client.receive();
            assert!(client.window.accept(packet.seq));
            assert_eq!(input_ack(schemas, &packet), newest);
            client.send(ACK, Vec::new());
        }
        newest += 48;
        let recoverable = input_batch(newest, 1, -31010);
        for _ in 0..=RECEIVE_BATCH {
            client.send(ACK, recoverable.clone());
        }
        await_input(client, newest);

        let mut open = vec![1, 1];
        open.extend(OWNER.to_le_bytes());
        open.extend(OWNER.to_le_bytes());
        send(client, 2, OPEN_OR_CLOSE, open);
        let deadline = Instant::now() + Duration::from_secs(2);
        let reopen_cmfd = loop {
            let packet = client.receive();
            assert!(client.window.accept(packet.seq));
            let records = world_records(schemas, &packet);
            if !describe(&records).is_empty() {
                assert_eq!(describe(&records), [(OWNER, None, Some(3))]);
                break verify_controller(&records, SOLDIER, true);
            }
            assert!(
                Instant::now() < deadline,
                "waiting for hero selection to reopen"
            );
        };
        assert!(reopen_cmfd > first_cmfd);

        let tracer = assets.heroes().find_hero("Tracer").unwrap().guid;
        send(client, 3, PICK_HERO, tracer.to_le_bytes().to_vec());
        let (swap, records) = next_body_frame(client, SECOND);
        assert_eq!(
            describe(&records),
            [
                (SECOND, Some(1), None),
                (OWNER, Some(2), None),
                (FIRST, Some(3), None),
                (OWNER, None, Some(4))
            ],
        );
        assert!(verify_controller(&records, tracer, false) > reopen_cmfd);
        assert!(client.window.accept(swap.seq));
        client.send(ACK, Vec::new());
    }
}
