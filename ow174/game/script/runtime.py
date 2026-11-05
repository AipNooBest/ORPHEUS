"""The statescript runtime: graph instances on an entity, their states, variables and events, run the way
the client runs them, so that the server reaches the same states the owning client predicts.

Read in the client (the addresses are in the docstrings below):
- Time is int64 milliseconds. Command frame K is run as the window [16K, 16K+15]: an update of every
  instance at each time an event is due, and one at the window's end (component update 0x7FF78AA9C910,
  step 0x7FF78AA9DE30). An update drains the instance's events up to now, ticks the ticking states and
  runs the deferred list, until the deferred list is empty (0x7FF78AAA27C0).
- Events (dispatcher 0x7FF78AAA1690): 0 a state op (begin, finish, abort), 1 a timer, 2 a deferred
  completion check, 3 an Entry, 8 a button edge, 9 "a watched variable changed". Inserted after every event
  of the same time (FIFO), never in the past.
- A plug walk (0x7FF78AA83FA0) follows the links in order. A state target only gets a begin (or abort)
  request: an event at now, so a state never begins inside a walk. Actions and conditions run at once.
  The server runs every node that is not client-only.
- DoBeginState 0x7FF78AA80790: exclusive states end, OnBegin, OnEnter, m_onBeginPlug, m_subgraphPlug, then
  the completion check (the transitions; a transition that is taken ends the state). DoAbortState
  0x7FF78AA80480 / DoFinishState 0x7FF78AA80B10: queued events dropped, the subgraph's states (the plug's
  m_FF3DAF1E list) aborted, the class's own end, then m_onAbortPlug / m_onFinishedPlug and m_onEndPlug.
- A variable is a base value plus links: the states that override it while they are active, in order, the
  last active one wins (resolver 0x7FF78AAB70B0). A Stack's entries are its links. Every variable a state
  reads while it evaluates registers the state as a watcher; a change sends it an event.

Node classes (nodes.py) subclass State and register their action handlers here.
"""

import bisect
import itertools
import logging
from dataclasses import dataclass

from ow174.game.script import expr
from ow174.game.script.expr import ENTITY, INSTANCE, Context, Entity
from ow174.game.script.graph import Graph, Node, is_plug

log = logging.getLogger("ow174.script")

# Event kinds.
STATE_OP, TIMER, CHECK, ENTRY, BUTTON, CHANGED, MESSAGE = 0, 1, 2, 3, 8, 9, 10
BEGIN, FINISH, ABORT = 0, 1, 2
NO_BUTTON = 171
EVENTS_PER_TIME = 1000  # the client stops an instance that queues more at one time
ABORT_PLUG = "m_F198FD3A"
SUBGRAPH_STATES = "m_FF3DAF1E"

STATE_CLASSES: dict[str, type["State"]] = {}  # node class -> runtime state class (nodes.py fills it)
HANDLERS: dict[str, object] = {}  # node class -> handler(instance, node, caller) -> accepted
ENTRY_CLASSES = {"STUStatescriptEntry", "STU_571CC72E"}  # run at Init (EntryClientOnlySharedVars agrees too)
MESSAGE_ENTRIES = {"STUStatescriptEntryGameMessage"}


def state_class(*names: str):
    def register(cls):
        for name in names:
            STATE_CLASSES[name] = cls
        return cls

    return register


def handler(*names: str):
    def register(function):
        for name in names:
            HANDLERS[name] = function
        return function

    return register


@dataclass
class Link:
    """A state that overrides a variable while it is active: a Stack entry, a ChaseVar or volley output."""

    state: "State"
    slot: int = 0
    priority: float = 0.0
    above: bool = True  # among equal priorities a new entry goes above the others
    stack: bool = False  # a stack entry (Stack, an Ability's push): sent as a binding


class Var:
    """One variable: the stored (base) value, the links that override it, the states that watch it."""

    __slots__ = ("base", "key", "links", "watchers")

    def __init__(self, key: tuple) -> None:
        self.key = key  # (instance id or 0 for the entity, var id)
        self.base = None
        self.links: list[Link] = []
        self.watchers: dict[State, None] = {}

    def value(self):
        value = self.base
        for link in self.links:
            if link.state.active:
                value = link.state.output(link.slot)
        return value

    def stack_states(self) -> list["State"]:
        return [link.state for link in self.links]


