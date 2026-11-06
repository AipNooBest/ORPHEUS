//! Project an initialized statescript body into the client baseline stream.
//!
//! The owner receives an H0 tree with all networked states off. An observer
//! receives the same tree and the initialized remote-synced states.

use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
};

use crate::bits::{BitError, BitWriter};
use crate::script_codecs::{Codec, CodecTable, PayloadError, PayloadMode, StatePayload};
use crate::script_expr::equal;
use crate::script_graph::{Graph, GraphStore, Node, asset_guid};
use crate::script_init::{Snapshot, StateKind};
use crate::script_stream::{BodyStream, ChangeKey, DeltaKind, ScriptChunk, StreamError};
use crate::script_wire::{self, Instance, Layout, Value, ValueEncoding, Variables, WireError};

const OWNER_HEALTH: u32 = 253;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum View {
    Owner,
    Observer,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectedSnapshot {
    entity: u32,
    view: View,
    entity_exclusions: BTreeSet<u32>,
    variables: Variables,
    instances: BTreeMap<u16, ProjectedInstance>,
}

impl ProjectedSnapshot {
    pub fn entity(&self) -> u32 {
        self.entity
    }

    pub fn view(&self) -> View {
        self.view
    }

    pub fn instance_count(&self) -> usize {
        self.instances.len()
    }
}

#[derive(Clone, Debug, PartialEq)]
struct ProjectedInstance {
    graph: u32,
    parent: Option<(u16, u16)>,
    variables: Variables,
    states: BTreeMap<u16, ProjectedState>,
    events: Vec<ProjectedEvent>,
}

#[derive(Clone, Debug, PartialEq)]
struct ProjectedState {
    active: bool,
    payload: StatePayload,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ProjectedEvent {
    due_ms: u64,
    raw_state: u16,
    finish: bool,
    parameter: u16,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StateSite {
    pub instance: u16,
    pub graph: u32,
    pub state: u16,
}

#[derive(Debug)]
pub enum SnapshotError {
    Incomplete {
        entity: u32,
        diagnostics: usize,
        first: String,
    },
    UnsupportedBindings {
        entity: u32,
        variables: usize,
    },
    MissingGraph {
        instance: u16,
        graph: u32,
    },
    MissingState(StateSite),
    MissingStateIndex {
        instance: u16,
        graph: u32,
        node: u16,
    },
    UnsupportedState {
        site: StateSite,
        class: String,
        kind: StateKind,
    },
    UnsupportedTimer {
        site: StateSite,
        class: String,
    },
    Payload {
        site: StateSite,
        source: PayloadError,
    },
    DifferentStream,
    ReboundInstance(u16),
    FrameTime,
    Wire(WireError),
    Stream(StreamError),
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Incomplete {
                entity,
                diagnostics,
                first,
            } => {
                write!(
                    f,
                    "body {entity:08X} has {diagnostics} initialization diagnostics, first {first}"
                )
            }
            Self::UnsupportedBindings { entity, variables } => {
                write!(
                    f,
                    "body {entity:08X} has {variables} variables with unsupported stack bindings"
                )
            }
            Self::MissingGraph { instance, graph } => {
                write!(f, "instance {instance} has no graph {graph:04X}")
            }
            Self::MissingState(site) => write!(f, "missing statescript state {site:?}"),
            Self::MissingStateIndex {
                instance,
                graph,
                node,
            } => {
                write!(
                    f,
                    "instance {instance} graph {graph:04X} remote node {node} has no state index"
                )
            }
            Self::UnsupportedState { site, class, kind } => {
                write!(f, "unsupported active state {class} ({kind:?}) at {site:?}")
            }
            Self::UnsupportedTimer { site, class } => {
                write!(f, "unsupported owner timer for {class} at {site:?}")
            }
            Self::Payload { site, source } => write!(f, "statescript state {site:?}: {source}"),
            Self::DifferentStream => f.write_str(
                "statescript baseline belongs to a different body, view or variable scope",
            ),
            Self::ReboundInstance(id) => {
                write!(f, "statescript instance {id} changed its definition")
            }
            Self::FrameTime => f.write_str("statescript frame time is outside the supported range"),
            Self::Wire(error) => error.fmt(f),
            Self::Stream(error) => error.fmt(f),
        }
    }
}

impl Error for SnapshotError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Payload { source, .. } => Some(source),
            Self::Wire(error) => Some(error),
            Self::Stream(error) => Some(error),
            _ => None,
        }
    }
}

impl From<WireError> for SnapshotError {
    fn from(error: WireError) -> Self {
        Self::Wire(error)
    }
}

impl From<BitError> for SnapshotError {
    fn from(error: BitError) -> Self {
        Self::Wire(error.into())
    }
}

impl From<StreamError> for SnapshotError {
    fn from(error: StreamError) -> Self {
        Self::Stream(error)
    }
}

pub struct SnapshotEncoder<'a> {
    graphs: &'a GraphStore,
    codecs: &'a CodecTable,
}

impl<'a> SnapshotEncoder<'a> {
    pub fn new(graphs: &'a GraphStore, codecs: &'a CodecTable) -> Self {
        Self { graphs, codecs }
    }

    pub fn project(
        &self,
        snapshot: &Snapshot,
        view: View,
    ) -> Result<ProjectedSnapshot, SnapshotError> {
        self.project_with_exclusions(snapshot, view, None)
    }

    /// A later projection keeps the initial graph tree's entity exclusions.
    /// Per-instance exclusions still follow each instance's own definition.
    pub fn project_next(
        &self,
        snapshot: &Snapshot,
        baseline: &ProjectedSnapshot,
    ) -> Result<ProjectedSnapshot, SnapshotError> {
        if snapshot.entity != baseline.entity {
            return Err(SnapshotError::DifferentStream);
        }
        self.project_with_exclusions(snapshot, baseline.view, Some(&baseline.entity_exclusions))
    }

