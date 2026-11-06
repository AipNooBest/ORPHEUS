//! Initialization and supported frame events of authored statescript graphs.
//!
//! This covers Entry, typed variable writes, conditional links and static
//! SubScript instances. Diagnostics make a partial snapshot unsuitable for
//! gameplay replication. The component also advances static waits and dispatches
//! scoped message entries. Input, combat and other authored actions still need
//! their corresponding runtime.

use std::{
    cell::Cell,
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
        self, Context, EmptyContext, EntityRelation, EvalError, Scope, StackPriority, Variable,
        equal, truthy,
    },
    script_graph::{Body, Graph, GraphStore, Node, asset_guid},
    script_runtime::{ButtonEdge, FRAME_MS, QueueFull, Timeline, button_edges, frame_start},
    script_wire::{Value, Variables},
};

pub const MAX_INIT_WORK: usize = 100_000;
const MAX_PLUG_DEPTH: usize = 64;
const MAX_EVENTS_PER_UPDATE: usize = 1000;
const MAX_PENDING_EVENTS: usize = 16_384;
const MAX_PENDING_SHOTS: usize = 256;
const ABILITY_RUNNING: u8 = 1;
const ABILITY_PUSHED: u8 = 2;
const ABILITY_HELD: u8 = 4;
const LINK_PRIORITY: StackPriority = StackPriority {
    level: 0,
    above_equals: true,
};
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

/// Initial body bindings, then the manager and equipped weapon slots in asset order.
pub fn player_roots(body: &Body, graphs: &GraphStore) -> Result<Vec<Root>, InitFault> {
    let mut roots = body_roots(body)?;
    let mut id = roots.last().map_or(0, |root| root.id);
    let mut append = |graph, slot: Option<usize>| {
        id = id.checked_add(1).ok_or(InitFault::InstanceIdExhausted)?;
        let mut root = Root::new(id, graph);
        if let Some(slot) = slot {
            root.variables.insert(28, Value::Integer(slot as i64));
        }
        roots.push(root);
        Ok::<_, InitFault>(())
    };
    if let Some(manager) = body.manager {
        append(manager, None)?;
    }
    for (slot, graph) in body.weapons.iter().enumerate() {
        if let Some(graph) = graph {
            append(*graph, Some(slot))?;
        }
    }
    if let Some(root) = roots.iter().find(|root| graphs.graph(root.graph).is_none()) {
        return Err(InitFault::MissingPlayerGraph(root.graph));
    }
    Ok(roots)
}

const OWNER_HEALTH: u32 = 253;
const HUD_MAX_HEALTH: u32 = 1737;

pub fn player_entity_variables(body: &Body, health: f64, maximum_health: f64) -> Variables {
    let slots = body
        .weapons
        .iter()
        .map(|weapon| Value::Bool(weapon.is_some()))
        .collect();
    let first = body.weapons.iter().position(Option::is_some).unwrap_or(0);
    Variables::from([
        (29, Value::Array(slots)),
        (16_515, Value::Integer(first as i64)),
        (OWNER_HEALTH, Value::Float(health)),
        (1732, Value::Float(health)),
        (HUD_MAX_HEALTH, Value::Float(maximum_health)),
        (342, Value::Float(0.0)),
    ])
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

    fn body_scale(&mut self, _site: Site, _target: u32, _override_value: &Value) -> Option<f64> {
        None
    }

    fn has_owner_health(&self, _site: Site) -> bool {
        false
    }

    /// None means this relationship is unsupported for the supplied entity.
    fn related_entity(
        &mut self,
        _site: Site,
        _relation: EntityRelation,
        _entity: u32,
    ) -> Option<u32> {
        None
    }

    /// None leaves an encountered AI candidate explicitly unsupported.
    fn selects_ai_behavior(&mut self, _site: Site) -> Option<bool> {
        None
    }

    /// The world owns delivery outside the current body.
    fn send_message(
        &mut self,
        _source: Site,
        _target: u32,
        _message: u64,
        _parameters: &Variables,
    ) -> bool {
        false
    }
}

#[derive(Default)]
pub struct EmptyWorld;

