//! Per-peer world delivery, including the owning player's body lifetime.

use std::{
    borrow::Borrow,
    collections::{BTreeMap, HashMap, HashSet, VecDeque},
    io,
    sync::Arc,
    time::Duration,
};

use crate::{
    commands::{Command, CommandQueue},
    game_body::{BodyCreate, BodyView, body_create, owner_pose},
    game_bots::BOT_RECORD_DELAY,
    game_clock::{MAX_LEASE_FRAME, SEND_EVERY},
    game_controller::ControllerFrame,
    game_egress::{DeliveryReport, FrameRecords, GameEgress, RecordSource, SendError, SentFrame},
    game_heroes::{Hero, SOLDIER},
    game_messages::Message,
    game_parameters::{ClientGameMessage, HeroSelectRequest},
    match_scene::{PlayerAppearance, Possession, team_bit},
    mover::Mover,
    mover_correction::CorrectionHistory,
    mover_kinematics::Poses,
    mover_modifiers::{Modifier, ModifierKey},
    scene_assets::PreparedScene,
    script_init::Snapshot,
    script_player::OwnerBodySpawn,
    script_snapshot_wire::{ProjectedSnapshot, SnapshotEncoder, View},
    script_stream::{BodyStream, DeltaKind, ScriptChunk, Stream},
    world::{EntityUpdate, GAME_MODE_ENTITY, LeaseBlock, MAX_PAYLOAD, Operation, SkinTheme, frame},
};

const BODY_REUSE_DELAY: Duration = Duration::from_secs(30);
// Resource bound for this worker. The reference keeps 64 predicted states and
// 32 commands. A farther-ahead command can be retried once the clock catches up.
const MAX_INPUT_LEAD: u32 = 96;
// Several retransmit windows fit before a peer exhausts this queue. One final
// leave marker may follow the accepted prefix when the cap is reached.
const MAX_PENDING_INPUT: usize = 256;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct BodyId {
    entity: u32,
    generation: u64,
}

#[derive(Clone, Copy)]
struct Body {
    id: BodyId,
    hero: u64,
    skin: SkinTheme,
}

/// A running owner VM and the authored data used to encode its snapshots.
/// A supported modifier projection is required before the mover can correct
/// the owner's prediction, even if the current set of modifiers is empty.
pub(crate) trait OwnerBodyRuntime: Send {
    fn snapshot(&self) -> Option<&Snapshot>;
    fn encoder(&self) -> SnapshotEncoder<'_>;
    fn advance(&mut self, command: Command, mover: &Mover) -> io::Result<()>;
    fn frame_ms(&self) -> u32;

    fn leases(&self) -> &[LeaseBlock] {
        &[]
    }

    fn movement_modifiers(&self) -> Option<&BTreeMap<ModifierKey, Modifier>> {
        None
    }
}

struct BodyScript {
    id: BodyId,
    runtime: Box<dyn OwnerBodyRuntime>,
    stream: BodyStream<ProjectedSnapshot>,
    frame: u32,
    unsent_delta: Option<(u32, DeltaKind)>,
    corrections: Option<CorrectionHistory>,
}

impl BodyScript {
    fn new(body: Body, frame: u32, runtime: Box<dyn OwnerBodyRuntime>) -> io::Result<Self> {
        let snapshot = runtime
            .snapshot()
            .ok_or_else(|| invalid("body VM is unavailable"))?;
        if snapshot.entity != body.id.entity {
            return Err(invalid("body VM has the wrong entity"));
        }
        runtime
            .encoder()
            .project(snapshot, View::Owner)
            .map_err(invalid)?;
        let corrections = (body.hero == SOLDIER && runtime.movement_modifiers().is_some())
            .then(|| CorrectionHistory::new(body.id.entity, body.id.generation));
        Ok(Self {
            id: body.id,
            runtime,
            stream: BodyStream::new(body.id.entity),
            frame,
            unsent_delta: None,
            corrections,
        })
    }

    fn step(&mut self, mover: &mut Mover, command: Command) -> io::Result<()> {
        if command.frame > self.frame {
            self.runtime.advance(command, mover)?;
            self.frame = command.frame;
        }
        if !self
            .runtime
            .snapshot()
            .is_some_and(Snapshot::initialization_complete)
        {
            return Err(invalid("body VM reached an unsupported state"));
        }
        if let Some(modifiers) = self.runtime.movement_modifiers() {
            mover.track_modifiers(command.frame, modifiers);
        } else if self.corrections.is_some() {
            return Err(invalid("body VM lost its movement projection"));
        }
        if let Some(history) = &mut self.corrections {
            history.record(mover.pose());
        }
        Ok(())
    }

    fn next_record(&mut self, created: bool) -> io::Result<Option<(Record, Option<u32>)>> {
        if !created {
            return Ok(None);
        }
        let encoder = self.runtime.encoder();
        let snapshot = self
            .runtime
            .snapshot()
            .ok_or_else(|| invalid("body VM is unavailable"))?;
        let (chunk, kind) = if self.stream.last() == 0 {
            let snapshot = encoder.project(snapshot, View::Owner).map_err(invalid)?;
            (
                encoder
                    .queue_full(&mut self.stream, &snapshot)
                    .map_err(invalid)?,
                None,
            )
        } else {
            let replace_initial =
                matches!(self.unsent_delta, Some((_, DeltaKind::InitialOwner(_))));
            let Some(kind) = self
                .stream
                .owner_kind(self.frame, false, replace_initial)
                .map_err(invalid)?
            else {
                return Ok(None);
            };
            let (_, base) = self
                .stream
                .base()
                .expect("owner delta requires the baseline");
            let snapshot = encoder.project_next(snapshot, base).map_err(invalid)?;
            let chunk = encoder
                .queue_delta(&mut self.stream, &snapshot, kind, self.runtime.frame_ms())
                .map_err(invalid)?
                .expect("owner deltas include a command frame");
            (chunk, Some(kind))
        };
        // An individual chunk that cannot fit alone needs fragmentation.
        // Fail this VM closed so it cannot hold up other entity records.
        let packet = frame(u32::MAX, u32::MAX, &[], &[], &[&chunk.update]).map_err(invalid)?;
        if packet.len() > MAX_PAYLOAD {
            return Err(invalid(
                "body script chunk exceeds the game datagram budget",
            ));
        }
        let last = chunk.last;
        let replaced = kind.and_then(|kind| {
            self.unsent_delta.replace((last, kind)).map(|(last, _)| {
                self.stream.discard_unsent(last);
                last
            })
        });
        let record = Record {
            update: chunk.update,
            delivery: Delivery::BodyScript {
                id: self.id,
                last,
                delta: kind.is_some(),
            },
        };
        Ok(Some((record, replaced)))
    }