    fn project_with_exclusions(
        &self,
        snapshot: &Snapshot,
        view: View,
        frozen: Option<&BTreeSet<u32>>,
    ) -> Result<ProjectedSnapshot, SnapshotError> {
        if !snapshot.initialization_complete() {
            return Err(SnapshotError::Incomplete {
                entity: snapshot.entity,
                diagnostics: snapshot.diagnostics.len(),
                first: format!("{:?}", snapshot.diagnostics.first()),
            });
        }
        if !snapshot.bindings.is_empty() {
            return Err(SnapshotError::UnsupportedBindings {
                entity: snapshot.entity,
                variables: snapshot.bindings.len(),
            });
        }
        let mut entity_exclusions = frozen
            .cloned()
            .unwrap_or_else(|| BTreeSet::from([OWNER_HEALTH]));
        let mut instances = BTreeMap::new();
        for (&id, instance) in &snapshot.instances {
            let graph = self.graph(id, instance.graph)?;
            let exclusions = server_only_variables(graph);
            let variables = replicated_variables(&instance.variables, &exclusions);
            if frozen.is_none() {
                entity_exclusions.extend(exclusions);
            }
            let mut states = BTreeMap::new();
            for (&raw, state) in &instance.states {
                let site = StateSite {
                    instance: id,
                    graph: graph.index,
                    state: raw,
                };
                let node = graph
                    .state(usize::from(raw))
                    .ok_or(SnapshotError::MissingState(site))?;
                if !node.is_networked()
                    || (view == View::Observer
                        && (presenter(node) || !graph.remote_positions().contains(&node.pos)))
                {
                    continue;
                }
                let codec = self.codecs.codec(&node.class);
                if !state.active && !(state.activation != 0 && codec.is_some_and(Codec::stec)) {
                    continue;
                }
                let payload = codec
                    .filter(|codec| codec.projected())
                    .and_then(|codec| state_payload(state.kind, codec))
                    .ok_or_else(|| SnapshotError::UnsupportedState {
                        site,
                        class: node.class.clone(),
                        kind: state.kind,
                    })?;
                states.insert(
                    raw,
                    ProjectedState {
                        active: state.active,
                        payload,
                    },
                );
            }
            instances.insert(
                id,
                ProjectedInstance {
                    graph: instance.graph,
                    parent: instance.parent,
                    variables,
                    states,
                    events: if view == View::Owner {
                        self.project_timers(snapshot, id, graph)?
                    } else {
                        Vec::new()
                    },
                },
            );
        }
        Ok(ProjectedSnapshot {
            entity: snapshot.entity,
            view,
            variables: replicated_variables(&snapshot.variables, &entity_exclusions),
            entity_exclusions,
            instances,
        })
    }

    /// The owner's first tree and the observer's first frame share the H0
    /// descriptor, variable, and remote slot writer.
    pub fn full(&self, snapshot: &ProjectedSnapshot) -> Result<BitWriter, SnapshotError> {
        let mut instances = Vec::with_capacity(snapshot.instances.len());
        for (&id, instance) in &snapshot.instances {
            let graph = self.graph(id, instance.graph)?;
            let mut wire = Instance::new(id, Layout::try_from(graph)?, instance.parent);
            wire.variables = instance.variables.clone();
            for (slot, (_, var)) in graph.presence_vars().enumerate() {
                if let Some(value) = var.0.and_then(|var| wire.variables.swap_remove(&var)) {
                    wire.presence.insert(slot, value);
                }
            }
            if snapshot.view == View::Observer {
                self.remote_states(id, graph, instance, &mut wire)?;
            }
            instances.push(wire);
        }
        Ok(script_wire::remote_full(&instances, &snapshot.variables)?)
    }

    /// Prepare the baseline for this body's delivery stream. The body create
    /// acknowledgement still controls when the resulting chunk may be sent.
    pub fn queue_full(
        &self,
        stream: &mut BodyStream<ProjectedSnapshot>,
        snapshot: &ProjectedSnapshot,
    ) -> Result<ScriptChunk, SnapshotError> {
        let payload = self.full(snapshot)?;
        let mut baseline = snapshot.clone();
        if baseline.view == View::Owner {
            for instance in baseline.instances.values_mut() {
                instance.states.clear();
                instance.events.clear();
            }
        }
        Ok(stream.full(&payload, baseline)?)
    }

    /// Encode a cumulative projection from the acknowledged baseline. The
    /// caller supplies the owner phase selected by the body's stream.
    pub fn queue_delta(
        &self,
        stream: &mut BodyStream<ProjectedSnapshot>,
        snapshot: &ProjectedSnapshot,
        kind: DeltaKind,
        frame_ms: u32,
    ) -> Result<Option<ScriptChunk>, SnapshotError> {
        let (_, base) = stream.base().ok_or(StreamError::BaselinePending)?;
        let view = match kind {
            DeltaKind::Observer { .. } => View::Observer,
            DeltaKind::InitialOwner(_) | DeltaKind::Correction(_) => View::Owner,
        };
        if base.entity != snapshot.entity
            || base.view != view
            || snapshot.view != view
            || base.entity_exclusions != snapshot.entity_exclusions
        {
            return Err(SnapshotError::DifferentStream);
        }
        let touched = stream.touched();
        let known: BTreeSet<_> = if view == View::Observer {
            base.instances
                .keys()
                .copied()
                .chain(
                    stream
                        .pending()
                        .flat_map(|(_, pending, _)| pending.instances.keys().copied()),
                )
                .collect()
        } else {
            BTreeSet::new()
        };
        let delta = self.differences(base, snapshot, &touched, &known)?;
        if view == View::Observer && delta.keys.is_empty() {
            return Ok(None);
        }
        let frame = self.delta_frame(snapshot, &delta, kind, frame_ms)?;
        Ok(Some(stream.delta(
            &frame,
            snapshot.clone(),
            delta.keys,
            kind,
        )?))
    }

