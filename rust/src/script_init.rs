//! Initialization and supported frame events of authored statescript graphs.
//!
//! This covers Entry, typed variable writes, conditional links and static
//! SubScript instances. Diagnostics make a partial snapshot unsuitable for
//! gameplay replication. The component also advances static waits and dispatches
//! scoped message entries. Input, combat and other authored actions still need
//! their corresponding runtime.

use std::{
    collections::{BTreeMap, HashSet},
    error::Error,
    fmt,
    sync::Arc,
};

use indexmap::IndexMap;
use serde_json::Value as Json;

use crate::{
    commands::Command,
    mover_modifiers::{Modifier, ModifierKey, Operation},
    script_expr::{
        self, Context, EmptyContext, EvalError, Scope, StackPriority, Variable, equal, truthy,
    },
    script_graph::{Body, Graph, GraphStore, Node, asset_guid},
    script_runtime::{ButtonEdge, FRAME_MS, QueueFull, Timeline, button_edges, frame_start},
    script_wire::{Value, Variables},
};

pub const MAX_INIT_WORK: usize = 100_000;
const MAX_PLUG_DEPTH: usize = 64;
const MAX_EVENTS_PER_UPDATE: usize = 1000;
const MAX_PENDING_EVENTS: usize = 16_384;
const ABILITY_RUNNING: u8 = 1;
const ABILITY_HELD: u8 = 4;
const ABORT_PLUG: &str = "m_F198FD3A";
const SUBGRAPH_STATES: &str = "m_FF3DAF1E";

#[derive(Clone, Debug)]
pub struct Root {
    pub id: u16,
    pub graph: u32,
    pub variables: Variables,
}

impl Root {
    pub fn new(id: u16, graph: u32) -> Self {
        Self {
            id,
            graph,
            variables: Variables::new(),
        }
    }
}

/// Initial body bindings use their asset order. Entries run after all roots exist.
pub fn body_roots(body: &Body) -> Result<Vec<Root>, InitFault> {
    let mut roots = Vec::new();
    for (offset, binding) in body.graphs.iter().enumerate() {
        let Some(graph) = binding.graph else {
            continue;
        };
        let id = u16::try_from(offset + 1).map_err(|_| InitFault::InstanceIdExhausted)?;
        let mut root = Root::new(id, graph);
        for field in binding.sync.iter().flatten() {
            let key = field
                .get("m_0D09D2D9")
                .and_then(asset_guid)
                .map_or(0, |guid| guid as u16);
            let value = script_expr::evaluate(
                field.get("m_value").unwrap_or(&Json::Null),
                &mut EmptyContext,
            )
            .map_err(|error| InitFault::RootBinding { id, graph, error })?;
            root.variables.insert(u32::from(key), value);
        }
        roots.push(root);
    }
    Ok(roots)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Site {
    pub entity: u32,
    pub instance: u16,
    pub graph: u32,
    pub node: Option<u16>,
}

/// External authored queries for this initialization. The caller owns mode,
/// ruleset, and entity data and can decline a query without guessing its value.
pub trait World {
    fn ruleset(&mut self, _id: u32) -> Option<Value> {
        None
    }

    fn entity_variable(&mut self, _entity: u32, _id: u32) -> Value {
        Value::Null
    }

    fn position(&mut self, _entity: u32) -> [f64; 3] {
        [0.0; 3]
    }

    fn native(&mut self, _site: Site, _config: &Json) -> Option<Value> {
        None
    }

    /// None leaves an encountered AI candidate explicitly unsupported.
    fn selects_ai_behavior(&mut self, _site: Site) -> Option<bool> {
        None
    }
}

#[derive(Default)]
pub struct EmptyWorld;

impl World for EmptyWorld {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StateKind {
    Plain,
    BooleanSwitch(bool),
    SubScript { child: Option<u16> },
    MessageIdle,
    Wait,
    WaitFrames { target: u32 },
    AiCandidate { selected: bool },
    LogicalButton { down: bool },
    Stack { top: bool, under: bool },
    Ability { flags: u8 },
    MovementMod,
}

#[derive(Clone, Copy, Debug)]
struct BufferedPress {
    time_ms: u64,
    activation: u16,
}

#[derive(Clone, Debug)]
pub struct State {
    pub active: bool,
    pub activation: u16,
    pub start_ms: u64,
    pub kind: StateKind,
    ending: bool,
    pending_change: bool,
    pending_timer: bool,
    buffered_press: Option<BufferedPress>,
    stack_output: Option<VarKey>,
}

#[derive(Clone, Debug)]
pub struct Instance {
    pub graph: u32,
    /// Parent instance and raw state index, as carried on the wire.
    pub parent: Option<(u16, u16)>,
    pub variables: Variables,
    pub states: IndexMap<u16, State>,
}

#[derive(Debug, Eq, PartialEq)]
pub enum Issue {
    MissingGraph(u32),
    MissingNode(u16),
    MissingState(u16),
    UnsupportedAction(String),
    UnsupportedState(String),
    UnsupportedField(&'static str),
    InvalidSubScript,
    Expression(EvalError),
}

#[derive(Debug, Eq, PartialEq)]
pub struct Diagnostic {
    pub site: Site,
    pub issue: Issue,
}

#[derive(Debug)]
pub struct Snapshot {
    pub entity: u32,
    pub variables: Variables,
    pub instances: IndexMap<u16, Instance>,
    /// Pending state timers and finishes in their per-instance execution order.
    pub pending_timers: IndexMap<u16, Vec<PendingTimer>>,
    /// Stack links by (instance or zero for the entity, variable).
    pub bindings: IndexMap<(u16, u32), Vec<StackBinding>>,
    pub diagnostics: Vec<Diagnostic>,
}

impl Snapshot {
    /// The reached initialization path was supported. This does not check
    /// dormant paths, later input, timers, messages, or gameplay.
    pub fn initialization_complete(&self) -> bool {
        self.diagnostics.is_empty()
    }
}

#[derive(Debug, Eq, PartialEq)]
pub enum InitFault {
    InvalidBudget,
    InvalidRoot(u16),
    InstanceIdExhausted,
    RootBinding {
        id: u16,
        graph: u32,
        error: EvalError,
    },
    WorkBudget {
        limit: usize,
        operation: &'static str,
    },
    EventBudget {
        instance: u16,
    },
    PendingEvents {
        limit: usize,
    },
    FrameOrder {
        frame: u32,
        now_ms: u64,
    },
    Halted,
    PlugDepth,
}

impl fmt::Display for InitFault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidBudget => {
                f.write_str("statescript initialization budget must be positive")
            }
            Self::InvalidRoot(id) => write!(f, "invalid or duplicate statescript root {id}"),
            Self::InstanceIdExhausted => {
                f.write_str("statescript initialization instance IDs exhausted")
            }
            Self::RootBinding { id, graph, error } => {
                write!(
                    f,
                    "statescript root {id} graph {graph:04X} binding failed: {error}"
                )
            }
            Self::WorkBudget { limit, operation } => {
                write!(
                    f,
                    "statescript initialization work budget {limit} exhausted during {operation}"
                )
            }
            Self::EventBudget { instance } => {
                write!(
                    f,
                    "statescript initialization event budget exhausted in instance {instance}"
                )
            }
            Self::PendingEvents { limit } => {
                write!(f, "statescript pending event limit {limit} exceeded")
            }
            Self::FrameOrder { frame, now_ms } => {
                write!(f, "statescript frame {frame} starts before {now_ms} ms")
            }
            Self::Halted => f.write_str("statescript component halted after a failed operation"),
            Self::PlugDepth => f.write_str("statescript initialization plug depth exceeded"),
        }
    }
}

impl Error for InitFault {}

