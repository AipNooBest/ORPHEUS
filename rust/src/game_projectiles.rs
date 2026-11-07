//! Predicted projectile leases and owner delivery for build 104319.
//!
//! Ported from ow174/game/projectiles.py. A report comes from the exact
//! datagram that carried an owner frame. Cumulative script acknowledgements
//! alone do not confirm a projectile.

use std::{collections::HashSet, io};

use indexmap::IndexMap;
use serde_json::Value as Json;

use crate::{
    game_clock::MAX_LEASE_FRAME,
    projectile_wire,
    script_expr::{EvalError, to_float},
    script_graph::{GraphStore, Node, asset_guid},
    script_init::{ShotEvent, Snapshot},
    script_wire::Value,
    world::{EntityUpdate, LeaseBlock, Operation},
};

const LEASE_FIRST: u32 = 0x0010_0000;
const LEASE_SPAN: u32 = 1 << 22;
const LEASE_WINDOWS: u32 = 2;
const DEFINITIONS_PER_BODY: usize = 4;
const PREDICTED_ID: u32 = 0xa000_0000;
const ID_LIMIT: u32 = 0x2000_0000;
const REPORT_WINDOW: u32 = 64;
const UNREPORTED_TICKS: u32 = 32;
const RELEASE_MARGIN: u32 = 6;
const RELEASE_AHEAD: u32 = 4096;
const DELIVERY_GRACE: u32 = 64;
const MAX_PROJECTILES: usize = 256;

#[derive(Clone, Debug, Default)]
pub struct ProjectileLeases {
    blocks: Vec<LeaseBlock>,
}

impl ProjectileLeases {
    pub fn new(
        slot: u8,
        frame: u32,
        definitions: impl IntoIterator<Item = u64>,
    ) -> io::Result<Self> {
        if frame > MAX_LEASE_FRAME {
            return Err(invalid("projectile leases cannot cross the command epoch"));
        }
        let mut blocks = Vec::new();
        let start = frame - frame % LEASE_SPAN;
        for definition in definitions {
            if blocks.len() == DEFINITIONS_PER_BODY * LEASE_WINDOWS as usize {
                break;
            }
            if definition == 0
                || blocks
                    .iter()
                    .any(|block: &LeaseBlock| block.definition == definition)
            {
                continue;
            }
            let index = blocks.len() / LEASE_WINDOWS as usize;
            let body = u32::from(slot) * DEFINITIONS_PER_BODY as u32 + index as u32;
            let first_id = LEASE_FIRST + body * LEASE_SPAN;
            if first_id > ID_LIMIT - LEASE_SPAN {
                return Err(invalid("projectile leases exceed the body slot range"));
            }
            for window in 0..LEASE_WINDOWS {
                let start = start + window * LEASE_SPAN;
                blocks.push(LeaseBlock {
                    definition,
                    start,
                    stop: start + LEASE_SPAN,
                    first_id,
                    per_frame: 1,
                });
            }
        }
        Ok(Self { blocks })
    }

    /// Inspect authored volley candidates in instance, state and field order.
    /// A conditional projectile field can name several predicted definitions.
    pub fn from_authored(
        graphs: &GraphStore,
        snapshot: &Snapshot,
        slot: u8,
        frame: u32,
        mut evaluate: impl FnMut(u16, u16, &Json) -> Result<Value, EvalError>,
    ) -> io::Result<Self> {
        let mut definitions = Vec::new();
        for (&instance, state) in &snapshot.instances {
            let graph = graphs
                .graph(state.graph)
                .ok_or_else(|| invalid("missing volley graph"))?;
            for raw in 0..graph.states.len() {
                let Some(node) = graph.state(raw) else {
                    continue;
                };
                if node.class != "STUStatescriptStateWeaponVolley" || !local_prediction(node) {
                    continue;
                }
                let raw = u16::try_from(raw).map_err(io::Error::other)?;
                if predicted_sync(node, |config| evaluate(instance, raw, config))
                    .map_err(io::Error::other)?
                {
                    if let Some(field) = node.field("m_projectileEntity") {
                        projectile_definitions(field, &mut definitions);
                    }
                }
            }
        }
        Self::new(slot, frame, definitions)
    }

    pub fn blocks(&self) -> &[LeaseBlock] {
        &self.blocks
    }