    fn differences<'s>(
        &self,
        base: &ProjectedSnapshot,
        current: &'s ProjectedSnapshot,
        touched: &BTreeSet<ChangeKey>,
        known: &BTreeSet<u16>,
    ) -> Result<SnapshotDelta<'s>, SnapshotError> {
        let mut delta = SnapshotDelta::default();
        delta.variables = delta_variables(
            0,
            Some(&base.variables),
            &current.variables,
            touched,
            &mut delta.keys,
        );
        for (&id, next) in &current.instances {
            let before = base.instances.get(&id);
            if before
                .is_some_and(|before| before.graph != next.graph || before.parent != next.parent)
            {
                return Err(SnapshotError::ReboundInstance(id));
            }
            let descriptor = before.is_none();
            if descriptor {
                delta.keys.insert(ChangeKey::Instance(id));
            }
            let variables = delta_variables(
                id,
                before.map(|before| &before.variables),
                &next.variables,
                touched,
                &mut delta.keys,
            );
            let graph = self.graph(id, next.graph)?;
            let raw_slots: BTreeSet<_> = next
                .states
                .keys()
                .copied()
                .chain(
                    before
                        .into_iter()
                        .flat_map(|before| before.states.keys().copied()),
                )
                .chain(
                    touched
                        .range(ChangeKey::State(id, 0)..=ChangeKey::State(id, u16::MAX))
                        .filter_map(|key| match key {
                            ChangeKey::State(_, state) => Some(*state),
                            _ => None,
                        }),
                )
                .collect();
            let mut states = BTreeMap::new();
            for raw in raw_slots {
                let now = next.states.get(&raw);
                let was = before.and_then(|before| before.states.get(&raw));
                let key = ChangeKey::State(id, raw);
                if now == was && !touched.contains(&key) {
                    continue;
                }
                let site = StateSite {
                    instance: id,
                    graph: graph.index,
                    state: raw,
                };
                let node = graph
                    .state(usize::from(raw))
                    .ok_or(SnapshotError::MissingState(site))?;
                if now.is_none() && self.codecs.codec(&node.class).is_some_and(Codec::stec) {
                    continue;
                }
                states.insert(raw, now);
                delta.keys.insert(key);
            }
            if descriptor || !variables.is_empty() || !states.is_empty() {
                delta.instances.insert(
                    id,
                    InstanceDelta {
                        current: Some(next),
                        descriptor,
                        variables,
                        states,
                    },
                );
            }
        }
        let previous_ids: BTreeSet<_> = base
            .instances
            .keys()
            .copied()
            .chain(
                touched
                    .range(ChangeKey::Instance(0)..=ChangeKey::Instance(u16::MAX))
                    .filter_map(|key| match key {
                        ChangeKey::Instance(id) => Some(*id),
                        _ => None,
                    }),
            )
            .collect();
        for id in previous_ids {
            if !current.instances.contains_key(&id)
                && (current.view == View::Owner || known.contains(&id))
            {
                delta.instances.insert(id, InstanceDelta::default());
                delta.keys.insert(ChangeKey::Instance(id));
            }
        }
        Ok(delta)
    }

    fn delta_frame(
        &self,
        current: &ProjectedSnapshot,
        delta: &SnapshotDelta<'_>,
        kind: DeltaKind,
        frame_ms: u32,
    ) -> Result<BitWriter, SnapshotError> {
        let (cmfd, correction, mode) = match kind {
            DeltaKind::Observer { .. } => (None, false, PayloadMode::Remote { full: false }),
            DeltaKind::InitialOwner(frame) | DeltaKind::Correction(frame) => {
                if frame == 0 {
                    return Err(WireError::ZeroCommandFrame.into());
                }
                let frame_time = i64::from(frame)
                    .checked_mul(i64::from(frame_ms))
                    .ok_or(SnapshotError::FrameTime)?;
                (
                    Some(frame),
                    matches!(kind, DeltaKind::Correction(_)),
                    PayloadMode::Owner {
                        full: false,
                        frame_time,
                        frame_ms,
                    },
                )
            }
        };
        let mut body = BitWriter::new();
        let mut last = 0;
        for (&id, instance) in &delta.instances {
            body.w_u16(id - last);
            last = id;
            body.bit(instance.current.is_none());
            if let Some(current) = instance.current {
                body.bit(instance.descriptor);
                if instance.descriptor {
                    let graph = u16::try_from(current.graph)
                        .map_err(|_| WireError::GraphIndex(current.graph))?;
                    script_wire::write_descriptor(&mut body, graph, current.parent)?;
                }
            }
        }
        body.w_u16(0);
        script_wire::variable_list(&mut body, &delta.variables, ValueEncoding::Compact)?;
        for (&id, instance) in &delta.instances {
            let Some(current) = instance.current else {
                continue;
            };
            let graph = self.graph(id, current.graph)?;
            write_delta_variables(&mut body, graph, &instance.variables)?;
            self.delta_states(&mut body, id, graph, &instance.states, mode)?;
        }
        if let PayloadMode::Owner { frame_time, .. } = mode {
            let frame_time = u64::try_from(frame_time).map_err(|_| SnapshotError::FrameTime)?;
            self.owner_events(&mut body, current, frame_time)?;
        }
        let mut frame = BitWriter::new();
        frame.bit(false);
        frame.bit(cmfd.is_some());
        if let Some(cmfd) = cmfd {
            frame.bit(correction);
            frame.w_var(cmfd);
            frame.w_var(u32::try_from(body.bit_len()).map_err(|_| WireError::ChunkRange)?);
        }
        frame.append(&body);
        if cmfd.is_some() {
            frame.bit(false);
        }
        frame.bits(0, 2)?;
        Ok(frame)
    }

    fn owner_events(
        &self,
        out: &mut BitWriter,
        current: &ProjectedSnapshot,
        frame_time: u64,
    ) -> Result<(), SnapshotError> {
        // Even an empty event list replaces that instance's former queue.
        out.bit(!current.instances.is_empty());
        if current.instances.is_empty() {
            return Ok(());
        }
        let mut last_id = 0;
        for (&id, instance) in &current.instances {
            out.bit(false);
            out.w_u16(id - last_id);
            last_id = id;
            let graph = self.graph(id, instance.graph)?;
            let mut events = instance.events.iter().collect::<Vec<_>>();
            events.sort_by_key(|event| event.due_ms);
            let mut last_time = frame_time;
            for event in events {
                if event.finish {
                    out.bits(2, 2)?;
                } else if event.parameter == 1 {
                    out.bits(0, 2)?;
                } else {
                    out.bits(1, 2)?;
                    out.bit(event.parameter == 0);
                    if event.parameter != 0 {
                        out.w_u16(event.parameter);
                    }
                }
                out.bits(
                    u64::from(event.raw_state),
                    usize::from(graph.header.states_bits),
                )?;
                out.bit(event.due_ms == last_time);
                if event.due_ms != last_time {
                    out.bit(event.due_ms > last_time);
                    let elapsed = u32::try_from(event.due_ms.abs_diff(last_time))
                        .map_err(|_| SnapshotError::FrameTime)?;
                    out.w_var(elapsed);
                }
                last_time = event.due_ms;
            }
            out.bits(3, 2)?;
        }
        out.bit(false);
        out.w_u16(0);
        Ok(())
    }

    fn delta_states(
        &self,
        out: &mut BitWriter,
        id: u16,
        graph: &Graph,
        changed: &BTreeMap<u16, Option<&ProjectedState>>,
        mode: PayloadMode,
    ) -> Result<(), SnapshotError> {
        let owner = matches!(mode, PayloadMode::Owner { .. });
        let mut slots = BTreeMap::new();
        if owner {
            for (&raw, &state) in changed {
                let site = StateSite {
                    instance: id,
                    graph: graph.index,
                    state: raw,
                };
                let node = graph
                    .state(usize::from(raw))
                    .filter(|node| node.is_networked())
                    .ok_or(SnapshotError::MissingState(site))?;
                slots.insert(usize::from(raw), (site, node, state));
            }
        } else {
            for index in 0..graph.remote_nodes.len() {
                if graph.remote_slot(index).is_none() {
                    continue;
                }
                let node = graph
                    .remote_node(index)
                    .expect("compiled remote slot references node");
                let raw = node.state.ok_or(SnapshotError::MissingStateIndex {
                    instance: id,
                    graph: graph.index,
                    node: node.pos,
                })?;
                if let Some(&state) = changed.get(&raw) {
                    let site = StateSite {
                        instance: id,
                        graph: graph.index,
                        state: raw,
                    };
                    slots.insert(index, (site, node, state));
                }
            }
        }
        let width = if owner {
            graph.states.len()
        } else {
            graph.remote_nodes.len()
        };
        write_mask(out, width, &slots.keys().copied().collect());
        for (site, node, state) in slots.into_values() {
            let Some(state) = state else {
                out.bit(false);
                continue;
            };
            let codec = self
                .codecs
                .codec(&node.class)
                .ok_or_else(|| SnapshotError::Payload {
                    site,
                    source: PayloadError::Unverified(node.class.clone()),
                })?;
            if codec.stec() {
                out.bit(true);
            }
            out.bit(state.active);
            if state.active || codec.stec() {
                let payload = codec
                    .payload(&state.payload, mode)
                    .map_err(|source| SnapshotError::Payload { site, source })?;
                out.append(&payload);
            }
        }
        Ok(())
    }

    fn graph(&self, instance: u16, graph: u32) -> Result<&Graph, SnapshotError> {
        self.graphs
            .graph(graph)
            .ok_or(SnapshotError::MissingGraph { instance, graph })
    }

    fn project_timers(
        &self,
        snapshot: &Snapshot,
        id: u16,
        graph: &Graph,
    ) -> Result<Vec<ProjectedEvent>, SnapshotError> {
        let Some(timers) = snapshot.pending_timers.get(&id) else {
            return Ok(Vec::new());
        };
        let mut events = Vec::with_capacity(timers.len());
        for timer in timers {
            let site = StateSite {
                instance: id,
                graph: graph.index,
                state: timer.raw_state,
            };
            let node = graph
                .state(usize::from(timer.raw_state))
                .ok_or(SnapshotError::MissingState(site))?;
            if !node.is_networked() {
                continue;
            }
            if !self.codecs.sendable(&node.class, true) {
                return Err(SnapshotError::UnsupportedTimer {
                    site,
                    class: node.class.clone(),
                });
            }
            events.push(ProjectedEvent {
                due_ms: timer.due_ms,
                raw_state: timer.raw_state,
                finish: timer.finish,
                parameter: timer.parameter,
            });
        }
        Ok(events)
    }

    fn remote_states(
        &self,
        id: u16,
        graph: &Graph,
        instance: &ProjectedInstance,
        wire: &mut Instance,
    ) -> Result<(), SnapshotError> {
        for (slot, &position) in graph.remote_positions().iter().enumerate() {
            let node = graph
                .node(position)
                .expect("compiled remote positions reference nodes");
            let raw = node.state.ok_or(SnapshotError::MissingStateIndex {
                instance: id,
                graph: graph.index,
                node: position,
            })?;
            let Some(state) = instance.states.get(&raw) else {
                continue;
            };
            let site = StateSite {
                instance: id,
                graph: graph.index,
                state: raw,
            };
            let codec = self
                .codecs
                .codec(&node.class)
                .ok_or_else(|| SnapshotError::Payload {
                    site,
                    source: PayloadError::Unverified(node.class.clone()),
                })?;
            let mut bits = BitWriter::new();
            bits.bit(true); // X: present in this full frame
            if codec.stec() {
                bits.bit(true); // Y: this StEC entry has a payload
            }
            bits.bit(state.active);
            let payload = codec
                .payload(&state.payload, PayloadMode::Remote { full: true })
                .map_err(|source| SnapshotError::Payload { site, source })?;
            bits.append(&payload);
            wire.states.insert(slot, bits);
        }
        Ok(())
    }
}

