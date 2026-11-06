"""A player's hero body run on the server, and the statescript frames that keep the owning client's
prediction.

The client predicts its own body: it runs the networked nodes of the body's instances from its own input
and records undo history per command frame. It does not run the server's part (the Entries of instances
the server made, the instances' first states), and it rolls back whatever the server does not confirm. So
the server runs the same graphs from the same input (runtime.py) and sends, for every command frame K it
has processed, an owner frame with C = 1 and CmFD = K: everything that differs from what the client last
confirmed, and every instance's pending timers.

Frames, in order:
1. the body's create (match.py);
2. a plain full frame (H = 0): every instance and every variable, every state off. The client binds its
   own instances 1..N and makes the server's (the weapon manager, the weapons, the SubScript children);
3. once it arrived: an owner delta with C = 0 at the newest command frame K: every state that is on, the
   variables that have bindings, the timers. Retry the same chunk until it is acknowledged;
4. once acknowledged and SETTLE_FRAMES have passed, an owner delta with C = 1 for every new K.
A correction describes the change since the last chunk the client acknowledged, plus everything the
chunks sent after it carried (the client may have applied them), so it needs no resend.
"""

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ow174.game import pools, ult_charge
from ow174.game.bits import BitWriter
from ow174.game.commands import FRAME_HALF, FRAME_MASK, frame_distance
from ow174.game.content import SOLDIER
from ow174.game.script import expr, graph, nodes, runtime
from ow174.game.script.codecs import stec_classes
from ow174.game.script.expr import INSTANCE, Asset, Entity, Vec3, f32
from ow174.game.statescript import (
    Binding,
    EventWire,
    InstanceWire,
    StateWire,
    body_full_frame,
    chunk,
    family,
    owner_delta,
    sendable,
)
from ow174.game.world import UNIT_ENTITY_SCALE

if TYPE_CHECKING:
    from ow174.game.match import Match
    from ow174.game.world import EntityUpdate

log = logging.getLogger("ow174.script")


class CommandEpochError(ValueError):
    """A forward uint32 rollover needs an explicit new simulation/owner-stream lifecycle."""

    def __init__(self, previous: int, incoming: int) -> None:
        super().__init__(
            f"command frame rolled over from {previous} to {incoming}; "
            "establish a new body/owner-stream epoch before continuing"
        )
        self.previous = previous
        self.incoming = incoming


PRACTICE_RANGE_MODE = 0x0230000000000018
SETTLE_FRAMES = 16  # 0.25 s after the first owner frame, before the first C = 1 frame
MAX_GAP = 256  # command frames run at most to catch up
MAX_SHOTS = 256  # shots kept on the component's list for combat.py
MAX_REPORTS = 128  # owner frames kept on the reports list for projectiles.py
KILL_FEED_PULSERS = {0x01CF: 108, 0x0C18: 8}  # their client-only plug posts 0765.025 to the mode UI
EDGE_BITS = (  # command button bit -> logical button, in the client's order (0x7FF789C66AA0)
    (0x40, 11), (0x80, 12), (0x01, 20), (0x02, 29), (0x08, 35), (0x10, 36), (0x20, 37), (0x04, 30),
    (0x800, 31), (0x400, 14), (0x01, 111), (0x02, 112), (0x04, 113), (0x08, 114), (0x10, 110), (0x20, 116),
    (0x40, 115),
)  # fmt: skip
ACTION_BUTTONS = {1: 15, 2: 16, 3: 17, 4: 18, 5: 19, 6: 21, 7: 22, 8: 56, 9: 71, 16: 120}
ACTION_BUTTONS.update({17: 97, 18: 98, 19: 99})
ACTION_BUTTONS.update({action: 81 + action for action in range(10, 16)})  # 10..15 -> 91..96
# Entity variables no graph of the body writes: the server's own code sets them.
WEAPON_SLOTS, DEFAULT_SLOT, OWNER_HEALTH, HUD_HEALTH, HUD_MAX_HEALTH = 29, 16515, 253, 1732, 1737
NOT_SENT = {OWNER_HEALTH}  # the client derives it itself (never sent)
BODY_SCALE_OVERRIDE = 31210  # the common body graph's size modifier input
# The world's config vars whose server answer is the client's for a human player's own body (computed, or
# answered null/false). A BooleanSwitch that reads any other one is uncertain and is not sent.
TRUSTED_NATIVES = {
    "STU_1FF67CA6",  # GetLocalPlayerPossessedView: the body
    "STU_310D6529",  # GetThrottle: the command's throttles
    "STU_37F20D24",  # GetGameMode: the match's mode
    "STU_4A101787",  # always false
    "STU_A77AC2AC",  # false
    "STU_A3DEF368",  # the mode entity and its configured initial values, when present in the bundle
    "STU_DB742151",  # Sprint's #2333: IsEntityAIBot(the possessor) false for a player ...
    "STU_295D117D",  # ... GetPrimaryPossessorEntity ...
    "STU_D3EB8D87",  # ... AIIsPathFollowing: unused for a player
}
POLLED_SWITCHES = {"STUStatescriptStateBooleanSwitch", "STU_DEBD057B"}
# The ultimate: its charge #342e is server data (no graph adds to it; the ultimate's graph sets it to 0 when
# it starts), its cost #338e comes from the Entries. Combat credits it (combat.py through ult_charge.py);
# the passive fills the rest in ULT_SECONDS (0: never) so a server with no combat rule still charges, and
# nothing charges while the ultimate runs.
ULT_CHARGE, ULT_COST = 342, 338
ULT_SECONDS = 60.0
VOLLEY = "STUStatescriptStateWeaponVolley"
_VOLLEY_INTRODUCTION = "volley_introduction"  # report metadata, not a cumulative wire key
THROTTLE_UNIT = f32(1 / 127)  # GetThrottle: x = right, y = forward (W is +y)


