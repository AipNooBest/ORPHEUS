"""Other players' bodies as the other clients see them: their statescript in plain (H = 0) frames.

Why a remote body showed no weapon: the third-person weapon is the weapon script's
CosmeticEntity state (Soldier: 0254 st11, remote index 5), and its shots, ability effects and animations are
the body's other remote-synced states. A client runs them for another player's body only from that body's
statescript frames, and only the owner got any (owner frames). So every other client now gets the body's
statescript as an H = 0 frame (statescript.remote_full_frame), after the body's create arrived, and then H =
0 deltas of what changed (statescript.remote_delta), from the owner's body script on the server (the same
instances and values the owner's client has):
- the instances: the client builds the body's initial graphs itself (1..N), the frame makes the server's
  (weapon manager, weapons, SubScript children) from their descriptors;
- the variables, without bindings (a binding names a state with the owner frames' width);
- every remote-synced state that is on (StEC states also while off, with their counters), with its H = 0
  payload: the weapon cosmetic, the volleys (the client shoots a rifle volley itself from it: sync NONE),
  animations, effects, the kill feed's pulser (01CF st108) and so on; not the HUD presenters (the owner's
  ammo and ability icons: no spectators here).
A body whose hero's statescript the server does not run (match.BODY_SCRIPT_HEROES) gets none: it still
shows no weapon.
"""

import logging

from ow174.game.script import expr, graph
from ow174.game.script.driver import NOT_SENT, BodyStream, Snapshot
from ow174.game.statescript import (
    STEC_CLASSES,
    InstanceWire,
    StateWire,
    family,
    remote_delta,
    remote_full_frame,
)

log = logging.getLogger("ow174.game")


def snapshot(script) -> Snapshot:
    """The body as other clients should have it: every instance, every value (no bindings) and the remote
    states that are on (or StEC states that ran)."""
    snap = Snapshot(script.frame)
    component = script.component
    for instance in component.instances.values():
        parent = (instance.parent.id, instance.parent_state) if instance.parent is not None else None
        snap.instances[instance.id] = (instance.graph.index, parent)
        for var_id, var in instance.vars.items():
            _add_var(snap, script, instance.id, var_id, var)
        for node in instance.graph.remote_states():
            state = instance.states.get(node.state)
            if state is None or presenter(node):
                continue
            cls = node.cls
            if not (state.active or (cls in STEC_CLASSES and state.counter)):
                continue
            if family(cls) == "unknown":
                expr.warn_once(f"observe{cls}", "remote state class %s has no H=0 payload", cls)
                continue
            payload = state.payload() if family(cls) != "none" else {}
            snap.states[(instance.id, node.state)] = (cls, state.active, tuple(sorted(payload.items())))
    for var_id, var in component.vars.items():
        if var_id not in NOT_SENT:
            _add_var(snap, script, 0, var_id, var)
    return snap


def presenter(node) -> bool:
    """A HUD presenter (UXPresenter, STU_E13B30A8: a screen m_45216F79 and its condition m_EC32138E): the
    owner's HUD (ammo, ability icons), which no other client draws for this body (no spectators here)."""
    return node.cls == "STUStatescriptStateUXPresenter" or "m_45216F79" in node.fields


def _add_var(snap: Snapshot, script, owner: int, var_id: int, var) -> None:
    if var_id in script.server_only:
        return
    value = var.value()
    if value is not None:
        snap.vars[(owner, var_id)] = (value, ())


def _graphs(script) -> dict:
    return script.graphs()


def full_frame(script, snap: Snapshot):
    graphs = _graphs(script)
    wires = {
        item: InstanceWire(item, graphs[item], parent=parent) for item, (_, parent) in snap.instances.items()
    }
    entity_vars = {}
    for (owner, var), value in snap.vars.items():
        if owner == 0:
            entity_vars[var] = value
        elif owner in wires:
            wires[owner].vars[var] = value
    for (owner, index), (cls, active, payload) in snap.states.items():
        if owner in wires:
            wires[owner].states[index] = StateWire(cls, active, dict(payload))
    return remote_full_frame(list(wires.values()), entity_vars, graphs)


def _same(a, b) -> bool:
    a = a or (None, ())
    b = b or (None, ())
    return expr.same(a[0], b[0])