@dataclass
class Event:
    time: int
    seq: int
    kind: int
    state: "State | None" = None
    op: int = 0
    param: int = 0
    node: Node | None = None  # an Entry, or a message entry
    button: int = 0
    pressed: bool = False
    message: tuple | None = None  # (message GUID, params, sender)
    transient: bool = False  # a re-check at now, never in an owner frame's event list

    def key(self):
        return self.time, self.seq


class InstanceContext(Context):
    """Evaluation context of one instance: variable reads register `watcher` (a state) when one is set."""

    def __init__(self, instance: "Instance", watcher: "State | None") -> None:
        self.instance = instance
        self.watcher = watcher
        self.rng = instance.component.rng

    def _read(self, var: Var | None):
        if var is None:
            return None
        if self.watcher is not None:
            self.watcher.watch(var)
        return var.value()

    def var(self, scope: int, var: int):
        return self._read(self.instance.find_var(scope, var, create=self.watcher is not None))

    def proxy_var(self, levels: int, scope: int, var: int):
        instance = self.instance
        for _ in range(levels):
            instance = instance.parent
            if instance is None:
                return None
        return self._read(instance.find_var(scope, var, create=self.watcher is not None))

    def proxy_entity(self, levels: int):
        return Entity(self.instance.component.entity)

    def param(self, var: int):
        for provider in reversed(self.instance.params):
            if provider is not None and (var in provider or None in provider):
                return provider.get(var, provider.get(None))
        return None

    def owner(self):
        return Entity(self.instance.component.entity)

    def member(self, entity: int, var: int):
        component = self.instance.component
        if entity == component.entity:
            return self._read(component.entity_var(var, create=self.watcher is not None))
        return component.world.entity_var(entity, var)

    def position(self, entity: int):
        return self.instance.component.world.position(entity)

    def ruleset(self, key: int):
        found = self.var(ENTITY, key)
        if found is not None:
            return found
        value = self.instance.component.world.ruleset(key)
        return 0 if value is None else value

    def would_stack_to_top(self, var, priority):
        if var is None:
            return False
        target = self.instance.find_var(var[0], var[1], create=True)
        if self.watcher is not None:
            self.watcher.watch(target)
        return would_stack_to_top(target, priority)

    def native(self, cfg: dict):
        """A config var the world answers: it is no variable, so a state that reads it re-checks every
        update (the client's poll list, 0x7FF78AAA5C20)."""
        if self.watcher is not None:
            self.watcher.natives.add(cfg.get("$"))
            if not self.watcher.polled:
                self.watcher.polled = True
                self.watcher.ticks()
        return self.instance.component.world.native(cfg, self.instance)


def insert_link(var: Var, link: Link) -> None:
    """A Stack push (0x7FF78AA82F90 -> the sort of PS4 sub_172E790): entries ascending by priority, the last
    is the top; a new entry goes after every lower priority, above the equal ones when its flag is set,
    below them otherwise. Pushing the same state again replaces its entry."""
    var.links = [item for item in var.links if item.state is not link.state]
    at = len(var.links)
    for index, item in enumerate(var.links):
        if item.priority > link.priority or (item.priority == link.priority and not link.above):
            at = index
            break
    var.links.insert(at, link)


def would_stack_to_top(var: Var, priority: tuple[float, bool], exclude: "State | None" = None) -> bool:
    """Whether an entry with this priority would be the top of the stack variable now."""
    level, above = priority
    for link in var.links:
        if link.state is exclude or not link.state.active:
            continue
        if link.priority > level or (link.priority == level and not above):
            return False
    return True


class World:
    """What the runtime asks outside its entity: the owner's hero and mode (for ruleset defaults), other
    entities, config vars the server cannot compute, and game messages to other entities. The base
    answers like an empty world."""

    def __init__(self, hero: int = 0, mode: int = 0) -> None:
        self.hero = hero
        self.mode = mode

    def ruleset(self, key: int):
        from ow174.game.script import rulesets

        return rulesets.default(key, self.hero, self.mode)

    def entity_var(self, entity: int, var: int):
        return None

    def position(self, entity: int):
        return expr.Vec3(0.0, 0.0, 0.0)

    def native(self, cfg: dict, instance: "Instance"):
        return expr.default_native(cfg)

    def send_message(self, target: int, message: int, params: dict, sender: int) -> None:
        expr.warn_once(f"message{message:X}", "game message %X to %08X is not delivered", message, target)


