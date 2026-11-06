//! Per-peer world delivery, including the owning player's body lifetime.

use std::{
    borrow::Borrow,
    collections::{HashMap, HashSet},
    io,
    sync::Arc,
    time::Duration,
};

use crate::{
    game_body::{BodyCreate, BodyView, body_create},
    game_bots::BOT_RECORD_DELAY,
    game_clock::SEND_EVERY,
    game_controller::ControllerFrame,
    game_egress::{DeliveryReport, RecordSource},
    game_heroes::Hero,
    game_messages::Message,
    game_parameters::{ClientGameMessage, HeroSelectRequest},
    match_scene::{PlayerAppearance, Possession, team_bit},
    scene_assets::PreparedScene,
    script_stream::{ScriptChunk, Stream},
    world::{EntityUpdate, GAME_MODE_ENTITY, Movement, Operation, SkinTheme},
};

const BODY_REUSE_DELAY: Duration = Duration::from_secs(30);

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
        }
    }

    pub(crate) fn clear(&mut self) {
        self.queued.clear();
        self.started = None;
        self.presentation_due = false;
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
                if !packet.arrived {
                    if !matches!(record.delivery, Delivery::Script { resend: false, .. }) {
                        retry.push(record);
                    }
                    continue;
                }
                match record.delivery {
                    Delivery::Baseline | Delivery::CreateBody(_) => {
                        if matches!(record.update.operation, Some(Operation::Create(_))) {
                            self.created.insert(record.update.entity);
                        }
                    }
                    Delivery::Possession(id) => self.bound = Some(id),
                    Delivery::DestroyBody(id) => {
                        self.bodies.insert(
                            id.entity,
                            (
                                id.generation,
                                Lifetime::ReusableAt(now.saturating_add(BODY_REUSE_DELAY)),
                            ),
                        );
                    }
                    Delivery::Script { last, .. } if record.update.entity == self.owner => {
                        self.controller.arrived(last);
                    }
                    Delivery::Script { last, .. } => {
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

    pub(crate) fn game_message(
        &mut self,
        scene: &PreparedScene,
        viewer: &PlayerAppearance<'_>,
        tick: u32,
        now: Duration,
        message: &Message,
    ) -> io::Result<()> {
        if self.started.is_none()
            || self.controller.latest_data() == 0
            || !self.created.contains(&self.owner)
        {
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
                    self.switch(scene, viewer, tick, entity, hero, skin)?;
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
        entity: u32,
        hero: &Hero,
        skin: SkinTheme,
    ) -> io::Result<()> {
        let (position, yaw) = scene
            .map()
            .spawn_point(viewer.team, 0)
            .ok_or_else(|| invalid("no spawn for the selected hero"))?;
        let yaw = (yaw * 65536.0 / 360.0).round_ties_even() as i32;
        let pose = Movement {
            position,
            yaw,
            ..Movement::default()
        };
        let create = body_create(
            BodyCreate {
                entity,
                hero,
                team_bit: team_bit(viewer.team)?,
                position,
                yaw,
                skin,
                vitals: None,
            },
            BodyView::Owner {
                pose: Some(pose),
                leases: &[],
            },
            scene.pools(),
        );
        let generation = self
            .generation
            .checked_add(1)
            .ok_or_else(|| invalid("player body lifetime exhausted"))?;
        let id = BodyId { entity, generation };
        let possession = Possession {
            body: entity,
            hero: hero.guid,
            skin: (skin.guid != 0).then_some(skin.guid),
        };
        let close = self.controller_chunk(scene, tick, false, Some(hero.guid))?;

        self.generation = generation;
        self.bodies.insert(entity, (generation, Lifetime::Live));
        self.created.remove(&entity);
        let old = self.body.replace(Body {
            id,
            hero: hero.guid,
            skin,
        });
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
        let chunk = self
            .controller
            .full_owner(tick, |cmfd| {
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

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
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