    pub fn entity(&self, definition: u64, frame: u32, index: u8) -> Option<u32> {
        let block = self.blocks.iter().find(|block| {
            block.definition == definition
                && (block.start..block.stop).contains(&frame)
                && index < block.per_frame
        })?;
        Some(
            PREDICTED_ID
                | (block.first_id
                    + (frame - block.start) * u32::from(block.per_frame)
                    + u32::from(index)),
        )
    }
}

pub(crate) fn local_prediction(node: &Node) -> bool {
    node.field("m_45271E8C").is_some_and(|value| {
        value
            .as_bool()
            .unwrap_or_else(|| value.as_i64().is_some_and(|value| value != 0))
    })
}

pub fn predicted_sync(
    node: &Node,
    mut evaluate: impl FnMut(&Json) -> Result<Value, EvalError>,
) -> Result<bool, EvalError> {
    let mut field = |name, default| authored_number(node.field(name), default, &mut evaluate);
    let explicit = field("m_5E4A4E85", 0.0)? as i64;
    if explicit != 0 {
        return Ok(explicit == 1);
    }
    if field("m_A17BE89B", 1.0)? >= 2.0 && field("m_numShotsPerSecond", 0.0)? >= 5.5 {
        return Ok(false);
    }
    Ok(field("m_projectileLifetime", 0.0)? >= 1.19e-5
        && field("m_numProjectilesPerShot", 1.0)? <= 7.0)
}

pub(crate) fn authored_number(
    config: Option<&Json>,
    default: f64,
    evaluate: &mut impl FnMut(&Json) -> Result<Value, EvalError>,
) -> Result<f64, EvalError> {
    let Some(config) = config.filter(|value| !value.is_null()) else {
        return Ok(default);
    };
    let value = evaluate(config)?;
    Ok(if value == Value::Null {
        default
    } else {
        to_float(&value)
    })
}