class Component:
    """The statescript component of one entity: its clock, its instances (in creation order) and its
    entity-scope variables."""

    def __init__(self, entity: int, frame_ms: int = 16, world: World | None = None, seed: int = 1) -> None:
        self.entity = entity
        self.frame_ms = frame_ms
        self.world = world or World()
        self.rng = expr.random.Random(seed)
        self.frame = 0
        self.now = 0
        self.instances: dict[int, Instance] = {}
        self.vars: dict[int, Var] = {}
        self.held: list[int] = []  # logical buttons held down, in press order
        self.button = NO_BUTTON  # the button of the edge being handled
        self.disabled: dict[int, list[State]] = {}  # button -> active DisableLogicalButton states
        self.seq = itertools.count()
        self.next_id = 1
        self.destroyed: list[Instance] = []
        self.shots: list = []  # (volley state, shot number, time) of the shots fired, for combat.py
        self.owner_dead = False  # the body's health is 0 (combat.py): presses and shots stop

    # --- variables ---------------------------------------------------------------------------------

    def changed(self, var: Var, before) -> None:
        """Tell a variable's watchers that it changed, when its value did: an event for each (9)."""
        if expr.same(before, var.value()):
            return
        for state in list(var.watchers):
            if state.active and not state.flags_changed:
                state.flags_changed = True
                state.instance.post(Event(self.now, 0, CHANGED, state=state))

    def set(self, var: int, value) -> None:
        """Set an entity variable from outside the graphs (the server's own code)."""
        target = self.entity_var(var)
        before = target.value()
        target.base = value
        self.changed(target, before)

    def entity_var(self, var: int, create: bool = True) -> Var | None:
        found = self.vars.get(var)
        if found is None and create:
            found = self.vars[var] = Var((0, var))
        return found

    # --- instances ---------------------------------------------------------------------------------

    def allocate_id(self) -> int:
        while self.next_id in self.instances:
            self.next_id += 1
        found = self.next_id
        self.next_id += 1
        return found

    def create(
        self,
        graph: Graph,
        instance_id: int | None = None,
        parent: "Instance | None" = None,
        parent_state: int | None = None,
        values: dict | None = None,
        init: bool = True,
    ) -> "Instance":
        """A new instance; with `init` its Entries are queued at now (they run in its next update)."""
        if instance_id is None:
            instance_id = self.allocate_id()
        if instance_id in self.instances:
            raise ValueError(f"instance {instance_id} exists")
        instance = Instance(self, instance_id, graph, parent, parent_state)
        self.instances[instance_id] = instance
        self.next_id = max(self.next_id, instance_id + 1)
        for (scope, var), value in (values or {}).items():
            instance.find_var(scope, var).base = value
        if init:
            instance.init()
        return instance

    def destroy(self, instance: "Instance") -> None:
        if instance.id not in self.instances:
            return
        instance.teardown = True
        for child in [item for item in self.instances.values() if item.parent is instance]:
            self.destroy(child)
        for state in list(instance.states.values()):
            state.end(False)
        instance.stopped = True
        instance.queue.clear()
        del self.instances[instance.id]
        self.destroyed.append(instance)

    # --- time --------------------------------------------------------------------------------------

    def next_event_time(self) -> int | None:
        times = [item.queue[0].time for item in self.instances.values() if item.queue and not item.stopped]
        return min(times) if times else None

    def edges(self, buttons: list[tuple[int, bool]]) -> None:
        """Button edges at now, on every instance (0x7FF789C66AA0 queues each on all the instances)."""
        for button, pressed in buttons:
            for instance in list(self.instances.values()):
                instance.post(Event(self.now, 0, BUTTON, button=button, pressed=pressed))

    def run_frame(self, frame: int, buttons: list[tuple[int, bool]] = ()) -> None:
        """Command frame `frame`: its edges at 16F, then an update at every due event time in [16F, 16F+15]
        and one at 16F+15."""
        self.frame = frame
        self.now = frame * self.frame_ms
        end = self.now + self.frame_ms - 1
        self.edges(list(buttons))
        for _ in range(10000):
            due = self.next_event_time()
            self.now = end if due is None or due > end else max(due, self.now)
            self.update()
            if self.now >= end:
                due = self.next_event_time()
                if due is None or due > end:
                    break
        self.now = end

    def run_now(self) -> None:
        """Update every instance at the current time until nothing more is due now (spawn)."""
        for _ in range(10000):
            self.update()
            due = self.next_event_time()
            if due is None or due > self.now:
                break

    def update(self) -> None:
        for instance in list(self.instances.values()):
            if instance.id in self.instances:
                instance.update()