impl From<QueueFull> for InitFault {
    fn from(error: QueueFull) -> Self {
        Self::PendingEvents { limit: error.limit }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PendingTimer {
    pub due_ms: u64,
    pub raw_state: u16,
    pub finish: bool,
    pub parameter: u16,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StackBinding {
    pub instance: u16,
    pub raw_state: u16,
}

/// A single body using the same authored graph engine as one-shot initialization.
pub struct Component {
    graphs: Arc<GraphStore>,
    machine: Machine,
    halted: bool,
}

impl Component {
    pub fn start(
        graphs: Arc<GraphStore>,
        entity: u32,
        entity_variables: Variables,
        roots: Vec<Root>,
        frame: u32,
        world: &mut impl World,
        max_work: usize,
    ) -> Result<Self, InitFault> {
        let mut machine = Machine::new(entity, entity_variables, frame, max_work)?;
        Vm::new(&graphs, world, &mut machine).start(roots)?;
        machine.capture_outputs();
        Ok(Self {
            graphs,
            machine,
            halted: false,
        })
    }

    pub fn snapshot(&self) -> Option<&Snapshot> {
        (!self.halted).then_some(&self.machine.snapshot)
    }

    pub fn now_ms(&self) -> u64 {
        self.machine.now_ms
    }

    pub fn resolved_variable(&self, instance: u16, variable: Variable) -> Option<Value> {
        (!self.halted).then(|| self.machine.value(source_key(instance, variable), None))
    }

    pub fn set_entity_variable(
        &mut self,
        id: u32,
        value: Value,
        world: &mut impl World,
    ) -> Result<(), InitFault> {
        self.run(world, |vm| vm.write(VarKey::Entity(id), value))
    }

    /// Authoritative modifiers require a fully supported runtime path.
    pub fn movement_modifiers(
        &mut self,
        world: &mut impl World,
    ) -> Result<Option<BTreeMap<ModifierKey, Modifier>>, InitFault> {
        if self.halted {
            return Err(InitFault::Halted);
        }
        if !self.machine.snapshot.initialization_complete() {
            return Ok(None);
        }
        let mut modifiers = BTreeMap::new();
        self.run(world, |vm| {
            modifiers = vm.collect_movement_modifiers()?;
            Ok(())
        })?;
        Ok(self
            .machine
            .snapshot
            .initialization_complete()
            .then_some(modifiers))
    }

    /// Queue the authored entries of one instance at the current clock.
    pub fn queue_message(
        &mut self,
        instance: u16,
        message: u64,
        parameters: Variables,
    ) -> Result<usize, InitFault> {
        if self.halted {
            return Err(InitFault::Halted);
        }
        let Some(item) = self.machine.snapshot.instances.get(&instance) else {
            return Ok(0);
        };
        let graph = self.graphs.graph(item.graph).expect("registered graph");
        let entries: Vec<_> = graph
            .nodes
            .iter()
            .filter(|node| {
                !node.client_only
                    && node
                        .field("m_gameMessage.m_gameMessage")
                        .and_then(asset_guid)
                        == Some(message)
                    && (is_message_entry(node)
                        || (node.is_state()
                            && node.state.is_some_and(|raw| {
                                item.states.get(&raw).is_some_and(|state| state.active)
                            })))
            })
            .map(|node| node.pos)
            .collect();
        self.machine.queues.reserve(entries.len())?;
        let parameters = Arc::new(parameters);
        for &position in &entries {
            self.machine.queues.push(
                instance,
                self.machine.now_ms,
                Event::Message {
                    position,
                    parameters: Arc::clone(&parameters),
                },
            )?;
        }
        Ok(entries.len())
    }

    pub fn run_now(&mut self, world: &mut impl World) -> Result<(), InitFault> {
        self.run(world, |vm| vm.run())
    }

    pub fn advance_frame(&mut self, frame: u32, world: &mut impl World) -> Result<(), InitFault> {
        self.advance(frame, None, world)
    }

    pub fn advance_command(
        &mut self,
        command: Command,
        world: &mut impl World,
    ) -> Result<(), InitFault> {
        self.advance(command.frame, Some(command), world)
    }

    pub fn set_owner_dead(&mut self, dead: bool) {
        self.machine.owner_dead = dead;
    }

    fn advance(
        &mut self,
        frame: u32,
        command: Option<Command>,
        world: &mut impl World,
    ) -> Result<(), InitFault> {
        if frame_start(frame) < self.machine.now_ms {
            return Err(InitFault::FrameOrder {
                frame,
                now_ms: self.machine.now_ms,
            });
        }
        self.run(world, |vm| vm.advance_frame(frame, command))
    }

    fn run<W: World>(
        &mut self,
        world: &mut W,
        operation: impl FnOnce(&mut Vm<'_, '_, '_, W>) -> Result<(), InitFault>,
    ) -> Result<(), InitFault> {
        if self.halted {
            return Err(InitFault::Halted);
        }
        self.machine.work_left = self.machine.max_work;
        let result = operation(&mut Vm::new(&self.graphs, world, &mut self.machine));
        self.halted = result.is_err();
        if result.is_ok() {
            self.machine.capture_outputs();
        }
        result
    }
}

pub fn initialize(
    graphs: &GraphStore,
    entity: u32,
    entity_variables: Variables,
    roots: Vec<Root>,
    world: &mut impl World,
    max_work: usize,
) -> Result<Snapshot, InitFault> {
    let mut machine = Machine::new(entity, entity_variables, 0, max_work)?;
    Vm::new(graphs, world, &mut machine).start(roots)?;
    machine.capture_outputs();
    Ok(machine.snapshot)
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum VarKey {
    Entity(u32),
    Instance(u16, u32),
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct StateKey {
    instance: u16,
    raw: u16,
}

struct StackEntry {
    state: StateKey,
    priority: StackPriority,
    value: Value,
}

#[derive(Clone, Copy)]
struct AbilityPress {
    button: u8,
    time_ms: u64,
    activation: u16,
}

enum Event {
    Entry(u16),
    Begin(u16),
    Finish(u16),
    Abort(u16),
    Changed(u16),
    Reevaluate(u16),
    Timer(u16, u16),
    Button(ButtonEdge),
    AbilityCheck(u16),
    Message {
        position: u16,
        parameters: Arc<Variables>,
    },
}

impl Event {
    fn state(&self) -> Option<u16> {
        match *self {
            Self::Entry(_) | Self::Message { .. } | Self::Button(_) => None,
            Self::Begin(state)
            | Self::Finish(state)
            | Self::Abort(state)
            | Self::Changed(state)
            | Self::Reevaluate(state)
            | Self::AbilityCheck(state)
            | Self::Timer(state, _) => Some(state),
        }
    }
}

struct Machine {
    snapshot: Snapshot,
    queues: Timeline<Event>,
    watchers: BTreeMap<VarKey, Vec<StateKey>>,
    ticking: BTreeMap<u16, IndexMap<u16, u64>>,
    stacks: BTreeMap<VarKey, Vec<StackEntry>>,
    ability_presses: BTreeMap<StateKey, Vec<AbilityPress>>,
    next_id: u32,
    work_left: usize,
    max_work: usize,
    frame: u32,
    now_ms: u64,
    command_buttons: u32,
    held_buttons: Vec<u8>,
    owner_dead: bool,
}

impl Machine {
    fn new(
        entity: u32,
        variables: Variables,
        frame: u32,
        max_work: usize,
    ) -> Result<Self, InitFault> {
        if max_work == 0 {
            return Err(InitFault::InvalidBudget);
        }
        Ok(Self {
            snapshot: Snapshot {
                entity,
                variables,
                instances: IndexMap::new(),
                pending_timers: IndexMap::new(),
                bindings: IndexMap::new(),
                diagnostics: Vec::new(),
            },
            queues: Timeline::new(MAX_PENDING_EVENTS),
            watchers: BTreeMap::new(),
            ticking: BTreeMap::new(),
            stacks: BTreeMap::new(),
            ability_presses: BTreeMap::new(),
            next_id: 1,
            work_left: max_work,
            max_work,
            frame,
            now_ms: frame_start(frame),
            command_buttons: 0,
            held_buttons: Vec::new(),
            owner_dead: false,
        })
    }

    fn capture_outputs(&mut self) {
        self.snapshot.pending_timers.clear();
        for &id in self.snapshot.instances.keys() {
            let timers: Vec<_> = self
                .queues
                .iter(id)
                .filter_map(|(due_ms, event)| {
                    let (raw_state, finish, parameter) = match event {
                        Event::Timer(raw, param) => (*raw, false, *param),
                        Event::Finish(raw) => (*raw, true, 0),
                        _ => return None,
                    };
                    Some(PendingTimer {
                        due_ms,
                        raw_state,
                        finish,
                        parameter,
                    })
                })
                .collect();
            if !timers.is_empty() {
                self.snapshot.pending_timers.insert(id, timers);
            }
        }
        self.snapshot.bindings = self
            .stacks
            .iter()
            .map(|(&variable, entries)| {
                let key = match variable {
                    VarKey::Entity(id) => (0, id),
                    VarKey::Instance(instance, id) => (instance, id),
                };
                (
                    key,
                    entries
                        .iter()
                        .map(|entry| StackBinding {
                            instance: entry.state.instance,
                            raw_state: entry.state.raw,
                        })
                        .collect(),
                )
            })
            .collect();
    }

    fn value(&self, key: VarKey, below: Option<StateKey>) -> Value {
        self.stacks
            .get(&key)
            .and_then(|entries| {
                entries
                    .iter()
                    .take_while(|entry| Some(entry.state) != below)
                    .last()
            })
            .map_or_else(|| stored(&self.snapshot, key), |entry| entry.value.clone())
    }

    fn allows_stack(
        &self,
        key: VarKey,
        priority: StackPriority,
        exclude: Option<StateKey>,
    ) -> bool {
        self.stacks.get(&key).is_none_or(|entries| {
            entries
                .iter()
                .filter(|entry| Some(entry.state) != exclude)
                .all(|entry| {
                    entry.priority.level < priority.level
                        || (entry.priority.level == priority.level && priority.above_equals)
                })
        })
    }
}

struct Vm<'graph, 'world, 'machine, W> {
    graphs: &'graph GraphStore,
    world: &'world mut W,
    machine: &'machine mut Machine,
    parameters: Option<Arc<Variables>>,
}

impl<'graph, 'world, 'machine, W: World> Vm<'graph, 'world, 'machine, W> {
    fn new(
        graphs: &'graph GraphStore,
        world: &'world mut W,
        machine: &'machine mut Machine,
    ) -> Self {
        Self {
            graphs,
            world,
            machine,
            parameters: None,
        }
    }

    fn start(&mut self, roots: Vec<Root>) -> Result<(), InitFault> {
        let mut seen = HashSet::new();
        for root in &roots {
            if root.id == 0 || !seen.insert(root.id) {
                return Err(InitFault::InvalidRoot(root.id));
            }
        }
        for root in roots {
            self.create(root.id, root.graph, None, root.variables)?;
        }
        self.run()
    }

    fn spend(&mut self, operation: &'static str) -> Result<(), InitFault> {
        self.machine.work_left =
            self.machine
                .work_left
                .checked_sub(1)
                .ok_or(InitFault::WorkBudget {
                    limit: self.machine.max_work,
                    operation,
                })?;
        Ok(())
    }

    fn create(
        &mut self,
        id: u16,
        graph: u32,
        parent: Option<(u16, u16)>,
        variables: Variables,
    ) -> Result<bool, InitFault> {
        self.spend("instance creation")?;
        let Some(definition) = self.graphs.graph(graph) else {
            self.report(
                Site {
                    entity: self.machine.snapshot.entity,
                    instance: id,
                    graph,
                    node: None,
                },
                Issue::MissingGraph(graph),
            );
            return Ok(false);
        };
        self.machine.next_id = self.machine.next_id.max(u32::from(id) + 1);
        self.machine.snapshot.instances.insert(
            id,
            Instance {
                graph,
                parent,
                variables,
                states: IndexMap::new(),
            },
        );
        for position in definition.entries.iter().flatten() {
            if definition
                .node(*position)
                .is_some_and(|node| !node.client_only && is_entry(node))
            {
                self.post(id, Event::Entry(*position))?;
            }
        }
        Ok(true)
    }

    fn run(&mut self) -> Result<(), InitFault> {
        loop {
            self.update()?;
            if self
                .machine
                .queues
                .next_due()
                .is_none_or(|due| due > self.machine.now_ms)
            {
                return Ok(());
            }
        }
    }

    fn advance_frame(&mut self, frame: u32, command: Option<Command>) -> Result<(), InitFault> {
        self.machine.frame = frame;
        self.machine.now_ms = frame_start(frame);
        if let Some(command) = command {
            self.queue_buttons(command)?;
        }
        let end = self.machine.now_ms + FRAME_MS - 1;
        loop {
            let due = self
                .machine
                .queues
                .next_due()
                .filter(|&due| due <= end)
                .unwrap_or(end);
            self.machine.now_ms = self.machine.now_ms.max(due);
            self.update()?;
            if self.machine.now_ms == end
                && self.machine.queues.next_due().is_none_or(|due| due > end)
            {
                return Ok(());
            }
        }
    }

    fn queue_buttons(&mut self, command: Command) -> Result<(), InitFault> {
        let edges: Vec<_> = button_edges(
            self.machine.command_buttons,
            command.buttons,
            command.action,
        )
        .collect();
        self.machine
            .queues
            .reserve(edges.len() * self.machine.snapshot.instances.len())?;
        for &id in self.machine.snapshot.instances.keys() {
            for &edge in &edges {
                self.machine
                    .queues
                    .push(id, self.machine.now_ms, Event::Button(edge))?;
            }
        }
        self.machine.command_buttons = command.buttons;
        Ok(())
    }

    fn button_edge(&mut self, id: u16, edge: ButtonEdge) -> Result<(), InitFault> {
        if edge.pressed {
            if !self.machine.held_buttons.contains(&edge.button) {
                self.machine.held_buttons.push(edge.button);
            }
        } else {
            self.machine
                .held_buttons
                .retain(|&button| button != edge.button);
        }
        let graph = self.graph(id);
        for raw in raw_indices(array_field(&graph.fields, "m_680A2CB2")) {
            let Some(node) = graph.state(usize::from(raw)) else {
                continue;
            };
            if node.client_only {
                continue;
            }
            let key = StateKey { instance: id, raw };
            if node.class == "STUStatescriptStateAbility" && supported_ability(node) {
                self.spend("ability button edge")?;
                self.ability_edge(key, node, edge)?;
                continue;
            }
            if node.class != "STUStatescriptStateLogicalButton" {
                continue;
            }
            self.spend("logical button edge")?;
            if self.logical_button_id(key, node) != Some(edge.button) {
                continue;
            }
            if edge.pressed
                && self.machine.owner_dead
                && !self
                    .evaluate(id, node, field(&node.fields, "m_5AE05408"), None)
                    .is_some_and(|value| truthy(&value))
            {
                continue;
            }
            if edge.pressed {
                let state = self.machine.snapshot.instances[&id]
                    .states
                    .entry(raw)
                    .or_insert(State {
                        active: false,
                        activation: 0,
                        start_ms: 0,
                        kind: StateKind::LogicalButton { down: false },
                        ending: false,
                        pending_change: false,
                        pending_timer: false,
                        buffered_press: None,
                        stack_output: None,
                    });
                state.buffered_press = Some(BufferedPress {
                    time_ms: self.machine.now_ms,
                    activation: state.activation.wrapping_add(u16::from(!state.active)),
                });
            }
            if self.state(key).is_some_and(|state| state.active) {
                self.set_button_down(key, node, edge.pressed, 0)?;
            }
        }
        Ok(())
    }

    fn logical_button_id(&mut self, key: StateKey, node: &Node) -> Option<u8> {
        let value = self.evaluate(
            key.instance,
            node,
            field(&node.fields, "m_logicalButton"),
            None,
        )?;
        u8::try_from(script_expr::to_int(&value, 0)).ok()
    }

    fn button_held(&mut self, key: StateKey, node: &Node) -> bool {
        self.logical_button_id(key, node)
            .is_some_and(|button| self.machine.held_buttons.contains(&button))
    }

    fn enter_button(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let state = self.state(key).expect("active logical button");
        let buffer = state
            .buffered_press
            .filter(|press| press.activation == state.activation);
        let duration = field(&node.fields, "m_246438AD");
        let early = if let Some(press) = buffer.filter(|_| !duration.is_null()) {
            let waited = ((self.machine.now_ms - press.time_ms) as f64 / 1000.0) as f32;
            self.evaluate(key.instance, node, duration, None)
                .is_some_and(|value| f64::from(waited) <= script_expr::to_float(&value))
        } else {
            false
        };
        if early {
            self.follow(key.instance, node, "m_onGoingDownPlug", Some(key), depth)?;
        }
        if !self.state(key).is_some_and(|state| state.active) {
            return Ok(());
        }
        let held = self.button_held(key, node);
        let down = held || early;
        self.state_mut(key).expect("active logical button").kind =
            StateKind::LogicalButton { down };
        let path = if down { "m_80AF45FB" } else { "m_1C9EF058" };
        self.follow(key.instance, node, path, Some(key), depth)?;
        if early && !held {
            self.post_after(key.instance, 1, Event::Timer(key.raw, 1))?;
        }
        Ok(())
    }

    fn set_button_down(
        &mut self,
        key: StateKey,
        node: &Node,
        down: bool,
        depth: usize,
    ) -> Result<(), InitFault> {
        self.state_mut(key).expect("active logical button").kind =
            StateKind::LogicalButton { down };
        let (previous, edge, next) = if down {
            ("m_1C9EF058", "m_onGoingDownPlug", "m_80AF45FB")
        } else {
            ("m_80AF45FB", "m_onComingUpPlug", "m_1C9EF058")
        };
        self.exit_subgraph(key.instance, node, previous, depth + 1)?;
        self.follow(key.instance, node, edge, Some(key), depth)?;
        if self.state(key).is_some_and(|state| state.active) {
            self.follow(key.instance, node, next, Some(key), depth)?;
        }
        Ok(())
    }

    fn ability_buttons(&mut self, key: StateKey, node: &Node) -> Option<[u8; 3]> {
        let mut buttons = [0; 3];
        for (button, field_name) in
            buttons
                .iter_mut()
                .zip(["m_A5F1A77B", "m_A368407D", "m_8CBD15DA"])
        {
            let value = self.evaluate(key.instance, node, field(&node.fields, field_name), None)?;
            *button = u8::try_from(script_expr::to_int(&value, 0)).ok()?;
        }
        Some(buttons)
    }

    fn ability_edge(
        &mut self,
        key: StateKey,
        node: &Node,
        edge: ButtonEdge,
    ) -> Result<(), InitFault> {
        if !edge.pressed
            || edge.button == 0
            || !self
                .ability_buttons(key, node)
                .is_some_and(|buttons| buttons.contains(&edge.button))
        {
            return Ok(());
        }
        let state = self.state(key);
        let active = state.is_some_and(|state| state.active);
        let activation = state
            .map_or(0, |state| state.activation)
            .wrapping_add(u16::from(!active));
        let now = self.machine.now_ms;
        let presses = self.machine.ability_presses.entry(key).or_default();
        presses.retain(|press| now - press.time_ms < 2000);
        presses.push(AbilityPress {
            button: edge.button,
            time_ms: now,
            activation,
        });
        if active {
            self.post(key.instance, Event::AbilityCheck(key.raw))?;
        }
        Ok(())
    }

    fn take_ability_press(&mut self, key: StateKey, node: &Node, button: u8) -> bool {
        let early = field(&node.fields, "m_246438AD");
        let window = if early.is_null() {
            0.0
        } else {
            let Some(value) = self.evaluate(key.instance, node, early, None) else {
                return false;
            };
            ((script_expr::to_float(&value) * 1000.0) as f32).round()
        };
        let now = self.machine.now_ms;
        let activation = self.state(key).expect("active ability").activation;
        let Some(presses) = self.machine.ability_presses.get_mut(&key) else {
            return false;
        };
        let Some(index) = presses.iter().position(|press| {
            press.button == button
                && (now - press.time_ms) as f32 <= window
                && press.activation <= activation
        }) else {
            return false;
        };
        presses.remove(index);
        true
    }

    fn ability_flags(&self, key: StateKey) -> Option<u8> {
        let StateKind::Ability { flags } = self.state(key).filter(|state| state.active)?.kind
        else {
            return None;
        };
        Some(flags)
    }

    fn set_ability_flags(&mut self, key: StateKey, flags: u8) {
        self.state_mut(key).expect("active ability").kind = StateKind::Ability { flags };
    }

    fn ability_condition(&mut self, key: StateKey, node: &Node, name: &str) -> bool {
        let config = field(&node.fields, name);
        config.is_null()
            || self
                .evaluate(key.instance, node, config, Some(key))
                .is_some_and(|value| truthy(&value))
    }

    fn ability_stack_allows(&mut self, key: StateKey, node: &Node, name: &str) -> bool {
        let config = field(&node.fields, name);
        if config.is_null() {
            return true;
        }
        let variable = match script_expr::lvalue(field(&node.fields, "m_out_StackVar")) {
            Ok(Some(variable)) => source_key(key.instance, variable),
            Ok(None) => return true,
            Err(error) => {
                self.report(self.site(key.instance, node), Issue::Expression(error));
                return false;
            }
        };
        match script_expr::stack_priority(config) {
            Ok(priority) => {
                self.watch(variable, key);
                self.machine.allows_stack(variable, priority, Some(key))
            }
            Err(error) => {
                self.report(self.site(key.instance, node), Issue::Expression(error));
                false
            }
        }
    }

    fn activate_ability(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(flags) = self.ability_flags(key) else {
            return Ok(());
        };
        let node = self
            .graph(key.instance)
            .state(usize::from(key.raw))
            .expect("ability state");
        if self.machine.owner_dead {
            return self.stop_ability(key, node, depth);
        }
        let Some(buttons) = self.ability_buttons(key, node) else {
            return Ok(());
        };
        if flags & ABILITY_RUNNING != 0 {
            self.keep_ability(key, node, flags, buttons, depth)
        } else {
            self.try_start_ability(key, node, flags, buttons, depth)
        }
    }

    fn keep_ability(
        &mut self,
        key: StateKey,
        node: &Node,
        mut flags: u8,
        [start, hold, stop]: [u8; 3],
        depth: usize,
    ) -> Result<(), InitFault> {
        if !self.machine.held_buttons.contains(&start) {
            flags &= !ABILITY_HELD;
            self.set_ability_flags(key, flags);
        }
        let keep_held = field(&node.fields, "m_755F9921");
        if hold == 0
            || (!keep_held.is_null()
                && self
                    .evaluate(key.instance, node, keep_held, Some(key))
                    .is_some_and(|value| truthy(&value)))
        {
            if stop != 0 && self.take_ability_press(key, node, stop) {
                if stop == start {
                    self.set_ability_flags(key, flags | ABILITY_HELD);
                }
                return self.stop_ability(key, node, depth);
            }
        } else if !self.machine.held_buttons.contains(&hold) {
            return self.stop_ability(key, node, depth);
        }
        if !self.ability_condition(key, node, "m_F495731F")
            || !self.ability_stack_allows(key, node, "m_343E99EE")
        {
            self.stop_ability(key, node, depth)?;
        }
        Ok(())
    }

    fn try_start_ability(
        &mut self,
        key: StateKey,
        node: &Node,
        flags: u8,
        [start, hold, _]: [u8; 3],
        depth: usize,
    ) -> Result<(), InitFault> {
        let mut pressed = false;
        if start != 0 {
            pressed = self.take_ability_press(key, node, start);
            if !pressed {
                if !self.machine.held_buttons.contains(&start) {
                    self.set_ability_flags(key, flags & !ABILITY_HELD);
                    return Ok(());
                }
                if flags & ABILITY_HELD != 0 {
                    return Ok(());
                }
            }
        } else if hold != 0 && !self.machine.held_buttons.contains(&hold) {
            return Ok(());
        }
        if !self.ability_condition(key, node, "m_150F0D92")
            || !self.ability_condition(key, node, "m_F495731F")
            || !self.ability_stack_allows(key, node, "m_3C0EB4A6")
        {
            if pressed {
                let activation = self.state(key).expect("active ability").activation;
                self.machine.ability_presses.entry(key).or_default().insert(
                    0,
                    AbilityPress {
                        button: start,
                        time_ms: self.machine.now_ms,
                        activation,
                    },
                );
            }
            return Ok(());
        }
        let id = key.instance;
        self.set_ability_flags(
            key,
            flags | ABILITY_RUNNING | if start != 0 { ABILITY_HELD } else { 0 },
        );
        self.follow(id, node, "m_B969AFC9", Some(key), depth)?;
        self.follow(id, node, "m_4E22B5C4", Some(key), depth)?;
        self.ability_vars(id, node, "m_EF3C5C73")
    }

    fn stop_ability(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let Some(flags) = self
            .ability_flags(key)
            .filter(|flags| flags & ABILITY_RUNNING != 0)
        else {
            return Ok(());
        };
        let id = key.instance;
        self.set_ability_flags(key, flags & !ABILITY_RUNNING);
        self.exit_subgraph(id, node, "m_B969AFC9", depth + 1)?;
        self.follow(id, node, "m_6EEEC0AA", Some(key), depth)?;
        self.ability_vars(id, node, "m_19899EAC")
    }

    fn ability_vars(&mut self, id: u16, node: &Node, name: &str) -> Result<(), InitFault> {
        for item in array_field(&node.fields, name)
            .iter()
            .filter(|item| item.is_object())
        {
            self.store(id, node, item, false)?;
        }
        Ok(())
    }

    fn update(&mut self) -> Result<(), InitFault> {
        self.spend("component update")?;
        let instances: Vec<_> = self.machine.snapshot.instances.keys().copied().collect();
        for id in instances {
            let mut events = 0;
            while let Some(event) = self.machine.queues.pop_due(id, self.machine.now_ms) {
                if events == MAX_EVENTS_PER_UPDATE {
                    return Err(InitFault::EventBudget { instance: id });
                }
                events += 1;
                self.spend("event dispatch")?;
                let key = |raw| StateKey { instance: id, raw };
                match event {
                    Event::Entry(position) => {
                        if let Some(node) = self.graph(id).node(position) {
                            self.run_node(id, node, None, 0)?;
                        }
                    }
                    Event::Begin(raw) => self.begin(key(raw), 0)?,
                    Event::Finish(raw) => self.end(key(raw), true, 0)?,
                    Event::Abort(raw) => self.end(key(raw), false, 0)?,
                    Event::Changed(raw) => self.changed(key(raw))?,
                    Event::Reevaluate(raw) => self.reevaluate(key(raw), 0)?,
                    Event::Timer(raw, parameter) => self.timer(key(raw), parameter)?,
                    Event::Button(edge) => self.button_edge(id, edge)?,
                    Event::AbilityCheck(raw) => {
                        self.activate_ability(key(raw), 0)?;
                        if self.state(key(raw)).is_some_and(|state| state.active) {
                            let node = self
                                .graph(id)
                                .state(usize::from(raw))
                                .expect("ability state");
                            self.try_complete(key(raw), node, 0)?;
                        }
                    }
                    Event::Message {
                        position,
                        parameters,
                    } => {
                        self.deliver(id, position, parameters)?;
                    }
                }
            }
            self.tick_states(id)?;
        }
        Ok(())
    }

    fn graph(&self, id: u16) -> &'graph Graph {
        let graph = self.machine.snapshot.instances[&id].graph;
        self.graphs.graph(graph).expect("registered graph")
    }

    fn site(&self, instance: u16, node: &Node) -> Site {
        Site {
            entity: self.machine.snapshot.entity,
            instance,
            graph: self.machine.snapshot.instances[&instance].graph,
            node: Some(node.pos),
        }
    }

    fn report(&mut self, site: Site, issue: Issue) {
        let diagnostic = Diagnostic { site, issue };
        if !self.machine.snapshot.diagnostics.contains(&diagnostic) {
            self.machine.snapshot.diagnostics.push(diagnostic);
        }
    }

    fn post(&mut self, id: u16, event: Event) -> Result<(), InitFault> {
        self.post_after(id, 0, event)
    }

    fn post_after(&mut self, id: u16, delay_ms: u64, event: Event) -> Result<(), InitFault> {
        self.machine
            .queues
            .push(id, self.machine.now_ms.saturating_add(delay_ms), event)?;
        Ok(())
    }

    fn deliver(
        &mut self,
        id: u16,
        position: u16,
        parameters: Arc<Variables>,
    ) -> Result<(), InitFault> {
        let Some(node) = self.graph(id).node(position) else {
            return Ok(());
        };
        if node.is_state() {
            if node.state.is_some_and(|raw| {
                self.state(StateKey { instance: id, raw })
                    .is_some_and(|state| state.active)
            }) {
                self.report(
                    self.site(id, node),
                    Issue::UnsupportedField("state game message delivery"),
                );
            }
            return Ok(());
        }
        self.parameters = Some(parameters);
        let result = self.deliver_entry(id, node);
        self.parameters = None;
        result
    }

    fn deliver_entry(&mut self, id: u16, node: &Node) -> Result<(), InitFault> {
        let site = self.site(id, node);
        for field in array_field(&node.fields, "m_params") {
            if !field.is_object() {
                continue;
            }
            let target = match script_expr::lvalue(field.get("m_out_Var").unwrap_or(&Json::Null)) {
                Ok(Some(target)) => source_key(id, target),
                Ok(None) => continue,
                Err(error) => {
                    self.report(site, Issue::Expression(error));
                    return Ok(());
                }
            };
            let key = field
                .get("m_B5051BCE")
                .and_then(asset_guid)
                .map_or(0, |guid| guid as u16);
            let value = self
                .parameters
                .as_deref()
                .and_then(|params| params.get(&u32::from(key)))
                .cloned()
                .unwrap_or(Value::Null);
            self.write(target, value)?;
        }
        self.follow(id, node, "m_outPlug", None, 0)?;
        Ok(())
    }

    fn state(&self, key: StateKey) -> Option<&State> {
        self.machine
            .snapshot
            .instances
            .get(&key.instance)?
            .states
            .get(&key.raw)
    }

    fn state_mut(&mut self, key: StateKey) -> Option<&mut State> {
        self.machine
            .snapshot
            .instances
            .get_mut(&key.instance)?
            .states
            .get_mut(&key.raw)
    }

    fn evaluate(
        &mut self,
        id: u16,
        node: &Node,
        config: &Json,
        watcher: Option<StateKey>,
    ) -> Option<Value> {
        let site = self.site(id, node);
        let mut context = InstanceContext {
            machine: self.machine,
            world: self.world,
            site,
            reads: Vec::new(),
            parameters: self.parameters.as_deref(),
            watcher,
            read_native: false,
        };
        let result = script_expr::evaluate(config, &mut context);
        let reads = context.reads;
        let read_native = context.read_native;
        if let Some(watcher) = watcher.filter(|&key| {
            self.state(key)
                .is_some_and(|state| state.active && !state.ending)
        }) {
            for key in reads {
                self.watch(key, watcher);
            }
            if let Some(start) = self
                .state(watcher)
                .filter(|state| read_native && matches!(state.kind, StateKind::BooleanSwitch(_)))
                .map(|state| state.start_ms)
            {
                self.register_tick(watcher, start);
            }
        }
        match result {
            Ok(value) => Some(value),
            Err(error) => {
                self.report(site, Issue::Expression(error));
                None
            }
        }
    }

    fn watch(&mut self, key: VarKey, watcher: StateKey) {
        let watchers = self.machine.watchers.entry(key).or_default();
        if !watchers.contains(&watcher) {
            watchers.push(watcher);
        }
    }

    fn write(&mut self, key: VarKey, value: Value) -> Result<(), InitFault> {
        let old = self.machine.value(key, None);
        match key {
            VarKey::Entity(id) => self.machine.snapshot.variables.insert(id, value),
            VarKey::Instance(instance, id) => self.machine.snapshot.instances[&instance]
                .variables
                .insert(id, value),
        };
        self.notify_value(key, &old, None)
    }

    fn notify_value(
        &mut self,
        key: VarKey,
        old: &Value,
        source: Option<StateKey>,
    ) -> Result<(), InitFault> {
        let changed = !same(old, &self.machine.value(key, None));
        let watchers = self.machine.watchers.get(&key).cloned().unwrap_or_default();
        for watcher in watchers {
            let Some(state) = self
                .state(watcher)
                .filter(|state| state.active && !state.ending && !state.pending_change)
            else {
                continue;
            };
            if state.stack_output == Some(key) {
                if let Some(source) = source {
                    let above = self.machine.stacks.get(&key).is_some_and(|entries| {
                        entries
                            .iter()
                            .skip_while(|entry| entry.state != source)
                            .skip(1)
                            .any(|entry| entry.state == watcher)
                    });
                    if !above {
                        continue;
                    }
                }
            } else if !changed {
                continue;
            }
            self.state_mut(watcher)
                .expect("active watcher")
                .pending_change = true;
            self.post(watcher.instance, Event::Changed(watcher.raw))?;
        }
        Ok(())
    }

    fn follow(
        &mut self,
        id: u16,
        node: &Node,
        path: &str,
        caller: Option<StateKey>,
        depth: usize,
    ) -> Result<bool, InitFault> {
        if depth >= MAX_PLUG_DEPTH {
            return Err(InitFault::PlugDepth);
        }
        self.spend("plug traversal")?;
        let mut taken = false;
        let graph = self.graph(id);
        for (position, input) in node.links(path) {
            self.spend("plug target")?;
            let Some(position) = position else {
                // The reference treats an unresolved plug as taken.
                taken = true;
                continue;
            };
            let Some(target) = graph.node(position) else {
                self.report(self.site(id, node), Issue::MissingNode(position));
                taken = true;
                continue;
            };
            if target.client_only {
                continue;
            }
            if target.is_state() {
                if let Some(raw) = target.state {
                    if target.class == "STUStatescriptStateAbility"
                        && input == "m_3B7BED2E"
                        && supported_ability(target)
                    {
                        self.stop_ability(StateKey { instance: id, raw }, target, depth + 1)?;
                        taken = true;
                        continue;
                    }
                    let event = if input == ABORT_PLUG {
                        Event::Abort(raw)
                    } else {
                        Event::Begin(raw)
                    };
                    self.post(id, event)?;
                    taken = true;
                }
            } else {
                let caller = if target.is_action() { None } else { caller };
                taken = self.run_node(id, target, caller, depth + 1)? || taken;
            }
        }
        Ok(taken)
    }

    fn run_node(
        &mut self,
        id: u16,
        node: &Node,
        caller: Option<StateKey>,
        depth: usize,
    ) -> Result<bool, InitFault> {
        self.spend("node execution")?;
        match node.class.as_str() {
            _ if is_entry(node) => self.follow(id, node, "m_outPlug", None, depth),
            "STUStatescriptActionSetVar" => {
                if !self.store(id, node, &node.fields, true)? {
                    return Ok(false);
                }
                self.follow(id, node, "m_outPlug", None, depth)?;
                Ok(true)
            }
            "STU_12F5D52D" => {
                let mut complete = true;
                for item in array_field(&node.fields, "m_143D4C5B") {
                    if item.is_object() {
                        complete = self.store(id, node, item, false)? && complete;
                    }
                }
                if complete {
                    self.follow(id, node, "m_outPlug", None, depth)?;
                }
                Ok(complete)
            }
            "STU_3387AB5D" => {
                let Some(value) =
                    self.evaluate(id, node, field(&node.fields, "m_condition"), caller)
                else {
                    return Ok(false);
                };
                if !truthy(&value) {
                    return self.follow(id, node, "m_falsePlug", caller, depth);
                }
                Ok(self.follow(id, node, "m_truePlug", caller, depth)?
                    || (node.links("m_truePlug").next().is_none()
                        && node.links("m_falsePlug").next().is_none()))
            }
            "STUStatescriptActionSwitch" => {
                let Some(value) = self.evaluate(id, node, field(&node.fields, "m_262AE982"), None)
                else {
                    return Ok(false);
                };
                let mut path = "m_CD38D5DB".to_owned();
                for (number, case) in array_field(&node.fields, "m_FDD34C25").iter().enumerate() {
                    if !case.is_object() {
                        continue;
                    }
                    let Some(candidate) = self.evaluate(id, node, field(case, "m_value"), None)
                    else {
                        return Ok(false);
                    };
                    if equal(&value, &candidate) {
                        path = format!("m_FDD34C25[{number}]");
                        break;
                    }
                }
                self.follow(id, node, &path, None, depth)?;
                self.follow(id, node, "m_outPlug", None, depth)?;
                Ok(true)
            }
            _ => {
                self.report(
                    self.site(id, node),
                    Issue::UnsupportedAction(node.class.clone()),
                );
                Ok(false)
            }
        }
    }

    fn store(
        &mut self,
        id: u16,
        node: &Node,
        fields: &Json,
        indexed: bool,
    ) -> Result<bool, InitFault> {
        let Some(value) = self.evaluate(id, node, field(fields, "m_value"), None) else {
            return Ok(false);
        };
        let site = self.site(id, node);
        let target = match script_expr::lvalue(field(fields, "m_out_Var")) {
            Ok(Some(target)) => source_key(id, target),
            Ok(None) => return Ok(true),
            Err(error) => {
                self.report(site, Issue::Expression(error));
                return Ok(false);
            }
        };
        if indexed && !field(fields, "m_key").is_null() {
            self.report(site, Issue::UnsupportedField("SetVar map key"));
            return Ok(false);
        }
        let index = field(fields, "m_index");
        if indexed && !index.is_null() {
            let index = if index.is_object() {
                let Some(value) = self.evaluate(id, node, index, None) else {
                    return Ok(false);
                };
                Some(script_expr::to_int(&value, 0))
            } else {
                index
                    .as_i64()
                    .or_else(|| index.as_f64().map(|value| value as i64))
                    .or_else(|| index.as_bool().map(i64::from))
                    .or_else(|| index.as_str()?.trim().parse().ok())
            };
            let Some(index) = index else {
                self.report(site, Issue::UnsupportedField("SetVar array index"));
                return Ok(false);
            };
            if let Ok(index) = usize::try_from(index) {
                let mut items = match self.machine.value(target, None) {
                    Value::Array(items) => items,
                    Value::Null => Vec::new(),
                    value => vec![value],
                };
                if index >= usize::from(u16::MAX) {
                    self.report(
                        site,
                        Issue::UnsupportedField("SetVar array exceeds the wire count"),
                    );
                    return Ok(false);
                }
                if index >= items.len() {
                    let extra = index + 1 - items.len();
                    self.machine.work_left =
                        self.machine
                            .work_left
                            .checked_sub(extra)
                            .ok_or(InitFault::WorkBudget {
                                limit: self.machine.max_work,
                                operation: "SetVar array extension",
                            })?;
                    items.resize(index + 1, Value::Null);
                }
                items[index] = value;
                self.write(target, Value::Array(items))?;
                return Ok(true);
            }
        }
        self.write(target, value)?;
        Ok(true)
    }

    fn begin(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let Some(node) = self.graph(id).state(usize::from(key.raw)) else {
            self.report(
                Site {
                    entity: self.machine.snapshot.entity,
                    instance: id,
                    graph: self.graph(id).index,
                    node: None,
                },
                Issue::MissingState(key.raw),
            );
            return Ok(());
        };
        if let Some(state) = self.state(key).filter(|state| state.active) {
            let kind = state.kind;
            if configured(node, "m_0DBAFD7F") {
                match kind {
                    StateKind::Wait => {
                        self.machine
                            .queues
                            .retain(id, |_, event| event.state() != Some(key.raw));
                        self.state_mut(key).expect("active wait").start_ms = self.machine.now_ms;
                        self.arm_wait(key, node)?;
                    }
                    StateKind::WaitFrames { .. } => self.arm_frame_wait(key, node)?,
                    _ => (),
                }
            }
            return Ok(());
        }
        let kind = match node.class.as_str() {
            "STUStatescriptState" | "STU_9D7BF987" => StateKind::Plain,
            "STUStatescriptStateBooleanSwitch" => StateKind::BooleanSwitch(false),
            "STU_37D754C8" | "STUStatescriptStateSubScript" => StateKind::SubScript { child: None },
            "STUStatescriptStateGameMessageEntry" => StateKind::MessageIdle,
            "STUStatescriptStateWait" if !configured(node, "m_BF1A93B0") => StateKind::Wait,
            "STU_D74D4F47" => StateKind::WaitFrames { target: 0 },
            "STU_91EF24A9" => StateKind::AiCandidate { selected: false },
            "STUStatescriptStateLogicalButton" => StateKind::LogicalButton { down: false },
            "STUStatescriptStateStack" | "STU_A97E6999" => StateKind::Stack {
                top: false,
                under: false,
            },
            "STUStatescriptStateAbility" if supported_ability(node) => {
                StateKind::Ability { flags: 0 }
            }
            "STU_316CFEF2" => StateKind::MovementMod,
            _ => {
                self.report(
                    self.site(id, node),
                    Issue::UnsupportedState(node.class.clone()),
                );
                return Ok(());
            }
        };
        let previous = self.state(key);
        let activation = previous.map_or(1, |state| state.activation.wrapping_add(1));
        let buffered_press = previous.and_then(|state| state.buffered_press);
        for raw in raw_indices(array_field(&node.fields, "m_exclusiveStateIndices")) {
            self.end(StateKey { instance: id, raw }, false, depth + 1)?;
            self.drop_begin(id, raw);
        }
        self.machine.snapshot.instances[&id].states.insert(
            key.raw,
            State {
                active: true,
                activation,
                start_ms: self.machine.now_ms,
                kind,
                ending: false,
                pending_change: false,
                pending_timer: false,
                buffered_press,
                stack_output: None,
            },
        );
        match kind {
            StateKind::BooleanSwitch(_) => {
                let Some(value) =
                    self.evaluate(id, node, field(&node.fields, "m_condition"), Some(key))
                else {
                    return Ok(());
                };
                let current = truthy(&value);
                self.state_mut(key).expect("active switch").kind =
                    StateKind::BooleanSwitch(current);
                self.enter_branch(key, node, current, depth)?;
            }
            StateKind::SubScript { .. } => {
                let child = self.spawn_child(key, node)?;
                self.state_mut(key).expect("active subscript").kind =
                    StateKind::SubScript { child };
            }
            StateKind::MessageIdle => {
                self.follow(id, node, "m_33DA5163", Some(key), depth)?;
            }
            StateKind::Wait => self.arm_wait(key, node)?,
            StateKind::WaitFrames { .. } => self.arm_frame_wait(key, node)?,
            StateKind::AiCandidate { .. } => {
                let site = self.site(id, node);
                if let Some(selected) = self.world.selects_ai_behavior(site) {
                    self.state_mut(key).expect("active AI candidate").kind =
                        StateKind::AiCandidate { selected };
                    if selected {
                        self.follow(id, node, "m_6F92F4BD", Some(key), depth)?;
                    }
                } else {
                    self.report(site, Issue::UnsupportedField("AI behavior selection"));
                }
            }
            StateKind::LogicalButton { .. } => self.enter_button(key, node, depth)?,
            StateKind::Stack { .. } => self.begin_stack(key, node, depth)?,
            StateKind::Ability { .. } => self.register_tick(key, self.machine.now_ms),
            StateKind::Plain | StateKind::MovementMod => (),
        }
        self.follow(id, node, "m_onBeginPlug", Some(key), depth)?;
        self.follow(id, node, "m_subgraphPlug", Some(key), depth)?;
        self.try_complete(key, node, depth)
    }

    fn begin_stack(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let Some(value) = self.evaluate(id, node, field(&node.fields, "m_value"), Some(key)) else {
            return Ok(());
        };
        let target = match script_expr::lvalue(field(&node.fields, "m_out_Var")) {
            Ok(Some(target)) => source_key(id, target),
            Ok(None) => return Ok(()),
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        };
        let priority = match script_expr::stack_priority(field(&node.fields, "m_priority")) {
            Ok(priority) => priority,
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        };
        let old = self.machine.value(target, None);
        match target {
            VarKey::Entity(id) => self
                .machine
                .snapshot
                .variables
                .entry(id)
                .or_insert(Value::Null),
            VarKey::Instance(instance, id) => self.machine.snapshot.instances[&instance]
                .variables
                .entry(id)
                .or_insert(Value::Null),
        };
        self.state_mut(key).expect("active stack").stack_output = Some(target);
        let entries = self.machine.stacks.entry(target).or_default();
        let at = entries
            .iter()
            .position(|entry| {
                entry.priority.level > priority.level
                    || (entry.priority.level == priority.level && !priority.above_equals)
            })
            .unwrap_or(entries.len());
        entries.insert(
            at,
            StackEntry {
                state: key,
                priority,
                value,
            },
        );
        self.notify_value(target, &old, None)?;
        self.sync_stack(target, depth)
    }

    fn sync_stack(&mut self, variable: VarKey, depth: usize) -> Result<(), InitFault> {
        if depth >= MAX_PLUG_DEPTH {
            return Err(InitFault::PlugDepth);
        }
        let Some(entries) = self.machine.stacks.get(&variable) else {
            return Ok(());
        };
        let states: Vec<_> = entries.iter().map(|entry| entry.state).collect();
        for key in states {
            self.spend("stack position change")?;
            let Some(state) = self.state(key).filter(|state| {
                state.active && !state.ending && state.stack_output == Some(variable)
            }) else {
                continue;
            };
            let top = self
                .machine
                .stacks
                .get(&variable)
                .and_then(|entries| entries.last())
                .is_some_and(|entry| entry.state == key);
            let kind = StateKind::Stack { top, under: !top };
            if state.kind == kind {
                continue;
            }
            self.state_mut(key).expect("active stack").kind = kind;
            let node = self
                .graph(key.instance)
                .state(usize::from(key.raw))
                .expect("stack state");
            let (old, next) = if top {
                ("m_underSubgraphPlug", "m_topSubgraphPlug")
            } else {
                ("m_topSubgraphPlug", "m_underSubgraphPlug")
            };
            self.exit_subgraph(key.instance, node, old, depth + 1)?;
            self.follow(key.instance, node, next, Some(key), depth)?;
        }
        Ok(())
    }

    fn unlink_stack(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(target) = self
            .state_mut(key)
            .and_then(|state| state.stack_output.take())
        else {
            return Ok(());
        };
        let old = self.machine.value(target, None);
        if let Some(entries) = self.machine.stacks.get_mut(&target) {
            entries.retain(|entry| entry.state != key);
            if entries.is_empty() {
                self.machine.stacks.remove(&target);
            }
        }
        self.notify_value(target, &old, None)?;
        self.sync_stack(target, depth)
    }

    fn update_stack(&mut self, key: StateKey) -> Result<(), InitFault> {
        let Some(target) = self.state(key).and_then(|state| state.stack_output) else {
            return Ok(());
        };
        let id = key.instance;
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("stack state");
        let value = field(&node.fields, "m_value");
        if value.is_null() {
            return Ok(());
        }
        let Some(value) = self.evaluate(id, node, value, Some(key)) else {
            return Ok(());
        };
        let old = self.machine.value(target, None);
        if let Some(entry) = self
            .machine
            .stacks
            .get_mut(&target)
            .and_then(|entries| entries.iter_mut().find(|entry| entry.state == key))
            .filter(|entry| !same(&entry.value, &value))
        {
            entry.value = value;
            self.notify_value(target, &old, Some(key))?;
        }
        Ok(())
    }

    fn collect_movement_modifiers(&mut self) -> Result<BTreeMap<ModifierKey, Modifier>, InitFault> {
        let states: Vec<_> = self
            .machine
            .snapshot
            .instances
            .iter()
            .flat_map(|(&id, instance)| {
                instance.states.iter().filter_map(move |(&raw, state)| {
                    (state.active && state.kind == StateKind::MovementMod)
                        .then_some(StateKey { instance: id, raw })
                })
            })
            .collect();
        let mut modifiers = BTreeMap::new();
        for key in states {
            let id = key.instance;
            let node = self
                .graph(id)
                .state(usize::from(key.raw))
                .expect("movement modifier state");
            for (slot, config) in array_field(&node.fields, "m_BC5E91CF").iter().enumerate() {
                self.spend("movement modifier")?;
                let shape = config.get("$").and_then(Json::as_str);
                let (operation, default) = match shape {
                    Some("STU_4CB0950F") => (Operation::Add, 0.0),
                    Some("STU_D8CC6B10") => (Operation::Multiply, 1.0),
                    Some("STU_56DEE1BB") => (Operation::Set, 0.0),
                    Some("STU_C61711C8") => (Operation::Minimum, f64::from(f32::MAX)),
                    Some("STU_F197CFBF") => (Operation::Maximum, f64::from(f32::MIN_POSITIVE)),
                    _ => {
                        self.report(
                            self.site(id, node),
                            Issue::UnsupportedField("movement modifier operation"),
                        );
                        continue;
                    }
                };
                let Some(index) = config
                    .get("m_A9561CA0")
                    .and_then(Json::as_u64)
                    .and_then(|value| u8::try_from(value).ok())
                else {
                    self.report(
                        self.site(id, node),
                        Issue::UnsupportedField("movement modifier index"),
                    );
                    continue;
                };
                let Some(value) =
                    self.movement_number(id, node, field(config, "m_9B7A63EA"), default)
                else {
                    continue;
                };
                let Some(priority) =
                    self.movement_number(id, node, field(config, "m_priority"), 0.0)
                else {
                    continue;
                };
                modifiers.insert(
                    ModifierKey {
                        instance: id,
                        state: key.raw,
                        slot,
                    },
                    Modifier {
                        index,
                        operation,
                        value,
                        priority,
                    },
                );
            }
        }
        Ok(modifiers)
    }

    fn movement_number(
        &mut self,
        id: u16,
        node: &Node,
        config: &Json,
        default: f64,
    ) -> Option<f64> {
        if config.is_null() {
            return Some(default);
        }
        let value = self.evaluate(id, node, config, None)?;
        Some(match value {
            Value::Bool(_) | Value::Integer(_) | Value::Float(_) => {
                f64::from(script_expr::to_float(&value) as f32)
            }
            _ => default,
        })
    }

    fn register_tick(&mut self, key: StateKey, start_ms: u64) {
        self.machine
            .ticking
            .entry(key.instance)
            .or_default()
            .entry(key.raw)
            .or_insert(start_ms);
    }

    fn tick_states(&mut self, id: u16) -> Result<(), InitFault> {
        let now = self.machine.now_ms;
        let due: Vec<_> = self
            .machine
            .ticking
            .get(&id)
            .into_iter()
            .flatten()
            .filter_map(|(&raw, &last)| (last != now).then_some(raw))
            .collect();
        for raw in due {
            self.spend("polled state tick")?;
            if let Some(last) = self
                .machine
                .ticking
                .get_mut(&id)
                .and_then(|states| states.get_mut(&raw))
            {
                *last = now;
                let key = StateKey { instance: id, raw };
                match self
                    .state(key)
                    .filter(|state| state.active)
                    .map(|state| state.kind)
                {
                    Some(StateKind::BooleanSwitch(_)) => {
                        self.check_switch(key, 0)?;
                    }
                    Some(StateKind::Ability { .. }) => self.activate_ability(key, 0)?,
                    Some(StateKind::WaitFrames { target }) if target <= self.machine.frame => {
                        self.post(id, Event::Finish(raw))?;
                    }
                    _ => (),
                }
            }
        }
        Ok(())
    }

    fn clear_tick(&mut self, key: StateKey) {
        if let Some(states) = self.machine.ticking.get_mut(&key.instance) {
            states.shift_remove(&key.raw);
            if states.is_empty() {
                self.machine.ticking.remove(&key.instance);
            }
        }
    }

    fn arm_wait(&mut self, key: StateKey, node: &Node) -> Result<(), InitFault> {
        let id = key.instance;
        let Some(seconds) = self.evaluate(id, node, field(&node.fields, "m_timeout"), Some(key))
        else {
            return Ok(());
        };
        let milliseconds = ((script_expr::to_float(&seconds) * 1000.0) as f32).round();
        if !milliseconds.is_finite() {
            self.report(self.site(id, node), Issue::UnsupportedField("Wait timeout"));
            return Ok(());
        }
        if milliseconds <= 0.0 {
            self.post(id, Event::Finish(key.raw))
        } else {
            self.post_after(id, milliseconds as u64, Event::Timer(key.raw, 1))
        }
    }

    fn arm_frame_wait(&mut self, key: StateKey, node: &Node) -> Result<(), InitFault> {
        let id = key.instance;
        let Some(value) = self.evaluate(id, node, field(&node.fields, "m_3016B9A1"), None) else {
            return Ok(());
        };
        let count = script_expr::to_int(&value, 0) as u32;
        let target = self.machine.frame.wrapping_add(count);
        self.state_mut(key).expect("active frame wait").kind = StateKind::WaitFrames { target };
        self.register_tick(key, self.machine.now_ms);
        if target <= self.machine.frame {
            self.post(id, Event::Finish(key.raw))
        } else {
            Ok(())
        }
    }

    fn timer(&mut self, key: StateKey, parameter: u16) -> Result<(), InitFault> {
        let Some(state) = self.state(key).filter(|state| state.active) else {
            return Ok(());
        };
        let kind = state.kind;
        if kind == StateKind::Wait && parameter == 1 {
            self.post(key.instance, Event::Finish(key.raw))?;
        }
        let node = self
            .graph(key.instance)
            .state(usize::from(key.raw))
            .expect("active timer state");
        if matches!(kind, StateKind::LogicalButton { .. })
            && parameter == 1
            && !self.button_held(key, node)
        {
            self.set_button_down(key, node, false, 0)?;
        }
        self.try_complete(key, node, 0)
    }

    fn spawn_child(&mut self, key: StateKey, node: &Node) -> Result<Option<u16>, InitFault> {
        let id = key.instance;
        let site = self.site(id, node);
        let Some(guid) = node
            .field("m_36F7B64C.m_36F7B64C.m_C974E369.m_graph")
            .and_then(asset_guid)
        else {
            self.report(site, Issue::InvalidSubScript);
            return Ok(None);
        };
        let graph = u32::from(guid as u16);
        if self.graphs.graph(graph).is_none() {
            self.report(site, Issue::MissingGraph(graph));
            return Ok(None);
        }
        let mut vars = Variables::new();
        let mut entity_vars = Variables::new();
        for item in array_field(&node.fields, "m_89402D74") {
            if !item.is_object() {
                continue;
            }
            let target = match script_expr::lvalue(field(item, "m_out_Var")) {
                Ok(Some(target)) => target,
                Ok(None) => continue,
                Err(error) => {
                    self.report(site, Issue::Expression(error));
                    return Ok(None);
                }
            };
            let Some(value) = self.evaluate(id, node, field(item, "m_value"), None) else {
                return Ok(None);
            };
            match target.scope {
                Scope::Instance => vars.insert(target.id, value),
                Scope::Entity => entity_vars.insert(target.id, value),
            };
        }
        let child =
            u16::try_from(self.machine.next_id).map_err(|_| InitFault::InstanceIdExhausted)?;
        if self.create(child, graph, Some((id, key.raw)), vars)? {
            // The reference sets child binding bases directly before Entry.
            self.machine.snapshot.variables.extend(entity_vars);
            return Ok(Some(child));
        }
        Ok(None)
    }

    fn try_complete(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        for (index, _) in array_field(&node.fields, "m_transitionPlug")
            .iter()
            .enumerate()
        {
            if self.follow(
                key.instance,
                node,
                &format!("m_transitionPlug[{index}]"),
                Some(key),
                depth,
            )? {
                self.post(key.instance, Event::Abort(key.raw))?;
                break;
            }
        }
        Ok(())
    }

    fn changed(&mut self, key: StateKey) -> Result<(), InitFault> {
        if let Some(state) = self.state_mut(key) {
            state.pending_change = false;
            if state.active
                && !state.pending_timer
                && matches!(state.kind, StateKind::BooleanSwitch(_))
            {
                state.pending_timer = true;
                self.post(key.instance, Event::Reevaluate(key.raw))?;
            } else if state.active && matches!(state.kind, StateKind::Stack { .. }) {
                self.update_stack(key)?;
            }
        }
        Ok(())
    }

    fn reevaluate(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(state) = self.state_mut(key).filter(|state| state.active) else {
            return Ok(());
        };
        state.pending_timer = false;
        if self.check_switch(key, depth)? {
            let node = self
                .graph(key.instance)
                .state(usize::from(key.raw))
                .expect("switch state");
            self.try_complete(key, node, depth)?;
        }
        Ok(())
    }

    fn check_switch(&mut self, key: StateKey, depth: usize) -> Result<bool, InitFault> {
        let Some(StateKind::BooleanSwitch(current)) = self
            .state(key)
            .filter(|state| state.active)
            .map(|state| state.kind)
        else {
            return Ok(false);
        };
        let id = key.instance;
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("active state node");
        if let Some(value) = self.evaluate(id, node, field(&node.fields, "m_condition"), Some(key))
        {
            let next = truthy(&value);
            if next != current {
                self.exit_subgraph(id, node, branch_path(current), depth + 1)?;
                self.state_mut(key).expect("active switch").kind = StateKind::BooleanSwitch(next);
                self.enter_branch(key, node, next, depth)?;
            }
            return Ok(true);
        }
        Ok(false)
    }

    fn enter_branch(
        &mut self,
        key: StateKey,
        node: &Node,
        branch: bool,
        depth: usize,
    ) -> Result<(), InitFault> {
        let output = if branch {
            "m_onTruePlug"
        } else {
            "m_onFalsePlug"
        };
        self.follow(key.instance, node, output, Some(key), depth)?;
        self.follow(key.instance, node, branch_path(branch), Some(key), depth)?;
        Ok(())
    }

    fn end(&mut self, key: StateKey, finished: bool, depth: usize) -> Result<(), InitFault> {
        if depth >= MAX_PLUG_DEPTH {
            return Err(InitFault::PlugDepth);
        }
        let Some(state) = self
            .state_mut(key)
            .filter(|state| state.active && !state.ending)
        else {
            return Ok(());
        };
        state.ending = true;
        let kind = state.kind;
        let id = key.instance;
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("active state node");
        self.machine
            .queues
            .retain(id, |_, event| event.state() != Some(key.raw));
        self.clear_tick(key);
        self.exit_subgraph(id, node, "m_subgraphPlug", depth + 1)?;
        match kind {
            StateKind::BooleanSwitch(branch) => {
                self.exit_subgraph(id, node, branch_path(branch), depth + 1)?
            }
            StateKind::SubScript { child: Some(child) } => self.destroy(child)?,
            StateKind::MessageIdle => {
                self.exit_subgraph(id, node, "m_33DA5163", depth + 1)?;
                self.exit_subgraph(id, node, "m_452862E6", depth + 1)?;
            }
            StateKind::AiCandidate { selected: true } => {
                self.exit_subgraph(id, node, "m_6F92F4BD", depth + 1)?;
            }
            StateKind::LogicalButton { .. } => {
                self.exit_subgraph(id, node, "m_80AF45FB", depth + 1)?;
                self.exit_subgraph(id, node, "m_1C9EF058", depth + 1)?;
            }
            StateKind::Stack { .. } => {
                self.exit_subgraph(id, node, "m_topSubgraphPlug", depth + 1)?;
                self.exit_subgraph(id, node, "m_underSubgraphPlug", depth + 1)?;
                self.unlink_stack(key, depth + 1)?;
            }
            StateKind::Ability { flags } => {
                self.set_ability_flags(key, flags & !ABILITY_HELD);
                self.stop_ability(key, node, depth + 1)?;
            }
            _ => (),
        }
        let state = self.state_mut(key).expect("ending state");
        state.active = false;
        state.ending = false;
        state.pending_change = false;
        state.pending_timer = false;
        if matches!(kind, StateKind::SubScript { .. }) {
            state.kind = StateKind::SubScript { child: None };
        }
        if matches!(kind, StateKind::LogicalButton { .. }) {
            state.kind = StateKind::LogicalButton { down: false };
        }
        if matches!(kind, StateKind::Stack { .. }) {
            state.kind = StateKind::Stack {
                top: false,
                under: false,
            };
        }
        self.machine.watchers.retain(|_, watchers| {
            watchers.retain(|watcher| *watcher != key);
            !watchers.is_empty()
        });
        let ending = if finished {
            "m_onFinishedPlug"
        } else {
            "m_onAbortPlug"
        };
        self.follow(id, node, ending, Some(key), depth)?;
        self.follow(id, node, "m_onEndPlug", Some(key), depth)?;
        Ok(())
    }

    fn exit_subgraph(
        &mut self,
        id: u16,
        node: &Node,
        path: &str,
        depth: usize,
    ) -> Result<(), InitFault> {
        let Some(plug) = node.field(path) else {
            return Ok(());
        };
        for raw in raw_indices(array_field(plug, SUBGRAPH_STATES)) {
            self.end(StateKey { instance: id, raw }, false, depth)?;
            self.drop_begin(id, raw);
        }
        Ok(())
    }

    fn drop_begin(&mut self, id: u16, raw: u16) {
        let now = self.machine.now_ms;
        self.machine.queues.retain(id, |due, event| {
            due != now || !matches!(event, Event::Begin(state) if *state == raw)
        });
    }

    fn destroy(&mut self, id: u16) -> Result<(), InitFault> {
        let mut pending = vec![(id, false)];
        while let Some((id, children_done)) = pending.pop() {
            if !children_done {
                self.spend("instance teardown")?;
                for state in self.machine.snapshot.instances[&id].states.values_mut() {
                    state.ending = true;
                }
                pending.push((id, true));
                pending.extend(self.machine.snapshot.instances.iter().rev().filter_map(
                    |(&child, instance)| {
                        instance
                            .parent
                            .is_some_and(|(parent, _)| parent == id)
                            .then_some((child, false))
                    },
                ));
                continue;
            }
            let linked: Vec<_> = self.machine.snapshot.instances[&id]
                .states
                .iter()
                .filter_map(|(&raw, state)| state.stack_output.is_some().then_some(raw))
                .collect();
            for raw in linked {
                self.unlink_stack(StateKey { instance: id, raw }, 0)?;
            }
            self.machine.snapshot.instances.shift_remove(&id);
            self.machine.queues.remove(id);
            self.machine.ticking.remove(&id);
            self.machine
                .ability_presses
                .retain(|key, _| key.instance != id);
            self.machine.watchers.retain(|key, watchers| {
                if matches!(key, VarKey::Instance(instance, _) if *instance == id) {
                    return false;
                }
                watchers.retain(|watcher| watcher.instance != id);
                !watchers.is_empty()
            });
        }
        Ok(())
    }
}

fn is_entry(node: &Node) -> bool {
    matches!(node.class.as_str(), "STUStatescriptEntry" | "STU_571CC72E")
}

fn is_message_entry(node: &Node) -> bool {
    node.class == "STUStatescriptEntryGameMessage"
}

fn supported_ability(node: &Node) -> bool {
    field(&node.fields, "m_out_CooldownVar").is_null()
        && field(&node.fields, "m_5E561E77").is_null()
}

fn configured(node: &Node, name: &str) -> bool {
    match field(&node.fields, name) {
        Json::Null | Json::Bool(false) => false,
        Json::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Json::String(value) => !value.is_empty(),
        Json::Array(value) => !value.is_empty(),
        Json::Object(value) => !value.is_empty(),
        Json::Bool(true) => true,
    }
}

fn field<'a>(fields: &'a Json, name: &str) -> &'a Json {
    fields.get(name).unwrap_or(&Json::Null)
}

fn array_field<'a>(fields: &'a Json, name: &str) -> &'a [Json] {
    field(fields, name).as_array().map_or(&[], Vec::as_slice)
}