fn projectile_definitions(value: &Json, out: &mut Vec<u64>) {
    match value {
        Json::Object(fields) => {
            if fields.get("$").and_then(Json::as_str) == Some("STU_8556841E") {
                if let Some(definition) = fields.get("m_entityDef").and_then(asset_guid) {
                    if !out.contains(&definition) {
                        out.push(definition);
                    }
                }
            }
            for value in fields.values() {
                projectile_definitions(value, out);
            }
        }
        Json::Array(values) => {
            for value in values {
                projectile_definitions(value, out);
            }
        }
        _ => {}
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProjectileToken(u64);

#[derive(Clone, Copy, Debug)]
struct OwnerReport {
    frame: u32,
    introduces: bool,
    arrived: Option<bool>,
}

struct PendingProjectile {
    token: ProjectileToken,
    shot: ShotEvent,
    definition: u64,
    bound: u32,
    reports: IndexMap<u32, OwnerReport>,
    creates: IndexMap<u32, bool>,
    created_at: Option<u32>,
    release_at: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProjectileDelivery {
    Confirm {
        token: ProjectileToken,
        entity: u32,
        definition: u64,
    },
    Destroy {
        token: ProjectileToken,
        entity: u32,
        explosion_frame: Option<u32>,
    },
}

impl ProjectileDelivery {
    pub fn update(self) -> Result<EntityUpdate, crate::bits::BitError> {
        match self {
            Self::Confirm {
                entity, definition, ..
            } => Ok(projectile_wire::confirmation(entity, definition)),
            Self::Destroy {
                entity,
                explosion_frame,
                ..
            } => Ok(EntityUpdate {
                entity,
                operation: Some(Operation::Destroy),
                ch2: explosion_frame
                    .map(|frame| projectile_wire::release(frame, 1))
                    .transpose()?,
                ..EntityUpdate::default()
            }),
        }
    }
}

/// One owner body generation's confirmation state. Keep it with the body
/// stream and retire it when that body is replaced or loses authority.
pub struct OwnerProjectiles {
    body: u32,
    leases: ProjectileLeases,
    pending: Vec<PendingProjectile>,
    occupied: HashSet<u32>,
    next: u64,
}

impl OwnerProjectiles {
    pub fn new(body: u32, leases: ProjectileLeases) -> Self {
        Self {
            body,
            leases,
            pending: Vec::new(),
            occupied: HashSet::new(),
            next: 0,
        }
    }

    pub fn admit(
        &mut self,
        shot: ShotEvent,
        definition: u64,
        flight_bound_ticks: u32,
    ) -> Option<ProjectileToken> {
        if shot.entity != self.body || self.pending.len() == MAX_PROJECTILES {
            return None;
        }
        self.leases.entity(definition, shot.frame, 0)?;
        if self
            .pending
            .iter()
            .any(|active| active.definition == definition && active.shot.frame == shot.frame)
        {
            return None; // the advertised lease has one projectile ID per definition and frame
        }
        self.next = self.next.checked_add(1)?;
        let token = ProjectileToken(self.next);
        self.pending.push(PendingProjectile {
            token,
            shot,
            definition,
            bound: flight_bound_ticks,
            reports: IndexMap::new(),
            creates: IndexMap::new(),
            created_at: None,
            release_at: None,
        });
        Some(token)
    }

    /// Call only for the first successful send of a logical owner report.
    pub fn owner_sent(
        &mut self,
        last: u32,
        frame: u32,
        identity: impl Fn(u16, u16) -> Option<(u16, u64)>,
        introduces: impl Fn(u16, u16) -> bool,
    ) {
        for pending in &mut self.pending {
            let shot = &pending.shot;
            if (shot.frame..=shot.frame.saturating_add(REPORT_WINDOW)).contains(&frame)
                && identity(shot.instance, shot.raw_state)
                    == Some((shot.activation, shot.activation_ms))
            {
                pending.reports.entry(last).or_insert(OwnerReport {
                    frame,
                    introduces: introduces(shot.instance, shot.raw_state),
                    arrived: None,
                });
            }
        }
    }

    pub fn owner_arrival(&mut self, last: u32, arrived: bool) {
        for pending in &mut self.pending {
            if let Some(report) = pending.reports.get_mut(&last) {
                if report.arrived.is_none() {
                    report.arrived = Some(arrived);
                }
            }
        }
    }

    pub fn create_arrived(&mut self, token: ProjectileToken, entity: u32) {
        if let Some(pending) = self
            .pending
            .iter_mut()
            .find(|pending| pending.token == token)
        {
            if let Some(arrived) = pending.creates.get_mut(&entity) {
                *arrived = true;
            }
        }
    }

    pub fn confirmation_current(&self, token: ProjectileToken, entity: u32) -> bool {
        self.pending
            .iter()
            .any(|pending| pending.token == token && pending.creates.contains_key(&entity))
    }

    pub fn flight_ended(&mut self, token: ProjectileToken, travelled_ticks: u32, tick: u32) {
        let Some(pending) = self
            .pending
            .iter_mut()
            .find(|pending| pending.token == token)
        else {
            return;
        };
        if pending.release_at.is_none() {
            let start = pending
                .reports
                .values()
                .find(|report| report.arrived != Some(false))
                .map_or(pending.shot.frame, |report| report.frame);
            pending.release_at = Some(
                start
                    .saturating_add(travelled_ticks)
                    .saturating_add(RELEASE_MARGIN)
                    .max(tick),
            );
        }
    }

    pub fn advance(&mut self, tick: u32) -> Vec<ProjectileDelivery> {
        let mut out = Vec::new();
        self.pending.retain_mut(|pending| {
            pending
                .reports
                .retain(|_, report| report.arrived != Some(false));
            let due = pending.release_at.or_else(|| {
                pending.created_at.map(|created| {
                    created
                        .saturating_add(pending.bound)
                        .saturating_add(RELEASE_MARGIN)
                })
            });
            if due.is_some_and(|due| tick >= due.saturating_add(DELIVERY_GRACE)) {
                retire(pending, &mut self.occupied, Some(tick), &mut out);
                return false;
            }
            let reports = pending
                .reports
                .values()
                .filter(|report| report.arrived == Some(true));
            let has_report = reports.clone().next().is_some();
            let fallback = pending.reports.is_empty()
                && pending.creates.is_empty()
                && tick >= pending.shot.frame.saturating_add(UNREPORTED_TICKS);
            if has_report || fallback {
                let frames = std::iter::once(pending.shot.frame).chain(
                    reports
                        .filter(|report| report.introduces)
                        .map(|report| report.frame),
                );
                for frame in frames {
                    let Some(entity) = self.leases.entity(pending.definition, frame, 0) else {
                        continue;
                    };
                    if self.occupied.insert(entity) {
                        pending.creates.insert(entity, false);
                        pending.created_at.get_or_insert(tick);
                        out.push(ProjectileDelivery::Confirm {
                            token: pending.token,
                            entity,
                            definition: pending.definition,
                        });
                    }
                }
            }
            if due.is_some_and(|due| tick >= due)
                && !pending.creates.is_empty()
                && pending.creates.values().all(|&arrived| arrived)
            {
                retire(pending, &mut self.occupied, Some(tick), &mut out);
                return false;
            }
            true
        });
        out
    }

    pub fn retire(&mut self) -> Vec<ProjectileDelivery> {
        let mut out = Vec::new();
        for pending in &self.pending {
            retire(pending, &mut self.occupied, None, &mut out);
        }
        self.pending.clear();
        out
    }

    pub(crate) fn cancel(&mut self, token: ProjectileToken, out: &mut Vec<ProjectileDelivery>) {
        if let Some(index) = self
            .pending
            .iter()
            .position(|pending| pending.token == token)
        {
            let pending = self.pending.remove(index);
            retire(&pending, &mut self.occupied, None, out);
        }
    }
}

fn retire(
    pending: &PendingProjectile,
    occupied: &mut HashSet<u32>,
    tick: Option<u32>,
    out: &mut Vec<ProjectileDelivery>,
) {
    for (&entity, &arrived) in &pending.creates {
        occupied.remove(&entity);
        out.push(ProjectileDelivery::Destroy {
            token: pending.token,
            entity,
            explosion_frame: tick
                .filter(|_| arrived)
                .map(|tick| tick.saturating_add(RELEASE_AHEAD)),
        });
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::script_init::{
        Component, EmptyWorld, MAX_INIT_WORK, player_entity_variables, player_roots,
    };
    use std::{path::PathBuf, sync::Arc};

    const BODY: u32 = 0xa000_0101;
    const CANISTER: u64 = 0x0400_0000_0000_072d;
    const VISOR: u64 = 0x0400_0000_0000_1c86;
    const HELIX: u64 = 0x0400_0000_0000_072c;

    fn shot(frame: u32) -> ShotEvent {
        ShotEvent {
            entity: BODY,
            instance: 11,
            graph: 0x0254,
            raw_state: 30,
            frame,
            time_ms: u64::from(frame) * 16,
            activation: 2,
            activation_ms: u64::from(frame) * 16,
            ordinal: 0,
        }
    }

    #[test]
    fn lease_windows_follow_body_slots_and_reserve_one_entity_per_frame() {
        let frame = LEASE_SPAN + 1234;
        let leases = ProjectileLeases::new(0, frame, [CANISTER, CANISTER, VISOR, HELIX]).unwrap();
        assert_eq!(leases.blocks().len(), 6);
        assert_eq!(
            leases.blocks()[4],
            LeaseBlock {
                definition: HELIX,
                start: LEASE_SPAN,
                stop: 2 * LEASE_SPAN,
                first_id: 0x0090_0000,
                per_frame: 1,
            }
        );
        assert_eq!(leases.entity(HELIX, frame, 0), Some(0xa090_04d2));
        assert_eq!(
            leases.entity(HELIX, frame + LEASE_SPAN, 0),
            Some(0xa090_04d2)
        );
        assert_eq!(leases.entity(HELIX, frame - LEASE_SPAN, 0), None);
        assert_eq!(leases.entity(HELIX, frame, 1), None);
        let next = ProjectileLeases::new(1, frame, [CANISTER, VISOR, HELIX]).unwrap();
        assert_eq!(next.entity(HELIX, frame, 0), Some(0xa190_04d2));
        assert!(ProjectileLeases::new(u8::MAX, frame, [HELIX]).is_err());
        assert!(ProjectileLeases::new(0, MAX_LEASE_FRAME + 1, [HELIX]).is_err());
        assert!(ProjectileLeases::new(0, MAX_LEASE_FRAME, [HELIX]).is_ok());
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 statescript catalog"]
    fn authored_soldier_leases_match_python_and_exclude_the_rifle() {
        let root = PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let graphs = Arc::new(GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap());
        let body = &graphs.bodies()[&0x0400_0000_0000_03cf];
        let mut component = Component::start(
            Arc::clone(&graphs),
            BODY,
            player_entity_variables(body, 200.0, 200.0),
            player_roots(body, &graphs).unwrap(),
            1000,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        component.advance_frame(1000, &mut EmptyWorld).unwrap();
        let leases = ProjectileLeases::from_authored(
            &graphs,
            component.snapshot().unwrap(),
            0,
            1000,
            |instance, raw, config| {
                component.evaluate_state_config(instance, raw, config, &mut EmptyWorld)
            },
        )
        .unwrap();
        assert_eq!(
            leases
                .blocks()
                .chunks_exact(2)
                .map(|windows| windows[0].definition)
                .collect::<Vec<_>>(),
            [CANISTER, VISOR, HELIX]
        );
        assert_eq!(leases.entity(0x0400_0000_0000_03d4, 1000, 0), None);
        assert_eq!(leases.entity(HELIX, 1234, 0), Some(0xa090_04d2));
    }

    #[test]
    fn only_a_delivered_owner_introduction_confirms_an_alias_and_release_waits_for_creates() {
        let frame = 1000;
        let shot = shot(frame);
        let leases = ProjectileLeases::new(0, frame, [CANISTER, VISOR, HELIX]).unwrap();
        let original = leases.entity(HELIX, frame, 0).unwrap();
        let alias = leases.entity(HELIX, frame + 4, 0).unwrap();
        let mut tracker = OwnerProjectiles::new(BODY, leases);
        let token = tracker.admit(shot, HELIX, 80).unwrap();
        assert!(tracker.admit(shot, HELIX, 80).is_none());
        let identity = |instance, state| {
            (instance == shot.instance && state == shot.raw_state)
                .then_some((shot.activation, shot.activation_ms))
        };
        tracker.owner_sent(2, frame + 2, identity, |_, _| true);
        tracker.owner_sent(3, frame + 4, identity, |_, _| true);
        tracker.owner_sent(4, frame + 5, identity, |_, _| false);
        assert!(tracker.advance(frame + 5).is_empty());
        tracker.owner_arrival(2, false);
        tracker.owner_arrival(3, true);
        tracker.owner_arrival(4, true);
        assert_eq!(
            tracker.advance(frame + 5),
            [
                ProjectileDelivery::Confirm {
                    token,
                    entity: original,
                    definition: HELIX
                },
                ProjectileDelivery::Confirm {
                    token,
                    entity: alias,
                    definition: HELIX
                },
            ]
        );
        tracker.flight_ended(token, 2, frame + 5);
        tracker.create_arrived(token, original);
        assert!(tracker.advance(frame + 12).is_empty());
        assert!(tracker.confirmation_current(token, alias));
        tracker.create_arrived(token, alias);
        let explosion_frame = Some(frame + 13 + RELEASE_AHEAD);
        assert_eq!(
            tracker.advance(frame + 13),
            [
                ProjectileDelivery::Destroy {
                    token,
                    entity: original,
                    explosion_frame
                },
                ProjectileDelivery::Destroy {
                    token,
                    entity: alias,
                    explosion_frame
                },
            ]
        );
        assert!(!tracker.confirmation_current(token, original));
        assert!(tracker.advance(frame + 14).is_empty());
    }

    #[test]
    fn lost_reports_fall_back_and_unacknowledged_confirmation_destroys_without_explosion() {
        let frame = 1000;
        let shot = shot(frame);
        let leases = ProjectileLeases::new(0, frame, [HELIX]).unwrap();
        let entity = leases.entity(HELIX, frame, 0).unwrap();
        let mut tracker = OwnerProjectiles::new(BODY, leases);
        let token = tracker.admit(shot, HELIX, 20).unwrap();
        tracker.owner_sent(
            2,
            frame + 1,
            |_, _| Some((shot.activation, shot.activation_ms)),
            |_, _| true,
        );
        tracker.owner_arrival(2, false);
        tracker.flight_ended(token, 2, frame + 2);
        assert!(tracker.advance(frame + UNREPORTED_TICKS - 1).is_empty());
        assert_eq!(
            tracker.advance(frame + UNREPORTED_TICKS),
            [ProjectileDelivery::Confirm {
                token,
                entity,
                definition: HELIX
            }]
        );
        assert!(tracker.advance(frame + 8 + DELIVERY_GRACE - 1).is_empty());
        assert_eq!(
            tracker.advance(frame + 8 + DELIVERY_GRACE),
            [ProjectileDelivery::Destroy {
                token,
                entity,
                explosion_frame: None
            }]
        );
        tracker.create_arrived(token, entity);
        assert!(!tracker.confirmation_current(token, entity));
        assert!(tracker.retire().is_empty());
    }
}