#[derive(Default)]
struct SnapshotDelta<'a> {
    variables: Variables,
    instances: BTreeMap<u16, InstanceDelta<'a>>,
    keys: BTreeSet<ChangeKey>,
}

#[derive(Default)]
struct InstanceDelta<'a> {
    current: Option<&'a ProjectedInstance>,
    descriptor: bool,
    variables: Variables,
    states: BTreeMap<u16, Option<&'a ProjectedState>>,
}

fn delta_variables(
    instance: u16,
    before: Option<&Variables>,
    current: &Variables,
    touched: &BTreeSet<ChangeKey>,
    changes: &mut BTreeSet<ChangeKey>,
) -> Variables {
    let ids: BTreeSet<_> = current
        .keys()
        .copied()
        .chain(before.into_iter().flat_map(|vars| vars.keys().copied()))
        .chain(
            touched
                .range(ChangeKey::Variable(instance, 0)..=ChangeKey::Variable(instance, u32::MAX))
                .filter_map(|key| match key {
                    ChangeKey::Variable(_, id) => Some(*id),
                    _ => None,
                }),
        )
        .collect();
    let mut result = Variables::new();
    for id in ids {
        let now = current.get(&id).unwrap_or(&Value::Null);
        let was = before
            .and_then(|vars| vars.get(&id))
            .unwrap_or(&Value::Null);
        let key = ChangeKey::Variable(instance, id);
        if touched.contains(&key) || !equal(now, was) {
            result.insert(id, now.clone());
            changes.insert(key);
        }
    }
    result
}