class Instance:
    """One graph instance."""

    def __init__(self, component: Component, instance_id: int, graph: Graph, parent=None, parent_state=None):
        self.component = component
        self.id = instance_id
        self.graph = graph
        self.parent: Instance | None = parent
        self.parent_state = parent_state  # the parent graph's m_states index of the SubScript state
        self.vars: dict[int, Var] = {}
        self.states: dict[int, State] = {}
        self.queue: list[Event] = []
        self.ticking: list[State] = []
        self.deferred: list[tuple[float, int, State]] = []
        self.params: list[dict | None] = []
        self.stopped = False
        self.teardown = False
        self.initialized = False
        self.start = 0

    def __repr__(self) -> str:
        return f"<Instance {self.id} {self.graph.index:04X}>"

    # --- variables ---------------------------------------------------------------------------------

    def find_var(self, scope: int, var: int, create: bool = True) -> Var | None:
        if scope == ENTITY:
            return self.component.entity_var(var, create)
        found = self.vars.get(var)
        if found is None and create:
            found = self.vars[var] = Var((self.id, var))
        return found

    def context(self, watcher: "State | None" = None) -> InstanceContext:
        return InstanceContext(self, watcher)

    def evaluate(self, cfg, watcher: "State | None" = None):
        return expr.evaluate(cfg, self.context(watcher))

    def write(self, target: tuple[int, int] | None, value) -> None:
        """A plain assignment with the value's own type (0x7FF78AAB5650)."""
        if target is None:
            return
        var = self.find_var(*target)
        before = var.value()
        var.base = value
        for link in list(var.links):
            if link.state.active:
                link.state.var_written(var)
        self.changed(var, before)

    def changed(self, var: Var, before) -> None:
        self.component.changed(var, before)

    # --- states ------------------------------------------------------------------------------------

    def state(self, index: int) -> "State | None":
        """The runtime state of an m_states index, made on first use (0x7FF78AAA1460)."""
        found = self.states.get(index)
        if found is None:
            node = self.graph.state(index)
            if node is None:
                return None
            found = self.states[index] = STATE_CLASSES.get(node.cls, State)(self, node, index)
        return found

    def node_state(self, node: Node) -> "State | None":
        return self.state(node.state) if node.state is not None else None

    # --- events ------------------------------------------------------------------------------------

    def post(self, event: Event) -> None:
        """Queue an event: never in the past, after every event of the same time (0x7FF78AAA2990)."""
        event.time = max(event.time, self.component.now)
        event.seq = next(self.component.seq)
        bisect.insort(self.queue, event, key=Event.key)

    def cancel(self, state: "State") -> None:
        """Drop every queued event of a state (0x7FF78AAA12F0)."""
        self.queue = [event for event in self.queue if event.state is not state]

    def drop_begins(self, states: list["State"]) -> None:
        """Drop the begin requests queued at now for these states (0x7FF78AAA10F0)."""
        now = self.component.now
        drop = {id(state) for state in states}
        self.queue = [
            event
            for event in self.queue
            if not (event.kind == STATE_OP and event.op == BEGIN and event.time == now)
            or id(event.state) not in drop
        ]

    def timers(self) -> list[Event]:
        """The pending events the owner frame's event list can carry: timers and finish requests."""
        return [
            event
            for event in self.queue
            if event.state is not None
            and not event.transient
            and (event.kind == TIMER or (event.kind == STATE_OP and event.op == FINISH))
            and event.state.networked
        ]

    # --- running -----------------------------------------------------------------------------------

    def init(self) -> None:
        """Init (0x7FF78AAA3F00): every Entry that runs on the server is queued at the start time."""
        self.initialized = True
        self.start = self.component.now
        for node in self.graph.entries:
            if node is not None and not node.client_only and node.cls in ENTRY_CLASSES:
                self.post(Event(self.start, 0, ENTRY, node=node))

    def update(self) -> None:
        """One update at the component's now (0x7FF78AAA27C0)."""
        if self.stopped or not self.initialized:
            return
        for _ in range(1000):
            self.drain()
            for state in list(self.ticking):
                if state.active and state.ticked_at != self.component.now:
                    state.ticked_at = self.component.now
                    state.tick()
            if not self.run_deferred():
                break

    def drain(self) -> None:
        now = self.component.now
        count = 0
        while self.queue and self.queue[0].time <= now and not self.stopped:
            event = self.queue.pop(0)
            count += 1
            if count > EVENTS_PER_TIME:
                log.warning("[script] instance %d: too many events at %d ms", self.id, now)
                self.stopped = True
                return
            self.dispatch(event)

    def dispatch(self, event: Event) -> None:
        state = event.state
        if event.kind == STATE_OP:
            if event.op == BEGIN:
                state.begin()
            else:
                state.end(event.op == FINISH)
        elif event.kind == TIMER:
            if state.active:
                state.timer(event.param)
                state.try_complete()
            else:
                state.timer_inactive(event.param)
        elif event.kind == CHECK:
            state.flags_check = False
            if state.active:
                state.try_complete()
        elif event.kind == CHANGED:
            state.flags_changed = False
            if state.active:
                state.dependency_changed()
        elif event.kind == ENTRY:
            self.run_node(event.node, None)
        elif event.kind == BUTTON:
            self.button_edge(event.button, event.pressed)
        elif event.kind == MESSAGE:
            self.deliver(event)

    def button_edge(self, button: int, pressed: bool) -> None:
        """A logical-button edge (event 8): the held list, then every LogicalButton/Ability state of the
        graph's m_logicalButtonStateIndices, active or not."""
        component = self.component
        component.button = button
        if pressed and button not in component.held:
            component.held.append(button)
        elif not pressed and button in component.held:
            component.held.remove(button)
        for index in self.graph.fields.get("m_680A2CB2") or []:
            state = self.state(index)
            if state is not None:
                state.button(button, pressed)
        component.button = NO_BUTTON

    def run_deferred(self) -> bool:
        """The deferred list (Defer), from the last entry down. Returns whether something ran."""
        if not self.deferred:
            return False
        work, self.deferred = self.deferred, []
        for _, _, state in reversed(work):
            if state.active:
                state.timer(1)
        return True

    # --- plugs -------------------------------------------------------------------------------------

    def accepts(self, node: Node | None) -> bool:
        """The server's node filter: everything that is not client-only."""
        return node is not None and not node.client_only

    def follow(self, node: Node, path: str, caller: "State | None" = None) -> bool:
        """Follow a plug of a node (0x7FF78AA83FA0). Returns whether a link was taken."""
        plug = node.field(path)
        if not is_plug(plug):
            return False
        taken = False
        for position, input_path in plug["links"]:
            if self.stopped:
                break
            target = self.graph.node(position) if position is not None else None
            if target is None:
                taken = True
                continue
            if not self.accepts(target):
                continue
            if target.is_state:
                state = self.node_state(target)
                if state is not None:
                    taken = state.plug_enter(input_path) or taken
                continue
            taken = self.run_node(target, None if target.is_action else caller) or taken
        return taken

    def follow_states(self, node: Node, path: str) -> list["State"]:
        """The states a subgraph plug lists (m_FF3DAF1E)."""
        plug = node.field(path)
        if not is_plug(plug):
            return []
        found = []
        for index in plug.get(SUBGRAPH_STATES) or []:
            state = self.state(index) if index is not None else None
            if state is not None:
                found.append(state)
        return found

    def exit_subgraph(self, node: Node, path: str) -> None:
        """Abort the states of a subgraph and drop their begins queued at now (0x7FF78AA7FD00); not while
        the instance is torn down."""
        if self.teardown:
            return
        states = self.follow_states(node, path)
        for state in states:
            if state.active:
                state.end(False)
        self.drop_begins(states)

    def run_node(self, node: Node, caller: "State | None") -> bool:
        """Run an action, condition or entry now; returns whether it took a path."""
        function = HANDLERS.get(node.cls)
        if function is None:
            if node.cls not in ENTRY_CLASSES and node.cls not in MESSAGE_ENTRIES:
                expr.warn_once(node.cls, "node class %s has no model: it only follows m_outPlug", node.cls)
            self.follow(node, "m_outPlug", None)
            return True
        return function(self, node, caller)

    # --- game messages -----------------------------------------------------------------------------

    def deliver(self, event: Event) -> None:
        message, params, sender = event.message
        node = event.node
        if node is None:
            return
        self.params.append(params)
        try:
            if node.is_state:
                state = self.node_state(node)
                if state is not None and state.active:
                    state.message(message, params, sender)
            else:
                outs = node.field("m_params") or []
                for item in outs:
                    if isinstance(item, dict):
                        target = expr.lvalue(item.get("m_out_Var"))
                        key = expr.guid_of(item.get("m_B5051BCE")) & 0xFFFF
                        if target is not None:
                            self.write(target, params.get(key))
                self.follow(node, "m_outPlug", None)
        finally:
            self.params.pop()