impl World for EmptyWorld {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StateKind {
    Plain,
    BooleanSwitch(bool),
    Switch {
        case: Option<usize>,
    },
    ActiveQuery,
    GamePadVibration,
    SubScript {
        child: Option<u16>,
    },
    MessageIdle,
    Wait,
    WaitFrames {
        target: u32,
    },
    AiCandidate {
        selected: bool,
    },
    LogicalButton {
        down: bool,
    },
    Stack {
        top: bool,
        under: bool,
    },
    Ability {
        flags: u8,
    },
    MovementMod,
    Anim,
    Effect,
    SendMessage,
    CosmeticEntity,
    ChaseVar,
    Watch,
    WeaponVolley {
        volleys: u8,
        offset_ms: u64,
        ammo0: i64,
        ammo_var: Option<Variable>,
    },
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
    pub chase: Option<ChasePayload>,
    pub ability_cooldown: Option<AbilityCooldown>,
    watch_seen: Option<Value>,
    ending: bool,
    pending_change: bool,
    pending_timer: bool,
    buffered_press: Option<BufferedPress>,
    linked_variable: Option<VarKey>,
    ability_stack: Option<VarKey>,
}

impl State {
    fn dormant(kind: StateKind) -> Self {
        Self {
            active: false,
            activation: 0,
            start_ms: 0,
            kind,
            chase: None,
            ability_cooldown: None,
            watch_seen: None,
            ending: false,
            pending_change: false,
            pending_timer: false,
            buffered_press: None,
            linked_variable: None,
            ability_stack: None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct AbilityCooldown {
    pub current: f64,
    pub rate: f64,
    pub last_ms: u64,
}

impl AbilityCooldown {
    fn remaining(self, now_ms: u64) -> f64 {
        let elapsed = (now_ms.saturating_sub(self.last_ms) as f64 * -f64::from(0.001_f32)) as f32;
        let consumed = (f64::from(elapsed) * self.rate) as f32;
        f64::from(((f64::from(consumed) + self.current) as f32).max(0.0))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ChasePayload {
    pub current: Value,
    pub reached: bool,
    pub remaining_ms: Option<i64>,
    pub last_ms: u64,
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
    /// Effective linked values, separate from their stored bases.
    pub resolved_bindings: IndexMap<(u16, u32), Value>,
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
    MissingPlayerGraph(u32),
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
    PendingShots,
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
            Self::MissingPlayerGraph(graph) => {
                write!(f, "player statescript graph {graph:04X} is missing")
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
            Self::PendingShots => f.write_str("statescript shot queue limit exceeded"),
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
    pub priority: StackPriority,
    pub slot: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ShotEvent {
    pub entity: u32,
    pub instance: u16,
    pub graph: u32,
    pub raw_state: u16,
    pub frame: u32,
    pub time_ms: u64,
    pub activation: u16,
    pub activation_ms: u64,
    pub ordinal: u32,
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

    /// Consume reports only after the body graph path is ready for authority.
    pub fn take_shots(&mut self) -> Option<Vec<ShotEvent>> {
        let shots = std::mem::take(&mut self.machine.shots);
        self.snapshot()?.initialization_complete().then_some(shots)
    }

    pub fn shot_node(&self, shot: &ShotEvent) -> Option<&Node> {
        let snapshot = self.snapshot()?;
        let instance = snapshot.instances.get(&shot.instance)?;
        if snapshot.entity != shot.entity || instance.graph != shot.graph {
            return None;
        }
        self.graphs
            .graph(shot.graph)?
            .state(usize::from(shot.raw_state))
            .filter(|node| node.class == "STUStatescriptStateWeaponVolley")
    }

    pub fn evaluate_shot_config(
        &self,
        shot: &ShotEvent,
        config: &Json,
        world: &mut impl World,
    ) -> Result<Value, EvalError> {
        self.shot_node(shot)
            .ok_or(EvalError::MalformedConfig("shot source"))?;
        self.evaluate_state_config(shot.instance, shot.raw_state, config, world)
    }

    pub fn evaluate_state_config(
        &self,
        instance: u16,
        raw_state: u16,
        config: &Json,
        world: &mut impl World,
    ) -> Result<Value, EvalError> {
        let snapshot = self
            .snapshot()
            .ok_or(EvalError::MalformedConfig("state source"))?;
        let graph = snapshot
            .instances
            .get(&instance)
            .ok_or(EvalError::MalformedConfig("state source"))?
            .graph;
        let node = self
            .graphs
            .graph(graph)
            .and_then(|graph| graph.state(usize::from(raw_state)))
            .ok_or(EvalError::MalformedConfig("state source"))?;
        let mut context = InstanceContext {
            machine: &self.machine,
            world,
            site: Site {
                entity: snapshot.entity,
                instance,
                graph,
                node: Some(node.pos),
            },
            reads: Vec::new(),
            parameters: None,
            watcher: None,
            read_native: false,
        };
        script_expr::evaluate(config, &mut context)
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
        self.run(world, |vm| {
            let key = VarKey::Entity(id);
            let old = vm.machine.value(key, None);
            vm.set_base(key, value);
            vm.notify_value(key, &old, None)
        })
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
        self.machine
            .queue_entries(&self.graphs, Some(instance), message, parameters)
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

enum LinkValue {
    Fixed(Value),
    Volley(u8),
    Chase,
    Cooldown,
    AbilityStack,
}

struct VariableLink {
    state: StateKey,
    priority: StackPriority,
    output: LinkValue,
    stack: bool,
}

struct Volley {
    start_ms: u64,
    ammo0: i64,
    per_shot: i64,
    rate: f64,
    spread: f64,
    planned: u32,
    reported: u32,
    outputs: [Option<VarKey>; 3],
}

impl Volley {
    fn ammo_at(&self, now_ms: u64) -> i64 {
        let Some(elapsed) = now_ms.checked_sub(self.start_ms) else {
            return self.ammo0;
        };
        let seconds = f64::from(elapsed as f32 * 0.001_f32);
        let shots = ((seconds * self.rate) as f32).floor() as i64;
        self.ammo0
            .saturating_sub(self.per_shot.saturating_mul(shots.saturating_add(1)))
            .max(0)
    }

    fn output(&self, slot: u8, now_ms: u64) -> Value {
        match slot {
            0 => Value::Integer(self.ammo_at(now_ms)),
            1 => Value::Float(self.rate),
            2 => Value::Float(self.spread),
            _ => unreachable!("weapon volley output slot"),
        }
    }
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
    ChaseReached(u16),
    WatchChanged(u16),
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
            | Self::ChaseReached(state)
            | Self::WatchChanged(state)
            | Self::Timer(state, _) => Some(state),
        }
    }
}

struct Machine {
    snapshot: Snapshot,
    queues: Timeline<Event>,
    watchers: BTreeMap<VarKey, Vec<StateKey>>,
    ticking: BTreeMap<u16, IndexMap<u16, u64>>,
    links: BTreeMap<VarKey, Vec<VariableLink>>,
    volleys: BTreeMap<StateKey, Volley>,
    shots: Vec<ShotEvent>,
    ability_presses: BTreeMap<StateKey, Vec<AbilityPress>>,
    next_id: u32,
    work_left: usize,
    max_work: usize,
    frame: u32,
    now_ms: u64,
    random_state: Cell<u64>,
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
                resolved_bindings: IndexMap::new(),
                diagnostics: Vec::new(),
            },
            queues: Timeline::new(MAX_PENDING_EVENTS),
            watchers: BTreeMap::new(),
            ticking: BTreeMap::new(),
            links: BTreeMap::new(),
            volleys: BTreeMap::new(),
            shots: Vec::new(),
            ability_presses: BTreeMap::new(),
            next_id: 1,
            work_left: max_work,
            max_work,
            frame,
            now_ms: frame_start(frame),
            random_state: Cell::new(u64::from(entity) << 32 | u64::from(frame)),
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
        self.snapshot.bindings.clear();
        self.snapshot.resolved_bindings.clear();
        for (&variable, entries) in &self.links {
            let key = match variable {
                VarKey::Entity(id) => (0, id),
                VarKey::Instance(instance, id) => (instance, id),
            };
            let Some(top) = entries.last() else { continue };
            let bindings: Vec<_> = entries
                .iter()
                .filter(|entry| entry.stack)
                .map(|entry| StackBinding {
                    instance: entry.state.instance,
                    raw_state: entry.state.raw,
                    priority: entry.priority,
                    slot: u8::from(matches!(entry.output, LinkValue::AbilityStack)),
                })
                .collect();
            if !bindings.is_empty() {
                self.snapshot.bindings.insert(key, bindings);
            }
            let resolved = self.link_value(top);
            self.snapshot.resolved_bindings.insert(key, resolved);
        }
    }

    fn queue_entries(
        &mut self,
        graphs: &GraphStore,
        recipient: Option<u16>,
        message: u64,
        parameters: Variables,
    ) -> Result<usize, InitFault> {
        let mut entries = Vec::new();
        for (&id, item) in &self.snapshot.instances {
            if recipient.is_some_and(|recipient| recipient != id) {
                continue;
            }
            let graph = graphs.graph(item.graph).expect("registered graph");
            entries.extend(
                graph
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
                    .map(|node| (id, node.pos)),
            );
        }
        self.queues.reserve(entries.len())?;
        let parameters = Arc::new(parameters);
        for &(instance, position) in &entries {
            self.queues.push(
                instance,
                self.now_ms,
                Event::Message {
                    position,
                    parameters: Arc::clone(&parameters),
                },
            )?;
        }
        Ok(entries.len())
    }

    fn value(&self, key: VarKey, below: Option<StateKey>) -> Value {
        self.links
            .get(&key)
            .and_then(|entries| {
                entries
                    .iter()
                    .take_while(|entry| Some(entry.state) != below)
                    .last()
            })
            .map_or_else(
                || stored(&self.snapshot, key),
                |entry| self.link_value(entry),
            )
    }

    fn link_value(&self, entry: &VariableLink) -> Value {
        match &entry.output {
            LinkValue::Fixed(value) => value.clone(),
            LinkValue::Volley(slot) => self.volleys[&entry.state].output(*slot, self.now_ms),
            LinkValue::Chase => self.snapshot.instances[&entry.state.instance].states
                [&entry.state.raw]
                .chase
                .as_ref()
                .expect("linked chase state")
                .current
                .clone(),
            LinkValue::Cooldown => Value::Float(
                self.snapshot.instances[&entry.state.instance].states[&entry.state.raw]
                    .ability_cooldown
                    .expect("linked cooldown state")
                    .remaining(self.now_ms),
            ),
            LinkValue::AbilityStack => Value::Bool(true),
        }
    }

    fn allows_stack(
        &self,
        key: VarKey,
        priority: StackPriority,
        exclude: Option<StateKey>,
    ) -> bool {
        self.links.get(&key).is_none_or(|entries| {
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
    output_write_depth: usize,
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
            output_write_depth: 0,
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
                return self.report_active_volleys();
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
            if node.class == "STUStatescriptStateAbility" {
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
                    .or_insert_with(|| State::dormant(StateKind::LogicalButton { down: false }));
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
        let active_flag = if ability_is_stacked(node) {
            ABILITY_PUSHED
        } else {
            ABILITY_RUNNING
        };
        if flags & active_flag != 0 {
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
        if self.cooldown_remaining(key) > 0.0
            || !self.ability_condition(key, node, "m_150F0D92")
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
        self.set_ability_flags(key, flags | if start != 0 { ABILITY_HELD } else { 0 });
        if ability_is_stacked(node) {
            self.push_ability(key, node, depth)
        } else {
            self.start_ability(key, node, depth)
        }
    }

    fn push_ability(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let target = match script_expr::lvalue(field(&node.fields, "m_out_StackVar")) {
            Ok(Some(target)) => source_key(id, target),
            Ok(None) => return Ok(()),
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        };
        let priority = match script_expr::stack_priority(field(&node.fields, "m_5E561E77")) {
            Ok(priority) => priority,
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        };
        if self.state(key).and_then(|state| state.linked_variable) == Some(target) {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("overlapping ability outputs"),
            );
            return Ok(());
        }
        let state = self.state_mut(key).expect("active ability");
        let StateKind::Ability { flags } = state.kind else {
            unreachable!("active ability")
        };
        state.kind = StateKind::Ability {
            flags: flags | ABILITY_PUSHED,
        };
        state.ability_stack = Some(target);
        self.insert_link(
            target,
            VariableLink {
                state: key,
                priority,
                output: LinkValue::AbilityStack,
                stack: true,
            },
            depth,
        )
    }

    fn start_ability(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let flags = self.ability_flags(key).expect("active ability");
        self.set_ability_flags(key, flags | ABILITY_RUNNING);
        self.follow(id, node, "m_B969AFC9", Some(key), depth)?;
        self.follow(id, node, "m_4E22B5C4", Some(key), depth)?;
        self.ability_vars(id, node, "m_EF3C5C73")
    }

    fn stop_ability(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        self.unlink_ability_stack(key, depth)?;
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

    fn unlink_ability_stack(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(flags) = self
            .ability_flags(key)
            .filter(|flags| flags & ABILITY_PUSHED != 0)
        else {
            return Ok(());
        };
        self.set_ability_flags(key, flags & !ABILITY_PUSHED);
        if let Some(target) = self
            .state_mut(key)
            .and_then(|state| state.ability_stack.take())
        {
            self.remove_link(key, target, None, depth + 1)?;
        }
        Ok(())
    }

    fn cooldown_remaining(&self, key: StateKey) -> f64 {
        self.state(key)
            .and_then(|state| state.ability_cooldown)
            .map_or(0.0, |cooldown| cooldown.remaining(self.machine.now_ms))
    }

    fn restart_cooldown(
        &mut self,
        key: StateKey,
        node: &Node,
        depth: usize,
    ) -> Result<(), InitFault> {
        let id = key.instance;
        let target = match script_expr::lvalue(field(&node.fields, "m_out_CooldownVar")) {
            Ok(Some(target)) => source_key(id, target),
            Ok(None) => return Ok(()),
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        };
        let modifier = self.machine.value(VarKey::Entity(3652), None);
        let mut rate = (script_expr::to_float(&modifier) + 1.0) as f32;
        let scalar = field(&node.fields, "m_F7CAF887");
        if !scalar.is_null() {
            let Some(value) = self.evaluate(id, node, scalar, None) else {
                return Ok(());
            };
            rate = (f64::from(rate) * script_expr::to_float(&value)) as f32;
        }
        let current = script_expr::to_float(&stored(&self.machine.snapshot, target)).max(0.0);
        let delay = ((current * 1000.0) as f32 / rate).round();
        if !current.is_finite() || !rate.is_finite() || (rate > 0.0 && !delay.is_finite()) {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("ability cooldown"),
            );
            return Ok(());
        }
        // The reference removes its previous slot before relinking at the same priority.
        if let Some(entries) = self.machine.links.get_mut(&target) {
            entries.retain(|entry| entry.state != key);
        }
        let cooldown = AbilityCooldown {
            current,
            rate: f64::from(rate),
            last_ms: self.machine.now_ms,
        };
        let state = self.state_mut(key).expect("active ability");
        state.linked_variable = Some(target);
        state.ability_cooldown = Some(cooldown);
        self.insert_link(
            target,
            VariableLink {
                state: key,
                priority: StackPriority {
                    level: 0,
                    above_equals: true,
                },
                output: LinkValue::Cooldown,
                stack: false,
            },
            depth,
        )?;
        self.machine.queues.retain(
            id,
            |_, event| !matches!(event, Event::Timer(raw, 2) if *raw == key.raw),
        );
        if current > 0.0 && rate > 0.0 {
            self.post_after(id, delay.max(1.0) as u64, Event::Timer(key.raw, 2))?;
        }
        Ok(())
    }

    fn finish_cooldown(&mut self, key: StateKey) -> Result<(), InitFault> {
        let remaining = self.cooldown_remaining(key);
        if let Some(target) = self
            .state_mut(key)
            .and_then(|state| state.linked_variable.take())
        {
            self.unlink_output(key, target, Some(Value::Float(remaining)))?;
        }
        Ok(())
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
                    Event::AbilityCheck(raw) => self.timer(key(raw), 1)?,
                    Event::ChaseReached(raw) => {
                        if self.state(key(raw)).is_some_and(|state| state.active) {
                            let node = self.graph(id).state(usize::from(raw)).expect("chase state");
                            self.try_complete(key(raw), node, 0)?;
                        }
                    }
                    Event::WatchChanged(raw) => self.deliver_watch(key(raw))?,
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
        self.parameters = Some(parameters);
        let result = self.deliver_entry(id, node);
        self.parameters = None;
        result
    }

    fn deliver_entry(&mut self, id: u16, node: &Node) -> Result<(), InitFault> {
        let path = if node.is_state() {
            let Some(raw) = node.state else { return Ok(()) };
            let key = StateKey { instance: id, raw };
            let Some(state) = self.state(key).filter(|state| state.active) else {
                return Ok(());
            };
            if state.kind != StateKind::MessageIdle {
                self.report(
                    self.site(id, node),
                    Issue::UnsupportedField("state game message delivery"),
                );
                return Ok(());
            }
            self.exit_subgraph(id, node, "m_33DA5163", 1)?;
            ("m_452862E6", Some(key))
        } else {
            ("m_outPlug", None)
        };
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
            let key = parameter_id(field);
            let value = self
                .parameters
                .as_deref()
                .and_then(|params| params.get(&key))
                .cloned()
                .unwrap_or(Value::Null);
            self.write(target, value)?;
        }
        self.follow(id, node, path.0, path.1, 0)?;
        Ok(())
    }

    fn send_game_message(&mut self, id: u16, node: &Node) -> Result<bool, InitFault> {
        let Some(message) = node
            .field("m_gameMessage.m_gameMessage")
            .and_then(asset_guid)
            .filter(|&id| id != 0)
        else {
            return Ok(true);
        };
        let mut parameters = Variables::new();
        for parameter in array_field(&node.fields, "m_params")
            .iter()
            .filter(|value| value.is_object())
        {
            self.spend("game message parameter")?;
            let Some(value) = self.evaluate(id, node, field(parameter, "m_value"), None) else {
                return Ok(false);
            };
            parameters.insert(parameter_id(parameter), value);
        }
        let site = self.site(id, node);
        let target = node
            .fields
            .get("m_targets")
            .or_else(|| node.fields.get("m_target"))
            .unwrap_or(&Json::Null);
        let value = if target.is_null() {
            Value::Entity(site.entity)
        } else {
            let Some(value) = self.evaluate(id, node, target, None) else {
                return Ok(false);
            };
            value
        };
        let entities: Vec<_> = match value {
            Value::Entity(entity) if entity != 0 => vec![entity],
            Value::Array(items) => items
                .into_iter()
                .filter_map(|value| {
                    let Value::Entity(entity) = value else {
                        return None;
                    };
                    (entity != 0 && entity & 0x1fff_ffff != 0x1fff_ffff).then_some(entity)
                })
                .collect(),
            _ => Vec::new(),
        };
        for entity in entities {
            self.spend("game message routing")?;
            if entity == site.entity {
                self.machine
                    .queue_entries(self.graphs, None, message, parameters.clone())?;
            } else if !self.world.send_message(site, entity, message, &parameters) {
                self.report(
                    site,
                    Issue::UnsupportedField("external game message delivery"),
                );
                return Ok(false);
            }
        }
        Ok(true)
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
                .filter(|state| {
                    read_native
                        && matches!(
                            state.kind,
                            StateKind::BooleanSwitch(_)
                                | StateKind::Switch { .. }
                                | StateKind::Watch
                        )
                })
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
        self.set_base(key, value);
        let outputs: Vec<_> = self
            .machine
            .links
            .get(&key)
            .into_iter()
            .flatten()
            .filter_map(|link| match link.output {
                LinkValue::Chase => Some((link.state, false)),
                LinkValue::Cooldown => Some((link.state, true)),
                _ => None,
            })
            .collect();
        for (state, cooldown) in outputs {
            if !self
                .state(state)
                .is_some_and(|state| state.active && state.linked_variable == Some(key))
            {
                continue;
            }
            if self.output_write_depth == MAX_PLUG_DEPTH {
                return Err(InitFault::PlugDepth);
            }
            self.output_write_depth += 1;
            let result = if cooldown {
                let node = self
                    .graph(state.instance)
                    .state(usize::from(state.raw))
                    .expect("ability state");
                self.restart_cooldown(state, node, self.output_write_depth)
            } else {
                self.chase_write(state, key)
            };
            self.output_write_depth -= 1;
            result?;
        }
        self.notify_value(key, &old, None)
    }

    fn set_base(&mut self, key: VarKey, value: Value) {
        match key {
            VarKey::Entity(id) => self.machine.snapshot.variables.insert(id, value),
            VarKey::Instance(instance, id) => self.machine.snapshot.instances[&instance]
                .variables
                .insert(id, value),
        };
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
            if matches!(state.kind, StateKind::Stack { .. }) && state.linked_variable == Some(key) {
                if let Some(source) = source {
                    let above = self.machine.links.get(&key).is_some_and(|entries| {
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
                    let key = StateKey { instance: id, raw };
                    if target.class == "STU_01178155" && input == "m_inPlug" {
                        let state = self.machine.snapshot.instances[&id]
                            .states
                            .entry(raw)
                            .or_insert_with(|| State::dormant(StateKind::ActiveQuery));
                        let output = if state.active {
                            "m_7273C81B"
                        } else {
                            "m_C6F3744E"
                        };
                        self.follow(id, target, output, Some(key), depth + 1)?;
                        taken = true;
                        continue;
                    }
                    if target.class == "STU_7B9E8909" && input == "m_inPlug" {
                        // The bridge consumes its input even when inactive. Only an
                        // explicit begin request can open it.
                        let state = self.machine.snapshot.instances[&id]
                            .states
                            .entry(raw)
                            .or_insert_with(|| State::dormant(StateKind::Plain));
                        if state.active {
                            self.follow(id, target, "m_outPlug", Some(key), depth + 1)?;
                        }
                        taken = true;
                        continue;
                    }
                    if target.class == "STUStatescriptStateAbility" && input == "m_3B7BED2E" {
                        self.stop_ability(key, target, depth + 1)?;
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
            "STUStatescriptActionSendGameMessage" => {
                if !self.send_game_message(id, node)? {
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
            if kind == StateKind::ChaseVar && configured(node, "m_9FF96195") {
                self.finish_chase(key, depth)?;
                self.begin_chase(key, node, depth)?;
            }
            return Ok(());
        }
        let kind = match node.class.as_str() {
            // These client presentation states use the base state lifecycle on the server.
            "STUStatescriptState"
            | "STU_9D7BF987"
            | "STU_B4CD94F4"
            | "STU_90C3BCEA"
            | "STUStatescriptStateDataFlowMapping"
            | "STU_E13B30A8"
            | "STUStatescriptStateUXPresenter" => StateKind::Plain,
            "STU_BD02E168" if is_client_subgraph_controller(self.graph(id), node) => {
                StateKind::Plain
            }
            // ForCodeSynced publishes to native game-code registration outside
            // the graph. Its VM plugs and empty wire payload use the base state.
            "STU_CD46AF93" if is_networked_for_code_synced(node) => StateKind::Plain,
            "STU_7B9E8909" => StateKind::Plain,
            "STUStatescriptStateBooleanSwitch" => StateKind::BooleanSwitch(false),
            "STU_DEBD057B" => StateKind::Switch { case: None },
            "STU_01178155" => StateKind::ActiveQuery,
            "STUStatescriptStateGamePadVibration" => StateKind::GamePadVibration,
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
            "STUStatescriptStateAbility" => StateKind::Ability { flags: 0 },
            "STU_316CFEF2" => StateKind::MovementMod,
            "STUStatescriptStateAnim" => StateKind::Anim,
            "STU_691BFA55" | "STU_7CA2FBAF" => StateKind::Effect,
            "STU_38EE1100" => StateKind::SendMessage,
            "STUStatescriptStateCosmeticEntity" => StateKind::CosmeticEntity,
            "STUStatescriptStateChaseVar" => StateKind::ChaseVar,
            "STU_2F4E2E3F" => StateKind::Watch,
            "STUStatescriptStateWeaponVolley" => StateKind::WeaponVolley {
                volleys: 1,
                offset_ms: 0,
                ammo0: 0,
                ammo_var: None,
            },
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
                buffered_press,
                ..State::dormant(kind)
            },
        );
        match kind {
            StateKind::GamePadVibration => self.post(id, Event::Finish(key.raw))?,
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
            StateKind::Switch { .. } => {
                let Some(case) = self.switch_case(key, node) else {
                    return Ok(());
                };
                self.state_mut(key).expect("active switch").kind = StateKind::Switch { case };
                self.follow(id, node, &case_path(case), Some(key), depth)?;
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
            StateKind::Ability { .. } => {
                self.restart_cooldown(key, node, depth)?;
                self.register_tick(key, self.machine.now_ms);
            }
            StateKind::Anim => self.arm_animation(key, node)?,
            StateKind::SendMessage => {
                self.send_game_message(id, node)?;
            }
            StateKind::CosmeticEntity => self.begin_cosmetic(key, node, depth)?,
            StateKind::ChaseVar => self.begin_chase(key, node, depth)?,
            StateKind::Watch => {
                let seen = self.evaluate(id, node, field(&node.fields, "m_B7A3CF78"), Some(key));
                self.state_mut(key).expect("active Watch").watch_seen = seen;
            }
            StateKind::WeaponVolley { .. } => self.begin_volley(key, node, depth)?,
            StateKind::Plain
            | StateKind::MovementMod
            | StateKind::Effect
            | StateKind::ActiveQuery => (),
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
        self.state_mut(key).expect("active stack").linked_variable = Some(target);
        self.insert_link(
            target,
            VariableLink {
                state: key,
                priority,
                output: LinkValue::Fixed(value),
                stack: true,
            },
            depth,
        )
    }

    fn begin_cosmetic(
        &mut self,
        key: StateKey,
        node: &Node,
        depth: usize,
    ) -> Result<(), InitFault> {
        let target = match script_expr::lvalue(field(&node.fields, "m_1DEF59FA")) {
            Ok(Some(target)) => source_key(key.instance, target),
            Ok(None) => return Ok(()),
            Err(error) => {
                self.report(self.site(key.instance, node), Issue::Expression(error));
                return Ok(());
            }
        };
        self.state_mut(key)
            .expect("active cosmetic")
            .linked_variable = Some(target);
        self.insert_link(
            target,
            VariableLink {
                state: key,
                priority: LINK_PRIORITY,
                stack: false,
                output: LinkValue::Fixed(Value::Handle {
                    kind: 1,
                    instance: u32::from(key.instance),
                    state: key.raw,
                    extra: 0,
                }),
            },
            depth,
        )
    }

    fn begin_chase(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let target = match script_expr::lvalue(field(&node.fields, "m_out_Var")) {
            Ok(target) => target.map(|target| source_key(id, target)),
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        };
        let initial = field(&node.fields, "m_EB0FC261");
        let current = if initial.is_null() {
            target.map_or(Value::Null, |target| self.machine.value(target, None))
        } else {
            let Some(value) = self.evaluate(id, node, initial, None) else {
                return Ok(());
            };
            value
        };
        let current = chase_value(&current);
        let remaining_ms = if configured(node, "m_5BEFF040") {
            let Some(value) = self.evaluate(id, node, field(&node.fields, "m_5BEFF040"), None)
            else {
                return Ok(());
            };
            Some((script_expr::to_float(&value) * 1000.0) as i64)
        } else {
            None
        };
        let now = self.machine.now_ms;
        let state = self.state_mut(key).expect("active chase");
        state.chase = Some(ChasePayload {
            current: current.clone(),
            reached: false,
            remaining_ms,
            last_ms: now,
        });
        state.linked_variable = target;
        if let Some(target) = target {
            let old = self.machine.value(target, None);
            self.set_base(target, current);
            self.insert_link(
                target,
                VariableLink {
                    state: key,
                    priority: LINK_PRIORITY,
                    output: LinkValue::Chase,
                    stack: false,
                },
                depth,
            )?;
            self.notify_value(target, &old, None)?;
        }
        self.register_tick(key, now);
        Ok(())
    }

    fn tick_chase(&mut self, key: StateKey) -> Result<(), InitFault> {
        let id = key.instance;
        let now = self.machine.now_ms;
        let Some(state) = self.state_mut(key) else {
            return Ok(());
        };
        let target = state.linked_variable;
        let Some(chase) = state.chase.as_mut() else {
            return Ok(());
        };
        let elapsed = (now - chase.last_ms) as i64;
        chase.last_ms = now;
        if elapsed == 0 || chase.reached {
            return Ok(());
        }
        let mut next = chase.clone();
        let before = target.map(|target| self.machine.value(target, None));
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("chase state");
        let Some(destination) =
            self.evaluate(id, node, field(&node.fields, "m_destination"), Some(key))
        else {
            return Ok(());
        };
        let destination = chase_value(&destination);
        if let Some(remaining) = next.remaining_ms {
            if remaining <= elapsed {
                next.current = destination;
                next.reached = true;
            } else {
                let share = f64::from((elapsed as f64 / remaining as f64) as f32);
                next.current = chase_lerp(next.current, destination, share);
                next.remaining_ms = Some(remaining - elapsed);
            }
        } else {
            let rate = field(&node.fields, "m_9B0385B4");
            let rate = if rate.is_null() {
                1.0
            } else {
                let Some(value) = self.evaluate(id, node, rate, Some(key)) else {
                    return Ok(());
                };
                script_expr::to_float(&value)
            };
            if rate > 0.0 {
                let per_ms = (rate * f64::from(0.001_f32)) as f32;
                let distance = (f64::from(per_ms) * elapsed as f64) as f32;
                let step = f64::from((f64::from(distance) + script_expr::EPSILON) as f32);
                (next.current, next.reached) = chase_step(next.current, destination, step);
            }
        }
        let reached = next.reached;
        self.state_mut(key).expect("active chase").chase = Some(next);
        if let (Some(target), Some(before)) = (target, before) {
            self.notify_value(target, &before, None)?
        }
        if reached {
            self.post(id, Event::ChaseReached(key.raw))?;
            if !configured(node, "m_CB671004") {
                self.post(id, Event::Finish(key.raw))?
            }
        }
        Ok(())
    }

    fn chase_write(&mut self, key: StateKey, target: VarKey) -> Result<(), InitFault> {
        let id = key.instance;
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("chase state");
        match field(&node.fields, "m_1E10B2B1").as_i64().unwrap_or(0) {
            0 => self.unlink_single_output(key, self.output_write_depth)?,
            1 => {
                let value = chase_value(&stored(&self.machine.snapshot, target));
                if let Some(chase) = self.state_mut(key).and_then(|state| state.chase.as_mut()) {
                    chase.current = value;
                }
            }
            _ => (),
        }
        Ok(())
    }

    fn finish_chase(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(state) = self.state_mut(key) else {
            return Ok(());
        };
        let Some(target) = state.linked_variable.take() else {
            return Ok(());
        };
        let current = state.chase.as_ref().map(|chase| chase.current.clone());
        self.remove_link(key, target, current, depth)
    }

    fn volley_config(&mut self, id: u16, node: &Node, name: &str, default: Value) -> Option<Value> {
        let config = field(&node.fields, name);
        if config.is_null() {
            Some(default)
        } else {
            self.evaluate(id, node, config, None)
        }
    }

    fn begin_volley(&mut self, key: StateKey, node: &Node, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let names = [
            "m_A17BE89B",
            "m_A55BC904",
            "m_5F40D9D3",
            "m_numShotsPerSecond",
        ];
        let mut config = [
            Value::Integer(1),
            Value::Integer(1),
            Value::Integer(1),
            Value::Float(1.0),
        ];
        for (value, name) in config.iter_mut().zip(names) {
            let Some(found) = self.volley_config(id, node, name, value.clone()) else {
                return Ok(());
            };
            *value = found;
        }
        let [max, min, per, rate] = config;
        let (Ok(maximum), Ok(minimum)) = (
            u32::try_from(script_expr::to_int(&max, 0).max(0)),
            u32::try_from(script_expr::to_int(&min, 0).max(0)),
        ) else {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("weapon volley shot count"),
            );
            return Ok(());
        };
        let per_shot = script_expr::to_int(&per, 0).max(0);
        let rate = if rate == Value::Null {
            1.0
        } else {
            script_expr::to_float(&rate).max(1e-5)
        };
        if !rate.is_finite() {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("weapon volley rate"),
            );
            return Ok(());
        }
        let mut outputs = [None; 3];
        for (output, name) in outputs
            .iter_mut()
            .zip(["m_24E351A4", "m_17770E7E", "m_3FFC9EB9"])
        {
            let cfg = field(&node.fields, name);
            if cfg.is_null() {
                continue;
            }
            *output = match script_expr::lvalue(cfg) {
                Ok(Some(var)) => Some(var),
                Ok(None) => {
                    self.report(
                        self.site(id, node),
                        Issue::UnsupportedField("weapon volley output"),
                    );
                    return Ok(());
                }
                Err(error) => {
                    self.report(self.site(id, node), Issue::Expression(error));
                    return Ok(());
                }
            };
        }
        let [ammo_var, _, _] = outputs;
        let outputs = outputs.map(|output| output.map(|var| source_key(id, var)));
        for (index, output) in outputs.iter().enumerate() {
            if output.is_some() && outputs[..index].contains(output) {
                self.report(
                    self.site(id, node),
                    Issue::UnsupportedField("duplicate weapon volley output"),
                );
                return Ok(());
            }
        }
        let ammo0 = outputs[0].map_or_else(
            || i64::from(maximum).saturating_mul(per_shot),
            |var| script_expr::to_int(&self.machine.value(var, None), 0).max(0),
        );
        let spread = outputs[2].map_or(0.0, |var| {
            script_expr::to_float(&self.machine.value(var, None)).clamp(0.0, 1.0)
        });
        if !spread.is_finite() {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("weapon volley spread"),
            );
            return Ok(());
        }
        let planned = if per_shot == 0 {
            maximum
        } else {
            (ammo0 / per_shot).min(i64::from(maximum)) as u32
        };
        let start_ms = self.machine.now_ms;
        self.state_mut(key).expect("active volley").kind = StateKind::WeaponVolley {
            volleys: 1,
            offset_ms: 0,
            ammo0,
            ammo_var,
        };
        self.machine.volleys.insert(
            key,
            Volley {
                start_ms,
                ammo0,
                per_shot,
                rate,
                spread,
                planned,
                reported: 0,
                outputs,
            },
        );
        for (slot, (target, base)) in outputs
            .into_iter()
            .zip([
                Value::Integer(ammo0),
                Value::Float(rate),
                Value::Float(spread),
            ])
            .enumerate()
        {
            let Some(target) = target else { continue };
            let before = self.machine.value(target, None);
            self.set_base(target, base);
            self.insert_link(
                target,
                VariableLink {
                    state: key,
                    priority: LINK_PRIORITY,
                    stack: false,
                    output: LinkValue::Volley(slot as u8),
                },
                depth,
            )?;
            self.notify_value(target, &before, None)?;
        }
        if per_shot == 0 || i64::from(minimum) < ammo0 / per_shot {
            self.post_after(
                id,
                shot_delay(minimum.saturating_sub(1), rate),
                Event::Timer(key.raw, 1),
            )?;
        }
        self.post_after(
            id,
            shot_delay(planned.saturating_sub(1), rate),
            Event::Timer(key.raw, 2),
        )
    }

    fn report_active_volleys(&mut self) -> Result<(), InitFault> {
        let active: Vec<_> = self
            .machine
            .snapshot
            .instances
            .iter()
            .flat_map(|(&id, instance)| {
                instance.states.iter().filter_map(move |(&raw, state)| {
                    (state.active && matches!(state.kind, StateKind::WeaponVolley { .. }))
                        .then_some(StateKey { instance: id, raw })
                })
            })
            .collect();
        for key in active {
            self.report_volley(key)?
        }
        Ok(())
    }

    fn report_volley(&mut self, key: StateKey) -> Result<(), InitFault> {
        if self.machine.owner_dead {
            return Ok(());
        }
        let Some(activation) = self.state(key).map(|state| state.activation) else {
            return Ok(());
        };
        let instance = &self.machine.snapshot.instances[&key.instance];
        let Some(volley) = self.machine.volleys.get_mut(&key) else {
            return Ok(());
        };
        while volley.reported < volley.planned {
            let time_ms = volley
                .start_ms
                .saturating_add(shot_delay(volley.reported, volley.rate));
            if time_ms > self.machine.now_ms {
                break;
            }
            if self.machine.shots.len() == MAX_PENDING_SHOTS {
                return Err(InitFault::PendingShots);
            }
            self.machine.shots.push(ShotEvent {
                entity: self.machine.snapshot.entity,
                instance: key.instance,
                graph: instance.graph,
                raw_state: key.raw,
                frame: (time_ms / FRAME_MS) as u32,
                time_ms,
                activation,
                activation_ms: volley.start_ms,
                ordinal: volley.reported,
            });
            volley.reported += 1;
        }
        Ok(())
    }

    fn finish_volley(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        self.report_volley(key)?;
        let Some(volley) = self.machine.volleys.get(&key) else {
            return Ok(());
        };
        let outputs = volley.outputs;
        let at = self.machine.now_ms.saturating_add(1);
        let values = [
            volley.output(0, at),
            volley.output(1, at),
            volley.output(2, at),
        ];
        for (target, value) in outputs.into_iter().zip(values) {
            if let Some(target) = target {
                self.remove_link(key, target, Some(value), depth)?
            }
        }
        self.machine.volleys.remove(&key);
        Ok(())
    }

    fn insert_link(
        &mut self,
        target: VarKey,
        link: VariableLink,
        depth: usize,
    ) -> Result<(), InitFault> {
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
        let entries = self.machine.links.entry(target).or_default();
        entries.retain(|entry| entry.state != link.state);
        let at = entries
            .iter()
            .position(|entry| {
                entry.priority.level > link.priority.level
                    || (entry.priority.level == link.priority.level && !link.priority.above_equals)
            })
            .unwrap_or(entries.len());
        entries.insert(at, link);
        self.notify_value(target, &old, None)?;
        self.sync_stack(target, depth)
    }

    fn sync_stack(&mut self, variable: VarKey, depth: usize) -> Result<(), InitFault> {
        if depth >= MAX_PLUG_DEPTH {
            return Err(InitFault::PlugDepth);
        }
        let Some(entries) = self.machine.links.get(&variable) else {
            return Ok(());
        };
        let states: Vec<_> = entries
            .iter()
            .filter(|entry| entry.stack)
            .map(|entry| entry.state)
            .collect();
        for key in states {
            self.spend("stack position change")?;
            let Some(state) = self
                .state(key)
                .filter(|state| state.active && !state.ending)
            else {
                continue;
            };
            let top = self
                .machine
                .links
                .get(&variable)
                .and_then(|entries| entries.last());
            if let StateKind::Ability { flags } = state.kind {
                if flags & ABILITY_PUSHED != 0 && state.ability_stack == Some(variable) {
                    let node = self
                        .graph(key.instance)
                        .state(usize::from(key.raw))
                        .expect("ability state");
                    if top.is_some_and(|entry| {
                        entry.state == key && matches!(entry.output, LinkValue::AbilityStack)
                    }) {
                        if flags & ABILITY_RUNNING == 0 {
                            self.start_ability(key, node, depth + 1)?;
                        }
                    } else {
                        self.stop_ability(key, node, depth + 1)?;
                    }
                }
                continue;
            }
            if state.linked_variable != Some(variable) {
                continue;
            }
            let top = top.is_some_and(|entry| entry.state == key);
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

    fn unlink_single_output(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(target) = self
            .state_mut(key)
            .and_then(|state| state.linked_variable.take())
        else {
            return Ok(());
        };
        self.remove_link(key, target, None, depth)
    }

    fn remove_link(
        &mut self,
        key: StateKey,
        target: VarKey,
        bake: Option<Value>,
        depth: usize,
    ) -> Result<(), InitFault> {
        self.unlink_output(key, target, bake)?;
        self.sync_stack(target, depth)
    }

    fn unlink_output(
        &mut self,
        key: StateKey,
        target: VarKey,
        bake: Option<Value>,
    ) -> Result<(), InitFault> {
        let old = self.machine.value(target, None);
        if let Some(value) = bake {
            self.set_base(target, value);
        }
        if let Some(entries) = self.machine.links.get_mut(&target) {
            entries.retain(|entry| entry.state != key);
            if entries.is_empty() {
                self.machine.links.remove(&target);
            }
        }
        self.notify_value(target, &old, None)
    }

    fn update_stack(&mut self, key: StateKey) -> Result<(), InitFault> {
        let Some(target) = self.state(key).and_then(|state| state.linked_variable) else {
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
        if let Some(LinkValue::Fixed(current)) = self
            .machine
            .links
            .get_mut(&target)
            .and_then(|entries| entries.iter_mut().find(|entry| entry.state == key))
            .map(|entry| &mut entry.output)
        {
            if !same(current, &value) {
                *current = value;
                self.notify_value(target, &old, Some(key))?;
            }
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
                    Some(StateKind::Switch { .. }) => self.post(id, Event::Reevaluate(raw))?,
                    Some(StateKind::Ability { .. }) => self.activate_ability(key, 0)?,
                    Some(StateKind::ChaseVar) => self.tick_chase(key)?,
                    Some(StateKind::Watch) => self.check_watch(key)?,
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

    fn arm_animation(&mut self, key: StateKey, node: &Node) -> Result<(), InitFault> {
        let id = key.instance;
        let duration = field(&node.fields, "m_86701C3E");
        if duration.is_null() {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("animation asset duration"),
            );
            return Ok(());
        }
        let Some(seconds) = self.evaluate(id, node, duration, None) else {
            return Ok(());
        };
        let delay = ((script_expr::to_float(&seconds) * 1000.0) as f32).round();
        if !delay.is_finite() {
            self.report(
                self.site(id, node),
                Issue::UnsupportedField("animation duration"),
            );
            return Ok(());
        }
        let removes_self = field(&node.fields, "m_154DBA91");
        let client_ends = if removes_self.is_null() {
            false
        } else {
            let Some(value) = self.evaluate(id, node, removes_self, None) else {
                return Ok(());
            };
            truthy(&value)
        };
        let delay_ms = delay.max(0.0) as u64;
        self.post_after(id, delay_ms, Event::Timer(key.raw, 1))?;
        if !client_ends {
            self.post_after(id, delay_ms, Event::Finish(key.raw))?;
        }
        Ok(())
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
        if kind == StateKind::Anim && parameter == 1 {
            self.follow(key.instance, node, "m_A3B86BB0", Some(key), 0)?;
        }
        if matches!(kind, StateKind::Ability { .. }) && matches!(parameter, 1 | 2) {
            self.activate_ability(key, 0)?;
        }
        if matches!(kind, StateKind::WeaponVolley { .. }) {
            match parameter {
                1 => {
                    self.follow(key.instance, node, "m_onMinShotsPlug", Some(key), 0)?;
                }
                2 => self.post(key.instance, Event::Finish(key.raw))?,
                _ => (),
            }
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
            if state.active && matches!(state.kind, StateKind::Switch { .. }) {
                self.post(key.instance, Event::Reevaluate(key.raw))?;
            } else if state.active
                && !state.pending_timer
                && matches!(state.kind, StateKind::BooleanSwitch(_))
            {
                state.pending_timer = true;
                self.post(key.instance, Event::Reevaluate(key.raw))?;
            } else if state.active && matches!(state.kind, StateKind::Stack { .. }) {
                self.update_stack(key)?;
            } else if state.active && state.kind == StateKind::Watch {
                self.check_watch(key)?;
            }
        }
        Ok(())
    }

    fn check_watch(&mut self, key: StateKey) -> Result<(), InitFault> {
        let Some(seen) = self
            .state(key)
            .filter(|state| state.active)
            .and_then(|state| state.watch_seen.clone())
        else {
            return Ok(());
        };
        let id = key.instance;
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("Watch state");
        if self
            .evaluate(id, node, field(&node.fields, "m_B7A3CF78"), Some(key))
            .is_some_and(|value| !equal(&value, &seen))
        {
            self.post(id, Event::WatchChanged(key.raw))?;
        }
        Ok(())
    }

    fn deliver_watch(&mut self, key: StateKey) -> Result<(), InitFault> {
        let Some(seen) = self
            .state(key)
            .filter(|state| state.active)
            .and_then(|state| state.watch_seen.clone())
        else {
            return Ok(());
        };
        let id = key.instance;
        let node = self
            .graph(id)
            .state(usize::from(key.raw))
            .expect("Watch state");
        match script_expr::lvalue(field(&node.fields, "m_48A8CDEC")) {
            Ok(Some(target)) => self.write(source_key(id, target), seen)?,
            Ok(None) => (),
            Err(error) => {
                self.report(self.site(id, node), Issue::Expression(error));
                return Ok(());
            }
        }
        self.follow(id, node, "m_A70DB860", Some(key), 0)?;
        let seen = self.evaluate(id, node, field(&node.fields, "m_B7A3CF78"), Some(key));
        if let Some(state) = self.state_mut(key) {
            state.watch_seen = seen
        }
        self.try_complete(key, node, 0)
    }

    fn reevaluate(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(state) = self.state_mut(key).filter(|state| state.active) else {
            return Ok(());
        };
        state.pending_timer = false;
        let kind = state.kind;
        let node = self
            .graph(key.instance)
            .state(usize::from(key.raw))
            .expect("switch state");
        let evaluated = if let StateKind::Switch { case } = kind {
            let Some(next) = self.switch_case(key, node) else {
                return Ok(());
            };
            if next != case {
                self.exit_subgraph(key.instance, node, &case_path(case), depth + 1)?;
                self.state_mut(key).expect("active switch").kind = StateKind::Switch { case: next };
                self.follow(key.instance, node, &case_path(next), Some(key), depth)?;
            }
            true
        } else {
            self.check_switch(key, depth)?
        };
        if evaluated {
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

    fn switch_case(&mut self, key: StateKey, node: &Node) -> Option<Option<usize>> {
        let value = self.evaluate(
            key.instance,
            node,
            field(&node.fields, "m_262AE982"),
            Some(key),
        )?;
        for (index, case) in array_field(&node.fields, "m_05706D1C").iter().enumerate() {
            if case.is_object() {
                let candidate = self.evaluate(key.instance, node, field(case, "m_value"), None)?;
                if equal(&value, &candidate) {
                    return Some(Some(index));
                }
            }
        }
        Some(None)
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
            StateKind::Switch { case } => {
                self.exit_subgraph(id, node, &case_path(case), depth + 1)?
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
                self.unlink_single_output(key, depth + 1)?;
            }
            StateKind::CosmeticEntity => self.unlink_single_output(key, depth + 1)?,
            StateKind::ChaseVar => self.finish_chase(key, depth + 1)?,
            StateKind::WeaponVolley { .. } => self.finish_volley(key, depth + 1)?,
            StateKind::Ability { flags } => {
                self.set_ability_flags(key, flags & !ABILITY_HELD);
                self.stop_ability(key, node, depth + 1)?;
                self.finish_cooldown(key)?;
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
            let outputs: Vec<_> = self.machine.snapshot.instances[&id]
                .states
                .iter()
                .filter_map(|(&raw, state)| {
                    (state.linked_variable.is_some()
                        || state.ability_stack.is_some()
                        || matches!(state.kind, StateKind::WeaponVolley { .. }))
                    .then_some((raw, state.kind))
                })
                .collect();
            for (raw, kind) in outputs {
                let key = StateKey { instance: id, raw };
                if matches!(kind, StateKind::WeaponVolley { .. }) {
                    self.finish_volley(key, 0)?;
                } else if kind == StateKind::ChaseVar {
                    self.finish_chase(key, 0)?;
                } else if matches!(kind, StateKind::Ability { .. }) {
                    self.unlink_ability_stack(key, 0)?;
                    self.finish_cooldown(key)?;
                } else {
                    self.unlink_single_output(key, 0)?;
                }
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

fn is_client_subgraph_controller(graph: &Graph, node: &Node) -> bool {
    // The controller is replicated, but its specialized branch starts on the client.
    const BRANCH: &str = "m_5B10D01F";
    let plug = field(&node.fields, BRANCH);
    node.is_networked()
        && field(plug, "$").as_str() == Some("STU_904BFCEC")
        && array_field(plug, "links").len() == 1
        && node
            .links(BRANCH)
            .next()
            .and_then(|(position, _)| graph.node(position?))
            .is_some_and(|target| target.client_only && !target.server_only)
}

fn is_networked_for_code_synced(node: &Node) -> bool {
    node.is_networked()
        && node.remote >= 0
        && field(&node.fields, "m_DC897C87").as_u64() == Some(1)
        && field(&node.fields, "m_598E32D5").as_u64() == Some(0)
        && field(&node.fields, "m_0582B0E4").as_u64() == Some(0)
        && field(&node.fields, "m_0B1AA8CA").is_null()
        && field(&node.fields, "m_values")
            .as_array()
            .is_some_and(|values| {
                !values.is_empty()
                    && values.iter().all(|value| {
                        field(value, "$").as_str() == Some("STUStatescriptForCodeValue")
                            && field(field(value, "m_value"), "$").as_str().is_some()
                    })
            })
}

fn ability_is_stacked(node: &Node) -> bool {
    !field(&node.fields, "m_out_StackVar").is_null() && !field(&node.fields, "m_5E561E77").is_null()
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

fn parameter_id(config: &Json) -> u32 {
    config
        .get("m_B5051BCE")
        .and_then(asset_guid)
        .map_or(0, |guid| u32::from(guid as u16))
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

fn case_path(case: Option<usize>) -> String {
    case.map_or_else(
        || "m_A5EF385D".into(),
        |index| format!("m_05706D1C[{index}]"),
    )
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

fn shot_delay(ordinal: u32, rate: f64) -> u64 {
    ((f64::from(ordinal) / rate) as f32 * 1000.0).round() as u64
}

fn chase_value(value: &Value) -> Value {
    match value {
        Value::Vector(_) => value.clone(),
        _ => Value::Float(script_expr::to_float(value)),
    }
}

fn chase_vector(value: Value) -> [f64; 3] {
    match value {
        Value::Vector(vector) => vector,
        _ => [script_expr::to_float(&value), 0.0, 0.0],
    }
}

fn chase_lerp(current: Value, destination: Value, share: f64) -> Value {
    let interpolate = |from: f64, to: f64| f64::from((from + (to - from) * share) as f32);
    if let (Value::Float(current), Value::Float(destination)) = (&current, &destination) {
        return Value::Float(interpolate(*current, *destination));
    }
    let current = chase_vector(current);
    let destination = chase_vector(destination);
    Value::Vector(std::array::from_fn(|axis| {
        interpolate(current[axis], destination[axis])
    }))
}

fn chase_step(current: Value, destination: Value, step: f64) -> (Value, bool) {
    if let (Value::Float(current), Value::Float(destination)) = (&current, &destination) {
        let delta = f64::from((*destination - *current) as f32);
        return if delta.abs() <= step {
            (Value::Float(*destination), true)
        } else {
            let next = *current + if delta > 0.0 { step } else { -step };
            (Value::Float(f64::from(next as f32)), false)
        };
    }
    let current = chase_vector(current);
    let destination = chase_vector(destination);
    let delta: [f64; 3] =
        std::array::from_fn(|axis| f64::from((destination[axis] - current[axis]) as f32));
    let length = f64::from((delta.iter().map(|value| value * value).sum::<f64>().sqrt()) as f32);
    if length <= step {
        (Value::Vector(destination), true)
    } else {
        let next = std::array::from_fn(|axis| {
            f64::from((current[axis] + delta[axis] * step / length) as f32)
        });
        (Value::Vector(next), false)
    }
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
        let below = self.watcher.filter(|watcher| {
            self.machine
                .snapshot
                .instances
                .get(&watcher.instance)
                .and_then(|instance| instance.states.get(&watcher.raw))
                .is_some_and(|state| {
                    matches!(state.kind, StateKind::Stack { .. })
                        && state.linked_variable == Some(key)
                })
        });
        self.machine.value(key, below)
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

    fn random_u64(&mut self) -> Option<u64> {
        // SplitMix64 gives each component a reproducible stream without OS calls in graph execution.
        let state = self
            .machine
            .random_state
            .get()
            .wrapping_add(0x9e37_79b9_7f4a_7c15);
        self.machine.random_state.set(state);
        let mixed = (state ^ (state >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        let mixed = (mixed ^ (mixed >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        Some(mixed ^ (mixed >> 31))
    }

    fn body_scale(&mut self, entity: u32) -> Option<f64> {
        if entity != self.site.entity {
            return None;
        }
        self.read_native = true;
        let scale_override = self.read(VarKey::Entity(31_210));
        self.world.body_scale(self.site, entity, &scale_override)
    }

    fn body_health(&mut self, entity: u32, maximum: bool) -> Option<f64> {
        if entity != self.site.entity || !self.world.has_owner_health(self.site) {
            return None;
        }
        let variable = if maximum {
            HUD_MAX_HEALTH
        } else {
            OWNER_HEALTH
        };
        match self.read(VarKey::Entity(variable)) {
            Value::Float(health) if health.is_finite() && health >= 0.0 => Some(health),
            _ => None,
        }
    }

    fn native(&mut self, config: &Json) -> Option<Value> {
        self.read_native = true;
        self.world.native(self.site, config)
    }

    fn related_entity(&mut self, relation: EntityRelation, entity: u32) -> Option<u32> {
        self.read_native = true;
        self.world.related_entity(self.site, relation, entity)
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
    fn bridge_forwards_input_only_between_begin_and_abort() {
        let message = |position, input| {
            node(
                position,
                "STUStatescriptEntryGameMessage",
                None,
                json!({
                    "m_gameMessage":{"m_gameMessage":format!("0x024000000000{position:04X}")},
                    "m_outPlug":plug(&[(0,input)], &[]),
                }),
            )
        };
        let fixture = graph(
            42,
            vec![
                node(
                    0,
                    "STU_7B9E8909",
                    Some(0),
                    json!({"m_outPlug":plug(&[(1,"m_inPlug")], &[])}),
                ),
                node(
                    1,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({"m_out_Var":var(100,true), "m_value":true}),
                ),
                message(2, "m_inPlug"),
                message(3, "m_beginPlug"),
                message(4, ABORT_PLUG),
            ],
            json!([0]),
            json!([2, 3, 4]),
        );
        let mut component = Component::start(
            Arc::new(store(vec![fixture])),
            42,
            Variables::new(),
            vec![Root::new(1, 42)],
            0,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        for (message, active, activation, forwarded) in [
            (2, false, 0, false),
            (3, true, 1, false),
            (2, true, 1, true),
            (2, true, 1, true),
            (4, false, 1, false),
            (2, false, 1, false),
        ] {
            component
                .set_entity_variable(100, Value::Bool(false), &mut EmptyWorld)
                .unwrap();
            component
                .queue_message(1, 0x0240_0000_0000_0000 | message, Variables::new())
                .unwrap();
            component.run_now(&mut EmptyWorld).unwrap();
            let snapshot = component.snapshot().unwrap();
            let state = &snapshot.instances[&1].states[&0];
            assert_eq!((state.active, state.activation), (active, activation));
            assert_eq!(snapshot.variables[&100], Value::Bool(forwarded));
            assert!(snapshot.initialization_complete());
        }
    }

    #[test]
    fn client_subgraph_controller_keeps_its_branch_off_the_server() {
        for client_only in [true, false] {
            let mut target = node(2, "STUStatescriptState", Some(1), json!({}));
            target["client_only"] = json!(client_only);
            let graphs = store(vec![graph(
                42,
                vec![
                    node(
                        0,
                        "STUStatescriptEntry",
                        None,
                        json!({"m_outPlug":plug(&[(1, "m_beginPlug")], &[])}),
                    ),
                    node(
                        1,
                        "STU_BD02E168",
                        Some(0),
                        json!({"m_5B10D01F":{
                            "$":"STU_904BFCEC","links":[[2, "m_beginPlug"]],"m_FF3DAF1E":[1]
                        }}),
                    ),
                    target,
                ],
                json!([1, 2]),
                json!([0]),
            )]);
            let snapshot = initialize(
                &graphs,
                0xa000_0100,
                Variables::new(),
                vec![Root::new(1, 42)],
                &mut EmptyWorld,
                MAX_INIT_WORK,
            )
            .unwrap();
            let states = &snapshot.instances[&1].states;
            assert!(!states.contains_key(&1));
            if client_only {
                assert!(snapshot.initialization_complete());
                assert!(states[&0].active);
                assert_eq!(states[&0].kind, StateKind::Plain);
            } else {
                assert!(!states.contains_key(&0));
                assert!(matches!(
                    snapshot.diagnostics.as_slice(),
                    [Diagnostic { issue: Issue::UnsupportedState(class), .. }]
                        if class == "STU_BD02E168"
                ));
            }
        }
    }

    #[test]
    fn networked_for_code_state_keeps_its_subgraph_lifetime() {
        for (server_only, flags) in [(false, 0), (true, 0), (false, 1)] {
            let mut for_code = node(
                1,
                "STU_CD46AF93",
                Some(0),
                json!({
                    "m_DC897C87":1, "m_598E32D5":flags, "m_0582B0E4":0,
                    "m_values":[{
                        "$":"STUStatescriptForCodeValue",
                        "m_identifier":"0x0D80000000000042",
                        "m_value":var(100,true),
                    }],
                    "m_subgraphPlug":plug(&[(2,"m_beginPlug")], &[1]),
                }),
            );
            for_code["remote"] = json!(0);
            for_code["server_only"] = json!(server_only);
            let mut fixture = graph(
                42,
                vec![
                    node(
                        0,
                        "STUStatescriptEntry",
                        None,
                        json!({"m_outPlug":plug(&[(1,"m_beginPlug")], &[])}),
                    ),
                    for_code,
                    node(2, "STUStatescriptState", Some(1), json!({})),
                    node(
                        3,
                        "STUStatescriptEntryGameMessage",
                        None,
                        json!({
                            "m_gameMessage":{"m_gameMessage":"0x0240000000000003"},
                            "m_outPlug":plug(&[(1,ABORT_PLUG)], &[]),
                        }),
                    ),
                ],
                json!([1, 2]),
                json!([0, 3]),
            );
            fixture["remote"] = json!([1]);
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
            let snapshot = component.snapshot().unwrap();
            let states = &snapshot.instances[&1].states;
            if server_only || flags != 0 {
                assert!(states.is_empty());
                assert!(matches!(
                    snapshot.diagnostics.as_slice(),
                    [Diagnostic { issue: Issue::UnsupportedState(class), .. }]
                        if class == "STU_CD46AF93"
                ));
                continue;
            }
            assert!(snapshot.initialization_complete());
            assert!(states[&0].active && states[&1].active);
            assert_eq!(states[&0].kind, StateKind::Plain);
            assert!(!snapshot.variables.contains_key(&0x42));
            component
                .queue_message(1, 0x0240_0000_0000_0003, Variables::new())
                .unwrap();
            component.run_now(&mut EmptyWorld).unwrap();
            let snapshot = component.snapshot().unwrap();
            let states = &snapshot.instances[&1].states;
            assert!(!states[&0].active && !states[&1].active);
        }
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
                    raw_state: 0,
                    slot: 0,
                    priority: StackPriority {
                        level: 0,
                        above_equals: false
                    }
                },
                StackBinding {
                    instance: 1,
                    raw_state: 1,
                    slot: 0,
                    priority: StackPriority {
                        level: 0,
                        above_equals: true
                    }
                }
            ]
        );
        assert_eq!(snapshot.resolved_bindings[&(0, 100)], Value::Float(11.0));
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

    #[test]
    fn ability_cooldown_restarts_and_stack_displacement_stops_the_running_ability() {
        let message = |position, message, target, input| {
            node(
                position,
                "STUStatescriptEntryGameMessage",
                None,
                json!({
                    "m_gameMessage":{"m_gameMessage":message},
                    "m_outPlug":plug(&[(target, input)], &[]),
                }),
            )
        };
        let mut fixture = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_beginPlug")],&[])}),
                ),
                node(
                    1,
                    "STUStatescriptStateAbility",
                    Some(0),
                    json!({
                        "m_out_CooldownVar":var(60,true),
                        "m_out_StackVar":var(61,true),
                        "m_5E561E77":{"m_priority":"0x0C40000000000001"},
                        "m_A5F1A77B":35,
                        "m_EF3C5C73":[{"m_out_Var":var(60,true), "m_value":0.1},
                                      {"m_out_Var":var(62,true), "m_value":true}],
                        "m_19899EAC":[{"m_out_Var":var(62,true), "m_value":false}]
                    }),
                ),
                node(
                    2,
                    "STUStatescriptStateStack",
                    Some(1),
                    json!({
                        "m_out_Var":var(61,true),
                        "m_value":7,
                        "m_priority":{"m_priority":"0x0C40000000000003"}
                    }),
                ),
                message(3, "0x0240000000000001", 2, "m_beginPlug"),
                message(4, "0x0240000000000002", 1, ABORT_PLUG),
            ],
            json!([1, 2]),
            json!([0, 3, 4]),
        );
        fixture["fields"]["m_680A2CB2"] = json!([0]);
        let mut component = Component::start(
            Arc::new(store(vec![fixture])),
            42,
            Variables::from([(3652, Value::Float(1.0)), (60, Value::Float(0.032))]),
            vec![Root::new(1, 42)],
            0,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let timer = |component: &Component| {
            component
                .snapshot()
                .unwrap()
                .pending_timers
                .get(&1)
                .and_then(|timers| timers.first().copied())
        };
        assert_eq!(
            timer(&component),
            Some(PendingTimer {
                due_ms: 16,
                raw_state: 0,
                finish: false,
                parameter: 2
            })
        );
        for frame in 0..=1 {
            component
                .advance_command(
                    Command {
                        frame,
                        buttons: 8,
                        ..Command::default()
                    },
                    &mut EmptyWorld,
                )
                .unwrap();
        }
        let snapshot = component.snapshot().unwrap();
        assert_eq!(snapshot.variables[&62], Value::Bool(true));
        assert_eq!(
            snapshot.instances[&1].states[&0].kind,
            StateKind::Ability { flags: 7 }
        );
        assert_eq!(snapshot.resolved_bindings[&(0, 61)], Value::Bool(true));
        assert_eq!(snapshot.bindings[&(0, 61)][0].slot, 1);
        assert_eq!(timer(&component).map(|timer| timer.due_ms), Some(66));
        let remaining = snapshot.resolved_bindings[&(0, 60)].clone();
        for message in [1, 2] {
            component
                .queue_message(1, 0x0240_0000_0000_0000 | message, Variables::new())
                .unwrap();
            component.run_now(&mut EmptyWorld).unwrap();
        }
        let snapshot = component.snapshot().unwrap();
        assert_eq!(snapshot.variables[&62], Value::Bool(false));
        assert_eq!(snapshot.variables[&60], remaining);
        assert_eq!(snapshot.resolved_bindings[&(0, 61)], Value::Integer(7));
        assert_eq!(snapshot.bindings[&(0, 61)][0].slot, 0);
        assert!(!snapshot.instances[&1].states[&0].active);
        assert!(timer(&component).is_none());
    }

    #[test]
    fn switch_exits_old_branch_and_active_query_does_not_begin_its_target() {
        let set = |position, variable, value| {
            node(
                position,
                "STUStatescriptActionSetVar",
                None,
                json!({
                    "m_out_Var":var(variable,true), "m_value":value,
                }),
            )
        };
        let fixture = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":plug(&[(1,"m_beginPlug"),(2,"m_inPlug"),(3,"m_beginPlug")],&[])
                    }),
                ),
                node(
                    1,
                    "STU_DEBD057B",
                    Some(0),
                    json!({
                        "m_262AE982":var(100,true),
                        "m_05706D1C":[
                            {"$":"Plug", "links":[[4,"m_beginPlug"]], "m_FF3DAF1E":[3], "m_value":2},
                            {"$":"Plug", "links":[[5,"m_beginPlug"]], "m_FF3DAF1E":[4], "m_value":2}
                        ],
                        "m_A5EF385D":plug(&[(6,"m_beginPlug")], &[5]),
                    }),
                ),
                node(
                    2,
                    "STU_01178155",
                    Some(1),
                    json!({
                        "m_7273C81B":plug(&[(7,"m_inPlug")],&[]),
                        "m_C6F3744E":plug(&[(8,"m_inPlug")],&[]),
                    }),
                ),
                node(
                    3,
                    "STUStatescriptStateGamePadVibration",
                    Some(2),
                    json!({
                        "m_onBeginPlug":plug(&[(9,"m_inPlug")],&[]),
                        "m_onFinishedPlug":plug(&[(10,"m_inPlug")],&[]),
                    }),
                ),
                node(4, "STUStatescriptState", Some(3), json!({})),
                node(5, "STUStatescriptState", Some(4), json!({})),
                node(6, "STUStatescriptState", Some(5), json!({})),
                set(7, 101, 1),
                set(8, 101, 0),
                set(9, 102, 1),
                set(10, 102, 2),
                node(
                    11,
                    "STUStatescriptEntryGameMessage",
                    None,
                    json!({
                        "m_gameMessage":{"m_gameMessage":"0x0240000000000001"},
                        "m_outPlug":plug(&[(2,"m_beginPlug")],&[]),
                    }),
                ),
                node(
                    12,
                    "STUStatescriptEntryGameMessage",
                    None,
                    json!({
                        "m_gameMessage":{"m_gameMessage":"0x0240000000000002"},
                        "m_outPlug":plug(&[(2,"m_inPlug")],&[]),
                    }),
                ),
            ],
            json!([1, 2, 3, 4, 5, 6]),
            json!([0, 11, 12]),
        );
        let mut component = Component::start(
            Arc::new(store(vec![fixture])),
            42,
            Variables::new(),
            vec![Root::new(1, 42)],
            0,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let initial = component.snapshot().unwrap();
        let states = &initial.instances[&1].states;
        assert_eq!(states[&0].kind, StateKind::Switch { case: None });
        assert!(!states[&1].active);
        assert_eq!(states[&1].activation, 0);
        assert!(!states[&2].active);
        assert!(states[&5].active);
        assert_eq!(initial.variables[&101], Value::Integer(0));
        assert_eq!(initial.variables[&102], Value::Integer(2));
        component
            .set_entity_variable(100, Value::Integer(2), &mut EmptyWorld)
            .unwrap();
        for message in [1, 2] {
            component
                .queue_message(1, 0x0240_0000_0000_0000 | message, Variables::new())
                .unwrap();
            component.run_now(&mut EmptyWorld).unwrap();
        }
        let updated = component.snapshot().unwrap();
        let states = &updated.instances[&1].states;
        assert_eq!(states[&0].kind, StateKind::Switch { case: Some(0) });
        assert!(states[&1].active);
        assert_eq!(states[&1].activation, 1);
        assert!(states[&3].active);
        assert!(!states.contains_key(&4));
        assert!(!states[&5].active);
        assert_eq!(updated.variables[&101], Value::Integer(1));
        assert!(updated.initialization_complete());
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
    fn volley_reports_exact_times_and_bakes_linked_ammo_on_finish() {
        let weapon = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_beginPlug")],&[])}),
                ),
                node(
                    1,
                    "STUStatescriptStateWeaponVolley",
                    Some(0),
                    json!({
                        "m_A17BE89B":3,
                        "m_numShotsPerSecond":9,
                        "m_24E351A4":var(53,false),
                        "m_3FFC9EB9":var(231,false),
                        "m_FEC435C6":180
                    }),
                ),
            ],
            json!([1]),
            json!([0]),
        );
        let mut root = Root::new(1, 42);
        root.variables.insert(53, Value::Integer(30));
        root.variables.insert(231, Value::Float(0.25));
        let mut component = Component::start(
            Arc::new(store(vec![weapon])),
            42,
            Variables::new(),
            vec![root],
            0,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let mut shots = Vec::new();
        for frame in 0..=14 {
            component.advance_frame(frame, &mut EmptyWorld).unwrap();
            shots.extend(component.take_shots().unwrap());
            if frame == 6 {
                let snapshot = component.snapshot().unwrap();
                assert!(snapshot.bindings.is_empty());
                assert_eq!(snapshot.instances[&1].variables[&53], Value::Integer(30));
                assert_eq!(snapshot.resolved_bindings[&(1, 53)], Value::Integer(29));
                assert_eq!(snapshot.resolved_bindings[&(1, 231)], Value::Float(0.25));
            }
        }
        assert_eq!(
            shots
                .iter()
                .map(|shot| (shot.frame, shot.time_ms, shot.ordinal))
                .collect::<Vec<_>>(),
            [(0, 0, 0), (6, 111, 1), (13, 222, 2)]
        );
        assert!(
            shots
                .iter()
                .all(|shot| shot.activation == 1 && shot.activation_ms == 0)
        );
        let node = component.shot_node(&shots[2]).unwrap();
        assert_eq!(
            component.evaluate_shot_config(
                &shots[2],
                node.field("m_FEC435C6").unwrap(),
                &mut EmptyWorld
            ),
            Ok(Value::Integer(180))
        );
        let snapshot = component.snapshot().unwrap();
        assert!(snapshot.pending_timers.is_empty());
        assert!(snapshot.resolved_bindings.is_empty());
        assert!(!snapshot.instances[&1].states[&0].active);
        assert_eq!(snapshot.instances[&1].variables[&53], Value::Integer(27));
    }

    struct RifleWorld(Arc<GraphStore>);

    impl World for RifleWorld {
        fn ruleset(&mut self, id: u32) -> Option<Value> {
            match id {
                0x2538 | 0x2819 | 0x32e8 => Some(Value::Float(1.0)),
                0x260f => Some(Value::Bool(true)),
                0x281a | 0x32e0 | 0x32e9 => Some(Value::Bool(false)),
                _ => None,
            }
        }

        fn entity_variable(&mut self, entity: u32, id: u32) -> Value {
            if entity != 0xd000_0010 {
                return Value::Null;
            }
            self.0
                .mode_variables(0x0230_0000_0000_0018)
                .and_then(|values| values.get(&id).copied())
                .map_or(Value::Null, Value::from)
        }

        fn native(&mut self, site: Site, config: &Json) -> Option<Value> {
            match config.get("$")?.as_str()? {
                "STU_1FF67CA6" => Some(Value::Entity(site.entity)),
                "STU_310D6529" => Some(Value::Vector([0.0; 3])),
                "STU_37F20D24" => Some(Value::Asset(0x0230_0000_0000_0018)),
                "STU_A3DEF368" if self.0.mode_variables(0x0230_0000_0000_0018).is_some() => {
                    Some(Value::Entity(0xd000_0010))
                }
                "STU_D3EB8D87" | "STU_A77AC2AC" => Some(Value::Null),
                "STU_4A101787" => Some(Value::Bool(false)),
                _ => None,
            }
        }
    }

    #[test]
    #[ignore = "requires a local 1.74 graph pack"]
    fn authored_helix_uses_its_six_second_cooldown_before_the_next_volley() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            Arc::new(GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap());
        let body = &graphs.bodies()[&0x0400_0000_0000_03cf];
        let mut world = RifleWorld(Arc::clone(&graphs));
        let mut component = Component::start(
            Arc::clone(&graphs),
            0xa000_0101,
            player_entity_variables(body, 200.0, 200.0),
            player_roots(body, &graphs).unwrap(),
            1000,
            &mut world,
            MAX_INIT_WORK,
        )
        .unwrap();
        component.advance_frame(1000, &mut world).unwrap();
        let mut shots = Vec::new();
        let cooldown = Variable {
            scope: Scope::Instance,
            id: 61,
        };
        for frame in 1001..=1411 {
            let buttons = if matches!(frame, 1030 | 1031 | 1040 | 1041 | 1410 | 1411) {
                2
            } else {
                0
            };
            component
                .advance_command(
                    Command {
                        frame,
                        buttons,
                        ..Command::default()
                    },
                    &mut world,
                )
                .unwrap();
            shots.extend(std::mem::take(&mut component.machine.shots));
            let (flags, last_ms, remaining, due) = match frame {
                1029 => (0, 16000, 0.0, None),
                1030 => (5, 16480, 5.985000133514404, Some(22480)),
                1040 => (1, 16480, 5.824999809265137, Some(22480)),
                1404 => (1, 16480, 0.0009999275207519531, Some(22480)),
                1405 => (1, 16480, 0.0, None),
                1410 => (1, 22560, 5.985000133514404, Some(28560)),
                _ => continue,
            };
            let snapshot = component.snapshot().unwrap();
            let ability = &snapshot.instances[&11].states[&31];
            assert_eq!(ability.kind, StateKind::Ability { flags }, "frame {frame}");
            assert_eq!(
                ability.ability_cooldown,
                Some(AbilityCooldown {
                    current: if frame == 1029 { 0.0 } else { 6.0 },
                    rate: 1.0,
                    last_ms,
                }),
                "frame {frame}"
            );
            assert_eq!(
                component.resolved_variable(11, cooldown),
                Some(Value::Float(remaining)),
                "frame {frame}"
            );
            assert_eq!(
                snapshot
                    .pending_timers
                    .get(&11)
                    .into_iter()
                    .flatten()
                    .find(|timer| timer.raw_state == 31 && timer.parameter == 2)
                    .map(|timer| timer.due_ms),
                due,
                "frame {frame}"
            );
        }
        assert_eq!(
            shots
                .iter()
                .map(|shot| (
                    shot.instance,
                    shot.raw_state,
                    shot.time_ms,
                    shot.activation,
                    shot.ordinal
                ))
                .collect::<Vec<_>>(),
            [(11, 16, 16480, 1, 0), (11, 16, 22560, 2, 0)]
        );
        let shot = &shots[0];
        let node = component.shot_node(shot).unwrap();
        let rocket =
            crate::projectile_flight::ProjectileBallistics::from_authored(node, |config| {
                component.evaluate_shot_config(shot, config, &mut world)
            })
            .unwrap()
            .unwrap();
        assert_eq!(rocket.definition, Some(0x0400_0000_0000_072c));
        assert_eq!((rocket.speed, rocket.lifetime), (50.0, 2.0));
        let direct = rocket.direct.unwrap();
        assert_eq!(
            (direct.amount, direct.loadout),
            (40.0, Some(0x0b90_0000_0000_008e))
        );
        let blast = rocket.blast.unwrap();
        assert_eq!(
            (blast.hit.amount, blast.hit.loadout),
            (80.0, Some(0x0b90_0000_0000_008e))
        );
        assert_eq!(blast.rings, [(1.0, 1.0), (3.0, 0.5)]);
    }

    #[test]
    #[ignore = "requires a local 1.74 graph pack"]
    fn authored_soldier_rifle_holds_releases_and_reloads_on_the_reference_clock() {
        let runtime = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        );
        let graphs =
            Arc::new(GraphStore::load(runtime.join("data/game_graphs_174.json.gz")).unwrap());
        let body = &graphs.bodies()[&0x0400_0000_0000_03cf];
        let mut world = RifleWorld(Arc::clone(&graphs));
        let mut component = Component::start(
            Arc::clone(&graphs),
            0xa000_0101,
            player_entity_variables(body, 200.0, 200.0),
            player_roots(body, &graphs).unwrap(),
            999,
            &mut world,
            MAX_INIT_WORK,
        )
        .unwrap();
        component.advance_frame(999, &mut world).unwrap();
        let mut shots = Vec::new();
        let ammo = Variable {
            scope: Scope::Instance,
            id: 53,
        };
        let reload = Variable {
            scope: Scope::Instance,
            id: 51,
        };
        for frame in 1000..=1201 {
            let buttons = match frame {
                1030..=1073 | 1200 => 1,
                1095 => 0x400,
                _ => 0,
            };
            component
                .advance_command(
                    Command {
                        frame,
                        buttons,
                        ..Command::default()
                    },
                    &mut world,
                )
                .unwrap();
            shots.extend(std::mem::take(&mut component.machine.shots));
            let expected = match frame {
                1029 => Some(30),
                1030 | 1036 => Some(29),
                1074 | 1095 | 1141 => Some(23),
                1142 | 1188 => Some(30),
                1201 => Some(29),
                _ => None,
            };
            if let Some(expected) = expected {
                assert_eq!(
                    component.resolved_variable(11, ammo),
                    Some(Value::Integer(expected)),
                    "frame {frame}, rifle states {:?}, diagnostics {:?}",
                    active(component.snapshot().unwrap(), 11),
                    component
                        .snapshot()
                        .unwrap()
                        .diagnostics
                        .iter()
                        .filter(|d| d.site.graph == 0x0254)
                        .collect::<Vec<_>>()
                );
            }
            match frame {
                1095 | 1142 => assert_eq!(
                    component.resolved_variable(11, reload),
                    Some(Value::Bool(true))
                ),
                1188 => assert_eq!(
                    component.resolved_variable(11, reload),
                    Some(Value::Bool(false))
                ),
                _ => (),
            }
        }
        assert_eq!(
            shots
                .iter()
                .map(|shot| (shot.time_ms, shot.activation, shot.ordinal))
                .collect::<Vec<_>>(),
            [
                (16480, 1, 0),
                (16591, 1, 1),
                (16702, 1, 2),
                (16813, 1, 3),
                (16924, 1, 4),
                (17036, 1, 5),
                (17147, 1, 6),
                (19200, 2, 0),
            ]
        );
        assert!(
            shots
                .iter()
                .all(|shot| shot.instance == 11 && shot.raw_state == 10)
        );
        let shot = &shots[0];
        let node = component.shot_node(shot).unwrap();
        let mut evaluate = |config: &Json| component.evaluate_shot_config(shot, config, &mut world);
        let hit = crate::game_hitscan::HitDefinition::from_authored(
            node.field("m_6396149F").unwrap(),
            &mut evaluate,
        )
        .unwrap()
        .unwrap();
        assert_eq!(hit.amount, 20.0);
        assert_eq!(hit.loadout, Some(0x0b90_0000_0000_008d));
        assert_eq!(hit.critical, 2.0);
        assert_eq!(
            hit.falloff,
            Some(crate::game_hitscan::DamageFalloff {
                start: 30.0,
                end: 50.0,
                floor: f64::from(0.3_f32),
            })
        );
        assert_eq!(
            evaluate(node.field("m_FEC435C6").unwrap()),
            Ok(Value::Integer(180))
        );
        assert!(component.take_shots().is_none());
    }

    #[test]
    fn animation_callback_routes_a_typed_self_message_to_the_live_receiver() {
        let message = json!({"$":"STUConfigVarGameMessage","m_gameMessage":"0x0240000000000001"});
        let source = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_beginPlug")],&[])}),
                ),
                node(
                    1,
                    "STUStatescriptStateAnim",
                    Some(0),
                    json!({
                        "m_86701C3E":{"$":"STUConfigVarFloat","m_value":0.025},
                        "m_A3B86BB0":plug(&[(2,"m_inPlug")],&[]),
                        "m_onFinishedPlug":plug(&[(3,"m_inPlug")],&[])
                    }),
                ),
                node(
                    2,
                    "STUStatescriptActionSendGameMessage",
                    None,
                    json!({
                        "m_gameMessage":message,
                        "m_params":[{"m_B5051BCE":"0x0A40000000000123","m_value":true}]
                    }),
                ),
                node(
                    3,
                    "STUStatescriptActionSetVar",
                    None,
                    json!({"m_out_Var":var(101,false),"m_value":true}),
                ),
            ],
            json!([1]),
            json!([0]),
        );
        let receiver = graph(
            43,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({"m_outPlug":plug(&[(1,"m_beginPlug")],&[])}),
                ),
                node(
                    1,
                    "STUStatescriptStateGameMessageEntry",
                    Some(0),
                    json!({
                        "m_gameMessage":message,
                        "m_33DA5163":plug(&[(2,"m_beginPlug")],&[1]),
                        "m_452862E6":plug(&[(3,"m_beginPlug")],&[2]),
                        "m_params":[{"m_B5051BCE":"0x0A40000000000123","m_out_Var":var(7,false)}]
                    }),
                ),
                node(2, "STUStatescriptState", Some(1), json!({})),
                node(3, "STU_691BFA55", Some(2), json!({})),
            ],
            json!([1, 2, 3]),
            json!([0]),
        );
        let mut component = Component::start(
            Arc::new(store(vec![source, receiver])),
            42,
            Variables::new(),
            vec![Root::new(1, 42), Root::new(2, 43)],
            100,
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        let initial = component.snapshot().unwrap();
        assert_eq!(initial.instances[&1].states[&0].kind, StateKind::Anim);
        assert_eq!(active(initial, 2), [0, 1]);
        assert_eq!(
            initial.pending_timers[&1],
            [
                PendingTimer {
                    due_ms: 1625,
                    raw_state: 0,
                    finish: false,
                    parameter: 1
                },
                PendingTimer {
                    due_ms: 1625,
                    raw_state: 0,
                    finish: true,
                    parameter: 0
                },
            ]
        );
        component.advance_frame(100, &mut EmptyWorld).unwrap();
        assert_eq!(active(component.snapshot().unwrap(), 2), [0, 1]);
        component.advance_frame(101, &mut EmptyWorld).unwrap();
        let snapshot = component.snapshot().unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        assert_eq!(snapshot.instances[&1].variables[&101], Value::Bool(true));
        assert!(!snapshot.instances[&1].states[&0].active);
        assert_eq!(active(snapshot, 2), [0, 2]);
        assert_eq!(snapshot.instances[&2].variables[&7], Value::Bool(true));
        assert_eq!(snapshot.instances[&2].states[&2].kind, StateKind::Effect);
        assert_eq!(snapshot.instances[&2].states[&2].start_ms, 1625);
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
