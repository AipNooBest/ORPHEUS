//! Same-time initialization of authored statescript graphs.
//!
//! This covers Entry, typed variable writes, conditional links and static
//! SubScript instances. Diagnostics make a partial snapshot unsuitable for
//! gameplay replication. Timed, message, input and gameplay actions need the
//! corresponding runtime before their graphs can be considered initialized.

use std::{
    collections::{BTreeMap, HashSet, VecDeque},
    error::Error,
    fmt,
};

use indexmap::IndexMap;
use serde_json::Value as Json;

use crate::{
    script_expr::{self, Context, EmptyContext, EvalError, Scope, Variable, equal, truthy},
    script_graph::{Body, Graph, GraphStore, Node, asset_guid},
    script_wire::{Value, Variables},
};

pub const MAX_INIT_WORK: usize = 100_000;
const MAX_PLUG_DEPTH: usize = 64;
const MAX_EVENTS_AT_START: usize = 1000;
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
}

#[derive(Clone, Debug)]
pub struct State {
    pub active: bool,
    pub activation: u16,
    pub kind: StateKind,
    ending: bool,
    pending_change: bool,
    pending_timer: bool,
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
            Self::PlugDepth => f.write_str("statescript initialization plug depth exceeded"),
        }
    }
}

impl Error for InitFault {}