fn raw_indices(values: &[Json]) -> impl Iterator<Item = u16> + '_ {
    values
        .iter()
        .filter_map(|value| u16::try_from(value.as_u64()?).ok())
}

fn branch_path(branch: bool) -> &'static str {
    if branch {
        "m_trueSubgraphPlug"
    } else {
        "m_falseSubgraphPlug"
    }
}

fn same(a: &Value, b: &Value) -> bool {
    a == b || matches!((a, b), (Value::Float(a), Value::Float(b)) if a.is_nan() && b.is_nan())
}

fn source_key(instance: u16, variable: Variable) -> VarKey {
    match variable.scope {
        Scope::Instance => VarKey::Instance(instance, variable.id),
        Scope::Entity => VarKey::Entity(variable.id),
    }
}

fn stored(snapshot: &Snapshot, key: VarKey) -> Value {
    match key {
        VarKey::Entity(id) => snapshot.variables.get(&id),
        VarKey::Instance(instance, id) => snapshot
            .instances
            .get(&instance)
            .and_then(|instance| instance.variables.get(&id)),
    }
    .cloned()
    .unwrap_or(Value::Null)
}

struct InstanceContext<'a, W> {
    machine: &'a Machine,
    world: &'a mut W,
    site: Site,
    reads: Vec<VarKey>,
    parameters: Option<&'a Variables>,
    watcher: Option<StateKey>,
    read_native: bool,
}