fn write_mask(out: &mut BitWriter, width: usize, chosen: &BTreeSet<usize>) {
    if width <= 8 {
        for slot in 0..width {
            out.bit(chosen.contains(&slot));
        }
        return;
    }
    let groups: Vec<_> = (0..width)
        .step_by(8)
        .map(|start| {
            let range = start..(start + 8).min(width);
            let present = chosen.range(range.clone()).next().is_some();
            out.bit(present);
            (range, present)
        })
        .collect();
    for (slots, present) in groups {
        if present {
            for slot in slots {
                out.bit(chosen.contains(&slot));
            }
        }
    }
}

fn write_delta_variables(
    out: &mut BitWriter,
    graph: &Graph,
    vars: &Variables,
) -> Result<(), WireError> {
    let mut sync = BTreeMap::new();
    let mut extra = vars.clone();
    for (slot, var) in graph.sync_vars.iter().enumerate() {
        if let Some(value) = var
            .0
            .filter(|_| var.1 == 0)
            .and_then(|id| extra.swap_remove(&id))
        {
            sync.insert(slot, value);
        }
    }
    write_mask(out, graph.sync_vars.len(), &sync.keys().copied().collect());
    for value in sync.values() {
        script_wire::variable(out, value, ValueEncoding::Compact)?;
    }
    script_wire::variable_list(out, &extra, ValueEncoding::Compact)
}

fn server_only_variables(graph: &Graph) -> BTreeSet<u32> {
    graph
        .fields
        .get("m_9CEC6985")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(asset_guid)
        .map(|guid| u32::from(guid as u16))
        .collect()
}

fn replicated_variables(values: &Variables, excluded: &BTreeSet<u32>) -> Variables {
    values
        .iter()
        .filter(|(id, value)| !excluded.contains(*id) && !matches!(*value, Value::Null))
        .map(|(&id, value)| (id, value.clone()))
        .collect()
}

fn presenter(node: &Node) -> bool {
    node.class == "STUStatescriptStateUXPresenter" || node.fields.get("m_45216F79").is_some()
}