pub fn initialize(
    graphs: &GraphStore,
    entity: u32,
    entity_variables: Variables,
    roots: Vec<Root>,
    world: &mut impl World,
    max_work: usize,
) -> Result<Snapshot, InitFault> {
    if max_work == 0 {
        return Err(InitFault::InvalidBudget);
    }
    let mut seen = HashSet::new();
    for root in &roots {
        if root.id == 0 || !seen.insert(root.id) {
            return Err(InitFault::InvalidRoot(root.id));
        }
    }
    let mut vm = Vm {
        graphs,
        world,
        snapshot: Snapshot {
            entity,
            variables: entity_variables,
            instances: IndexMap::new(),
            diagnostics: Vec::new(),
        },
        queues: BTreeMap::new(),
        watchers: BTreeMap::new(),
        next_id: 1,
        work_left: max_work,
        max_work,
    };
    for root in roots {
        vm.create(root.id, root.graph, None, root.variables)?;
    }
    vm.run()?;
    Ok(vm.snapshot)
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum VarKey {
    Entity(u32),
    Instance(u16, u32),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct StateKey {
    instance: u16,
    raw: u16,
}

#[derive(Clone, Copy)]
enum Event {
    Entry(u16),
    Begin(u16),
    Abort(u16),
    Changed(u16),
    Reevaluate(u16),
}

impl Event {
    fn state(self) -> Option<u16> {
        match self {
            Self::Entry(_) => None,
            Self::Begin(state)
            | Self::Abort(state)
            | Self::Changed(state)
            | Self::Reevaluate(state) => Some(state),
        }
    }
}

struct Vm<'graph, 'world, W> {
    graphs: &'graph GraphStore,
    world: &'world mut W,
    snapshot: Snapshot,
    queues: BTreeMap<u16, VecDeque<Event>>,
    watchers: BTreeMap<VarKey, Vec<StateKey>>,
    next_id: u32,
    work_left: usize,
    max_work: usize,
}

impl<'graph, W: World> Vm<'graph, '_, W> {
    fn spend(&mut self, operation: &'static str) -> Result<(), InitFault> {
        self.work_left = self.work_left.checked_sub(1).ok_or(InitFault::WorkBudget {
            limit: self.max_work,
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
                    entity: self.snapshot.entity,
                    instance: id,
                    graph,
                    node: None,
                },
                Issue::MissingGraph(graph),
            );
            return Ok(false);
        };
        self.next_id = self.next_id.max(u32::from(id) + 1);
        self.snapshot.instances.insert(
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
                self.post(id, Event::Entry(*position));
            }
        }
        Ok(true)
    }

    fn run(&mut self) -> Result<(), InitFault> {
        while self.queues.values().any(|events| !events.is_empty()) {
            self.spend("component update")?;
            let instances: Vec<_> = self.snapshot.instances.keys().copied().collect();
            for id in instances {
                let mut events = 0;
                while let Some(event) = self.queues.get_mut(&id).and_then(VecDeque::pop_front) {
                    if events == MAX_EVENTS_AT_START {
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
                        Event::Abort(raw) => self.end(key(raw), 0)?,
                        Event::Changed(raw) => self.changed(key(raw)),
                        Event::Reevaluate(raw) => self.reevaluate(key(raw), 0)?,
                    }
                }
            }
        }
        Ok(())
    }

    fn graph(&self, id: u16) -> &'graph Graph {
        let graph = self.snapshot.instances[&id].graph;
        self.graphs.graph(graph).expect("registered graph")
    }

    fn site(&self, instance: u16, node: &Node) -> Site {
        Site {
            entity: self.snapshot.entity,
            instance,
            graph: self.snapshot.instances[&instance].graph,
            node: Some(node.pos),
        }
    }

    fn report(&mut self, site: Site, issue: Issue) {
        let diagnostic = Diagnostic { site, issue };
        if !self.snapshot.diagnostics.contains(&diagnostic) {
            self.snapshot.diagnostics.push(diagnostic);
        }
    }

    fn post(&mut self, id: u16, event: Event) {
        self.queues.entry(id).or_default().push_back(event);
    }

    fn state(&self, key: StateKey) -> Option<&State> {
        self.snapshot
            .instances
            .get(&key.instance)?
            .states
            .get(&key.raw)
    }

    fn state_mut(&mut self, key: StateKey) -> Option<&mut State> {
        self.snapshot
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
            snapshot: &self.snapshot,
            world: self.world,
            site,
            reads: Vec::new(),
        };
        let result = script_expr::evaluate(config, &mut context);
        let reads = context.reads;
        if let Some(watcher) = watcher.filter(|&key| {
            self.state(key)
                .is_some_and(|state| state.active && !state.ending)
        }) {
            for key in reads {
                let watchers = self.watchers.entry(key).or_default();
                if !watchers.contains(&watcher) {
                    watchers.push(watcher);
                }
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

    fn write(&mut self, key: VarKey, value: Value) {
        let old = stored(&self.snapshot, key);
        let changed = !same(&old, &value);
        match key {
            VarKey::Entity(id) => self.snapshot.variables.insert(id, value),
            VarKey::Instance(instance, id) => self.snapshot.instances[&instance]
                .variables
                .insert(id, value),
        };
        if !changed {
            return;
        }
        let watchers = self.watchers.get(&key).cloned().unwrap_or_default();
        for watcher in watchers {
            if let Some(state) = self.state_mut(watcher) {
                if state.active && !state.pending_change {
                    state.pending_change = true;
                    self.post(watcher.instance, Event::Changed(watcher.raw));
                }
            }
        }
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
                    let event = if input == ABORT_PLUG {
                        Event::Abort(raw)
                    } else {
                        Event::Begin(raw)
                    };
                    self.post(id, event);
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
                let mut items = match stored(&self.snapshot, target) {
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
                    self.work_left =
                        self.work_left
                            .checked_sub(extra)
                            .ok_or(InitFault::WorkBudget {
                                limit: self.max_work,
                                operation: "SetVar array extension",
                            })?;
                    items.resize(index + 1, Value::Null);
                }
                items[index] = value;
                self.write(target, Value::Array(items));
                return Ok(true);
            }
        }
        self.write(target, value);
        Ok(true)
    }

    fn begin(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let id = key.instance;
        let Some(node) = self.graph(id).state(usize::from(key.raw)) else {
            self.report(
                Site {
                    entity: self.snapshot.entity,
                    instance: id,
                    graph: self.graph(id).index,
                    node: None,
                },
                Issue::MissingState(key.raw),
            );
            return Ok(());
        };
        if self.state(key).is_some_and(|state| state.active) {
            return Ok(());
        }
        let kind = match node.class.as_str() {
            "STUStatescriptState" | "STU_9D7BF987" => StateKind::Plain,
            "STUStatescriptStateBooleanSwitch" => StateKind::BooleanSwitch(false),
            "STU_37D754C8" | "STUStatescriptStateSubScript" => StateKind::SubScript { child: None },
            "STUStatescriptStateGameMessageEntry" => StateKind::MessageIdle,
            _ => {
                self.report(
                    self.site(id, node),
                    Issue::UnsupportedState(node.class.clone()),
                );
                return Ok(());
            }
        };
        let activation = self
            .state(key)
            .map_or(1, |state| state.activation.wrapping_add(1));
        for raw in raw_indices(array_field(&node.fields, "m_exclusiveStateIndices")) {
            self.end(StateKey { instance: id, raw }, depth + 1)?;
            self.drop_begin(id, raw);
        }
        self.snapshot.instances[&id].states.insert(
            key.raw,
            State {
                active: true,
                activation,
                kind,
                ending: false,
                pending_change: false,
                pending_timer: false,
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
            StateKind::Plain => (),
        }
        self.follow(id, node, "m_onBeginPlug", Some(key), depth)?;
        self.follow(id, node, "m_subgraphPlug", Some(key), depth)?;
        self.try_complete(key, node, depth)
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
        let child = u16::try_from(self.next_id).map_err(|_| InitFault::InstanceIdExhausted)?;
        if self.create(child, graph, Some((id, key.raw)), vars)? {
            // The reference sets child binding bases directly before Entry.
            self.snapshot.variables.extend(entity_vars);
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
                self.post(key.instance, Event::Abort(key.raw));
                break;
            }
        }
        Ok(())
    }

    fn changed(&mut self, key: StateKey) {
        if let Some(state) = self.state_mut(key) {
            state.pending_change = false;
            if state.active
                && !state.pending_timer
                && matches!(state.kind, StateKind::BooleanSwitch(_))
            {
                state.pending_timer = true;
                self.post(key.instance, Event::Reevaluate(key.raw));
            }
        }
    }

    fn reevaluate(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
        let Some(state) = self.state_mut(key).filter(|state| state.active) else {
            return Ok(());
        };
        state.pending_timer = false;
        let StateKind::BooleanSwitch(current) = state.kind else {
            return Ok(());
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
            self.try_complete(key, node, depth)?;
        }
        Ok(())
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

    fn end(&mut self, key: StateKey, depth: usize) -> Result<(), InitFault> {
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
        if let Some(queue) = self.queues.get_mut(&id) {
            queue.retain(|event| event.state() != Some(key.raw));
        }
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
        self.watchers.retain(|_, watchers| {
            watchers.retain(|watcher| *watcher != key);
            !watchers.is_empty()
        });
        self.follow(id, node, "m_onAbortPlug", Some(key), depth)?;
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
            self.end(StateKey { instance: id, raw }, depth)?;
            self.drop_begin(id, raw);
        }
        Ok(())
    }

    fn drop_begin(&mut self, id: u16, raw: u16) {
        if let Some(queue) = self.queues.get_mut(&id) {
            queue.retain(|event| !matches!(event, Event::Begin(state) if *state == raw));
        }
    }

    fn destroy(&mut self, id: u16) -> Result<(), InitFault> {
        let mut pending = vec![id];
        while let Some(id) = pending.pop() {
            self.spend("instance teardown")?;
            pending.extend(
                self.snapshot
                    .instances
                    .iter()
                    .filter_map(|(&child, instance)| {
                        instance
                            .parent
                            .is_some_and(|(parent, _)| parent == id)
                            .then_some(child)
                    }),
            );
            self.snapshot.instances.shift_remove(&id);
            self.queues.remove(&id);
            self.watchers.retain(|key, watchers| {
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
    snapshot: &'a Snapshot,
    world: &'a mut W,
    site: Site,
    reads: Vec<VarKey>,
}

impl<W: World> InstanceContext<'_, W> {
    fn read(&mut self, key: VarKey) -> Value {
        if !self.reads.contains(&key) {
            self.reads.push(key);
        }
        stored(self.snapshot, key)
    }
}

impl<W: World> Context for InstanceContext<'_, W> {
    fn variable(&mut self, variable: Variable) -> Value {
        self.read(source_key(self.site.instance, variable))
    }

    fn parent_variable(&mut self, levels: u8, variable: Variable) -> Value {
        let mut id = self.site.instance;
        for _ in 0..levels {
            let Some((parent, _)) = self.snapshot.instances[&id].parent else {
                return Value::Null;
            };
            id = parent;
        }
        self.read(source_key(id, variable))
    }

    fn parent_entity(&mut self, _levels: u8) -> Value {
        self.owner()
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
        self.world.native(self.site, config)
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

    struct ReferenceWorld;

    impl World for ReferenceWorld {
        fn native(&mut self, site: Site, config: &Json) -> Option<Value> {
            // Values observed in the Python fixtures, for this test only.
            match (site.graph, config.get("$")?.as_str()?) {
                (0x0c18, "STU_AC829876") => Some(Value::Bool(true)),
                (0x0c18, "STU_4A101787" | "STU_A77AC2AC") => Some(Value::Null),
                (0x288a, "STU_37F20D24") => Some(Value::Null),
                _ => None,
            }
        }
    }

    fn active(snapshot: &Snapshot, id: u16) -> Vec<u16> {
        snapshot.instances[&id]
            .states
            .iter()
            .filter_map(|(&raw, state)| state.active.then_some(raw))
            .collect()
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