    fn sent(&mut self, last: u32) {
        self.stream.sent(last);
        if self.unsent_delta.is_some_and(|(queued, _)| queued == last) {
            self.unsent_delta = None;
        }
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Lifetime {
    Live,
    Retiring,
    ReusableAt(Duration),
}

#[derive(Clone, Copy)]
enum Delivery {
    Baseline,
    CreateBody(BodyId),
    Possession(BodyId),
    DestroyBody(BodyId),
    BodyScript {
        id: BodyId,
        last: u32,
        delta: bool,
    },
    Script {
        last: u32,
        close: bool,
        resend: bool,
    },
}

pub(crate) struct Record {
    update: EntityUpdate,
    delivery: Delivery,
}

impl Record {
    pub(crate) fn baseline(update: EntityUpdate) -> Self {
        Self {
            update,
            delivery: Delivery::Baseline,
        }
    }

    fn script(chunk: ScriptChunk, close: bool) -> Self {
        Self {
            update: chunk.update,
            delivery: Delivery::Script {
                last: chunk.last,
                close,
                resend: chunk.resend,
            },
        }
    }
}

pub(crate) struct Ready<'a> {
    index: usize,
    record: &'a Arc<Record>,
}

impl Ready<'_> {
    pub(crate) fn tracked(&self) -> Arc<Record> {
        Arc::clone(self.record)
    }
}

impl Borrow<EntityUpdate> for Ready<'_> {
    fn borrow(&self) -> &EntityUpdate {
        &self.record.update
    }
}

enum InputWork {
    Message {
        message: Message,
        tick: u32,
        received: Duration,
    },
    Command {
        command: Command,
        pending: Option<Poses>,
    },
    Leave,
}

#[derive(Default)]
struct InputQueue {
    work: VecDeque<InputWork>,
    first_admitted: Option<u32>,
    admitted: Option<u32>,
    processed: Option<u32>,
    closing: bool,
}

impl InputQueue {
    fn push(&mut self, work: InputWork) -> bool {
        if self.closing {
            return false;
        }
        if self.work.len() == MAX_PENDING_INPUT {
            self.close_after_prefix();
            return false;
        }
        self.work.push_back(work);
        true
    }

    fn commands(&mut self, tick: u32, commands: &[Command]) {
        if self.closing || commands.is_empty() {
            return;
        }
        let (fresh, boundary) = command_prefix(self.admitted, tick, commands);
        for command in fresh {
            if !self.push(InputWork::Command {
                command,
                pending: None,
            }) {
                return;
            }
            self.first_admitted.get_or_insert(command.frame);
            self.admitted = Some(command.frame);
        }
        if boundary.is_some() {
            self.close_after_prefix();
        }
    }

    fn close_after_prefix(&mut self) {
        self.work.push_back(InputWork::Leave);
        self.closing = true;
    }

    fn clear(&mut self) {
        self.work.clear();
        self.admitted = self.processed;
        self.closing = false;
    }
}

pub(crate) struct WorldDelivery {
    owner: u32,
    queued: Vec<Arc<Record>>,
    created: HashSet<u32>,
    started: Option<u32>,
    pub(crate) presentation_due: bool,
    controller: Stream,
    mode: Stream,
    select_open: bool,
    body: Option<Body>,
    bound: Option<BodyId>,
    generation: u64,
    bodies: HashMap<u32, (u64, Lifetime)>,
    input: InputQueue,
    mover: Option<Mover>,
    body_script: Option<BodyScript>,
}

impl WorldDelivery {
    pub(crate) fn new(owner: u32) -> Self {
        Self {
            owner,
            queued: Vec::new(),
            created: HashSet::new(),
            started: None,
            presentation_due: false,
            controller: Stream::new(owner),
            mode: Stream::new(GAME_MODE_ENTITY),
            select_open: true,
            body: None,
            bound: None,
            generation: 0,
            bodies: HashMap::new(),
            input: InputQueue::default(),
            mover: None,
            body_script: None,
        }
    }

    pub(crate) fn clear(&mut self) {
        self.queued.clear();
        self.started = None;
        self.presentation_due = false;
        self.mover = None;
        self.cancel_body_script();
        self.input.clear();
    }

    fn push(&mut self, record: Record) {
        self.queued.push(Arc::new(record));
    }

    fn current(&self, record: &Record) -> bool {
        match record.delivery {
            Delivery::Baseline | Delivery::Script { close: false, .. } => true,
            Delivery::Script {
                last, close: true, ..
            } => last == self.controller.latest_data(),
            Delivery::CreateBody(id) => {
                self.bodies.get(&id.entity) == Some(&(id.generation, Lifetime::Live))
            }
            Delivery::DestroyBody(id) => {
                self.bodies.get(&id.entity) == Some(&(id.generation, Lifetime::Retiring))
            }
            Delivery::Possession(id) => self.body.is_some_and(|body| body.id == id),
            Delivery::BodyScript { id, .. } => self
                .body_script
                .as_ref()
                .is_some_and(|script| script.id == id),
        }
    }

    pub(crate) fn acknowledge(&mut self, report: DeliveryReport<Arc<Record>>, now: Duration) {
        if self.started.is_none() {
            return;
        }
        let mut retry = Vec::new();
        for packet in report.packets {
            for record in packet.records {
                if !self.current(&record) {
                    continue;
                }
                match (packet.arrived, record.delivery) {
                    (arrived, Delivery::BodyScript { last, .. }) => {
                        let stream = &mut self
                            .body_script
                            .as_mut()
                            .expect("current body stream")
                            .stream;
                        if arrived {
                            stream.arrived(last);
                        } else if stream.lost(last).retry {
                            retry.push(record);
                        }
                    }
                    (false, Delivery::Script { resend: false, .. }) => {}
                    (false, _) => retry.push(record),
                    (true, Delivery::Baseline) => {
                        if matches!(record.update.operation, Some(Operation::Create(_))) {
                            self.created.insert(record.update.entity);
                        }
                    }
                    (true, Delivery::CreateBody(id)) => {
                        self.created.insert(id.entity);
                        if let Some(script) =
                            self.body_script.as_mut().filter(|script| script.id == id)
                        {
                            script.stream.create_arrived();
                        }
                    }
                    (true, Delivery::Possession(id)) => self.bound = Some(id),
                    (true, Delivery::DestroyBody(id)) => {
                        self.bodies.insert(
                            id.entity,
                            (
                                id.generation,
                                Lifetime::ReusableAt(now.saturating_add(BODY_REUSE_DELAY)),
                            ),
                        );
                    }
                    (true, Delivery::Script { last, .. }) if record.update.entity == self.owner => {
                        self.controller.arrived(last);
                    }
                    (true, Delivery::Script { last, .. }) => {
                        self.mode.arrived(last);
                    }
                }
            }
        }
        if !retry.is_empty() {
            retry.append(&mut self.queued);
            self.queued = retry;
            self.remove_stale();
        }
    }

    fn remove_stale(&mut self) {
        let mut queued = std::mem::take(&mut self.queued);
        queued.retain(|record| self.current(record));
        self.queued = queued;
    }