def button_edges(previous: int, buttons: int, action: int = 0) -> list[tuple[int, bool]]:
    """The logical-button edges of one command (0x7FF789C66AA0): one per changed bit, pressed = the bit is
    now set; an action byte presses and releases its button."""
    edges = []
    for bit, button in EDGE_BITS:
        if (previous ^ buttons) & bit:
            edges.append((button, bool(buttons & bit)))
    if action in ACTION_BUTTONS:
        edges += [(ACTION_BUTTONS[action], True), (ACTION_BUTTONS[action], False)]
    return edges


class BodyWorld(runtime.World):
    """What the body's graphs ask outside the body. Config vars the server cannot compute answer what the
    owning client sees in a normal game; the others are null (logged once)."""

    def __init__(self, hero: int, mode: int, body: int) -> None:
        super().__init__(hero, mode)
        self.body = body
        self.throttles = (0, 0)  # the command being run: (forward, right), -127..127
        self.match: Match | None = None

    def position(self, entity: int) -> Vec3:
        if self.match is not None:
            position = self.match.script_body_position(entity, self.body)
            if position is not None:
                return position
        return super().position(entity)

    def native(self, cfg: dict, instance):
        cls = cfg.get("$")
        if cls == "STU_1FF67CA6":  # GetLocalPlayerPossessedView: the owner's client views its body
            return Entity(self.body)
        if cls == "STU_310D6529":  # GetThrottle (0x7FF789BDA130): the command's throttle bytes * f32(1/127)
            forward, right = self.throttles
            return Vec3(f32(right * THROTTLE_UNIT), f32(forward * THROTTLE_UNIT), 0.0)
        if cls == "STU_37F20D24":  # GetGameMode: the match's mode (what component 114 announces)
            return Asset(self.mode)
        if cls == "STU_FDBB0B67":  # entity size used by the authored movement and effect graphs
            target = cfg.get("m_entity")
            component = instance.component
            if (component.entity == self.body and expr.valid_entity(self.body)
                    and isinstance(target, dict)
                    and target.get("$") in {"STU_B5A0CAF0", "STUConfigVarDynamic"}
                    and instance.evaluate(target) == Entity(self.body)):
                override = component.entity_var(BODY_SCALE_OVERRIDE, create=False)
                scale = override.value() if override is not None else None
                if scale is None or (expr.kind(scale) in ("int", "float") and scale == UNIT_ENTITY_SCALE):
                    return UNIT_ENTITY_SCALE
        if cls == "STU_4A101787":  # its evaluator (0x7FF789BFF730) always answers false
            return False
        if cls == "STU_AC829876":  # does an entity pass a filter (teams, tags): not modelled
            expr.warn_once(cls, "entity filters (STU_AC829876) answer false")
            return False
        return super().native(cfg, instance)

    def send_message(self, target: int, message: int, params: dict, sender: int) -> None:
        match = self.match
        if (match is not None and sender == self.body
                and match.queue_body_message(target, message, params, sender)):
            return
        super().send_message(target, message, params, sender)