def delta(script, stream: BodyStream, snap: Snapshot):
    """An H = 0 delta from what the client acknowledged (stream.base), carrying again everything the chunks
    since then carried (the client may have applied them). Returns (frame, keys); frame None when nothing
    changed."""
    base = stream.base
    touched = stream.touched()
    graphs = _graphs(script)
    known = dict(base.instances)
    for pending, _ in stream.pending.values():
        known.update(pending.instances)
    wires: dict[int, InstanceWire] = {}
    keys: set = set()

    def wire(item: int) -> InstanceWire:
        if item not in wires:
            graph_index, parent = snap.instances.get(item) or known[item]
            wires[item] = InstanceWire(item, graphs.get(item) or graph.graph(graph_index), parent=parent)
        return wires[item]

    for item in snap.instances:
        if item not in base.instances:
            wire(item).descriptor = True
            keys.add(("inst", item))
    for item in set(base.instances) | {key[1] for key in touched if key[0] == "inst"}:
        if item not in snap.instances and item in known:
            wire(item).gone = True
            keys.add(("inst", item))
    entity_vars = {}
    touched_vars = {key[1:] for key in touched if key[0] == "var"}
    for key in set(snap.vars) | set(base.vars) | touched_vars:
        owner, var = key
        if owner and owner not in snap.instances:
            continue
        now = snap.vars.get(key)
        if _same(now, base.vars.get(key)) and key not in touched_vars:
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
            if cls in STEC_CLASSES:
                continue  # it never reached the client as on
            state = StateWire(cls, False)
        else:
            state = StateWire(now[0], now[1], dict(now[2]))
        keys.add(("state", *key))
        wire(owner).states[index] = state
    if not keys:
        return None, keys
    return remote_delta(list(wires.values()), entity_vars, graphs), keys


LAZY_TICKS = 8  # a delta of values only (no state on or off) goes at most this often (the ultimate's meter)


def _activity(snap: Snapshot) -> tuple:
    """What must reach the other clients at once: the instances, the states that are on, the StEC states'
    payloads (their counters start them again)."""
    on = frozenset(key for key, (_, active, _) in snap.states.items() if active)
    stec = frozenset((key, value) for key, value in snap.states.items() if value[0] in STEC_CLASSES)
    return frozenset(snap.instances), on, stec


class Watch:
    """One other player's body on one client: its create's arrival and the chunks it got."""

    def __init__(self, body: int) -> None:
        self.body = body
        self.arrived = False
        self.stream = BodyStream(body)
        self.sent: tuple | None = None  # _activity of the last snapshot sent
        self.next_lazy = 0  # the tick from which a values-only delta may go


class _CreateArrived:
    def __init__(self, watch: Watch) -> None:
        self.watch = watch

    def arrived(self, last: int) -> None:
        self.watch.arrived = True


class Observers:
    """The H = 0 statescript frames of every player's body for every other client."""

    def __init__(self, match) -> None:
        self.match = match
        self.watches: dict[tuple[int, int], Watch] = {}  # (viewer slot, body) -> its watch

    def created(self, viewer, other, update) -> None:
        """Another player's body create for the viewer's client (match.remote_body_create): its frames go
        once the client has it."""
        watch = Watch(other.body)
        self.watches[(viewer.slot, other.body)] = watch
        update.stream = _CreateArrived(watch)

    def has(self, viewer, body: int) -> bool:
        """Whether the viewer's client has the body's statescript (its first frame arrived)."""
        watch = self.watches.get((viewer.slot, body))
        return watch is not None and watch.stream.base is not None

    def update(self) -> None:
        match = self.match
        snaps: dict[int, Snapshot] = {}  # body -> its snapshot this tick, shared by every viewer
        for viewer in match.players:
            if viewer.client is None or not viewer.spawned:
                continue
            for other in match.players:
                if (
                    other is viewer
                    or not other.has_body
                    or other.body_script is None
                    or other.body_script.retired
                ):
                    continue
                watch = self.watches.get((viewer.slot, other.body))
                if watch is None or not watch.arrived or other.body not in viewer.seen:
                    continue
                try:
                    if other.body not in snaps:
                        snaps[other.body] = snapshot(other.body_script)
                    self._frames(viewer, other, watch, snaps[other.body])
                except Exception:
                    log.exception(
                        "[game] %s: %s's body frames for %s failed", match.label(), other.name, viewer.name
                    )
                    del self.watches[(viewer.slot, other.body)]
        live = {(viewer.slot, viewer_body) for viewer in match.players for viewer_body in viewer.seen}
        for key in [key for key in self.watches if key not in live]:
            del self.watches[key]

    def _frames(self, viewer, other, watch: Watch, snap: Snapshot) -> None:
        script = other.body_script
        stream = watch.stream
        tick = self.match.tick
        if not stream.last:
            update = stream.full(full_frame(script, snap), snap)
            viewer.client.queue_entities([update])
            watch.sent, watch.next_lazy = _activity(snap), tick + LAZY_TICKS
            return
        if stream.base is None:
            return  # the full frame is still on its way
        activity = _activity(snap)
        if activity == watch.sent and tick < watch.next_lazy:
            return
        frame, keys = delta(script, stream, snap)
        if frame is not None:
            viewer.client.queue_entities([stream.delta(frame, snap, keys, 0)])
            watch.sent, watch.next_lazy = activity, tick + LAZY_TICKS