    pub(crate) fn ready(&self) -> Vec<Ready<'_>> {
        let body = self.body.map(|body| body.id);
        let mut created = body.is_none_or(|id| self.created.contains(&id.entity));
        let mut bound = body == self.bound;
        self.queued
            .iter()
            .enumerate()
            .filter_map(|(index, record)| {
                match record.delivery {
                    Delivery::CreateBody(id) if Some(id) == body => created = true,
                    Delivery::Possession(_) if !created => return None,
                    Delivery::Possession(_) => bound = true,
                    Delivery::DestroyBody(_) | Delivery::Script { close: true, .. } if !bound => {
                        return None;
                    }
                    Delivery::BodyScript { id, last, delta }
                        if (delta && !bound)
                            || !self.body_script.as_ref().is_some_and(|script| {
                                script.id == id && script.stream.ready_to_send(last)
                            }) =>
                    {
                        return None;
                    }
                    _ => {}
                }
                Some(Ready { index, record })
            })
            .collect()
    }

    /// Retain blocked records and remove only the exact records selected by the
    /// successful send, even when eligibility skipped an earlier queue entry.
    pub(crate) fn sent(&mut self, indexes: &[usize]) {
        if indexes.is_empty() {
            return;
        }
        if let Some(script) = &mut self.body_script {
            for &index in indexes {
                match self.queued[index].delivery {
                    Delivery::BodyScript { id, last, .. } if id == script.id => script.sent(last),
                    _ => {}
                }
            }
        }
        let mut selected = indexes.iter().copied().peekable();
        let mut index = 0;
        self.queued.retain(|_| {
            let keep = selected.next_if_eq(&index).is_none();
            index += 1;
            keep
        });
    }

    pub(crate) fn sent_indices(ready: &[Ready<'_>], sent: &[RecordSource]) -> Vec<usize> {
        sent.iter()
            .filter_map(|source| match *source {
                RecordSource::Queued(index) => Some(ready[index].index),
                RecordSource::Movement(_) => None,
            })
            .collect()
    }

    pub(crate) fn send_frame<E>(
        &mut self,
        tick: u32,
        mut movements: Vec<EntityUpdate>,
        reliable_only: bool,
        egress: &mut GameEgress<Arc<Record>>,
        send: impl FnOnce(Vec<u8>) -> Result<u32, E>,
    ) -> Result<SentFrame, SendError<E>> {
        if !reliable_only {
            self.prepare_body_script();
        }
        let script = self.body_script.as_ref();
        let history = script.and_then(|script| script.corrections.as_ref());
        let correction = if reliable_only {
            None
        } else {
            script.and_then(|script| {
                history?.candidate(tick, self.created.contains(&script.id.entity))
            })
        };
        let owner_index = correction.map(|correction| {
            let index = movements.len();
            movements.push(correction.update());
            index
        });
        let queued = self.ready();
        let newest = self.command_ack(tick);
        let sent = egress.send_frame(
            FrameRecords {
                tick,
                queued: &queued,
                movements: &movements,
                owner_correction: owner_index.is_some(),
                reliable_only,
            },
            |records| {
                let selected = owner_index
                    .filter(|&index| {
                        records
                            .iter()
                            .any(|record| std::ptr::eq(*record, &movements[index]))
                    })
                    .and(correction.as_ref());
                history.map_or(newest, |history| {
                    history.command_ack(tick, Some(newest), selected)
                })
            },
            |source| match source {
                RecordSource::Queued(index) => queued[index].tracked(),
                RecordSource::Movement(_) => unreachable!("movement records are not retried"),
            },
            send,
        )?;
        let indexes = Self::sent_indices(&queued, &sent.records);
        self.sent(&indexes);
        let selected = owner_index
            .zip(correction)
            .filter(|(index, _)| sent.records.contains(&RecordSource::Movement(*index)));
        let history = self
            .body_script
            .as_mut()
            .and_then(|script| script.corrections.as_mut());
        if let (Some((_, correction)), Some(history)) = (selected, history) {
            history.sent(&correction);
        }
        Ok(sent)
    }

    fn prepare_body_script(&mut self) {
        let Some(script) = &mut self.body_script else {
            return;
        };
        // A split input gap can update physics history immediately. The owner
        // VM stream publishes only after the queued command completes.
        if matches!(
            self.input.work.front(),
            Some(InputWork::Command {
                pending: Some(_),
                ..
            })
        ) {
            return;
        }
        let (record, replaced) = match script.next_record(self.created.contains(&script.id.entity))
        {
            Ok(Some(record)) => record,
            Ok(None) => return,
            Err(_) => {
                self.cancel_body_script();
                return;
            }
        };
        if let Some(replaced) = replaced {
            let id = script.id;
            self.queued.retain(|queued| {
                !matches!(queued.delivery, Delivery::BodyScript { id: queued_id, last, .. }
                    if queued_id == id && last == replaced)
            });
        }
        self.push(record);
    }

    fn cancel_body_script(&mut self) {
        if let Some(mut script) = self.body_script.take() {
            script.stream.retire();
            if let Some(mover) = &mut self.mover {
                let frame = mover.pose().frame.unwrap_or(script.frame);
                mover.track_modifiers(frame, &BTreeMap::new());
            }
            self.remove_stale();
        }
    }

    pub(crate) fn start(
        &mut self,
        scene: &PreparedScene,
        viewer: &PlayerAppearance<'_>,
        tick: u32,
    ) -> io::Result<()> {
        let records = scene.initial_world(viewer)?;
        self.queued.extend(
            records
                .into_iter()
                .map(|update| Arc::new(Record::baseline(update))),
        );
        self.started = Some(tick);
        Ok(())
    }

    pub(crate) fn present(&mut self, scene: &PreparedScene, tick: u32) -> io::Result<()> {
        if self.controller.latest_data() == 0 && self.created.contains(&self.owner) {
            self.queue_controller(scene, tick, true, None)?;
        }
        if self.mode.latest_data() == 0 && self.created.contains(&GAME_MODE_ENTITY) {
            let chunk = self.mode.full(scene.mode_ui()).map_err(invalid)?;
            self.push(Record::script(chunk, false));
        }
        self.presentation_due = self.controller.latest_data() == 0 || self.mode.latest_data() == 0;
        Ok(())
    }

    fn can_handle_game_message(&self) -> bool {
        self.started.is_some()
            && self.controller.latest_data() != 0
            && self.created.contains(&self.owner)
    }

    fn game_message(
        &mut self,
        scene: &PreparedScene,
        viewer: &PlayerAppearance<'_>,
        tick: u32,
        now: Duration,
        quanta: u32,
        message: &Message,
    ) -> io::Result<()> {
        if !self.can_handle_game_message() {
            return Ok(());
        }
        let Some(message) = ClientGameMessage::read(message) else {
            return Ok(());
        };
        if message.entity != self.owner {
            return Ok(());
        }
        match message.hero_select(scene.heroes()) {
            Some(HeroSelectRequest::Pick(Some(hero))) => {
                if viewer
                    .card
                    .role
                    .is_some_and(|role| role != 0 && hero.role_number() != u8::try_from(role).ok())
                {
                    return Ok(());
                }
                let skin = viewer
                    .card
                    .skins
                    .get(&hero.guid)
                    .copied()
                    .unwrap_or_default();
                if self
                    .body
                    .is_some_and(|body| (body.hero, body.skin) == (hero.guid, skin))
                {
                    self.queue_controller(scene, tick, false, Some(hero.guid))?;
                } else if let Some(entity) = self.next_body(now) {
                    self.switch(scene, viewer, tick, quanta, entity, (hero, skin))?;
                }
            }
            Some(HeroSelectRequest::OpenOrClose(wanted)) => {
                let open = self.body.is_none() || wanted.unwrap_or(!self.select_open);
                self.queue_controller(scene, tick, open, self.body.map(|body| body.hero))?;
            }
            _ => {}
        }
        Ok(())
    }

    pub(crate) fn command_ack(&self, tick: u32) -> u32 {
        self.input.processed.unwrap_or_else(|| {
            self.input
                .first_admitted
                .map_or(tick, |frame| tick.min(frame.saturating_sub(1)))
        })
    }

    pub(crate) fn queue_game_message(&mut self, tick: u32, received: Duration, message: Message) {
        if self.can_handle_game_message() {
            self.input.push(InputWork::Message {
                message,
                tick,
                received,
            });
        }
    }

    pub(crate) fn queue_commands(&mut self, tick: u32, commands: &[Command]) {
        if self.started.is_some() {
            self.input.commands(tick, commands);
        }
    }

    pub(crate) fn has_input_work(&self) -> bool {
        !self.input.work.is_empty()
    }

    /// One unit per physics pose, pre-body command, or queued game message.
    /// A fresh frame is committed only after its final pose has completed.
    pub(crate) fn advance_input(
        &mut self,
        scene: &PreparedScene,
        viewer: &PlayerAppearance<'_>,
        quanta: u32,
        budget: usize,
    ) -> io::Result<(usize, bool)> {
        let dt = f64::from(quanta) / 1_000_000.0;
        let mut remaining = budget;
        while remaining != 0 {
            let Some(work) = self.input.work.pop_front() else {
                break;
            };
            match work {
                InputWork::Message {
                    message,
                    tick,
                    received,
                } => {
                    let _ = self.game_message(scene, viewer, tick, received, quanta, &message);
                    remaining -= 1;
                }
                InputWork::Command {
                    command,
                    mut pending,
                } => {
                    if let Some(mover) = &mut self.mover {
                        let poses = pending.get_or_insert_with(|| mover.pending_poses(command));
                        let mut failed = false;
                        let script = &mut self.body_script;
                        let stepped = mover.step_pending(
                            scene.movement_index(),
                            poses,
                            dt,
                            remaining,
                            |mover, command| {
                                if !failed
                                    && script
                                        .as_mut()
                                        .is_some_and(|script| script.step(mover, command).is_err())
                                {
                                    mover.track_modifiers(command.frame, &BTreeMap::new());
                                    failed = true;
                                }
                            },
                        );
                        if failed {
                            self.cancel_body_script();
                        }
                        remaining -= stepped.map_err(invalid)?;
                        if poses.len() != 0 {
                            self.input
                                .work
                                .push_front(InputWork::Command { command, pending });
                            break;
                        }
                    } else {
                        remaining -= 1;
                    }
                    self.input.processed = Some(command.frame);
                }
                InputWork::Leave => return Ok((budget - remaining + 1, true)),
            }
        }
        Ok((budget - remaining, false))
    }

    fn next_body(&self, now: Duration) -> Option<u32> {
        let suffix = self.body.map_or(0, |body| body.id.entity & 0xff);
        (0..255)
            .map(|offset| self.owner | ((suffix + offset) % 255 + 1))
            .find(|&entity| {
                self.bodies.get(&entity).is_none_or(
                    |(_, lifetime)| matches!(lifetime, Lifetime::ReusableAt(at) if now >= *at),
                )
            })
    }

    fn switch(
        &mut self,
        scene: &PreparedScene,
        viewer: &PlayerAppearance<'_>,
        tick: u32,
        quanta: u32,
        entity: u32,
        selection: (&Hero, SkinTheme),
    ) -> io::Result<()> {
        let (hero, skin) = selection;
        let (position, yaw) = scene
            .map()
            .spawn_point(viewer.team, 0)
            .ok_or_else(|| invalid("no spawn for the selected hero"))?;
        let yaw = (yaw * 65536.0 / 360.0).round_ties_even() as i32;
        let mover = scene.player_mover(hero.body, viewer.team, position, yaw)?;
        let pose = owner_pose(mover.pose());
        let generation = self
            .generation
            .checked_add(1)
            .ok_or_else(|| invalid("player body lifetime exhausted"))?;
        let id = BodyId { entity, generation };
        let body = Body {
            id,
            hero: hero.guid,
            skin,
        };
        let start_frame = self.input.processed.unwrap_or(tick);
        let script = scene
            .owner_body_runtime(
                hero,
                OwnerBodySpawn {
                    entity,
                    frame: start_frame,
                    position: pose.position,
                    quanta,
                },
            )
            .ok()
            .flatten()
            .and_then(|runtime| BodyScript::new(body, start_frame, runtime).ok());
        let create = body_create(
            BodyCreate {
                entity,
                hero,
                team_bit: team_bit(viewer.team)?,
                position: pose.position,
                yaw: pose.yaw,
                skin,
                vitals: None,
            },
            BodyView::Owner {
                pose: Some(pose),
                leases: script
                    .as_ref()
                    .map_or(&[], |script| script.runtime.leases()),
            },
            scene.pools(),
        );
        let possession = Possession {
            body: entity,
            hero: hero.guid,
            skin: (skin.guid != 0).then_some(skin.guid),
        };
        let close = self.controller_chunk(scene, tick, false, Some(hero.guid))?;

        self.generation = generation;
        self.bodies.insert(entity, (generation, Lifetime::Live));
        self.created.remove(&entity);
        let old = self.body.replace(body);
        self.cancel_body_script();
        self.body_script = script;
        self.mover = Some(mover);
        self.bound = None;
        self.select_open = false;
        self.push(Record {
            update: create,
            delivery: Delivery::CreateBody(id),
        });
        self.push(Record {
            update: EntityUpdate {
                entity: self.owner,
                operation: Some(Operation::Update(possession.components().into())),
                ..EntityUpdate::default()
            },
            delivery: Delivery::Possession(id),
        });
        if let Some(old) = old {
            self.created.remove(&old.id.entity);
            self.bodies
                .insert(old.id.entity, (old.id.generation, Lifetime::Retiring));
            self.push(Record {
                update: EntityUpdate {
                    entity: old.id.entity,
                    operation: Some(Operation::Destroy),
                    ..EntityUpdate::default()
                },
                delivery: Delivery::DestroyBody(old.id),
            });
        }
        self.push(close);
        self.remove_stale();
        Ok(())
    }

    fn controller_chunk(
        &mut self,
        scene: &PreparedScene,
        tick: u32,
        open: bool,
        hero: Option<u64>,
    ) -> io::Result<Record> {
        let command_frame = self.command_ack(tick);
        let chunk = self
            .controller
            .full_owner(command_frame, |cmfd| {
                scene.controller().controller_frame(
                    cmfd,
                    ControllerFrame {
                        select_open: open,
                        skins: open,
                        hide_assemble: true,
                        hero,
                        ..ControllerFrame::default()
                    },
                )
            })
            .map_err(invalid)?;
        Ok(Record::script(chunk, hero.is_some() && !open))
    }

    fn queue_controller(
        &mut self,
        scene: &PreparedScene,
        tick: u32,
        open: bool,
        hero: Option<u64>,
    ) -> io::Result<()> {
        let record = self.controller_chunk(scene, tick, open, hero)?;
        self.select_open = open;
        self.push(record);
        self.remove_stale();
        Ok(())
    }

    pub(crate) fn movement(&self, scene: &PreparedScene, tick: u32) -> Vec<EntityUpdate> {
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

fn command_prefix(
    anchor: Option<u32>,
    tick: u32,
    commands: &[Command],
) -> (Vec<Command>, Option<u32>) {
    // Classify the lease boundary independently of the resource window so a
    // fresh unencodable frame still closes its old link.
    let (mut fresh, boundary) = CommandQueue::new().before_limit(commands, MAX_LEASE_FRAME, anchor);
    let limit = tick.saturating_add(MAX_INPUT_LEAD);
    fresh.truncate(fresh.partition_point(|command| command.frame <= limit));
    (fresh, boundary)
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        bits::BitReader,
        collision_index::TriangleIndex,
        contact_world::ContactWorld,
        game_clock::FIRST_TICK,
        game_content::GameMap,
        game_heroes::SOLDIER,
        game_instance::RETAIL_QUANTA,
        game_parameters::{CLIENT_GAME_MESSAGE, PICK_HERO},
        jam_codec::{Fields, Value},
        match_scene::PlayerCard,
        mover_settings::MoverSettings,
        scene_assets::SceneAssets,
        script_codecs::CodecTable,
        script_graph::GraphStore,
        script_init::{Component, EmptyWorld, MAX_INIT_WORK, Root},
        script_runtime::FRAME_MS,
        world::{MAX_ENTITY_RECORDS, Movement},
    };
    use std::sync::atomic::{AtomicBool, Ordering};

    fn forward(frame: u32) -> Command {
        Command {
            frame,
            forward: 127,
            ..Command::default()
        }
    }

    #[test]
    fn native_command_window_keeps_duplicates_and_future_input_unacknowledged() {
        let (fresh, boundary) = command_prefix(
            Some(1000),
            1000,
            &[
                forward(1000),
                forward(1001),
                forward(1096),
                forward(1097),
                forward(20_000),
            ],
        );
        assert_eq!(fresh, [forward(1001), forward(1096)]);
        assert_eq!(boundary, None);

        let (fresh, boundary) = command_prefix(None, 1000, &[forward(MAX_LEASE_FRAME + 1)]);
        assert!(fresh.is_empty());
        assert_eq!(boundary, Some(MAX_LEASE_FRAME + 1));
        let (fresh, boundary) = command_prefix(
            None,
            MAX_LEASE_FRAME - 2,
            &[forward(MAX_LEASE_FRAME - 1), forward(MAX_LEASE_FRAME + 1)],
        );
        assert_eq!(fresh, [forward(MAX_LEASE_FRAME - 1)]);
        assert_eq!(boundary, Some(MAX_LEASE_FRAME + 1));
    }

    #[test]
    fn overlapping_bursts_enqueue_each_frame_once_and_do_not_ack_pending_work() {
        let mut world = WorldDelivery::new(0xa000_0100);
        world.started = Some(1000);
        let first: Vec<_> = (980..1012).map(forward).collect();
        let next: Vec<_> = (1000..1032).map(forward).collect();
        world.queue_commands(1000, &first);
        world.queue_commands(1000, &next);
        world.queue_commands(1000, &first);
        let frames: Vec<_> = world
            .input
            .work
            .iter()
            .map(|work| {
                let InputWork::Command {
                    command,
                    pending: None,
                } = work
                else {
                    panic!("fresh command");
                };
                command.frame
            })
            .collect();
        assert_eq!(frames, (980..1032).collect::<Vec<_>>());
        assert_eq!(world.command_ack(1000), 979);
        world.clear();
        assert!(!world.has_input_work());
        assert_eq!(world.command_ack(1001), 979);
    }

    #[test]
    fn pending_queue_closes_after_the_prefix_at_a_boundary_or_capacity() {
        let mut input = InputQueue::default();
        input.commands(
            MAX_LEASE_FRAME - 2,
            &[forward(MAX_LEASE_FRAME - 1), forward(MAX_LEASE_FRAME + 1)],
        );
        assert!(input.closing);
        assert_eq!(input.admitted, Some(MAX_LEASE_FRAME - 1));
        assert!(matches!(
            input.work.pop_front(),
            Some(InputWork::Command { .. })
        ));
        assert!(matches!(input.work.pop_front(), Some(InputWork::Leave)));

        let mut input = InputQueue::default();
        for frame in 1..=MAX_PENDING_INPUT as u32 + 2 {
            input.commands(frame, &[forward(frame)]);
        }
        assert_eq!(input.work.len(), MAX_PENDING_INPUT + 1);
        assert_eq!(input.admitted, Some(MAX_PENDING_INPUT as u32));
        assert!(matches!(input.work.back(), Some(InputWork::Leave)));
        assert_eq!(input.processed, None);
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range pack without bot VM graphs"]
    fn native_world_resumes_a_gap_before_switching_the_body_and_acknowledging_input() {
        let runtime = std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap();
        let assets = SceneAssets::load(runtime).unwrap();
        let scene = assets.prepare(GameMap::practice_range()).unwrap();
        let owner = 0xa000_0100;
        let viewer = PlayerAppearance {
            entity: owner,
            slot: 0,
            team: 0,
            card: &PlayerCard::default(),
        };
        let mut world = WorldDelivery::new(owner);
        let first = FIRST_TICK + 1;
        world.queue_commands(FIRST_TICK, &[forward(first)]);
        assert!(!world.has_input_work());
        assert_eq!(world.command_ack(FIRST_TICK), FIRST_TICK);
        world.start(&scene, &viewer, FIRST_TICK).unwrap();
        world.queue_commands(FIRST_TICK, &[forward(first)]);
        assert_eq!(
            world
                .advance_input(&scene, &viewer, RETAIL_QUANTA, 16)
                .unwrap(),
            (1, false)
        );
        assert_eq!(world.command_ack(FIRST_TICK + 2), first);
        world.created.insert(owner);
        world.present(&scene, FIRST_TICK).unwrap();
        let hero = scene.heroes().get(SOLDIER).unwrap();
        world
            .switch(
                &scene,
                &viewer,
                FIRST_TICK,
                RETAIL_QUANTA,
                owner | 1,
                (hero, SkinTheme::default()),
            )
            .unwrap();
        assert!(world.body_script.is_none());
        let initial = world.mover.as_ref().unwrap().pose();
        let actual = Command {
            yaw: initial.yaw,
            ..forward(first + 1)
        };
        let gap = Command {
            frame: actual.frame + 64,
            ..actual
        };
        let later = Command {
            frame: gap.frame + 1,
            ..actual
        };
        world.queue_commands(FIRST_TICK + 2, &[actual]);
        assert_eq!(
            world
                .advance_input(&scene, &viewer, RETAIL_QUANTA, 16)
                .unwrap(),
            (1, false)
        );
        world.queue_commands(FIRST_TICK + 2, &[actual, gap]);

        let tracer = scene.heroes().find_hero("Tracer").unwrap();
        let switch = Message {
            id: CLIENT_GAME_MESSAGE,
            fields: Fields::from([
                (
                    0x78,
                    Value::Struct(Fields::from([(0, Value::Unsigned(u64::from(owner)))])),
                ),
                (0x80, Value::Blob(tracer.guid.to_le_bytes().to_vec())),
                (0xa8, Value::Unsigned(PICK_HERO)),
                (0xb0, Value::Signed(3)),
            ]),
        };
        world.queue_game_message(FIRST_TICK + 3, Duration::ZERO, switch);
        world.queue_commands(FIRST_TICK + 3, &[gap, later]);

        for turn in 1..=4 {
            assert_eq!(
                world
                    .advance_input(&scene, &viewer, RETAIL_QUANTA, 16)
                    .unwrap(),
                (16, false)
            );
            assert_eq!(world.body.unwrap().id.entity, owner | 1);
            assert_eq!(
                world.mover.as_ref().unwrap().pose().frame,
                Some(actual.frame + turn * 16)
            );
            assert_eq!(
                world.command_ack(FIRST_TICK + 90),
                if turn == 4 { gap.frame } else { actual.frame }
            );
        }
        let (position, yaw) = scene.map().spawn_point(viewer.team, 0).unwrap();
        let yaw = (yaw * 65536.0 / 360.0).round_ties_even() as i32;
        let mut synchronous = scene
            .player_mover(hero.body, viewer.team, position, yaw)
            .unwrap();
        for command in [actual, gap] {
            synchronous
                .step(scene.movement_index(), command, 0.016, |_, _| {})
                .unwrap();
        }
        assert_eq!(world.mover.as_ref().unwrap().pose(), synchronous.pose());
        assert_ne!(synchronous.pose().position, initial.position);

        assert_eq!(
            world
                .advance_input(&scene, &viewer, RETAIL_QUANTA, 16)
                .unwrap(),
            (2, false)
        );
        assert_eq!(world.body.unwrap().id.entity, owner | 2);
        assert_eq!(world.command_ack(FIRST_TICK + 90), later.frame);
        let mut synchronous = scene
            .player_mover(tracer.body, viewer.team, position, yaw)
            .unwrap();
        synchronous
            .step(scene.movement_index(), later, 0.016, |_, _| {})
            .unwrap();
        assert_eq!(world.mover.as_ref().unwrap().pose(), synchronous.pose());

        world.queue_commands(FIRST_TICK + 3, &[forward(later.frame + 1)]);
        world.clear();
        assert!(!world.has_input_work());
        assert_eq!(world.command_ack(FIRST_TICK + 90), later.frame);
    }
    use crate::game_egress::PacketDelivery;

    const OWNER: u32 = 0xa000_0100;

    fn avatar(suffix: u32, generation: u64) -> BodyId {
        BodyId {
            entity: OWNER | suffix,
            generation,
        }
    }

    fn body(id: BodyId) -> Body {
        Body {
            id,
            hero: 7,
            skin: SkinTheme::default(),
        }
    }

    fn record(entity: u32, delivery: Delivery) -> Record {
        let operation = match delivery {
            Delivery::CreateBody(_) => {
                Operation::Create(Box::new(crate::world::Create::new(1, Vec::new())))
            }
            Delivery::DestroyBody(_) => Operation::Destroy,
            _ => Operation::Update(Vec::new()),
        };
        Record {
            update: EntityUpdate {
                entity,
                operation: Some(operation),
                ..EntityUpdate::default()
            },
            delivery,
        }
    }

    fn outcome(record: Arc<Record>, arrived: bool) -> DeliveryReport<Arc<Record>> {
        DeliveryReport {
            requeued_reliable: 0,
            packets: vec![PacketDelivery {
                sequence: 0,
                arrived,
                records: vec![record],
            }],
        }
    }

    fn eligible(world: &WorldDelivery) -> Vec<usize> {
        world.ready().iter().map(|ready| ready.index).collect()
    }

    struct SyntheticOwner {
        component: Component,
        graphs: Arc<GraphStore>,
        codecs: CodecTable,
        modifiers: BTreeMap<ModifierKey, Modifier>,
        failed: Arc<AtomicBool>,
    }

    impl OwnerBodyRuntime for SyntheticOwner {
        fn snapshot(&self) -> Option<&Snapshot> {
            (!self.failed.load(Ordering::Relaxed))
                .then(|| self.component.snapshot())
                .flatten()
        }

        fn encoder(&self) -> SnapshotEncoder<'_> {
            SnapshotEncoder::new(&self.graphs, &self.codecs)
        }

        fn advance(&mut self, command: Command, _mover: &Mover) -> io::Result<()> {
            self.component
                .advance_command(command, &mut EmptyWorld)
                .map_err(invalid)
        }

        fn frame_ms(&self) -> u32 {
            FRAME_MS as u32
        }

        fn movement_modifiers(&self) -> Option<&BTreeMap<ModifierKey, Modifier>> {
            Some(&self.modifiers)
        }
    }

    fn synthetic_owner(
        entity: u32,
        frame: u32,
        failed: Arc<AtomicBool>,
    ) -> Box<dyn OwnerBodyRuntime> {
        let graphs = Arc::new(
            GraphStore::from_json(
                br#"{"version":1,"bodies":{},"missing":[],"graphs":[{
            "index":42,"items":0,"nodes":[],"states":[],"entries":[],"remote":[],
            "sync_vars":[],"containers":{},"unknown_nodes":[],"fields":{},
            "header":{"m_nodesBitCount":4,"m_statesBitCount":4,
                "m_remoteSyncNodesBitCount":4,"m_syncVarsBitCount":4}
        }]}"#,
            )
            .unwrap(),
        );
        let component = Component::start(
            Arc::clone(&graphs),
            entity,
            Default::default(),
            vec![Root::new(1, 42)],
            frame,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        Box::new(SyntheticOwner {
            component,
            graphs,
            codecs: CodecTable::from_json(r#"{"classes":{}}"#).unwrap(),
            modifiers: BTreeMap::new(),
            failed,
        })
    }

    fn activate_script(world: &mut WorldDelivery, id: BodyId, hero: u64, failed: Arc<AtomicBool>) {
        let body = Body { hero, ..body(id) };
        world.body = Some(body);
        world.bound = None;
        world
            .bodies
            .insert(id.entity, (id.generation, Lifetime::Live));
        world.created.remove(&id.entity);
        world.body_script =
            Some(BodyScript::new(body, 100, synthetic_owner(id.entity, 100, failed)).unwrap());
    }

    fn transmit(
        world: &mut WorldDelivery,
        egress: &mut GameEgress<Arc<Record>>,
        tick: u32,
        sequence: u32,
    ) -> (Vec<u8>, SentFrame) {
        let mut bytes = Vec::new();
        let sent = world
            .send_frame(tick, Vec::new(), false, egress, |payload| {
                bytes = payload;
                Ok::<_, ()>(sequence)
            })
            .unwrap();
        (bytes, sent)
    }

    fn transport_outcome(
        world: &mut WorldDelivery,
        egress: &mut GameEgress<Arc<Record>>,
        sequence: u32,
        arrived: bool,
    ) {
        world.acknowledge(
            egress.acknowledge(sequence + 1, u64::from(arrived)),
            Duration::ZERO,
        );
    }

    #[test]
    fn body_script_retries_frozen_initial_frames_and_ignores_old_generation_reports() {
        let mut world = WorldDelivery::new(OWNER);
        let id = avatar(1, 1);
        world.started = Some(100);
        activate_script(&mut world, id, 7, Arc::default());
        world.push(record(id.entity, Delivery::CreateBody(id)));
        let mut egress = GameEgress::default();
        assert_eq!(
            transmit(&mut world, &mut egress, 100, 0).1.records,
            [RecordSource::Queued(0)]
        );
        assert!(
            transmit(&mut world, &mut egress, 100, 1)
                .1
                .records
                .is_empty()
        );
        transport_outcome(&mut world, &mut egress, 0, true);
        let (h0, sent) = transmit(&mut world, &mut egress, 101, 2);
        assert_eq!(sent.records, [RecordSource::Queued(0)]);
        transport_outcome(&mut world, &mut egress, 2, false);
        assert_eq!(transmit(&mut world, &mut egress, 101, 3).0, h0);
        transport_outcome(&mut world, &mut egress, 3, true);
        assert_eq!(
            world.body_script.as_ref().unwrap().stream.base().unwrap().0,
            1
        );

        let blocked = transmit(&mut world, &mut egress, 101, 4).1;
        assert!(blocked.records.is_empty());
        let mut mover = Mover::new(
            MoverSettings::default(),
            Arc::new(ContactWorld::new(Vec::new()).unwrap()),
            [0.0; 3],
            0,
            0x18,
        )
        .unwrap();
        let rays = TriangleIndex::new([]).unwrap();
        let command = forward(101);
        mover
            .step(&rays, command, 0.016, |mover, command| {
                world
                    .body_script
                    .as_mut()
                    .unwrap()
                    .step(mover, command)
                    .unwrap();
            })
            .unwrap();
        assert!(
            transmit(&mut world, &mut egress, 101, 5)
                .1
                .records
                .is_empty()
        );
        let stream = &world.body_script.as_ref().unwrap().stream;
        assert_eq!(
            stream.pending().map(|(last, ..)| last).collect::<Vec<_>>(),
            [3]
        );
        assert_eq!(stream.command_frame(), 101);

        world.push(record(OWNER, Delivery::Possession(id)));
        assert_eq!(
            transmit(&mut world, &mut egress, 101, 6).1.records,
            [RecordSource::Queued(0)]
        );
        transport_outcome(&mut world, &mut egress, 6, true);
        let (c0, sent) = transmit(&mut world, &mut egress, 102, 7);
        assert_eq!(sent.records, [RecordSource::Queued(0)]);
        let command = forward(117);
        mover
            .step(&rays, command, 0.016, |mover, command| {
                world
                    .body_script
                    .as_mut()
                    .unwrap()
                    .step(mover, command)
                    .unwrap();
            })
            .unwrap();
        assert!(
            transmit(&mut world, &mut egress, 102, 8)
                .1
                .records
                .is_empty()
        );
        transport_outcome(&mut world, &mut egress, 7, false);
        assert_eq!(transmit(&mut world, &mut egress, 102, 9).0, c0);
        transport_outcome(&mut world, &mut egress, 9, true);
        assert_eq!(
            transmit(&mut world, &mut egress, 117, 10).1.records,
            [RecordSource::Queued(0)]
        );
        transport_outcome(&mut world, &mut egress, 10, false);
        assert!(
            transmit(&mut world, &mut egress, 118, 11)
                .1
                .records
                .is_empty()
        );

        mover
            .step(&rays, forward(118), 0.016, |mover, command| {
                world
                    .body_script
                    .as_mut()
                    .unwrap()
                    .step(mover, command)
                    .unwrap();
            })
            .unwrap();
        assert_eq!(
            transmit(&mut world, &mut egress, 118, 12).1.records,
            [RecordSource::Queued(0)]
        );
        world.cancel_body_script();
        let reused = avatar(1, 257);
        activate_script(&mut world, reused, 7, Arc::default());
        transport_outcome(&mut world, &mut egress, 12, true);
        assert_eq!(world.body_script.as_ref().unwrap().stream.last(), 0);
        assert!(world.queued.is_empty());
    }

    fn read_ack(payload: &[u8]) -> u32 {
        let mut bits = BitReader::new(payload);
        bits.var_a().unwrap();
        assert!(!bits.bit().unwrap());
        bits.var_a().unwrap()
    }

    #[test]
    fn owner_correction_respects_create_budget_and_failed_transport() {
        let mut world = WorldDelivery::new(OWNER);
        let id = avatar(1, 1);
        world.started = Some(100);
        let failed = Arc::new(AtomicBool::new(false));
        activate_script(&mut world, id, SOLDIER, Arc::clone(&failed));
        world.input.processed = Some(125);
        let history = world
            .body_script
            .as_mut()
            .unwrap()
            .corrections
            .as_mut()
            .unwrap();
        history.record(Movement {
            frame: Some(100),
            ..Movement::default()
        });
        let mut egress = GameEgress::default();
        let (blocked, sent) = transmit(&mut world, &mut egress, 100, 0);
        assert_eq!(read_ack(&blocked), 102);
        assert!(sent.records.is_empty());
        world.acknowledge(
            outcome(Arc::new(record(id.entity, Delivery::CreateBody(id))), true),
            Duration::ZERO,
        );
        let (first, sent) = transmit(&mut world, &mut egress, 100, 1);
        assert_eq!(read_ack(&first), 102);
        assert!(sent.records.contains(&RecordSource::Movement(0)));
        transport_outcome(&mut world, &mut egress, 1, true);
        let history = world
            .body_script
            .as_mut()
            .unwrap()
            .corrections
            .as_mut()
            .unwrap();
        assert_eq!(history.corrected(), Some(100));
        history.record(Movement {
            frame: Some(106),
            ..Movement::default()
        });
        history.record(Movement {
            frame: Some(112),
            ..Movement::default()
        });
        for i in 0..MAX_ENTITY_RECORDS {
            world.push(Record::baseline(EntityUpdate {
                entity: 0x8000_0100 + i as u32,
                operation: Some(Operation::Destroy),
                ..EntityUpdate::default()
            }));
        }
        let (crowded, sent) = transmit(&mut world, &mut egress, 107, 2);
        assert_eq!(read_ack(&crowded), 102);
        assert_eq!(sent.records.len(), MAX_ENTITY_RECORDS);
        assert!(!sent.records.contains(&RecordSource::Movement(0)));
        transport_outcome(&mut world, &mut egress, 2, true);
        let rejected = world.send_frame(107, Vec::new(), false, &mut egress, |payload| {
            assert_eq!(read_ack(&payload), 108);
            Err(())
        });
        assert!(matches!(rejected, Err(SendError::Transport(()))));
        assert_eq!(
            world
                .body_script
                .as_ref()
                .unwrap()
                .corrections
                .as_ref()
                .unwrap()
                .corrected(),
            Some(100)
        );
        let (selected, sent) = transmit(&mut world, &mut egress, 107, 3);
        assert_eq!(read_ack(&selected), 108);
        assert_eq!(sent.records, [RecordSource::Movement(0)]);
        assert!(egress.acknowledge(4, 0).packets.is_empty());
        assert!(
            transmit(&mut world, &mut egress, 107, 4)
                .1
                .records
                .is_empty()
        );

        failed.store(true, Ordering::Relaxed);
        let (payload, sent) = transmit(&mut world, &mut egress, 112, 5);
        assert_eq!(read_ack(&payload), 125);
        assert!(world.body_script.is_none());
        assert!(sent.records.is_empty());
        assert!(world.queued.is_empty());
    }

    #[test]
    #[ignore = "requires a prepared 104319 Practice Range pack without bot VM graphs"]
    fn prepared_worker_advances_an_attached_vm_and_cancels_it_on_a_pose_fault() {
        let runtime = std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap();
        let assets = SceneAssets::load(runtime).unwrap();
        let mut scene = assets.prepare(GameMap::practice_range()).unwrap();
        let (factory, _) = crate::script_player::fixture_body_factory();
        scene.owner_body_factory = Some(Arc::new(move |_, spawn| {
            Ok(Some(Box::new(factory.start(spawn)?)))
        }));
        let viewer = PlayerAppearance {
            entity: OWNER,
            slot: 0,
            team: 0,
            card: &PlayerCard::default(),
        };
        let mut world = WorldDelivery::new(OWNER);
        world.started = Some(FIRST_TICK);
        let hero = scene.heroes().get(SOLDIER).unwrap();
        world
            .switch(
                &scene,
                &viewer,
                FIRST_TICK,
                RETAIL_QUANTA,
                OWNER | 1,
                (hero, SkinTheme::default()),
            )
            .unwrap();
        let tick = FIRST_TICK + 1;
        world.queue_commands(tick, &[forward(tick)]);
        assert_eq!(
            world
                .advance_input(&scene, &viewer, RETAIL_QUANTA, 16)
                .unwrap(),
            (1, false)
        );
        assert_eq!(world.body_script.as_ref().unwrap().frame, tick);
        let mut egress = GameEgress::default();
        assert!(
            !transmit(&mut world, &mut egress, tick, 0)
                .1
                .records
                .contains(&RecordSource::Movement(0))
        );
        transport_outcome(&mut world, &mut egress, 0, true);
        assert_eq!(
            transmit(&mut world, &mut egress, tick, 1).1.records,
            [RecordSource::Queued(0), RecordSource::Movement(0)]
        );

        let after_unposed_gap = tick + 65;
        world.queue_commands(after_unposed_gap, &[forward(after_unposed_gap)]);
        assert_eq!(
            world
                .advance_input(&scene, &viewer, RETAIL_QUANTA, 16)
                .unwrap(),
            (1, false)
        );
        assert_eq!(world.command_ack(after_unposed_gap), after_unposed_gap);
        assert!(world.body_script.is_none());
        transport_outcome(&mut world, &mut egress, 1, false);
        assert!(
            transmit(&mut world, &mut egress, after_unposed_gap, 2)
                .1
                .records
                .is_empty()
        );
    }

    #[test]
    fn possession_and_retirement_wait_when_budget_splits_the_prerequisites() {
        let mut world = WorldDelivery::new(OWNER);
        world.started = Some(1001);
        let previous = avatar(1, 1);
        let current = avatar(2, 2);
        world.body = Some(body(current));
        world
            .bodies
            .insert(previous.entity, (1, Lifetime::Retiring));
        world.bodies.insert(current.entity, (2, Lifetime::Live));
        world.push(record(current.entity, Delivery::CreateBody(current)));
        world.push(record(OWNER, Delivery::Possession(current)));
        world.push(record(previous.entity, Delivery::DestroyBody(previous)));
        assert_eq!(eligible(&world), [0, 1, 2]);

        let create = world.ready()[0].tracked();
        world.sent(&[0]); // The packet budget only fit the create.
        assert!(eligible(&world).is_empty());
        world.acknowledge(outcome(Arc::clone(&create), false), Duration::ZERO);
        assert_eq!(eligible(&world), [0, 1, 2]);
        world.sent(&[0]);
        world.acknowledge(outcome(create, true), Duration::ZERO);
        assert_eq!(eligible(&world), [0, 1]);

        let possess = world.ready()[0].tracked();
        world.sent(&[0]);
        assert!(eligible(&world).is_empty());
        world.acknowledge(outcome(possess, true), Duration::ZERO);
        assert_eq!(eligible(&world), [0]);
        let destroy = world.ready()[0].tracked();
        world.sent(&[0]);
        let now = Duration::from_secs(5);
        world.acknowledge(outcome(destroy, true), now);
        assert!(matches!(
            world.bodies[&previous.entity].1,
            Lifetime::ReusableAt(at) if at == now + BODY_REUSE_DELAY
        ));
    }

    #[test]
    fn a_late_ack_or_loss_cannot_resurrect_a_retired_or_reused_body() {
        let mut world = WorldDelivery::new(OWNER);
        world.started = Some(1001);
        let old = avatar(1, 1);
        let reused = avatar(1, 257);
        world.body = Some(body(reused));
        world
            .bodies
            .insert(reused.entity, (reused.generation, Lifetime::Live));
        let stale = Arc::new(record(old.entity, Delivery::CreateBody(old)));
        world.acknowledge(outcome(Arc::clone(&stale), true), Duration::ZERO);
        world.acknowledge(outcome(stale, false), Duration::ZERO);
        assert!(!world.created.contains(&reused.entity));
        assert!(world.queued.is_empty());
        world.acknowledge(
            outcome(Arc::new(record(OWNER, Delivery::Possession(old))), true),
            Duration::ZERO,
        );
        assert!(world.bound.is_none());
    }

    #[test]
    fn ring_reuses_only_acknowledged_destroy_after_the_retention_window() {
        let mut world = WorldDelivery::new(OWNER);
        world.body = Some(body(avatar(255, 255)));
        for suffix in 1..=255 {
            world
                .bodies
                .insert(OWNER | suffix, (u64::from(suffix), Lifetime::Live));
        }
        let target = avatar(1, 1);
        world.bodies.insert(target.entity, (1, Lifetime::Retiring));
        assert_eq!(world.next_body(Duration::from_secs(1000)), None);
        world
            .bodies
            .insert(target.entity, (1, Lifetime::ReusableAt(BODY_REUSE_DELAY)));
        assert_eq!(
            world.next_body(BODY_REUSE_DELAY - Duration::from_millis(1)),
            None
        );
        assert_eq!(world.next_body(BODY_REUSE_DELAY), Some(target.entity));
    }
}