@dataclass
class Snapshot:
    """The body as the client has it (or will, after a chunk): instances, variables, states, timers."""

    k: int = 0
    instances: dict = field(default_factory=dict)  # id -> (graph index, parent)
    vars: dict = field(default_factory=dict)  # (instance or 0, var) -> (value, bindings)
    states: dict = field(default_factory=dict)  # (instance, state) -> (cls, on, payload)
    events: dict = field(default_factory=dict)  # instance -> ((time, state, finish, param), ...)


@dataclass
class OwnerReport:
    """A transmitted body delta and the outcome of its own datagram."""

    number: int
    cmfd: int
    last: int
    volleys: dict[tuple[int, int], tuple[int, int]]  # (instance, state) -> (counter, start)
    introductions: frozenset[tuple[int, int]]
    received: bool | None = None


def _same_var(a, b) -> bool:
    a = a or (None, ())
    b = b or (None, ())
    return expr.same(a[0], b[0]) and a[1] == b[1]


class ReplicatedBody:
    component: runtime.Component
    server_only: set[int]

    def set_health(self, health: float, most: float) -> None:
        """Update the shared health input. Player bodies also use the maximum for their HUD."""
        self.component.set(OWNER_HEALTH, float(health))

    def initial_exclusions(self) -> set[int]:
        """Collect the existing entity-scope exclusions from the initialized graph tree."""
        return {
            var_id
            for instance in self.component.instances.values()
            for var_id in _server_only_vars(instance.graph)
        }

    def replicated_instances(self) -> Iterable[runtime.Instance]:
        return self.component.instances.values()

    def graphs(self) -> dict[int, object]:
        return {instance.id: instance.graph for instance in self.replicated_instances()}

    def _report_shots(self) -> None:
        """Collect shots due in active volleys. Volleys that ended reported their own shots."""
        self.component.ensure_valid()
        shots = self.component.shots
        for instance in self.component.instances.values():
            for state in instance.states.values():
                if isinstance(state, nodes.WeaponVolley) and state.active:
                    state.report(self.component.now)
        if len(shots) > MAX_SHOTS:
            del shots[: len(shots) - MAX_SHOTS]

    def replicated_vars(self, instance: runtime.Instance | None = None) -> Iterator[tuple[int, runtime.Var]]:
        """Variables eligible for both wire snapshots. Instance variable IDs are local to their graph."""
        if instance is None:
            variables = self.component.vars
            excluded = self.server_only | NOT_SENT
        else:
            variables = instance.vars
            excluded = _server_only_vars(instance.graph)
        return ((var_id, var) for var_id, var in variables.items() if var_id not in excluded)