impl<W: World> InstanceContext<'_, W> {
    fn read(&mut self, key: VarKey) -> Value {
        if !self.reads.contains(&key) {
            self.reads.push(key);
        }
        self.machine.value(key, self.watcher)
    }
}

impl<W: World> Context for InstanceContext<'_, W> {
    fn variable(&mut self, variable: Variable) -> Value {
        self.read(source_key(self.site.instance, variable))
    }

    fn parent_variable(&mut self, levels: u8, variable: Variable) -> Value {
        let mut id = self.site.instance;
        for _ in 0..levels {
            let Some((parent, _)) = self.machine.snapshot.instances[&id].parent else {
                return Value::Null;
            };
            id = parent;
        }
        self.read(source_key(id, variable))
    }

    fn parent_entity(&mut self, _levels: u8) -> Value {
        self.owner()
    }

    fn parameter(&mut self, id: u32) -> Value {
        self.parameters
            .and_then(|parameters| parameters.get(&id))
            .cloned()
            .unwrap_or(Value::Null)
    }

    fn owner(&mut self) -> Value {
        Value::Entity(self.site.entity)
    }

    fn entity_member(&mut self, entity: u32, id: u32) -> Value {
        if entity == self.site.entity {
            self.read(VarKey::Entity(id))
        } else {
            self.world.entity_variable(entity, id)
        }
    }

    fn ruleset(&mut self, id: u32) -> Value {
        match self.read(VarKey::Entity(id)) {
            Value::Null => self.world.ruleset(id).unwrap_or(Value::Integer(0)),
            value => value,
        }
    }

    fn position(&mut self, entity: u32) -> [f64; 3] {
        self.world.position(entity)
    }

    fn native(&mut self, config: &Json) -> Option<Value> {
        self.read_native = true;
        self.world.native(self.site, config)
    }

    fn would_stack_to_top(&mut self, variable: Variable, priority: StackPriority) -> Option<bool> {
        let key = source_key(self.site.instance, variable);
        self.read(key);
        Some(self.machine.allows_stack(key, priority, None))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    use serde_json::json;

    fn node(position: u16, class: &str, state: Option<u16>, mut fields: Json) -> Json {
        fields["m_2BBEEAB8"] = json!(state.is_some());
        fields["m_ADEB6E05"] = json!(class.contains("Action") || class == "STU_12F5D52D");
        json!({
            "pos":position, "class":class, "uid":position, "rid":position, "remote":-1,
            "state":state, "client_only":false, "server_only":false, "fields":fields,
        })
    }

    fn graph(index: u32, nodes: Vec<Json>, states: Json, entries: Json) -> Json {
        json!({
            "index":index, "items":nodes.len(), "nodes":nodes, "states":states,
            "entries":entries, "remote":[], "sync_vars":[], "containers":{},
            "unknown_nodes":[], "fields":{},
            "header":{
                "m_nodesBitCount":4, "m_statesBitCount":4, "m_remoteSyncNodesBitCount":4,
                "m_syncVarsBitCount":4
            }
        })
    }

    fn store(graphs: Vec<Json>) -> GraphStore {
        GraphStore::from_json(
            &serde_json::to_vec(&json!({
                "version":1, "graphs":graphs, "bodies":{}, "missing":[]
            }))
            .unwrap(),
        )
        .unwrap()
    }

    fn plug(links: &[(u16, &str)], states: &[u16]) -> Json {
        json!({"$":"Plug","links":links,"m_FF3DAF1E":states})
    }

    fn var(id: u32, entity: bool) -> Json {
        json!({"$":"STUConfigVarDynamic","m_identifier":id,"m_60DB8F99":entity})
    }

    fn parent_var(id: u32) -> Json {
        json!({"$":"STU_919FD47C","m_expression":{
            "m_opcodes":[10,1,0,0], "m_dynamicVars":[var(id, false)]
        }})
    }

    #[test]
    fn startup_queues_states_rechecks_scoped_writes_and_starts_children_after_roots() {
        let mut client_only = node(6, "UnportedClientAction", None, json!({}));
        client_only["client_only"] = json!(true);
        let first = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":plug(&[(1,"m_beginPlug"),(2,"m_beginPlug")], &[])
                    }),
                ),
                node(
                    1,
                    "STUStatescriptStateBooleanSwitch",
                    Some(0),
                    json!({
                        "m_condition":var(100, true),
                        "m_falseSubgraphPlug":plug(&[(3,"m_beginPlug")], &[2]),
                        "m_trueSubgraphPlug":plug(&[(4,"m_beginPlug")], &[3]),
                    }),
                ),
                node(
                    2,
                    "STUStatescriptState",
                    Some(1),
                    json!({
                        "m_onBeginPlug":plug(&[(5,"m_inPlug"),(6,"m_inPlug")], &[])
                    }),
                ),
                node(3, "STUStatescriptState", Some(2), json!({})),
                node(
                    4,
                    "STU_37D754C8",
                    Some(3),
                    json!({
                        "m_36F7B64C":{"m_36F7B64C":{"m_C974E369":{"m_graph":"0x058000000000002B"}}},
                        "m_89402D74":[{"m_out_Var":var(4, false), "m_value":var(9, false)}]
                    }),
                ),
                node(
                    5,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(100, true), "m_value":{"$":"STUConfigVarBool","m_value":true}
                    }),
                ),
                client_only,
            ],
            json!([1, 2, 3, 4]),
            json!([0]),
        );
        let child = graph(
            43,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_inPlug")], &[])}),
                ),
                node(
                    1,
                    "STU_12F5D52D",
                    None,
                    json!({
                        "m_143D4C5B":[
                            {"m_out_Var":var(12, true),"m_value":parent_var(9)},
                            {"m_out_Var":var(14, true),"m_value":var(4, false)},
                            {"m_out_Var":var(15, true),"m_value":var(13, true)}
                        ]
                    }),
                ),
            ],
            json!([]),
            json!([0]),
        );
        let second = graph(
            44,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_inPlug")], &[])}),
                ),
                node(
                    1,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(13, true), "m_value":{"$":"STUConfigVarInt","m_value":99}
                    }),
                ),
            ],
            json!([]),
            json!([0]),
        );
        let graphs = store(vec![first, child, second]);
        let mut root = Root::new(7, 42);
        root.variables.insert(9, Value::Integer(3));
        let snapshot = initialize(
            &graphs,
            0xa000_0100,
            Variables::from([(100, Value::Bool(false))]),
            vec![root, Root::new(1, 44)],
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        assert_eq!(
            snapshot.instances.keys().copied().collect::<Vec<_>>(),
            [7, 1, 8]
        );
        assert_eq!(snapshot.instances[&8].parent, Some((7, 3)));
        assert_eq!(snapshot.instances[&8].variables[&4], Value::Integer(3));
        let states = &snapshot.instances[&7].states;
        assert_eq!(states[&0].kind, StateKind::BooleanSwitch(true));
        assert!(!states[&2].active);
        assert_eq!(states[&2].activation, 1);
        assert_eq!(states[&3].kind, StateKind::SubScript { child: Some(8) });
        assert_eq!(snapshot.variables[&12], Value::Integer(3));
        assert_eq!(snapshot.variables[&14], Value::Integer(3));
        assert_eq!(snapshot.variables[&15], Value::Integer(99));
    }

    #[test]
    fn unsupported_branches_are_reported_and_cyclic_actions_are_bounded() {
        let diagnostics = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":plug(&[(1,"m_inPlug"),(2,"m_beginPlug")], &[])
                    }),
                ),
                node(
                    1,
                    "UnknownAction",
                    None,
                    json!({"m_outPlug":plug(&[(3,"m_inPlug")], &[])}),
                ),
                node(2, "STUStatescriptState", Some(0), json!({})),
                node(
                    3,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(42, true), "m_value":true
                    }),
                ),
            ],
            json!([2]),
            json!([0]),
        );
        let cycle = graph(
            43,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_inPlug")], &[])}),
                ),
                node(
                    1,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(1, false), "m_value":true, "m_outPlug":plug(&[(1,"m_inPlug")], &[])
                    }),
                ),
            ],
            json!([]),
            json!([0]),
        );
        let graphs = store(vec![diagnostics, cycle]);
        let run = |graph, budget| {
            initialize(
                &graphs,
                42,
                Variables::new(),
                vec![Root::new(1, graph)],
                &mut EmptyWorld,
                budget,
            )
        };
        let snapshot = run(42, MAX_INIT_WORK).unwrap();
        assert!(!snapshot.initialization_complete());
        assert!(snapshot.instances[&1].states[&0].active);
        assert!(!snapshot.variables.contains_key(&42));
        assert_eq!(snapshot.diagnostics[0].site.node, Some(1));
        assert_eq!(
            snapshot.diagnostics[0].issue,
            Issue::UnsupportedAction("UnknownAction".into())
        );
        assert!(matches!(
            run(43, 9),
            Err(InitFault::WorkBudget { limit: 9, .. })
        ));
        assert!(matches!(run(43, MAX_INIT_WORK), Err(InitFault::PlugDepth)));
    }

    #[test]
    fn queued_messages_abort_and_rearm_waits_without_crossing_instance_scope() {
        let message_node = |position, message, input| {
            node(
                position,
                "STUStatescriptEntryGameMessage",
                None,
                json!({
                    "m_gameMessage": {"m_gameMessage":message},
                    "m_params": [{"m_B5051BCE":"0x0A40000000000004","m_out_Var":var(4,false)}],
                    "m_outPlug": plug(&[(1,input)],&[])
                }),
            )
        };
        let timed = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":plug(&[(1,"m_beginPlug"),(5,"m_beginPlug")],&[])
                    }),
                ),
                node(
                    1,
                    "STUStatescriptStateWait",
                    Some(0),
                    json!({
                        "m_timeout":{"$":"STUConfigVarFloat","m_value":0.02},
                        "m_0DBAFD7F":1,
                        "m_onFinishedPlug":plug(&[(2,"m_inPlug")],&[]),
                        "m_onAbortPlug":plug(&[(3,"m_inPlug")],&[]),
                    }),
                ),
                node(
                    2,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(10,false), "m_value": true
                    }),
                ),
                node(
                    3,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(10,false), "m_value": false
                    }),
                ),
                message_node(4, "0x0240000000000001", "m_beginPlug"),
                node(
                    5,
                    "STU_D74D4F47",
                    Some(1),
                    json!({
                        "m_3016B9A1":{"$":"STUConfigVarInt","m_value":1},
                        "m_onFinishedPlug":plug(&[(6,"m_inPlug")],&[]),
                    }),
                ),
                node(
                    6,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(11,false), "m_value": true
                    }),
                ),
                message_node(7, "0x0240000000000002", ABORT_PLUG),
            ],
            json!([1, 5]),
            json!([0, 4, 7]),
        );
        let mut component = Component::start(
            Arc::new(store(vec![timed])),
            42,
            Variables::new(),
            vec![Root::new(7, 42), Root::new(1, 42)],
            1,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let timer = |due_ms| PendingTimer {
            due_ms,
            raw_state: 0,
            finish: false,
            parameter: 1,
        };
        let snapshot = component.snapshot().unwrap();
        assert!(snapshot.initialization_complete());
        assert_eq!(snapshot.pending_timers[&7], [timer(36)]);
        assert_eq!(snapshot.pending_timers[&1], [timer(36)]);
        assert_eq!(
            snapshot.instances[&7].states[&1].kind,
            StateKind::WaitFrames { target: 2 }
        );

        component.advance_frame(1, &mut EmptyWorld).unwrap();
        assert_eq!(component.now_ms(), 31);
        assert_eq!(
            component
                .queue_message(
                    7,
                    0x0240_0000_0000_0001,
                    Variables::from([(4, Value::Integer(15))])
                )
                .unwrap(),
            1
        );
        component.run_now(&mut EmptyWorld).unwrap();
        let snapshot = component.snapshot().unwrap();
        assert_eq!(snapshot.pending_timers[&7], [timer(51)]);
        assert_eq!(snapshot.pending_timers[&1], [timer(36)]);
        assert_eq!(snapshot.instances[&7].variables[&4], Value::Integer(15));
        assert!(!snapshot.instances[&1].variables.contains_key(&4));
        assert_eq!(snapshot.instances[&7].states[&0].activation, 1);
        assert_eq!(snapshot.instances[&7].states[&0].start_ms, 31);
        assert!(matches!(
            component.advance_frame(1, &mut EmptyWorld),
            Err(InitFault::FrameOrder { .. })
        ));
        component.advance_frame(2, &mut EmptyWorld).unwrap();
        assert_eq!(
            component
                .queue_message(7, 0x0240_0000_0000_0002, Variables::new())
                .unwrap(),
            1
        );
        component.run_now(&mut EmptyWorld).unwrap();
        component.advance_frame(3, &mut EmptyWorld).unwrap();
        let snapshot = component.snapshot().unwrap();
        assert!(snapshot.pending_timers.is_empty());
        assert_eq!(snapshot.instances[&7].variables[&10], Value::Bool(false));
        assert_eq!(snapshot.instances[&1].variables[&10], Value::Bool(true));
        assert_eq!(snapshot.instances[&7].variables[&11], Value::Bool(true));
        assert_eq!(snapshot.instances[&1].variables[&11], Value::Bool(true));

        for _ in 0..=MAX_EVENTS_PER_UPDATE {
            component
                .queue_message(7, 0x0240_0000_0000_0001, Variables::new())
                .unwrap();
        }
        assert_eq!(
            component.run_now(&mut EmptyWorld),
            Err(InitFault::EventBudget { instance: 7 })
        );
        assert!(component.snapshot().is_none());
        assert_eq!(component.run_now(&mut EmptyWorld), Err(InitFault::Halted));
    }

    struct ReferenceWorld;

    impl World for ReferenceWorld {
        fn native(&mut self, site: Site, config: &Json) -> Option<Value> {
            // Values observed in the Python fixtures, for this test only.
            match (site.graph, config.get("$")?.as_str()?) {
                (0x0c18, "STU_AC829876") => Some(Value::Bool(true)),
                (0x0c18, "STU_4A101787" | "STU_A77AC2AC") => Some(Value::Null),
                (0x288a, "STU_37F20D24") => Some(Value::Null),
                (0x0b92, "STU_71A87E92" | "STU_2200A66D") if matches!(site.node, Some(7 | 11)) => {
                    Some(Value::Null)
                }
                _ => None,
            }
        }

        fn selects_ai_behavior(&mut self, site: Site) -> Option<bool> {
            (site.graph == 0x0b92 && site.node == Some(1)).then_some(false)
        }
    }

    #[test]
    fn ordered_stack_entries_use_the_value_below_and_restore_the_hidden_base() {
        let message = |position, message, target| {
            node(
                position,
                "STUStatescriptEntryGameMessage",
                None,
                json!({
                    "m_gameMessage":{"m_gameMessage":message},
                    "m_outPlug":plug(&[(target,ABORT_PLUG)],&[])
                }),
            )
        };
        let expression = json!({"$":"STU_919FD47C","m_expression":{
            "m_opcodes":[8,0,6,0,23,0],
            "m_dynamicVars":[var(100,true)],"m_D99EF254":[1]
        }});
        let fixture = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_beginPlug"),(2,"m_beginPlug")],&[])}),
                ),
                node(
                    1,
                    "STUStatescriptStateStack",
                    Some(0),
                    json!({
                        "m_out_Var":var(100,true), "m_value":10,
                        "m_priority":{"m_priority":"0x0C40000000000001"}
                    }),
                ),
                node(
                    2,
                    "STUStatescriptStateStack",
                    Some(1),
                    json!({
                        "m_out_Var":var(100,true), "m_value":expression
                    }),
                ),
                message(3, "0x0240000000000001", 1),
                message(4, "0x0240000000000002", 2),
            ],
            json!([1, 2]),
            json!([0, 3, 4]),
        );
        let mut component = Component::start(
            Arc::new(store(vec![fixture])),
            42,
            Variables::from([(100, Value::Integer(5))]),
            vec![Root::new(1, 42)],
            0,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let variable = Variable {
            scope: Scope::Entity,
            id: 100,
        };
        assert_eq!(
            component.resolved_variable(1, variable),
            Some(Value::Float(11.0))
        );
        let snapshot = component.snapshot().unwrap();
        assert_eq!(snapshot.variables[&100], Value::Integer(5));
        assert_eq!(
            snapshot.bindings[&(0, 100)],
            [
                StackBinding {
                    instance: 1,
                    raw_state: 0
                },
                StackBinding {
                    instance: 1,
                    raw_state: 1
                }
            ]
        );
        assert_eq!(
            snapshot.instances[&1].states[&0].kind,
            StateKind::Stack {
                top: false,
                under: true
            }
        );
        assert_eq!(
            snapshot.instances[&1].states[&1].kind,
            StateKind::Stack {
                top: true,
                under: false
            }
        );
        component
            .set_entity_variable(100, Value::Integer(7), &mut EmptyWorld)
            .unwrap();
        component.run_now(&mut EmptyWorld).unwrap();
        assert_eq!(
            component.resolved_variable(1, variable),
            Some(Value::Float(11.0))
        );
        for (message, expected) in [(1, Value::Float(8.0)), (2, Value::Integer(7))] {
            component
                .queue_message(1, 0x0240_0000_0000_0000 | message, Variables::new())
                .unwrap();
            component.run_now(&mut EmptyWorld).unwrap();
            assert_eq!(component.resolved_variable(1, variable), Some(expected));
        }
        assert!(component.snapshot().unwrap().bindings.is_empty());
    }

    #[derive(Default)]
    struct SprintWorld {
        forward: i8,
    }

    impl World for SprintWorld {
        fn native(&mut self, site: Site, config: &Json) -> Option<Value> {
            if site.graph != 0x0255 {
                return None;
            }
            match config.get("$")?.as_str()? {
                "STU_D3EB8D87" | "STU_A77AC2AC" => Some(Value::Bool(false)),
                "STU_310D6529" => {
                    let unit = (1.0_f64 / 127.0) as f32;
                    let forward = f64::from(f32::from(self.forward) * unit);
                    Some(Value::Vector([0.0, forward, 0.0]))
                }
                _ => None,
            }
        }
    }

    #[test]
    #[ignore = "requires a local 1.74 graph pack"]
    fn authored_sprint_stack_gates_track_input_and_reveal_the_base_on_release() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            Arc::new(GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap());
        let mut world = SprintWorld::default();
        let mut component = Component::start(
            graphs,
            0xa000_0101,
            Variables::new(),
            vec![Root::new(5, 0x0255)],
            1029,
            &mut world,
            MAX_INIT_WORK,
        )
        .unwrap();
        let var = |component: &Component, id| {
            component.resolved_variable(
                5,
                Variable {
                    scope: Scope::Instance,
                    id,
                },
            )
        };
        assert_eq!(var(&component, 9570), Some(Value::Bool(true)));
        for (frame, forward, buttons, throttle, held) in [
            (1030, 63, 0, Value::Null, Value::Null),
            (1031, 64, 0, Value::Bool(true), Value::Null),
            (1032, 127, 0x881, Value::Bool(true), Value::Bool(true)),
            (1033, 0, 0, Value::Null, Value::Null),
        ] {
            world.forward = forward;
            component
                .advance_command(
                    Command {
                        frame,
                        forward,
                        buttons,
                        ..Command::default()
                    },
                    &mut world,
                )
                .unwrap();
            assert_eq!(var(&component, 2333), Some(throttle));
            assert_eq!(var(&component, 7681), Some(held));
        }
        component
            .set_entity_variable(9535, Value::Bool(false), &mut world)
            .unwrap();
        component.run_now(&mut world).unwrap();
        assert_eq!(var(&component, 9570), Some(Value::Null));
        assert!(
            !component
                .snapshot()
                .unwrap()
                .bindings
                .contains_key(&(5, 9570))
        );
    }

    #[test]
    #[ignore = "requires a local 1.74 graph pack"]
    fn isolated_soldier_sprint_ability_matches_the_authored_toggle_and_modifier_states() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            Arc::new(GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap());
        let mut world = SprintWorld::default();
        let mut component = Component::start(
            graphs,
            0xa000_0101,
            Variables::new(),
            vec![Root::new(5, 0x0255)],
            1029,
            &mut world,
            MAX_INIT_WORK,
        )
        .unwrap();
        let rows = [
            (1030, 0, 8, 0, None),
            (1031, 63, 8, 0, None),
            (1032, 64, 8, 0, None),
            (1033, 64, 8, 5, Some(1)),
            (1034, 127, 0, 1, Some(1)),
            (1035, 127, 8, 4, Some(0)),
            (1036, 127, 8, 4, Some(0)),
            (1037, 127, 0, 0, Some(0)),
            (1038, 127, 8, 5, Some(1)),
            (1039, 63, 0, 0, Some(0)),
            (1040, 127, 0, 0, Some(0)),
            (1041, 127, 9, 0, Some(0)),
        ];
        for (frame, forward, buttons, flags, hud) in rows {
            world.forward = forward;
            component
                .advance_command(
                    Command {
                        frame,
                        forward,
                        buttons,
                        ..Command::default()
                    },
                    &mut world,
                )
                .unwrap();
            let snapshot = component.snapshot().unwrap();
            let instance = &snapshot.instances[&5];
            assert_eq!(
                instance.states[&1].kind,
                StateKind::Ability { flags },
                "frame {frame}"
            );
            assert_eq!(
                instance.variables.get(&699).cloned(),
                hud.map(Value::Integer),
                "frame {frame}"
            );
            for raw in [0, 20] {
                assert_eq!(
                    instance.states.get(&raw).is_some_and(|state| state.active),
                    flags & 1 != 0,
                    "frame {frame}, state {raw}"
                );
            }
            let projected = Vm::new(&component.graphs, &mut world, &mut component.machine)
                .collect_movement_modifiers()
                .unwrap();
            let modifiers: Vec<_> = projected
                .iter()
                .map(|(key, modifier)| {
                    (
                        key.state,
                        modifier.index,
                        modifier.operation,
                        modifier.value,
                        modifier.priority,
                    )
                })
                .collect();
            if flags & 1 == 0 {
                assert!(modifiers.is_empty());
            } else {
                assert_eq!(
                    modifiers,
                    [
                        (0, 0, Operation::Add, 0.5, 0.0),
                        (20, 21, Operation::Set, 0.0, 0.0)
                    ]
                );
            }
        }
        assert!(!component.snapshot().unwrap().initialization_complete());
        assert!(component.movement_modifiers(&mut world).unwrap().is_none());
    }

    fn active(snapshot: &Snapshot, id: u16) -> Vec<u16> {
        snapshot.instances[&id]
            .states
            .iter()
            .filter_map(|(&raw, state)| state.active.then_some(raw))
            .collect()
    }

    #[test]
    fn buffered_action_press_uses_the_shared_clock_and_releases_after_one_millisecond() {
        let mut fixture = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntryGameMessage",
                    None,
                    json!({
                        "m_gameMessage":{"m_gameMessage":"0x0240000000000001"},
                        "m_outPlug":plug(&[(1,"m_beginPlug")],&[]),
                    }),
                ),
                node(
                    1,
                    "STUStatescriptStateLogicalButton",
                    Some(0),
                    json!({
                        "m_logicalButton":{"$":"STUConfigVarLogicalButton","m_logicalButton":15},
                        "m_246438AD":{"$":"STUConfigVarFloat","m_value":0.03},
                        "m_onGoingDownPlug":plug(&[(2,"m_inPlug")],&[]),
                        "m_onComingUpPlug":plug(&[(3,"m_inPlug")],&[]),
                    }),
                ),
                node(
                    2,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(10,false), "m_value": true,
                    }),
                ),
                node(
                    3,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({
                        "m_out_Var":var(10,false), "m_value": false,
                    }),
                ),
            ],
            json!([1]),
            json!([0]),
        );
        fixture["fields"]["m_680A2CB2"] = json!([0]);
        let graphs = Arc::new(store(vec![fixture]));
        for (dead, delay) in [(false, 0), (false, 2), (true, 0)] {
            let mut component = Component::start(
                Arc::clone(&graphs),
                42,
                Variables::new(),
                vec![Root::new(1, 42)],
                100,
                &mut EmptyWorld,
                MAX_INIT_WORK,
            )
            .unwrap();
            component.set_owner_dead(dead);
            component
                .advance_command(
                    Command {
                        frame: 100,
                        action: 1,
                        ..Command::default()
                    },
                    &mut EmptyWorld,
                )
                .unwrap();
            if delay != 0 {
                component
                    .advance_frame(100 + delay, &mut EmptyWorld)
                    .unwrap();
            }
            let buffered = !dead && delay == 0;
            assert_eq!(
                component
                    .queue_message(1, 0x0240_0000_0000_0001, Variables::new())
                    .unwrap(),
                1
            );
            component.run_now(&mut EmptyWorld).unwrap();
            let snapshot = component.snapshot().unwrap();
            let instance = &snapshot.instances[&1];
            assert_eq!(instance.states[&0].activation, 1);
            assert_eq!(
                instance.states[&0].kind,
                StateKind::LogicalButton { down: buffered }
            );
            if buffered {
                assert_eq!(instance.variables[&10], Value::Bool(true));
                assert_eq!(
                    snapshot.pending_timers[&1],
                    [PendingTimer {
                        due_ms: 1616,
                        raw_state: 0,
                        finish: false,
                        parameter: 1
                    }]
                );
                component.advance_frame(101, &mut EmptyWorld).unwrap();
                let snapshot = component.snapshot().unwrap();
                assert_eq!(
                    snapshot.instances[&1].states[&0].kind,
                    StateKind::LogicalButton { down: false }
                );
                assert_eq!(snapshot.instances[&1].variables[&10], Value::Bool(false));
                assert!(snapshot.pending_timers.is_empty());
            } else {
                assert!(instance.variables.get(&10).is_none());
                assert!(snapshot.pending_timers.is_empty());
            }
        }
    }

    #[test]
    #[ignore = "requires a local 1.74 graph pack"]
    fn soldier_sprint_graph_reads_primary_crouch_and_melee_edges_as_in_python() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            Arc::new(GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap());
        let mut component = Component::start(
            graphs,
            0xa000_0101,
            Variables::new(),
            vec![Root::new(5, 0x0255)],
            1029,
            &mut ReferenceWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let sprint = |snapshot: &Snapshot| {
            [4, 7, 16].map(|raw| {
                (
                    snapshot.instances[&5].states[&raw].kind,
                    snapshot.instances[&5].states[&raw].activation,
                )
            })
        };
        let up = StateKind::LogicalButton { down: false };
        let down = StateKind::LogicalButton { down: true };
        assert_eq!(sprint(component.snapshot().unwrap()), [(up, 1); 3]);
        component
            .advance_command(
                Command {
                    frame: 1030,
                    buttons: 0x881,
                    ..Command::default()
                },
                &mut ReferenceWorld,
            )
            .unwrap();
        let snapshot = component.snapshot().unwrap();
        assert_eq!(sprint(snapshot), [(down, 1); 3]);
        assert_eq!(snapshot.instances[&5].variables[&1919], Value::Bool(true));
        assert!(!snapshot.initialization_complete());
        component
            .advance_command(
                Command {
                    frame: 1031,
                    ..Command::default()
                },
                &mut ReferenceWorld,
            )
            .unwrap();
        assert_eq!(sprint(component.snapshot().unwrap()), [(up, 1); 3]);
    }

    #[test]
    #[ignore = "requires a local 1.74 Practice Range NPC graph pack with graph 0B92"]
    fn firing_placement_reset_and_timer_boundaries_match_python() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            Arc::new(GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap());
        assert!(
            graphs.graph(0x0b92).is_some(),
            "the runtime pack lacks Practice Range graph 0B92"
        );
        let mut component = Component::start(
            graphs,
            0xa100_0000,
            Variables::new(),
            vec![Root::new(1, 0x0b92)],
            100,
            &mut ReferenceWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        // Python BotWorld with no selected AI target, frame 100, scoped RESET.
        assert_eq!(
            component
                .queue_message(1, 0x0240_0000_0000_0013, Variables::new())
                .unwrap(),
            1
        );
        component.run_now(&mut ReferenceWorld).unwrap();
        let snapshot = component.snapshot().unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        assert_eq!(active(snapshot, 1), [5, 12]);
        assert_eq!(snapshot.instances[&1].variables[&889], Value::Bool(false));
        assert_eq!(
            snapshot.pending_timers[&1],
            [
                PendingTimer {
                    due_ms: 2100,
                    raw_state: 5,
                    finish: false,
                    parameter: 1
                },
                PendingTimer {
                    due_ms: 4350,
                    raw_state: 12,
                    finish: false,
                    parameter: 1
                },
            ]
        );
        for frame in 101..=130 {
            component.advance_frame(frame, &mut ReferenceWorld).unwrap();
        }
        assert_eq!(component.now_ms(), 2095);
        assert_eq!(active(component.snapshot().unwrap(), 1), [5, 12]);
        component.advance_frame(131, &mut ReferenceWorld).unwrap();
        let snapshot = component.snapshot().unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        assert_eq!(active(snapshot, 1), [12, 0]);
        assert_eq!(
            snapshot.instances[&1].states[&0].kind,
            StateKind::AiCandidate { selected: false }
        );
        assert_eq!(snapshot.instances[&1].states[&0].start_ms, 2100);
        assert_eq!(snapshot.instances[&1].variables[&889], Value::Bool(false));
        for frame in 132..=270 {
            component.advance_frame(frame, &mut ReferenceWorld).unwrap();
        }
        assert_eq!(component.now_ms(), 4335);
        assert_eq!(active(component.snapshot().unwrap(), 1), [12, 0]);
        component.advance_frame(271, &mut ReferenceWorld).unwrap();
        let snapshot = component.snapshot().unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        assert_eq!(active(snapshot, 1), [0]);
        assert_eq!(snapshot.instances[&1].variables[&889], Value::Bool(true));
        assert!(snapshot.pending_timers.is_empty());
    }

    #[test]
    #[ignore = "requires an authorized 104319 graph pack in ORPHEUS_RUNTIME_DIR"]
    fn controller_and_training_bot_supported_startup_edges_match_python() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs = GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap();
        let run = |entity, roots| {
            initialize(
                &graphs,
                entity,
                Variables::new(),
                roots,
                &mut ReferenceWorld,
                MAX_INIT_WORK,
            )
        };
        let hud = run(0xa000_0100, vec![Root::new(1, 0x20e4)]).unwrap();
        assert!(hud.initialization_complete(), "{:?}", hud.diagnostics);
        assert_eq!(active(&hud, 1), [25]);

        let mut controller = Root::new(1, 0x13c1);
        controller.variables.insert(8419, Value::Bool(true));
        let owner = run(0xa000_0100, vec![controller]).unwrap();
        let instances: Vec<_> = owner
            .instances
            .iter()
            .map(|(&id, instance)| (id, instance.graph, instance.parent))
            .collect();
        // Python Component with the practice controller and v8419 enabled.
        assert_eq!(
            instances,
            [
                (1, 0x13c1, None),
                (2, 0x20e4, Some((1, 42))),
                (3, 0x288a, Some((1, 43))),
                (4, 0x288b, Some((3, 0))),
                (5, 0x288d, Some((3, 1))),
            ]
        );
        assert_eq!(active(&owner, 2), [25]);
        assert!(!owner.initialization_complete());

        // Only the enriched pack includes the NPC body bindings.
        if let Some(body) = graphs.entity_body(0x0400_0000_0000_1403) {
            let roots = body_roots(body).unwrap();
            assert_eq!(
                roots
                    .iter()
                    .map(|root| (root.id, root.graph))
                    .collect::<Vec<_>>(),
                [
                    (1, 0x0033),
                    (2, 0x0ba8),
                    (3, 0x0be2),
                    (4, 0x0ba3),
                    (5, 0x174f),
                    (6, 0x2206)
                ],
            );
            assert_eq!(roots[0].variables[&199], Value::Integer(200));
            let bot = run(0xa100_0000, vec![Root::new(1, 0x0c18)]).unwrap();
            assert_eq!(bot.instances[&2].graph, 0x0d24);
            assert_eq!(bot.instances[&2].parent, Some((1, 3)));
            assert_eq!(active(&bot, 2), [56, 76]);
            assert!(!bot.initialization_complete());
        }
    }
}