fn state_payload(kind: StateKind, codec: &Codec) -> Option<StatePayload> {
    Some(match (kind, codec.codec.as_deref()) {
        (StateKind::Plain | StateKind::Wait | StateKind::AiCandidate { .. }, Some("none"))
            if codec.empty() =>
        {
            StatePayload::new()
        }
        (StateKind::BooleanSwitch(current), Some("switch")) => {
            StatePayload::from([("current".into(), Value::Bool(current))])
        }
        (StateKind::SubScript { child }, Some("subscript")) => StatePayload::from([(
            "child".into(),
            Value::Integer(i64::from(child.unwrap_or_default())),
        )]),
        (StateKind::MessageIdle, Some("message")) => StatePayload::new(),
        (StateKind::WaitFrames { target }, Some("frames")) => {
            StatePayload::from([("target".into(), Value::Integer(i64::from(target)))])
        }
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::script_init::{
        Diagnostic, EmptyWorld, Issue, MAX_INIT_WORK, Root, Site, initialize,
    };
    use serde_json::{Value as Json, json};

    const ENTITY: u32 = 0xa000_0101;
    const CODECS: &str = r#"{"classes":{
        "12340001":{"name":"STUStatescriptState","codec":"none","stec":false,"status":"VERIFIED","layout":[]},
        "12340002":{"name":"STUStatescriptStateBooleanSwitch","codec":"switch","stec":false,"status":"VERIFIED",
            "layout":[{"op":"bits","f":"current","n":1}]},
        "12340003":{"name":"STU_37D754C8","codec":"subscript","stec":false,"status":"VERIFIED",
            "layout":[{"op":"if","c":"H","then":[{"op":"w_u16","f":"child"},{"op":"bits","f":"flag","n":1}],"else":[]}]},
        "12340004":{"name":"STUStatescriptStateGameMessageEntry","codec":"message","stec":false,"status":"VERIFIED",
            "layout":[{"op":"if","c":"H","then":[{"op":"bits","f":"has_sender","n":1}],"else":[]},
                      {"op":"bits","f":"stacked","n":1}]},
        "12340005":{"name":"STUStatescriptStateWait","codec":"none","stec":false,"status":"VERIFIED","layout":[]},
        "D74D4F47":{"name":"STU_D74D4F47","codec":"frames","stec":false,"status":"VERIFIED",
            "layout":[{"op":"bits","f":"target","n":32}]}
    }}"#;

    fn node(position: u16, class: &str, state: Option<u16>, mut fields: Json) -> Json {
        fields["m_2BBEEAB8"] = json!(state.is_some());
        fields["m_ADEB6E05"] = json!(false);
        json!({
            "pos":position, "class":class, "uid":position, "rid":position,
            "remote":-1, "state":state, "client_only":false, "server_only":false,
            "fields":fields,
        })
    }

    fn graph(
        index: u32,
        nodes: Vec<Json>,
        states: Json,
        entries: Json,
        remote: Json,
        sync: Json,
    ) -> Json {
        json!({
            "index":index, "items":nodes.len(), "nodes":nodes, "states":states,
            "entries":entries, "remote":remote, "sync_vars":sync, "containers":{},
            "unknown_nodes":[], "fields":{"m_9CEC6985":["0x0D800000000001A4"]},
            "header":{
                "m_nodesBitCount":4, "m_statesBitCount":4,
                "m_remoteSyncNodesBitCount":4, "m_syncVarsBitCount":4
            }
        })
    }

    fn fixture() -> (GraphStore, Snapshot) {
        let root = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":{"$":"Plug","links":[[1,"m_beginPlug"],[2,"m_beginPlug"],
                            [3,"m_beginPlug"],[4,"m_beginPlug"]],"m_FF3DAF1E":[]}
                    }),
                ),
                node(
                    1,
                    "STUStatescriptStateBooleanSwitch",
                    Some(0),
                    json!({
                        "m_condition":{"$":"STUConfigVarBool","m_value":true}
                    }),
                ),
                node(
                    2,
                    "STU_37D754C8",
                    Some(1),
                    json!({
                        "m_36F7B64C":{"m_36F7B64C":{"m_C974E369":{"m_graph":"0x058000000000002B"}}}
                    }),
                ),
                node(
                    3,
                    "STUStatescriptState",
                    Some(2),
                    json!({"m_45216F79":null}),
                ),
                node(4, "STUStatescriptStateGameMessageEntry", Some(3), json!({})),
            ],
            json!([1, 2, 3, 4]),
            json!([0]),
            json!([null, 3, 1, 2, 4, 1]),
            json!([[100, 0, 0], [100, 0, 0], [200, 1, 0], [null, 0, 0]]),
        );
        let child = graph(43, vec![], json!([]), json!([]), json!([]), json!([]));
        let graphs = GraphStore::from_json(
            &serde_json::to_vec(&json!({
                "version":1, "graphs":[root,child], "bodies":{}, "missing":[]
            }))
            .unwrap(),
        )
        .unwrap();
        let entity = Variables::from([
            (7, Value::Asset(0x0580_0000_0000_0042)),
            (55, Value::Integer(-12)),
            (253, Value::Float(200.0)),
            (420, Value::Integer(666)),
        ]);
        let mut root = Root::new(1, 42);
        root.variables.extend([
            (100, Value::Integer(17)),
            (200, Value::Bool(true)),
            (300, Value::Float(3.5)),
            (400, Value::Null),
            (420, Value::Integer(2)),
        ]);
        let snapshot = initialize(
            &graphs,
            ENTITY,
            entity,
            vec![root],
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        (graphs, snapshot)
    }

    fn check(bits: &BitWriter, length: usize, hex: &str) {
        let actual: String = bits
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!((bits.bit_len(), actual.as_str()), (length, hex));
    }

    #[test]
    fn baseline_matches_python_for_both_views_and_enters_the_acknowledged_stream() {
        let (graphs, snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let owner = encoder.project(&snapshot, View::Owner).unwrap();
        let observer = encoder.project(&snapshot, View::Observer).unwrap();
        assert_eq!(owner.entity(), ENTITY);
        assert_eq!(owner.instance_count(), 2);
        assert_eq!(owner.instances[&2].parent, Some((1, 1)));
        assert_eq!(owner.variables.keys().copied().collect::<Vec<_>>(), [7, 55]);
        assert_eq!(
            owner.instances[&1]
                .variables
                .keys()
                .copied()
                .collect::<Vec<_>>(),
            [100, 200, 300]
        );
        assert!(owner.instances[&1].states.contains_key(&2));
        assert!(!observer.instances[&1].states.contains_key(&2));

        check(
            &encoder.full(&owner).unwrap(),
            250,
            "081500102b00142000e820031610e246e8012811840cc5128000006040000000",
        );
        check(
            &encoder.full(&observer).unwrap(),
            257,
            "081500102b00142000e820031610e246e8012811840cc512800000604000f83b00",
        );
        let mut stream = BodyStream::new(ENTITY);
        let chunk = encoder.queue_full(&mut stream, &owner).unwrap();
        assert_eq!(chunk.update.entity, ENTITY);
        assert!(chunk.resend);
        assert!(!stream.ready_to_send(chunk.last));
        assert!(stream.base().is_none());
        check(
            chunk.update.chunk.as_ref().unwrap(),
            286,
            "000004a08f500100b102400102800e326001216e841e801241c8502c0108000006040000",
        );
        stream.create_arrived();
        assert!(stream.ready_to_send(chunk.last));
        assert!(stream.arrived(chunk.last).advanced);
        let (_, base) = stream.base().unwrap();
        assert_eq!(base.view(), View::Owner);
        assert!(base.instances.values().all(|item| item.states.is_empty()));
        assert!(!owner.instances[&1].states.is_empty());
    }

    #[test]
    fn unsupported_active_states_and_initialization_diagnostics_are_explicit() {
        let (graphs, mut snapshot) = fixture();
        let mut raw: Json = serde_json::from_str(CODECS).unwrap();
        raw["classes"].as_object_mut().unwrap().remove("12340001");
        let codecs = CodecTable::from_json(&raw.to_string()).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        assert!(matches!(
            encoder.project(&snapshot, View::Owner),
            Err(SnapshotError::UnsupportedState {
                site: StateSite {
                    instance: 1,
                    graph: 42,
                    state: 2
                },
                ..
            })
        ));
        assert!(encoder.project(&snapshot, View::Observer).is_ok());
        snapshot.diagnostics.push(Diagnostic {
            site: Site {
                entity: ENTITY,
                instance: 1,
                graph: 42,
                node: None,
            },
            issue: Issue::UnsupportedAction("fixture".into()),
        });
        assert!(matches!(
            encoder.project(&snapshot, View::Observer),
            Err(SnapshotError::Incomplete { entity: ENTITY, diagnostics: 1, first })
                if first.contains("fixture")
        ));
    }

    #[test]
    fn stack_bindings_block_initial_and_later_projection_for_both_views() {
        let (graphs, mut snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let baseline = encoder.project(&snapshot, View::Owner).unwrap();
        snapshot.bindings.insert(
            (0, 55),
            vec![crate::script_init::StackBinding {
                instance: 1,
                raw_state: 2,
            }],
        );
        for view in [View::Owner, View::Observer] {
            assert!(matches!(
                encoder.project(&snapshot, view),
                Err(SnapshotError::UnsupportedBindings {
                    entity: ENTITY,
                    variables: 1
                })
            ));
        }
        assert!(matches!(
            encoder.project_next(&snapshot, &baseline),
            Err(SnapshotError::UnsupportedBindings {
                entity: ENTITY,
                variables: 1
            })
        ));
    }

    fn after_body_change(mut snapshot: ProjectedSnapshot) -> ProjectedSnapshot {
        snapshot.variables.swap_remove(&55);
        let root = snapshot.instances.get_mut(&1).unwrap();
        root.variables.insert(100, Value::Integer(99));
        root.variables.swap_remove(&200);
        root.states
            .get_mut(&0)
            .unwrap()
            .payload
            .insert("current".into(), Value::Bool(false));
        root.states.remove(&1);
        root.states.remove(&2);
        snapshot.instances.remove(&2);
        snapshot
    }

    #[test]
    fn initial_owner_and_corrections_match_python_bytes_in_both_views() {
        let (graphs, snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let owner = encoder.project(&snapshot, View::Owner).unwrap();
        let mut stream = BodyStream::new(ENTITY);
        let baseline = encoder.queue_full(&mut stream, &owner).unwrap();
        stream.create_arrived();
        stream.arrived(baseline.last);
        let initial = encoder
            .queue_delta(&mut stream, &owner, DeltaKind::InitialOwner(1000), 16)
            .unwrap()
            .unwrap();
        check(
            initial.update.chunk.as_ref().unwrap(),
            127,
            "040000b0a5d007e80800f02726313100",
        );
        stream.sent(initial.last);
        stream.arrived(initial.last);
        let owner = after_body_change(owner);
        let correction = encoder
            .queue_delta(&mut stream, &owner, DeltaKind::Correction(1016), 16)
            .unwrap()
            .unwrap();
        check(
            correction.update.chunk.as_ref().unwrap(),
            181,
            "06000010e9f007c00944d0cd0f80903121ff00b8480c00",
        );
        assert!(stream.touched().contains(&ChangeKey::Instance(2)));
        assert!(stream.touched().contains(&ChangeKey::Variable(1, 200)));

        let observer = encoder.project(&snapshot, View::Observer).unwrap();
        let mut stream = BodyStream::new(ENTITY);
        let baseline = encoder.queue_full(&mut stream, &observer).unwrap();
        stream.create_arrived();
        stream.arrived(baseline.last);
        let kind = DeltaKind::Observer { resend: false };
        assert!(
            encoder
                .queue_delta(&mut stream, &observer, kind, 16)
                .unwrap()
                .is_none()
        );
        assert!(matches!(
            encoder.queue_delta(&mut stream, &owner, kind, 16),
            Err(SnapshotError::DifferentStream)
        ));
        let observer = after_body_change(observer);
        let delta = encoder
            .queue_delta(&mut stream, &observer, kind, 16)
            .unwrap()
            .unwrap();
        check(
            delta.update.chunk.as_ref().unwrap(),
            140,
            "04000080864004ddfc00081913f20f003601",
        );
    }

    #[test]
    fn observer_delta_removes_an_instance_introduced_by_an_unacknowledged_chunk() {
        let (graphs, snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let observer = encoder.project(&snapshot, View::Observer).unwrap();
        let mut stream = BodyStream::new(ENTITY);
        let baseline = encoder.queue_full(&mut stream, &observer).unwrap();
        stream.create_arrived();
        stream.arrived(baseline.last);
        let mut temporary = observer.clone();
        temporary
            .instances
            .insert(3, temporary.instances[&2].clone());
        let kind = DeltaKind::Observer { resend: false };
        let created = encoder
            .queue_delta(&mut stream, &temporary, kind, 16)
            .unwrap()
            .unwrap();
        check(
            created.update.chunk.as_ref().unwrap(),
            92,
            "040000808371058002040000",
        );
        stream.sent(created.last);
        stream.lost(created.last);
        let removed = encoder
            .queue_delta(&mut stream, &observer, kind, 16)
            .unwrap()
            .unwrap();
        check(removed.update.chunk.as_ref().unwrap(), 52, "04000400810900");
        stream.arrived(removed.last);
        assert!(
            encoder
                .queue_delta(&mut stream, &observer, kind, 16)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn owner_timer_replacement_preserves_signed_time_steps_and_fifo_ties() {
        let (graphs, snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let mut owner = encoder.project(&snapshot, View::Owner).unwrap();
        owner.instances.get_mut(&1).unwrap().states.clear();
        let mut stream = BodyStream::new(ENTITY);
        let baseline = encoder.queue_full(&mut stream, &owner).unwrap();
        stream.create_arrived();
        stream.arrived(baseline.last);
        owner.instances.get_mut(&1).unwrap().events = vec![
            ProjectedEvent {
                due_ms: 16000,
                raw_state: 0,
                finish: false,
                parameter: 1,
            },
            ProjectedEvent {
                due_ms: 16004,
                raw_state: 1,
                finish: false,
                parameter: 0,
            },
            ProjectedEvent {
                due_ms: 16004,
                raw_state: 2,
                finish: true,
                parameter: 1,
            },
            ProjectedEvent {
                due_ms: 15992,
                raw_state: 3,
                finish: false,
                parameter: 7,
            },
        ];
        let delta = encoder
            .queue_delta(&mut stream, &owner, DeltaKind::InitialOwner(1000), 16)
            .unwrap()
            .unwrap();
        check(
            delta.update.chunk.as_ref().unwrap(),
            163,
            "040000f0a7d007780189b801022084860894130300",
        );
    }

    #[test]
    fn larger_masks_preserve_group_presence_before_slot_bits() {
        let mut bits = BitWriter::new();
        write_mask(&mut bits, 22, &BTreeSet::from([0, 8, 17]));
        check(&bits, 25, "0f081000");
    }

    #[test]
    fn later_snapshots_keep_initial_entity_scope_exclusions_after_graph_removal() {
        let (graphs, mut snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let initial = encoder.project(&snapshot, View::Observer).unwrap();
        snapshot.instances.clear();
        let next = encoder.project_next(&snapshot, &initial).unwrap();
        assert!(!next.variables.contains_key(&420));
        assert!(!next.variables.contains_key(&OWNER_HEALTH));
        assert!(
            encoder
                .project(&snapshot, View::Observer)
                .unwrap()
                .variables
                .contains_key(&420)
        );
    }

    #[test]
    fn initialized_waits_supply_owner_timers_and_remote_frame_targets() {
        let timed = graph(
            44,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":{"$":"Plug","links":[[1,"m_beginPlug"],[2,"m_beginPlug"]],
                            "m_FF3DAF1E":[]}
                    }),
                ),
                node(
                    1,
                    "STUStatescriptStateWait",
                    Some(0),
                    json!({
                        "m_timeout":{"$":"STUConfigVarFloat","m_value":1.0}
                    }),
                ),
                node(
                    2,
                    "STU_D74D4F47",
                    Some(1),
                    json!({
                        "m_3016B9A1":{"$":"STUConfigVarInt","m_value":3}
                    }),
                ),
            ],
            json!([1, 2]),
            json!([0]),
            json!([1, 2]),
            json!([]),
        );
        let graphs = GraphStore::from_json(
            &serde_json::to_vec(&json!({
                "version":1, "graphs":[timed], "bodies":{}, "missing":[]
            }))
            .unwrap(),
        )
        .unwrap();
        let snapshot = initialize(
            &graphs,
            ENTITY,
            Variables::new(),
            vec![Root::new(1, 44)],
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        assert_eq!(snapshot.pending_timers[&1].len(), 1);
        assert_eq!(snapshot.pending_timers[&1][0].due_ms, 1000);
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let owner = encoder.project(&snapshot, View::Owner).unwrap();
        let observer = encoder.project(&snapshot, View::Observer).unwrap();
        assert!(observer.instances[&1].events.is_empty());
        check(&encoder.full(&observer).unwrap(), 72, "08160000fc00000000");
        let mut stream = BodyStream::new(ENTITY);
        let baseline = encoder.queue_full(&mut stream, &owner).unwrap();
        stream.create_arrived();
        stream.arrived(baseline.last);
        assert!(stream.base().unwrap().1.instances[&1].events.is_empty());
        let update = encoder
            .queue_delta(&mut stream, &owner, DeltaKind::InitialOwner(1), 16)
            .unwrap()
            .unwrap();
        check(
            update.update.chunk.as_ref().unwrap(),
            151,
            "040000302701b604801f0000004800863d3000",
        );
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 graph and codec tables"]
    fn actual_shared_ui_graph_baselines_match_python() {
        use sha2::{Digest, Sha256};
        use std::path::{Path, PathBuf};

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let graphs = GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap();
        let codecs = CodecTable::load(root.join("data/statescript_codecs_174.json")).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        for (graph, expected_owner, expected_observer, delta_bits, expected_delta) in [
            (
                0x20e4,
                "8b5be8f21a99210445094b65c6ba62621a489da48170c97ef9062a99127ec0f6",
                "7e3658bf64c37a631561b21b33fff4f68119e038896d153ee84d7f4cb6e29649",
                73,
                "3fee631d2eede1aa07fd6b2505734ac71f1f6c0e015524a62d04419db413a3ee",
            ),
            (
                0x0cb6,
                "6de347e8a7097efa974f5fc61cce965c69998e849c11c11a3c96165dd8408f48",
                "e62bc520a0acf2f9064de7de9b053ba46c3861c4b4c481f606e357560eca6eea",
                76,
                "3a927d71037d9d56bb2e3d0ef1a532f252df632e590875823dca787120dd5153",
            ),
        ] {
            let snapshot = initialize(
                &graphs,
                ENTITY,
                Variables::new(),
                vec![Root::new(1, graph)],
                &mut EmptyWorld,
                MAX_INIT_WORK,
            )
            .unwrap();
            assert!(
                snapshot.initialization_complete(),
                "{:?}",
                snapshot.diagnostics
            );
            for (view, expected_bits, expected) in [
                (View::Owner, 37, expected_owner),
                (View::Observer, 38, expected_observer),
            ] {
                let projection = encoder.project(&snapshot, view).unwrap();
                let frame = encoder.full(&projection).unwrap();
                assert_eq!(frame.bit_len(), expected_bits);
                assert_eq!(format!("{:x}", Sha256::digest(frame.as_bytes())), expected);
            }
            let owner = encoder.project(&snapshot, View::Owner).unwrap();
            let mut stream = BodyStream::new(ENTITY);
            let baseline = encoder.queue_full(&mut stream, &owner).unwrap();
            stream.create_arrived();
            stream.arrived(baseline.last);
            let (_, base) = stream.base().unwrap();
            let delta = encoder
                .differences(base, &owner, &BTreeSet::new(), &BTreeSet::new())
                .unwrap();
            let frame = encoder
                .delta_frame(&owner, &delta, DeltaKind::InitialOwner(1000), 16)
                .unwrap();
            assert_eq!(frame.bit_len(), delta_bits);
            assert_eq!(
                format!("{:x}", Sha256::digest(frame.as_bytes())),
                expected_delta
            );
        }
    }
}