class BodyScript(ReplicatedBody):
    """The statescript of one hero body: `spawn_frames()` once the body exists on the client, then
    `command()` for every new client command frame and `frames()` every server tick."""

    def __init__(
        self,
        hero: int,
        entity: int,
        start_frame: int,
        quanta: int = 16000,
        mode: int = PRACTICE_RANGE_MODE,
        health: float | None = None,
        ult_seconds: float = ULT_SECONDS,
        *,
        match: "Match | None" = None,
    ) -> None:
        if type(start_frame) is not int or not 0 <= start_frame <= FRAME_MASK:
            raise ValueError("start_frame must be uint32")
        record = next((item for item in graph.bodies().values() if int(item["hero"], 16) == hero), None)
        if record is None:
            raise ValueError(f"no statescript data for the hero {hero:016X}")
        self.hero = hero
        self.entity = entity
        self.record = record
        self.frame_ms = max(1, quanta // 1000)
        self.world = BodyWorld(hero, mode, entity)
        self.world.match = match
        self.component = runtime.Component(entity, self.frame_ms, self.world)
        self.frame = start_frame  # the last command frame run
        self.buttons = 0
        self.stream = BodyStream(entity)
        self.create_arrived = False  # the client has the body (see `created`)
        self.retired = False
        self.first_owner_frame: int | None = None
        self.ult_seconds = ult_seconds
        self.ultimates: list = []  # the Ability states that start on #342e (not charged while they run)
        self.reports: list[OwnerReport] = []
        self.report_count = 0  # owner frames sent so far
        self._spawn(start_frame, sum(pools.body_pools(hero)) if health is None else health)

    def __repr__(self) -> str:
        return f"<BodyScript {self.entity:08X} {self.record.get('name')} frame {self.frame}>"

    # --- the instances -----------------------------------------------------------------------------

    def _spawn(self, frame: int, health: float) -> None:
        """The body's instances: its initial graphs 1..N in definition order with
        their schema overrides, the weapon manager, the weapon scripts (#28 = the slot), all run from their
        Entries in the start frame."""
        component = self.component
        component.frame = frame
        component.now = frame * self.frame_ms
        weapons = self.record.get("weapons") or []
        component.set(WEAPON_SLOTS, tuple(item is not None for item in weapons))
        component.set(DEFAULT_SLOT, next((slot for slot, item in enumerate(weapons) if item is not None), 0))
        self.set_health(health, health)
        component.set(ULT_CHARGE, 0.0)
        created = create_initial_graphs(component, self.record)
        manager = graph.graph(self.record["manager"]) if self.record.get("manager") is not None else None
        if manager is not None:
            created.append(component.create(manager, init=False))
        for slot, item in enumerate(weapons):
            if item is not None and graph.graph(item) is not None:
                created.append(component.create(graph.graph(item), values={(INSTANCE, 28): slot}, init=False))
        for instance in created:
            instance.init()
        component.run_frame(frame)
        self.server_only = self.initial_exclusions()
        for instance in component.instances.values():
            for node in instance.graph.states:
                if node is None or node.cls != "STUStatescriptStateAbility":
                    continue
                if _reads(node.field("m_150F0D92"), ULT_CHARGE):  # the ultimate: starts on its charge
                    self.ultimates.append(instance.state(node.state))

    def set_health(self, health: float, most: float) -> None:
        """The owner's health: #253e for the graphs (01CF st13: 0 or less is dead), #1732e/#1737e for the
        HUD panel's number (no graph writes them: current and maximum). The
        body's pools added up, health, armour and shields (unverified: as OW1's HUD showed one number)."""
        super().set_health(health, most)
        self.component.set(HUD_HEALTH, float(health))
        self.component.set(HUD_MAX_HEALTH, float(most))
        self.component.owner_dead = health <= 0  # as the client sees its owner (comp 51, 0x7FF789ADBF00)

    # --- running -----------------------------------------------------------------------------------

    def command(self, frame: int, buttons: int, action: int = 0, forward: int = 0, right: int = 0) -> None:
        """Run the command frames up to `frame`: frames the client skipped keep the last buttons (a long gap
        runs only its last MAX_GAP frames; what was due before runs in the first of them).

        Old/duplicate uint32 frames are ignored. A genuine forward rollover is distinct:
        the current component's absolute timers and owner CmFD belong to the old epoch.
        Raise before changing anything, so the caller can establish a fresh lifecycle;
        resetting self.frame would silently keep incompatible timers and client history.
        """
        self.component.ensure_valid()
        if type(frame) is not int or not 0 <= frame <= FRAME_MASK:
            raise ValueError("command frame must be uint32")
        distance = frame_distance(frame, self.frame)
        if distance == 0 or distance >= FRAME_HALF:
            return
        if frame < self.frame:
            raise CommandEpochError(self.frame, frame)
        for missing in range(max(self.frame + 1, frame - MAX_GAP), frame):
            self._run(missing, [])
        self.world.throttles = (forward, right)
        self._run(frame, button_edges(self.buttons, buttons, action))
        self.buttons = buttons
        self.frame = frame

    def _run(self, frame: int, edges: list) -> None:
        self.component.run_frame(frame, edges)
        self._report_shots()
        self._charge_ult()

    def set_ult_charge(self, points: float) -> None:
        """Set the ultimate's charge (#342e; the cost is #338e): a GM command, or damage and healing later."""
        self.component.set(ULT_CHARGE, f32(points))

    def credit_ult(self, kind: str, amount: float) -> None:
        """A combat event of this body (combat.py): dealt damage or done healing in HP, or an elimination
        (amount unused). The hero's rule in ult_charge.py turns it into points of the meter's own scale;
        a rule that does not charge gives nothing, and the meter never passes its cost."""
        cost = expr.to_float(self.component.entity_var(ULT_COST).value())
        points = ult_charge.points(kind, self.hero, amount, cost)
        if points <= 0.0:
            return
        charge = expr.to_float(self.component.entity_var(ULT_CHARGE).value())
        self.set_ult_charge(max(0.0, min(cost, f32(charge + points))))

    def _charge_ult(self) -> None:
        blocked = self.ult_seconds <= 0 or any(
            state.flags & nodes.Ability.RUNNING for state in self.ultimates
        )
        if blocked != getattr(self, "_ult_passive_blocked", None):
            log.info(
                "[game] %X: ultimate passive %s", self.entity, "paused" if blocked else "running"
            )
            self._ult_passive_blocked = blocked
        if blocked:
            return
        cost = expr.to_float(self.component.entity_var(ULT_COST).value())
        charge = expr.to_float(self.component.entity_var(ULT_CHARGE).value())
        if cost > 0 and charge < cost:
            step = f32(cost * self.frame_ms / (1000.0 * self.ult_seconds))
            self.set_ult_charge(min(cost, f32(charge + step)))
        self._note_ult_stall(charge)

    _ULT_STALL_SECONDS = 10.0

    def _note_ult_stall(self, charge: float) -> None:
        """A stalled meter with a running rule means the meter's frames do not reach the client (or the
        client's commands stop the script); say so once per interval instead of leaving it silent."""
        now = self.component.now / 1000.0
        seen = getattr(self, "_ult_stall", None)
        if seen is None:
            self._ult_stall = seen = [charge, now]
        elif charge > seen[0]:
            seen[0], seen[1] = charge, now
        elif seen[1] + self._ULT_STALL_SECONDS < now:
            base = self.stream.base
            acked = next((v for (owner, var), (v, _) in base.vars.items() if var == ULT_CHARGE), None)
            log.info(
                "[game] %X: ultimate meter stuck at %.1f for %.0f s (client has %s; frames %d acked %d,"
                " pending %d, cmfd %d, commands %d)",
                self.entity, charge, now - seen[1], acked, self.stream.last, self.stream.base_last,
                len(self.stream.pending), self.stream.cmfd, self.frame,
            )
            seen[1] = now  # report again after the next interval if it stays stuck

    # --- what the client gets ----------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """The body as the client should have it after the last frame run."""
        self.component.ensure_valid()
        snap = Snapshot(self.frame)
        for instance in self.component.instances.values():
            parent = (instance.parent.id, instance.parent_state) if instance.parent is not None else None
            snap.instances[instance.id] = (instance.graph.index, parent)
            for var_id, var in self.replicated_vars(instance):
                self._add_var(snap, instance.id, var_id, var)
            for index, state in instance.states.items():
                self._add_state(snap, instance.id, index, state)
            snap.events[instance.id] = tuple(
                (event.time, event.state.index, event.kind == runtime.STATE_OP, event.param)
                for event in instance.timers()
                if sendable(event.state.node.cls, True)
            )
        for var_id, var in self.replicated_vars():
            self._add_var(snap, 0, var_id, var)
        return snap

    def _add_var(self, snap: Snapshot, owner: int, var_id: int, var: runtime.Var) -> None:
        bindings = tuple(
            Binding(link.state.instance.id, link.state.index, link.slot, link.priority, link.above)
            for link in var.links
            if link.stack and link.state.active and link.state.networked
        )
        value = var.value()
        if value is not None or bindings:
            snap.vars[(owner, var_id)] = (value, bindings)

    @staticmethod
    def _add_state(snap: Snapshot, owner: int, index: int, state: runtime.State) -> None:
        cls = state.node.cls
        if not state.networked or not (state.active or (cls in stec_classes() and state.counter)):
            return
        if not sendable(cls, state.active):
            expr.warn_once(f"send{cls}", "state class %s is on but cannot be sent", cls)
            return
        if cls in POLLED_SWITCHES and state.natives - TRUSTED_NATIVES:
            # Uncertain: the client would re-check it every update with its own answer and flip it against
            # ours in every frame. Not sent, it stays off there (its live subgraph is sent).
            return
        payload = state.payload() if family(cls) != "none" else {}
        snap.states[(owner, index)] = (cls, state.active, tuple(sorted(payload.items())))

    def tree_frame(self) -> tuple[BitWriter, Snapshot]:
        """The plain full frame (step 2) and what the client has after it: the instances and the values,
        every networked state off, no timers."""
        snap = self.snapshot()
        graphs = self.graphs()
        wires = {}
        for item, (_, parent) in snap.instances.items():
            wires[item] = InstanceWire(item, graphs[item], parent=parent)
        entity_vars = {}
        base = Snapshot(snap.k, dict(snap.instances))
        for (owner, var), (value, _) in snap.vars.items():
            if owner == 0:
                entity_vars[var] = (value, ())
            else:
                wires[owner].vars[var] = (value, ())
            base.vars[(owner, var)] = (value, ())
        return body_full_frame(list(wires.values()), entity_vars, graphs), base

    def delta(
        self, base: Snapshot, touched: set, cmfd: int, correction: bool, *, kill_feed: bool | None = None,
    ):
        """An owner delta at CmFD from `base`; `touched` are the keys sent since `base`. Returns the frame,
        the keys it carries and the snapshot it leaves the client with."""
        snap = self.snapshot()
        if kill_feed is not None:
            snap = self.stream.kill_feed_snapshot(snap, allowed=kill_feed)
        graphs = self.graphs()
        wires: dict[int, InstanceWire] = {}

        def wire(item: int) -> InstanceWire:
            if item not in wires:
                graph_index, parent = snap.instances[item]
                wires[item] = InstanceWire(item, graphs.get(item) or graph.graph(graph_index), parent=parent)
            return wires[item]

        keys = set()
        for item in snap.instances:
            if item not in base.instances:  # new (a repeated descriptor is taken as the same instance)
                wire(item).descriptor = True
                keys.add(("inst", item))
        for item in set(base.instances) | {key[1] for key in touched if key[0] == "inst"}:
            if item not in snap.instances:  # gone, or made and gone again since the base
                wires[item] = InstanceWire(item, None, gone=True)
                keys.add(("inst", item))
        entity_vars = {}
        touched_vars = {key[1:] for key in touched if key[0] == "var"}
        for key in set(snap.vars) | set(base.vars) | touched_vars:
            owner, var = key
            if owner and owner not in snap.instances:
                continue
            now = snap.vars.get(key)
            if _same_var(now, base.vars.get(key)) and key not in touched_vars:
                continue
            keys.add(("var", *key))
            if owner == 0:
                entity_vars[var] = now or (None, ())
            else:
                wire(owner).vars[var] = now or (None, ())
        touched_states = {key[1:] for key in touched if key[0] == "state"}
        for key in set(snap.states) | set(base.states) | touched_states:
            owner, index = key
            if owner not in snap.instances:
                continue
            now, was = snap.states.get(key), base.states.get(key)
            if now == was and key not in touched_states:
                continue
            if now is None:
                cls = graphs[owner].states[index].cls
                if cls in stec_classes():
                    continue  # it never reached the client as on
                state = StateWire(cls, False)
            else:
                state = StateWire(now[0], now[1], dict(now[2]))
            keys.add(("state", *key))
            wire(owner).states[index] = state
            if state.cls != VOLLEY:
                continue
            previous = dict(was[2]) if was is not None else {}
            counter = state.payload["counter"]
            if (counter, state.payload["start"]) != (previous.get("counter"), previous.get("start")):
                # A single-shot volley can finish in the frame that introduces it.
                keys.add((_VOLLEY_INTRODUCTION, *key))
            # The last acknowledged frame can still show a previous activation as on.
            if state.active and (was is None or not was[1] or previous["counter"] != counter):
                self._volley_start(snap, owner, index, wire, entity_vars, keys)
        for item in snap.instances:
            wire(item).events = [EventWire(*event) for event in snap.events.get(item, ())]
        frame = owner_delta(cmfd, correction, list(wires.values()), entity_vars, graphs, self.frame_ms)
        return frame, keys, snap

    def _volley_start(self, snap: Snapshot, owner: int, index: int, wire, entity_vars: dict, keys: set):
        """A volley the client begins from this frame takes its ammunition from m_out_Ammo once, after the
        frame (its deferred StartFiring) and never links it: so that frame carries the volley's starting
        ammunition, the later ones the count."""
        state = self.component.instances[owner].states[index]
        target = expr.lvalue(state.node.fields.get("m_24E351A4"))
        if target is None:
            return
        scope, var = target
        key = (0, var) if scope else (owner, var)
        value = (state.ammo0, ())
        snap.vars[key] = value
        keys.add(("var", *key))
        if scope:
            entity_vars[var] = value
        else:
            wire(owner).vars[var] = value

    # --- frames for the world ------------------------------------------------------------------------

    def created(self, update):
        """Hook the body's create record (a world.EntityUpdate): the transport tells us when the client
        acknowledged it, and the full frame goes out after that."""
        update.stream = _CreateArrived(self)
        return update

    def retire(self) -> None:
        """The body is gone (a hero switch, the player left): no more frames, and a lost full frame is not
        sent again."""
        self.retired = True
        self.world.match = None
        if self.stream.full_update is not None:
            self.stream.full_update.resend = False
        if self.stream.initial_owner is not None:
            self.stream.initial_owner.resend = False
            self.stream.initial_owner = None

    def spawn_frames(self) -> list:
        """The full frame that puts the body's statescript on the client (send it once the client has the
        body; it goes again when its datagram is lost)."""
        frame, base = self.tree_frame()
        return [self.stream.full(frame, base)]

    def _unsent_health_changed(self, queued: "Iterable[EntityUpdate]") -> bool:
        stream = self.stream
        pending = stream.pending.get(stream.last)
        if pending is None or stream.last in stream.sent_reports:
            return False
        snapshot, _ = pending
        changed = any(
            not expr.same(snapshot.vars.get((0, var), (None, ()))[0], self.component.vars[var].value())
            for var in (HUD_HEALTH, HUD_MAX_HEALTH)
        )
        return changed and any(
            item.stream is stream and item.chunk_last == stream.last and not item.resend for item in queued
        )

    def frames(
        self, *, kill_feed: bool | None = None, replace_initial: bool = False,
        queued: "Iterable[EntityUpdate]" = (),
    ) -> list:
        """This tick's body frames: the full frame once the body's create arrived (see `created`), the first
        owner frame (C = 0) once the full frame arrived, then, after SETTLE_FRAMES, a C = 1 frame whenever the
        client's command frame moved on. Health can also refresh a queued owner frame at the same
        command frame before its first send."""
        self.component.ensure_valid()
        stream = self.stream
        if self.retired:
            return []
        if not stream.last:
            return self.spawn_frames() if self.create_arrived else []
        if stream.base is None or self.frame < stream.cmfd:
            return []
        if stream.initial_owner is not None and stream.initial_owner.resend:
            return []  # transport is delivering the exact first owner frame
        if self.frame == stream.cmfd and not self._unsent_health_changed(queued):
            return []
        if self.first_owner_frame is None or replace_initial:
            self.first_owner_frame = self.frame
            correction = False
        elif self.frame < self.first_owner_frame + SETTLE_FRAMES:
            return []
        else:
            correction = True
        frame, keys, snap = self.delta(
            stream.base, stream.touched(), self.frame, correction, kill_feed=kill_feed,
        )
        update = stream.delta(frame, snap, keys, self.frame)
        if not correction:
            stream.initial_owner = update
        return [update]

    def report_sent(self, last: int) -> None:
        """Record the owner delta actually transmitted for projectile confirmation."""
        stream = self.stream
        pending = stream.pending.get(last)
        if pending is None or last in stream.sent_reports:
            return
        snapshot, keys = pending
        volleys = {}
        for key in keys:
            if key[0] != "state":
                continue
            state_key = key[1:]
            state = snapshot.states.get(state_key)
            if state is not None and state[0] == VOLLEY:
                values = dict(state[2])
                volleys[state_key] = (values["counter"], values["start"])
        self.report_count += 1
        introductions = frozenset(key[1:] for key in keys if key[0] == _VOLLEY_INTRODUCTION)
        report = OwnerReport(self.report_count, snapshot.k, last, volleys, introductions)
        stream.sent_reports[last] = report
        self.reports.append(report)
        del self.reports[:-MAX_REPORTS]
        if stream.initial_owner is not None and stream.initial_owner.chunk_last == last:
            # Before the first actual send, the queued initial can still coalesce.
            # After send, retain its original chunk and report through transport retries.
            stream.initial_owner.resend = True


def create_initial_graphs(component: runtime.Component, record: dict) -> list[runtime.Instance]:
    """Allocate the definition's instances in asset order, before running their entries."""
    created = []
    for number, item in enumerate(record["graphs"], 1):
        definition = graph.graph(item["graph"])
        if definition is None:
            continue
        values = {}
        for entry in item.get("m_1EB5A024") or ():
            var = expr.guid_of(entry.get("m_0D09D2D9")) & 0xFFFF
            values[(INSTANCE, var)] = expr.evaluate(entry.get("m_value"), expr.Context())
        created.append(component.create(definition, number, values=values, init=False))
    return created


def _server_only_vars(definition: graph.Graph) -> set[int]:
    return {expr.guid_of(value) & 0xFFFF for value in definition.fields.get("m_9CEC6985") or ()}


def _reads(cfg, var: int) -> bool:
    """Whether a config var reads the entity variable `var`."""
    if isinstance(cfg, dict):
        if cfg.get("$") == "STUConfigVarDynamic":
            return cfg.get("m_identifier") == var and cfg.get("m_60DB8F99") == 1
        return any(_reads(value, var) for value in cfg.values())
    if isinstance(cfg, list):
        return any(_reads(value, var) for value in cfg)
    return False


def warm_up() -> None:
    """Load shared graph and codec data before the first match update."""
    BodyScript(SOLDIER, 0, 0)
    stec_classes()


class _CreateArrived:
    """Stands in for a stream on the body's create record: the transport's ack calls `arrived`."""

    def __init__(self, script: BodyScript) -> None:
        self.script = script

    def arrived(self, last: int) -> None:
        self.script.create_arrived = True


class BodyStream:
    """The body entity's chunks: their numbers, what the client acknowledged and the snapshot each chunk
    leaves it with. A delta goes from the last acknowledged chunk; the client takes it when
    `first <= applied + 1` and `last > applied`."""

    def __init__(self, entity: int) -> None:
        self.entity = entity
        self.last = 0  # the last frame number sent
        self.cmfd = 0
        self.base: Snapshot | None = None  # what the client has after the chunk ending at base_last
        self.base_last = 0
        self.pending: dict[int, tuple[Snapshot, set]] = {}  # last -> (snapshot, keys), not acknowledged
        self.sent_reports: dict[int, OwnerReport] = {}  # sent owner deltas with unknown transport outcomes
        self.lost_keys: dict[tuple, int] = {}  # each key's newest lost owner chunk
        self.latest_lost: Snapshot | None = None  # newest owner projection, for kill-feed pulse freshness
        self.full_update = None  # the full frame's record (sent again when lost)
        self.initial_owner = None  # first C = 0 update, retried once actually sent

    def _update(self, frame: BitWriter, first: int, resend: bool):
        from ow174.game.world import EntityUpdate

        update = EntityUpdate(self.entity, chunk=chunk(frame, first, self.last - first))
        update.chunk_last = self.last
        update.stream = self
        update.resend = resend
        return update

    def full(self, frame: BitWriter, snapshot: Snapshot):
        self.last += 1
        self.pending[self.last] = (snapshot, set())
        self.latest_lost = None
        self.full_update = self._update(frame, 0, resend=True)
        return self.full_update

    def delta(self, frame: BitWriter, snapshot: Snapshot, keys: set, cmfd: int):
        self.last += 1
        self.cmfd = cmfd
        self.pending[self.last] = (snapshot, keys)
        self.latest_lost = None
        return self._update(frame, self.base_last + 1, resend=False)

    def lost(self, last: int) -> None:
        """An owner delta's packet is retired without a positive ACK. Keep its keys cumulative."""
        self._report_delivery(last, False)
        pending = self.pending.pop(last, None)
        if pending is None:
            return
        snapshot, keys = pending
        for key in keys:
            if key[0] != _VOLLEY_INTRODUCTION:
                self.lost_keys[key] = max(last, self.lost_keys.get(key, 0))
        if last == self.last:
            self.latest_lost = snapshot

    def touched(self) -> set:
        found = set(self.lost_keys)
        for _, keys in self.pending.values():
            found |= keys
        return {key for key in found if key[0] != _VOLLEY_INTRODUCTION}

    def kill_feed_snapshot(self, snap: Snapshot, *, allowed: bool) -> Snapshot:
        """Gate the first new death pulse and keep its wire value until acknowledged."""
        latest = self.pending.get(self.last)
        pending = latest[0] if latest is not None else self.latest_lost
        states = None
        for owner, (graph_index, _) in snap.instances.items():
            index = KILL_FEED_PULSERS.get(graph_index)
            if index is None:
                continue
            key = (owner, index)
            before = self.base.states.get(key) if self.base is not None else None
            sent = pending.states.get(key) if pending is not None else None
            current = snap.states.get(key)
            count = dict(before[2])["count"] if before is not None and before[1] else None
            current_count = dict(current[2])["count"] if current is not None and current[1] else None
            if sent is not None and sent[1] and dict(sent[2])["count"] != count:
                value = sent
            elif current_count is not None and current_count != count:
                value = before
                if allowed:
                    value = (current[0], True, (("count", current_count), ("fresh", True)))
            else:
                continue
            if value == current:
                continue
            if states is None:
                states = dict(snap.states)
            if value is None:
                del states[key]
            else:
                states[key] = value
        return snap if states is None else replace(snap, states=states)

    def arrived(self, last: int) -> None:
        """The client acknowledged the datagram with the chunk that ends at `last`."""
        self._report_delivery(last, True)
        if last <= self.base_last or last not in self.pending:
            return
        self.base = self.pending[last][0]
        self.base_last = last
        self.pending = {key: value for key, value in self.pending.items() if key > last}
        self.lost_keys = {key: number for key, number in self.lost_keys.items() if number > last}
        if self.initial_owner is not None and self.initial_owner.chunk_last <= last:
            self.initial_owner.resend = False
            self.initial_owner = None

    def _report_delivery(self, last: int, received: bool) -> None:
        # A newer cumulative acknowledgement can discard a snapshot before the
        # transport learns what happened to this report's own datagram.
        report = self.sent_reports.pop(last, None)
        if report is not None:
            report.received = received


__all__ = ["BodyScript", "BodyStream", "CommandEpochError", "Snapshot", "button_edges", "nodes", "warm_up"]