class State:
    """A state node's runtime object: the base class is the plain State (7C37840C)."""

    def __init__(self, instance: Instance, node: Node, index: int) -> None:
        self.instance = instance
        self.node = node
        self.index = index
        self.active = False
        self.ending = False
        self.counter = 0  # activation counter (u16)
        self.start = 0
        self.ended_at = 0
        self.ticked_at = None
        self.watched: dict[Var, None] = {}
        self.flags_changed = False
        self.flags_check = False
        self.polled = False
        self.natives: set[str] = set()  # the classes of the world's config vars it read (see BodyScript)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} i{self.instance.id} st{self.index} {'on' if self.active else 'off'}>"

    @property
    def networked(self) -> bool:
        return not self.node.client_only and not self.node.server_only

    @property
    def now(self) -> int:
        return self.instance.component.now

    # --- requests (events at now) ------------------------------------------------------------------

    def request(self, op: int, delay: int = 0) -> None:
        self.instance.post(Event(self.now + delay, 0, STATE_OP, state=self, op=op))

    def request_finish(self, delay: int = 0) -> None:
        self.request(FINISH, delay)

    def timer_at(self, delay: int, param: int = 1, transient: bool = False) -> None:
        self.instance.post(Event(self.now + delay, 0, TIMER, state=self, param=param, transient=transient))

    def plug_enter(self, input_path: str) -> bool:
        """A link enters the state (base 0x7FF78AA83D80): the abort plug asks for an abort, others a begin."""
        if input_path == ABORT_PLUG:
            self.request(ABORT)
        else:
            self.request(BEGIN)
        return True

    # --- begin and end -----------------------------------------------------------------------------

    def begin(self, backdate: int = 0) -> None:
        """DoBeginState (0x7FF78AA80790)."""
        if self.active:
            self.reentry()
            return
        self.counter = (self.counter + 1) & 0xFFFF
        indexes = self.node.fields.get("m_exclusiveStateIndices") or []
        exclusive = [self.instance.state(index) for index in indexes]
        for state in exclusive:
            if state is not None and state.active:
                state.end(False)
        self.instance.drop_begins([state for state in exclusive if state is not None])
        self.active = True
        self.start = self.now - backdate
        self.ticked_at = self.now
        self.on_begin()
        self.on_enter()
        if not self.active:
            return
        self.instance.follow(self.node, "m_onBeginPlug", self)
        self.instance.follow(self.node, "m_subgraphPlug", self)
        if self.active:
            self.try_complete()

    def end(self, finished: bool) -> None:
        """DoFinishState (finished) / DoAbortState (0x7FF78AA80B10 / 0x7FF78AA80480)."""
        if not self.active or self.ending:
            return
        self.ending = True
        self.instance.cancel(self)
        self.instance.exit_subgraph(self.node, "m_subgraphPlug")
        self.on_end(finished)
        self.unwatch()
        self.polled = False
        if self in self.instance.ticking:
            self.instance.ticking.remove(self)
        self.active = False
        self.ending = False
        self.ended_at = self.now
        if self.instance.teardown:
            return
        self.instance.follow(self.node, "m_onFinishedPlug" if finished else "m_onAbortPlug", self)
        self.instance.follow(self.node, "m_onEndPlug", self)

    def try_complete(self) -> None:
        """TryComplete (0x7FF78AA81570): when IsComplete, the state queues its own abort at now."""
        if self.active and not self.ending and self.is_complete():
            self.request(ABORT)

    def is_complete(self) -> bool:
        """Base IsComplete (0x7FF78AA83EC0): the first transition plug that takes a link."""
        for number, plug in enumerate(self.node.fields.get("m_transitionPlug") or []):
            if is_plug(plug) and self.instance.follow(self.node, f"m_transitionPlug[{number}]", self):
                return True
        return False

    # --- watching ----------------------------------------------------------------------------------

    def watch(self, var: Var) -> None:
        if self.active and not self.ending:
            var.watchers[self] = None
            self.watched[var] = None

    def unwatch(self) -> None:
        for var in self.watched:
            var.watchers.pop(self, None)
        self.watched.clear()

    def evaluate(self, cfg, watch: bool = True):
        return self.instance.evaluate(cfg, self if watch else None)

    def ticks(self) -> None:
        if self not in self.instance.ticking:
            self.instance.ticking.append(self)

    # --- class hooks -------------------------------------------------------------------------------

    def on_begin(self) -> None:
        """OnBegin (vt+0xB8): before the base plugs."""

    def on_enter(self) -> None:
        """OnEnter (vt+0x60)."""

    def reentry(self) -> None:
        """A begin while active (vt+0x68)."""

    def on_end(self, finished: bool) -> None:
        """OnFinish/OnAbort and OnEnd (vt+0xA0/0xA8, vt+0xB0)."""

    def timer(self, param: int) -> None:
        """A timer of an active state (vt+0x90)."""

    def timer_inactive(self, param: int) -> None:
        """A timer of an inactive state (vt+0x98)."""

    def tick(self) -> None:
        """Once per update while registered as ticking (vt+0x70); a polled state re-checks."""
        if self.polled:
            self.dependency_changed()

    def dependency_changed(self) -> None:
        """A watched variable changed (vt+0xC8)."""

    def button(self, button: int, pressed: bool) -> None:
        """A logical-button edge (vt+0x128), active or not."""

    def message(self, message: int, params: dict, sender: int) -> None:
        """A game message for this state (GameMessageEntry)."""

    def var_written(self, var: Var) -> None:
        """Someone wrote a variable this state is linked to (vt+0xD0)."""

    def stack_changed(self, var: Var) -> None:
        """An entry was pushed on or popped from a stack variable this state is on (vt+0xD8)."""

    def output(self, slot: int):
        """The value this state gives a variable it is linked to (vt+0x188)."""
        return None

    def payload(self) -> dict:
        """The class's owner-frame payload, as values the encoder writes."""
        return {}

    # --- links -------------------------------------------------------------------------------------

    def link(self, target, slot: int = 0, priority: float = 0.0, above: bool = True, stack: bool = False):
        var = self.instance.find_var(*target)
        before = var.value()
        insert_link(var, Link(self, slot, priority, above, stack))
        self.instance.changed(var, before)
        for state in var.stack_states():
            state.stack_changed(var)
        return var

    def unlink(self, var: Var, bake: bool = False) -> None:
        before = var.value()
        if bake:
            var.base = self.output(0)
        var.links = [item for item in var.links if item.state is not self]
        self.instance.changed(var, before)
        for state in var.stack_states():
            state.stack_changed(var)


def run_entry(instance: Instance, node: Node, caller: State | None) -> bool:
    return instance.follow(node, "m_outPlug", None)


for _name in ENTRY_CLASSES:
    HANDLERS[_name] = run_entry


def message_entries(instance: Instance, message: int) -> list[Node]:
    """The EntryGameMessage nodes and active GameMessageEntry states of an instance for a message."""
    found = []
    for node in instance.graph.nodes:
        if node.client_only:
            continue
        cfg = node.field("m_gameMessage")
        if not isinstance(cfg, dict) or expr.guid_of(cfg.get("m_gameMessage")) != message:
            continue
        if node.cls in MESSAGE_ENTRIES:
            found.append(node)
        elif node.is_state:
            state = instance.node_state(node)
            if state is not None and state.active:
                found.append(node)
    return found


def send_message(component: Component, message: int, params: dict, sender: int) -> int:
    """Deliver a game message to every instance of the component (at now). Returns how many nodes get it."""
    count = 0
    for instance in list(component.instances.values()):
        for node in message_entries(instance, message):
            delivery = (message, dict(params), sender)
            instance.post(Event(component.now, 0, MESSAGE, node=node, message=delivery))
            count += 1
    return count


INSTANCE_SCOPE, ENTITY_SCOPE = INSTANCE, ENTITY
