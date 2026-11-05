"""The node classes the runtime models: state classes (what they do at begin, end, on timers, button edges
and variable changes, what they give linked variables, and their owner-frame payload) and the actions and
conditions.

Semantics: read in the PC client (and the PS4 build where the PC code is obfuscated), field meanings
from the client's STU type objects (field hash -> offset, matched
to the offsets the class code reads). A state class not listed here is a plain State: it begins, follows
its plugs and stays active until something ends it, which is what the presentation classes (UXPresenter,
DataFlowMapping, Effect, ...) do on a server.
"""

import math

from ow174.game.script import expr
from ow174.game.script.expr import (
    EPSILON,
    Asset,
    Handle,
    Vec3,
    equal,
    f32,
    lvalue,
    round_half_away,
    stack_priority,
    to_float,
    to_int,
    truthy,
)
from ow174.game.script.graph import guid
from ow174.game.script.runtime import (
    ABORT,
    Component,
    Instance,
    State,
    Var,
    handler,
    send_message,
    state_class,
    would_stack_to_top,
)

MS = f32(0.001)  # 0.0010000000475, the client's float constant
REEVALUATE = 1  # BooleanSwitch / Switch / Watch timer param


def _field(state: State, name: str):
    return state.node.fields.get(name)


def shot_time(k: int, rate: float) -> int:
    """When shot k (0-based) leaves the gun, in ms after the volley start: R((k / rate) * 1000) in float32
    (PC 1.74, 0x7FF789C702E0)."""
    return round_half_away(f32(f32(k / rate) * 1000.0))


def seconds_to_ms(value) -> int:
    return round_half_away(f32(to_float(value) * 1000.0))


# --- states --------------------------------------------------------------------------------------


@state_class("STUStatescriptStateBooleanSwitch")
class BooleanSwitch(State):
    """OnBegin 0x7FF78AAA8F50: evaluate m_condition (its variables become watched), follow the true or the
    false plug and subgraph. A watched change queues one re-evaluation; the branch switches only when
    the result changed."""

    family = "switch"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.current = False
        self.pending = False

    def condition(self) -> bool:
        return truthy(self.evaluate(_field(self, "m_condition")))

    def on_begin(self) -> None:
        self.pending = False
        self.current = self.condition()
        self.enter()

    def enter(self) -> None:
        side = "True" if self.current else "False"
        self.instance.follow(self.node, f"m_on{side}Plug", self)
        if self.active:
            self.instance.follow(self.node, f"m_{side.lower()}SubgraphPlug", self)

    def dependency_changed(self) -> None:
        if not self.pending:
            self.pending = True
            self.timer_at(0, REEVALUATE, transient=True)

    def tick(self) -> None:
        if self.polled:  # a condition on something that is no variable: re-evaluated every update
            self.reevaluate()

    def timer(self, param: int) -> None:
        if param == REEVALUATE:
            self.pending = False
            self.reevaluate()

    def reevaluate(self) -> None:
        current = self.condition()
        if current == self.current:
            return
        self.exit()
        self.current = current
        self.enter()

    def exit(self) -> None:
        side = "m_trueSubgraphPlug" if self.current else "m_falseSubgraphPlug"
        self.instance.exit_subgraph(self.node, side)

    def on_end(self, finished: bool) -> None:
        self.exit()

    def payload(self) -> dict:
        return {"current": self.current}


@state_class("STUStatescriptStateStack", "STU_A97E6999")
class Stack(State):
    """OnBegin 0x7FF78AAA5FF0: push an entry {state, priority} on m_out_Var. The last entry is the top: its
    state follows m_topSubgraphPlug, every other m_underSubgraphPlug (only on a change). The variable reads
    the top's m_value. The entry is popped at the end."""

    family = "stack"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.top = False
        self.under = False
        self.value = None
        self.var: Var | None = None

    def on_begin(self) -> None:
        self.top = self.under = False
        self.value = self.evaluate(_field(self, "m_value")) if _field(self, "m_value") is not None else None
        target = lvalue(_field(self, "m_out_Var"))
        if target is None:
            return
        priority, above = stack_priority(_field(self, "m_priority"))
        self.var = self.instance.find_var(*target)
        self.link(target, 0, priority, above, stack=True)

    def stack_changed(self, var: Var) -> None:
        if not self.active or self.ending or var is not self.var or not var.links:
            return
        if var.links[-1].state is self:
            if not self.top:
                self.top, self.under = True, False
                self.instance.exit_subgraph(self.node, "m_underSubgraphPlug")
                self.instance.follow(self.node, "m_topSubgraphPlug", self)
        elif not self.under:
            self.top, self.under = False, True
            self.instance.exit_subgraph(self.node, "m_topSubgraphPlug")
            self.instance.follow(self.node, "m_underSubgraphPlug", self)

    def dependency_changed(self) -> None:
        if self.var is None or _field(self, "m_value") is None:
            return
        value = self.evaluate(_field(self, "m_value"))
        if not expr.same(value, self.value):
            before = self.var.value()
            self.value = value
            self.instance.changed(self.var, before)

    def on_end(self, finished: bool) -> None:
        self.instance.exit_subgraph(self.node, "m_topSubgraphPlug")
        self.instance.exit_subgraph(self.node, "m_underSubgraphPlug")
        if self.var is not None:
            var, self.var = self.var, None
            self.unlink(var)
        self.top = self.under = False

    def output(self, slot: int):
        return self.value

    def payload(self) -> dict:
        return {"top": self.top, "under": self.under}


@state_class("STUStatescriptStateLogicalButton")
class LogicalButton(State):
    """A held button: edge handler 0x7FF78AAAC570, OnEnter 0x7FF78AAACA80. Down:
    m_onGoingDownPlug then the down subgraph (m_80AF45FB); up: m_onComingUpPlug then the up subgraph
    (m_1C9EF058). A press while inactive is kept for the next activation (m_246438AD, the early press)."""

    family = "button"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.down = False
        self.pressed_at: int | None = None
        self.early_counter = -1

    def button_id(self) -> int:
        return to_int(self.evaluate(_field(self, "m_logicalButton"), watch=False))

    def blocked(self) -> bool:
        if not self.instance.component.disabled.get(self.button_id()):
            return False
        ignore = _field(self, "m_53C4B6A6")
        return ignore is None or not truthy(self.evaluate(ignore, watch=False))

    def held(self) -> bool:
        return self.button_id() in self.instance.component.held

    def allowed_dead(self) -> bool:
        """m_allowWhenOwnerDead (m_5AE05408, node +248, the PS4 build's name): the edge handler evaluates it
        before it asks the component whether its owner is dead (vt+488)."""
        allow = _field(self, "m_5AE05408")
        return allow is not None and truthy(self.evaluate(allow, watch=False))

    def on_enter(self) -> None:
        blocked = self.blocked()
        early = False
        duration = _field(self, "m_246438AD")
        if self.early_counter == self.counter and self.pressed_at is not None and duration is not None:
            waited = f32((self.now - self.pressed_at) / 1000.0)
            early = waited <= to_float(self.evaluate(duration, watch=False)) and not blocked
        if early:
            self.instance.follow(self.node, "m_onGoingDownPlug", self)
        if not self.active:
            return
        if not blocked and self.held():
            self.down = True
            self.instance.follow(self.node, "m_80AF45FB", self)
        elif early:
            self.down = True
            self.instance.follow(self.node, "m_80AF45FB", self)
            self.timer_at(1, 1)
        else:
            self.down = False
            self.instance.follow(self.node, "m_1C9EF058", self)

    def button(self, button: int, pressed: bool) -> None:
        if button != self.button_id():
            return
        if pressed and self.instance.component.owner_dead and not self.allowed_dead():
            return  # the edge handler drops a press while the owner is dead (0x7FF78AAAC5FC)
        if self.blocked():
            return
        if pressed:
            self.pressed_at = self.now
            self.early_counter = (self.counter + (0 if self.active else 1)) & 0xFFFF
        if self.active:
            self.go_down() if pressed else self.go_up()

    def go_down(self) -> None:
        self.down = True
        self.instance.exit_subgraph(self.node, "m_1C9EF058")
        self.instance.follow(self.node, "m_onGoingDownPlug", self)
        if self.active:
            self.instance.follow(self.node, "m_80AF45FB", self)

    def go_up(self) -> None:
        self.down = False
        self.instance.exit_subgraph(self.node, "m_80AF45FB")
        self.instance.follow(self.node, "m_onComingUpPlug", self)
        if self.active:
            self.instance.follow(self.node, "m_1C9EF058", self)

    def timer(self, param: int) -> None:
        if param == 1 and not self.held():
            self.go_up()

    def dependency_changed(self) -> None:
        down = self.held() and not self.blocked()
        if down != self.down:
            self.go_down() if down else self.go_up()

    def on_end(self, finished: bool) -> None:
        self.instance.exit_subgraph(self.node, "m_80AF45FB")
        self.instance.exit_subgraph(self.node, "m_1C9EF058")
        self.down = False

    def payload(self) -> dict:
        return {"counter": self.counter}


@state_class("STU_E4A30BCF")
class DisableLogicalButton(State):
    """While active, the entity's LogicalButtons for this button are blocked (OnBegin 0x7FF78AA82530)."""

    family = "link"

    def on_begin(self) -> None:
        self.button_id = to_int(self.evaluate(_field(self, "m_logicalButton"), watch=False))
        self.instance.component.disabled.setdefault(self.button_id, []).append(self)
        self.resync()

    def on_end(self, finished: bool) -> None:
        states = self.instance.component.disabled.get(self.button_id, [])
        if self in states:
            states.remove(self)
        self.resync()

    def resync(self) -> None:
        for instance in list(self.instance.component.instances.values()):
            for state in list(instance.states.values()):
                if isinstance(state, LogicalButton) and state.active and state.button_id() == self.button_id:
                    state.dependency_changed()


@state_class("STUStatescriptStateWait")
class Wait(State):
    """OnEnter 0x7FF78AAA7570: `ms = R(timeout * 1000)`; 0 or less finishes now, else a timer (param 1) at
    now + ms. m_0DBAFD7F (+256) re-arms on a re-entry; m_BF1A93B0 (+257) re-evaluates the timeout every
    tick."""

    def on_enter(self) -> None:
        self.arm()

    def dynamic(self) -> bool:
        return bool(_field(self, "m_BF1A93B0"))

    def arm(self) -> None:
        self.ms = seconds_to_ms(self.evaluate(_field(self, "m_timeout")))
        if self.dynamic():
            self.ticks()
            self.tick()
        elif self.ms <= 0:
            self.request_finish()
        else:
            self.timer_at(self.ms, 1)

    def reentry(self) -> None:
        if _field(self, "m_0DBAFD7F"):
            self.instance.cancel(self)
            self.start = self.now
            self.arm()

    def timer(self, param: int) -> None:
        if param == 1:
            self.request_finish()

    def tick(self) -> None:
        self.ms = seconds_to_ms(self.evaluate(_field(self, "m_timeout")))
        if self.now - self.start >= self.ms:
            self.request_finish()


@state_class("STU_E3CEF6B9")
class WaitForServerFrames(State):
    """Finishes after its number of server frames (unverified: one frame = one command frame)."""

    def on_enter(self) -> None:
        count = 1
        for value in self.node.fields.values():
            if isinstance(value, dict) and value.get("$") in ("STUConfigVarInt", "STU_919FD47C"):
                count = to_int(self.evaluate(value, watch=False))
                break
        self.request_finish(max(0, count) * self.instance.component.frame_ms)


@state_class("STU_D74D4F47")
class WaitFrames(State):
    """The game's class for this node class (vtable 0x7FF78B5836C8, over the library's plain state):
    OnEnter (0x7FF789C54760) sets the frame it finishes in, the component frame plus m_3016B9A1 (node
    +0xE8, none = 0), and checks it at once; the tick (0x7FF789C54BC0) asks for the finish once that frame
    is reached (unsigned); a re-entry starts it again only with m_0DBAFD7F (node +0x100). That frame is
    its payload (a u32 of its property block)."""

    family = "frames"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.target = 0

    def on_enter(self) -> None:
        count = _field(self, "m_3016B9A1")
        frames = to_int(self.evaluate(count, watch=False)) if count is not None else 0
        self.target = (self.instance.component.frame + frames) & 0xFFFFFFFF
        self.ticks()
        self.tick()

    def reentry(self) -> None:
        if _field(self, "m_0DBAFD7F"):
            self.on_enter()

    def tick(self) -> None:
        if self.target <= self.instance.component.frame & 0xFFFFFFFF:
            self.request_finish()

    def payload(self) -> dict:
        return {"target": self.target}


@state_class("STUStatescriptStateChaseVar")
class ChaseVar(State):
    """Moves m_out_Var to m_destination at m_rate per second (step = rate * 0.001 * dt +
    eps), linked to the variable while active and written into it at the end. Fields (type object):
    m_EB0FC261 initial value, m_9B0385B4 rate, m_5BEFF040 fixed duration, m_1E10B2B1 on a write by others,
    m_CB671004 stays active when reached, m_9FF96195 resets to the initial value on a re-entry."""

    family = "chase"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.cur = 0.0
        self.reached = False
        self.remaining: int | None = None
        self.last = 0
        self.var: Var | None = None
        self.interrupted = False

    def on_enter(self) -> None:
        target = lvalue(_field(self, "m_out_Var"))
        initial = _field(self, "m_EB0FC261")
        if initial is not None:
            value = self.evaluate(initial, watch=False)
        else:
            value = self.instance.find_var(*target).value() if target else None
        self.cur = value if isinstance(value, Vec3) else to_float(value)
        self.reached = False
        self.interrupted = False
        duration = _field(self, "m_5BEFF040")
        self.remaining = int(to_float(self.evaluate(duration, watch=False)) * 1000) if duration else None
        self.last = self.now
        if target is not None:
            var = self.instance.find_var(*target)
            before = var.value()
            var.base = self.cur
            self.var = self.link(target)
            self.instance.changed(var, before)
        self.ticks()

    def reentry(self) -> None:
        if _field(self, "m_9FF96195"):
            self.on_end(False)
            self.on_enter()

    def destination(self):
        value = self.evaluate(_field(self, "m_destination"))
        return value if isinstance(value, Vec3) else to_float(value)

    def tick(self) -> None:
        dt = self.now - self.last
        if dt <= 0 or self.reached:
            self.last = self.now
            return
        before = self.var.value() if self.var else None
        dest = self.destination()
        if self.remaining is not None:
            if self.remaining <= dt:
                self.cur = dest
                self.reached = True
            else:
                share = f32(dt / self.remaining)
                self.cur = self._lerp(self.cur, dest, share)
                self.remaining -= dt
        else:
            rate = to_float(self.evaluate(_field(self, "m_9B0385B4"))) if _field(self, "m_9B0385B4") else 1.0
            if rate > 0:
                step = f32(f32(f32(rate * MS) * dt) + EPSILON)
                self.cur, self.reached = self._step(self.cur, dest, step)
        self.last = self.now
        if self.var is not None:
            self.instance.changed(self.var, before)
        if self.reached:
            self.timer_at(0, 3, transient=True)
            if not _field(self, "m_CB671004"):
                self.request_finish()

    @staticmethod
    def _lerp(cur, dest, share: float):
        if isinstance(cur, Vec3) or isinstance(dest, Vec3):
            a = cur if isinstance(cur, Vec3) else Vec3(cur, 0.0, 0.0)
            b = dest if isinstance(dest, Vec3) else Vec3(dest, 0.0, 0.0)
            return Vec3(*(f32(x + (y - x) * share) for x, y in zip(a, b, strict=True)))
        return f32(cur + (dest - cur) * share)

    @staticmethod
    def _step(cur, dest, step: float):
        if isinstance(cur, Vec3) or isinstance(dest, Vec3):
            a = cur if isinstance(cur, Vec3) else Vec3(cur, 0.0, 0.0)
            b = dest if isinstance(dest, Vec3) else Vec3(dest, 0.0, 0.0)
            d = Vec3(*(f32(y - x) for x, y in zip(a, b, strict=True)))
            length = f32(math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z))
            if length <= step:
                return b, True
            return Vec3(*(f32(x + v * step / length) for x, v in zip(a, d, strict=True))), False
        if abs(f32(dest - cur)) <= step:
            return dest, True
        return f32(cur + step if dest > cur else cur - step), False

    def var_written(self, var: Var) -> None:
        mode = to_int(_field(self, "m_1E10B2B1") or 0)
        if mode == 0:
            self.interrupted = True
            self.unlink(var)
            self.var = None
        elif mode == 1:
            self.cur = to_float(var.base) if not isinstance(var.base, Vec3) else var.base

    def on_end(self, finished: bool) -> None:
        if self.var is not None:
            var, self.var = self.var, None
            self.unlink(var, bake=not self.interrupted)

    def output(self, slot: int):
        return self.cur

    def payload(self) -> dict:
        return {"cur": self.cur, "reached": self.reached, "remaining": self.remaining, "last": self.last}


@state_class("STUStatescriptStateWeaponVolley")
class WeaponVolley(State):
    """A burst of shots: shot k at start + R((k / rate) * 1000);
    n = min(ammo / per shot, max shots); a finish timer (param 2) at the last shot's time and a minimum-shots
    timer (param 1). While active m_out_Ammo reads `max(0, ammo0 - perShot * (shots + 1))`; at the end the
    variable keeps the value one ms later (the +1 ms rule, 0x7FF78A07F310)."""

    family = "volley"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.offset = 0
        self.volleys = 0  # the volley counter (the record's low byte); 1 after the first StartFiring
        self.per_shot = 1
        self.ammo0 = 0
        self.rate = 1.0
        self.spread0 = 0.0
        self.links: list[Var] = []
        self.reported = 0  # shots of this activation already put on the component's shot list

    def on_enter(self) -> None:
        self.offset = 0
        self.volleys = max(1, self.volleys)
        self.reported = 0
        self.params()
        self.arm()

    def report(self, until: int) -> None:
        """Put the shots fired up to `until` (ms) that are not on the component's list yet on it: shot k
        leaves at start + offset + shot_time(k), at most the planned shots (combat.py hits with them)."""
        if self.instance.component.owner_dead:
            # unverified (PS4 build): a dead owner's volley shoots no more (allowShotsWhileDead: not read)
            return
        fired = min(self.shots_planned(), self.shots_at(until - self.start - self.offset) + 1)
        while self.reported < fired:
            time = self.start + self.offset + shot_time(self.reported, self.rate)
            self.instance.component.shots.append((self, self.reported, time))
            self.reported += 1

    def _config(self, name: str, default):
        cfg = _field(self, name)
        return default if cfg is None else self.evaluate(cfg, watch=False)

    def params(self) -> None:
        self.max_shots = to_int(self._config("m_A17BE89B", 1))
        self.min_shots = to_int(self._config("m_A55BC904", 1))
        self.per_shot = max(0, to_int(self._config("m_5F40D9D3", 1)))
        rate = self._config("m_numShotsPerSecond", 1.0)
        self.rate = max(1e-5, to_float(rate)) if rate is not None else 1.0
        ammo = self._out("m_24E351A4")
        self.ammo0 = max(0, to_int(ammo.value())) if ammo else self.max_shots * self.per_shot
        spread = self._out("m_3FFC9EB9")
        self.spread0 = min(1.0, max(0.0, to_float(spread.value()))) if spread else 0.0
        self.links = []
        for slot, (name, value) in enumerate(
            (("m_24E351A4", self.ammo0), ("m_17770E7E", self.rate), ("m_3FFC9EB9", self.spread0))
        ):
            target = lvalue(_field(self, name))
            if target is None:
                continue
            var = self.instance.find_var(*target)
            before = var.value()
            var.base = value
            self.links.append(self.link(target, slot))
            self.instance.changed(var, before)

    def _out(self, name: str) -> Var | None:
        target = lvalue(_field(self, name))
        return self.instance.find_var(*target) if target else None

    def shots_at(self, elapsed: int) -> int:
        """Shots fired up to `elapsed` ms after the start, minus one (-1 before the start)."""
        if elapsed < 0:
            return -1
        return math.floor(f32(f32(f32(elapsed) * MS) * self.rate))

    def shots_planned(self) -> int:
        if self.per_shot <= 0:
            return self.max_shots
        return min(self.ammo0 // self.per_shot, self.max_shots)

    def arm(self) -> None:
        delay = self.offset - (self.now - self.start)
        if self.per_shot == 0 or self.min_shots < self.ammo0 // self.per_shot:
            self.timer_at(delay + shot_time(max(self.min_shots, 1) - 1, self.rate), 1)
        self.timer_at(delay + shot_time(max(self.shots_planned(), 1) - 1, self.rate), 2)

    def timer(self, param: int) -> None:
        if param == 1:
            self.instance.follow(self.node, "m_onMinShotsPlug", self)
        elif param == 2:
            self.request_finish()

    def ammo_at(self, elapsed: int) -> int:
        if elapsed < 0:
            return self.ammo0
        return max(0, self.ammo0 - self.per_shot * (self.shots_at(elapsed) + 1))

    def output(self, slot: int):
        if slot == 0:
            return self.ammo_at(self.now - self.start - self.offset)
        if slot == 1:
            return self.rate
        return self.spread0

    def on_end(self, finished: bool) -> None:
        self.report(self.now)
        elapsed = self.now - self.start - self.offset
        values = {0: self.ammo_at(elapsed + 1), 1: self.rate, 2: self.spread0}
        for var in self.links:
            link = next((item for item in var.links if item.state is self), None)
            if link is not None:
                before = var.value()
                var.base = values.get(link.slot)
                self.unlink(var)
                self.instance.changed(var, before)
        self.links = []

    def payload(self) -> dict:
        return {"start": self.start, "offset": self.offset, "volleys": self.volleys, "counter": self.counter}


@state_class("STUStatescriptStateAnim")
class Anim(State):
    """An animation (OnBegin 0x7FF78981DD90): with m_playOverDuration (m_86701C3E) set, a timer at start + d
    follows m_A3B86BB0 (animation finished), and unless m_clientAnimRemovesSelf (m_154DBA91) the state
    finishes at start + d. Without it the duration is the animation asset's: not known here."""

    family = "anim"

    def on_enter(self) -> None:
        duration = _field(self, "m_86701C3E")
        if duration is None:
            expr.warn_once("anim", "an Anim state without m_playOverDuration does not end on its own")
            return
        ms = seconds_to_ms(self.evaluate(duration, watch=False))
        self.timer_at(max(0, ms), 1)
        removes_self = _field(self, "m_154DBA91")
        if removes_self is None or not truthy(self.evaluate(removes_self, watch=False)):
            self.request_finish(max(0, ms))

    def timer(self, param: int) -> None:
        if param == 1:
            self.instance.follow(self.node, "m_A3B86BB0", self)

    def payload(self) -> dict:
        return {"counter": self.counter}


@state_class("STUStatescriptStateAbility")
class Ability(State):
    """An ability on a button, as the PC client runs it: update 0x7FF789B95560, keep 0x7FF789BB87E0, start
    0x7FF789BA9460, stop 0x7FF789BA6BC0, push 0x7FF789BA6D40, stack hook 0x7FF789B94DC0, output
    0x7FF789B94D00, field offsets from the class's field list. The start check (0x7FF789BB8D20) is
    obfuscated; it follows the PS4 build's code (unverified): a buffered press of the start button (within the
    early-press time, for this activation) or the start button held, the cooldown at 0, the start and
    continue conditions true, and the stack variable taking the "starting" priority.

    Every update while active: a running ability checks whether it keeps running (else it stops); one that is
    not running checks whether it can start. Keep: with no hold button (or m_755F9921 set) a press of the stop
    button stops it, and when that is the start button the press is latched (flag 4) so that it does not start
    it again while it is down; with a hold button, letting it go stops it; then the continue condition and the
    stack variable's "continuing" priority. An ability with a stack variable and a priority to set
    (m_5E561E77) does not start at once: it pushes itself onto the variable (flag 2, slot 1, value true) and
    starts when its entry is the top; when its entry is no longer the top it stops. Running (flag 1): the
    ability subgraph, the on-start plug and its variables. The cooldown variable is linked as slot 0: it falls
    by `rate` per second from the value last written to it."""

    family = "ability"
    RUNNING, PUSHED, HELD = 1, 2, 4

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.flags = 0
        self.presses: list[tuple[int, int, int]] = []  # (time, button, activation)
        self.cur = 0.0
        self.cooldown_rate = 1.0
        self.last = 0
        self.var: Var | None = None
        self.stack_var: Var | None = None

    # the cooldown

    def on_begin(self) -> None:
        self.flags = 0
        self.restart()
        self.ticks()

    def rate(self) -> float:
        modifier = self.instance.component.entity_var(3652, create=False)
        value = modifier.value() if modifier is not None else None
        rate = f32(to_float(value) + 1.0) if value is not None else 1.0
        scalar = _field(self, "m_F7CAF887")
        return f32(rate * to_float(self.evaluate(scalar, watch=False))) if scalar is not None else rate

    def restart(self) -> None:
        """The cooldown from the variable's value now (a write to it restarts it)."""
        target = lvalue(_field(self, "m_out_CooldownVar"))
        if target is None:
            return
        var = self.instance.find_var(*target)
        if self.var is var:
            var.links = [item for item in var.links if item.state is not self]
        self.cur = max(0.0, to_float(var.base))
        self.cooldown_rate = self.rate()
        self.last = self.now
        self.var = self.link(target)
        queue = self.instance.queue
        self.instance.queue = [event for event in queue if not (event.state is self and event.param == 2)]
        if self.cur > 0 and self.cooldown_rate > 0:
            self.timer_at(max(1, round_half_away(f32(f32(self.cur * 1000.0) / self.cooldown_rate))), 2)

    def remaining(self) -> float:
        return max(0.0, f32(f32(f32(f32(self.now - self.last) * -MS) * self.cooldown_rate) + self.cur))

    def output(self, slot: int):
        """Slot 0: the cooldown left (the cooldown variable); slot 1: true (the stack variable)."""
        return self.remaining() if slot == 0 else True

    def var_written(self, var: Var) -> None:
        if var is self.var:
            self.restart()

    # buttons

    def button_id(self, name: str) -> int:
        cfg = _field(self, name)
        return to_int(self.evaluate(cfg, watch=False)) if cfg is not None else 0

    def buttons(self) -> tuple[int, int, int]:
        return self.button_id("m_A5F1A77B"), self.button_id("m_A368407D"), self.button_id("m_8CBD15DA")

    def held(self, button: int) -> bool:
        component = self.instance.component
        return button in component.held and not component.disabled.get(button)

    def button(self, button: int, pressed: bool) -> None:
        if not pressed or button == 0 or self.instance.component.disabled.get(button):
            return
        if button in self.buttons():
            self.presses = [item for item in self.presses if self.now - item[0] < 2000]
            self.presses.append((self.now, button, (self.counter + (0 if self.active else 1)) & 0xFFFF))
            if self.active:
                self.timer_at(0, 1, transient=True)

    def take_press(self, button: int) -> bool:
        early = _field(self, "m_246438AD")
        limit = seconds_to_ms(self.evaluate(early, watch=False)) if early is not None else 0
        for item in self.presses:
            time, which, activation = item
            if which == button and self.now - time <= limit and activation <= self.counter:
                self.presses.remove(item)
                return True
        return False

    # starting and stopping

    def tick(self) -> None:
        self.activate()

    def timer(self, param: int) -> None:
        if param in (1, 2):
            self.activate()

    def plug_enter(self, input_path: str) -> bool:
        if input_path == "m_3B7BED2E":  # m_stopAbility
            self.stop()
            return True
        return super().plug_enter(input_path)

    def condition(self, name: str) -> bool:
        cfg = _field(self, name)
        return cfg is None or truthy(self.evaluate(cfg))

    def stack_allows(self, name: str) -> bool:
        target = lvalue(_field(self, "m_out_StackVar"))
        if target is None or _field(self, name) is None:
            return True
        var = self.instance.find_var(*target)
        self.watch(var)
        return would_stack_to_top(var, stack_priority(_field(self, name)), exclude=self)

    def stacked(self) -> bool:
        return _field(self, "m_out_StackVar") is not None and _field(self, "m_5E561E77") is not None

    def activate(self) -> None:
        on = self.flags & (self.PUSHED if self.stacked() else self.RUNNING)
        if on:
            if not self.keep():
                self.stop()
        elif self.can_start():
            if self.stacked():
                self.push()
            else:
                self.start_ability()

    def can_start(self) -> bool:
        if self.instance.component.owner_dead:
            return False  # unverified (PS4 build): no start while the owner is dead ("allowed": not read)
        start, hold, _ = self.buttons()
        pressed = False
        if start:
            pressed = self.take_press(start)
            if not pressed:
                if not self.held(start):
                    self.flags &= ~self.HELD
                    return False
                if self.flags & self.HELD:
                    return False
        elif hold and not self.held(hold):
            return False
        ready = (
            self.remaining() <= 0
            and self.condition("m_150F0D92")
            and self.condition("m_F495731F")
            and self.stack_allows("m_3C0EB4A6")
        )
        if not ready:
            if pressed:
                self.presses.insert(0, (self.now, start, self.counter))  # the press waits
            return False
        if start:
            self.flags |= self.HELD
        return True

    def keep(self) -> bool:
        if self.instance.component.owner_dead:
            return False  # unverified (the PS4 build): a running ability stops when its owner dies
        start, hold, stop = self.buttons()
        if self.flags & self.HELD and not self.held(start):
            self.flags &= ~self.HELD
        keep_held = _field(self, "m_755F9921")
        if not hold or (keep_held is not None and truthy(self.evaluate(keep_held))):
            if stop and self.take_press(stop):
                if stop == start:
                    self.flags |= self.HELD  # the stopping press must not start it again while it is down
                return False
        elif not self.held(hold):
            return False
        return self.condition("m_F495731F") and self.stack_allows("m_343E99EE")

    def push(self) -> None:
        """Onto the stack variable at the priority to set; the stack hook starts it when it is the top."""
        target = lvalue(_field(self, "m_out_StackVar"))
        priority, above = stack_priority(_field(self, "m_5E561E77"))
        self.flags |= self.PUSHED
        self.stack_var = self.instance.find_var(*target)
        self.link(target, 1, priority, above, stack=True)

    def stack_changed(self, var: Var) -> None:
        if var is not self.stack_var or not self.active or not self.flags & self.PUSHED:
            return
        top = var.links[-1] if var.links else None
        if top is not None and top.state is self and top.slot == 1:
            if not self.flags & self.RUNNING:
                self.start_ability()
        else:
            self.stop()

    def set_vars(self, name: str) -> None:
        for item in _field(self, name) or []:
            if isinstance(item, dict):
                value = self.evaluate(item.get("m_value"), watch=False)
                self.instance.write(lvalue(item.get("m_out_Var")), value)

    def start_ability(self) -> None:
        self.flags |= self.RUNNING
        self.instance.follow(self.node, "m_B969AFC9", self)
        self.instance.follow(self.node, "m_4E22B5C4", self)
        self.set_vars("m_EF3C5C73")

    def stop(self) -> None:
        """Off the stack variable, then out of running: the subgraph exits, the on-stop plug and its
        variables."""
        if self.flags & self.PUSHED:
            self.flags &= ~self.PUSHED
            var, self.stack_var = self.stack_var, None
            if var is not None:
                self.unlink(var)
        if self.flags & self.RUNNING:
            self.flags &= ~self.RUNNING
            self.instance.exit_subgraph(self.node, "m_B969AFC9")
            self.instance.follow(self.node, "m_6EEEC0AA", self)
            self.set_vars("m_19899EAC")

    def on_end(self, finished: bool) -> None:
        self.flags &= ~self.HELD
        self.stop()
        if self.var is not None:
            var, self.var = self.var, None
            before = var.value()
            var.base = self.remaining()
            var.links = [item for item in var.links if item.state is not self]
            self.instance.changed(var, before)

    def payload(self) -> dict:
        return {"flags": self.flags, "cur": self.cur, "rate": self.cooldown_rate, "last": self.last}


@state_class("STU_9C9A422A")
class Defer(State):
    """Runs its out plug (m_FD0C0646) and subgraph (m_8E484B2E) in the deferred phase of the update, after
    the ticks."""

    def on_begin(self) -> None:
        priority = to_float(self.evaluate(_field(self, "m_priority"), watch=False))
        self.instance.deferred.append((priority, self.counter, self))
        self.instance.deferred.sort(key=lambda item: item[0])

    def timer(self, param: int) -> None:
        if param == 1:
            self.instance.follow(self.node, "m_FD0C0646", self)
            if self.active:
                self.instance.follow(self.node, "m_8E484B2E", self)

    def on_end(self, finished: bool) -> None:
        self.instance.deferred = [item for item in self.instance.deferred if item[2] is not self]
        self.instance.exit_subgraph(self.node, "m_8E484B2E")


@state_class("STU_2F4E2E3F")
class Watch(State):
    """Follows m_onChangedPlug (m_A70DB860) when the watched value (m_B7A3CF78) changes, after writing the
    old value into m_48A8CDEC."""

    def on_begin(self) -> None:
        self.seen = self.evaluate(_field(self, "m_B7A3CF78"))

    def dependency_changed(self) -> None:
        if not equal(self.evaluate(_field(self, "m_B7A3CF78")), self.seen):
            self.timer_at(0, REEVALUATE, transient=True)

    def timer(self, param: int) -> None:
        if param != REEVALUATE:
            return
        previous = lvalue(_field(self, "m_48A8CDEC"))
        if previous is not None:
            self.instance.write(previous, self.seen)
        self.instance.follow(self.node, "m_A70DB860", self)
        self.seen = self.evaluate(_field(self, "m_B7A3CF78"))


@state_class("STU_DEBD057B")
class Switch(State):
    """A switch over subgraphs (m_05706D1C cases, m_A5EF385D otherwise), re-evaluated like a BooleanSwitch."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.case: str | None = None

    def pick(self) -> str:
        value = self.evaluate(_field(self, "m_262AE982"))
        for number, case in enumerate(_field(self, "m_05706D1C") or []):
            if isinstance(case, dict) and equal(value, self.evaluate(case.get("m_value"), watch=False)):
                return f"m_05706D1C[{number}]"
        return "m_A5EF385D"

    def on_begin(self) -> None:
        self.case = self.pick()
        self.instance.follow(self.node, self.case, self)

    def dependency_changed(self) -> None:
        self.timer_at(0, REEVALUATE, transient=True)

    def timer(self, param: int) -> None:
        case = self.pick()
        if param == REEVALUATE and case != self.case:
            self.instance.exit_subgraph(self.node, self.case)
            self.case = case
            self.instance.follow(self.node, case, self)

    def on_end(self, finished: bool) -> None:
        if self.case:
            self.instance.exit_subgraph(self.node, self.case)


@state_class("STU_37D754C8")
class SubScript(State):
    """Runs a child instance of another graph while active (OnBegin 0x7FF78AAB1F30): its variables from
    m_89402D74 (evaluated here), destroyed at the end. The child gets the next free instance id."""

    family = "subscript"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.child: Instance | None = None

    def graph_index(self) -> int | None:
        number = guid(self.node.field("m_36F7B64C.m_36F7B64C.m_C974E369.m_graph"))
        return number & 0xFFFF if number else None

    def on_begin(self) -> None:
        from ow174.game.script import graph as graphs

        index = self.graph_index()
        found = graphs.graph(index) if index is not None else None
        if found is None:
            expr.warn_once(f"sub{index}", "SubScript graph %s is not in the data", f"{index or 0:04X}")
            return
        values = {}
        for item in _field(self, "m_89402D74") or []:
            if isinstance(item, dict):
                target = lvalue(item.get("m_out_Var"))
                if target is not None:
                    values[target] = self.evaluate(item.get("m_value"), watch=False)
        component = self.instance.component
        self.child = component.create(found, parent=self.instance, parent_state=self.index, values=values)

    def on_end(self, finished: bool) -> None:
        if self.child is not None:
            self.instance.component.destroy(self.child)
            self.child = None

    def payload(self) -> dict:
        return {"child": self.child.id if self.child is not None else 0}


@state_class("STUStatescriptStateGameMessageEntry")
class GameMessageEntry(State):
    """Waits for its game message: the idle subgraph (m_33DA5163) until it comes, then the receive subgraph
    (m_452862E6)."""

    family = "message"

    def on_begin(self) -> None:
        self.received = False
        self.instance.follow(self.node, "m_33DA5163", self)

    def message(self, message: int, params: dict, sender: int) -> None:
        self.instance.exit_subgraph(self.node, "m_33DA5163")
        self.received = True
        for item in _field(self, "m_params") or []:
            if isinstance(item, dict):
                target = lvalue(item.get("m_out_Var"))
                if target is not None:
                    self.instance.write(target, params.get(expr.guid_of(item.get("m_B5051BCE")) & 0xFFFF))
        self.instance.follow(self.node, "m_452862E6", self)

    def on_end(self, finished: bool) -> None:
        self.instance.exit_subgraph(self.node, "m_33DA5163")
        self.instance.exit_subgraph(self.node, "m_452862E6")

    def payload(self) -> dict:
        return {"sender": None, "stacked": False}


@state_class("STU_38EE1100")
class SendGameMessageState(State):
    """Sends its game message at the begin (the class's own payload carries an optional reply id)."""

    family = "send"

    def on_begin(self) -> None:
        send(self.instance, self.node, self)


@state_class("STUStatescriptStateCosmeticEntity")
class CosmeticEntity(State):
    """A cosmetic child entity (the gun model): its out variable (m_1DEF59FA) holds a handle to this state
    while active, {kind 1, instance, state, 0} (0254 #49)."""

    def on_begin(self) -> None:
        target = lvalue(_field(self, "m_1DEF59FA"))
        self.var = self.link(target) if target is not None else None

    def on_end(self, finished: bool) -> None:
        if self.var is not None:
            var, self.var = self.var, None
            self.unlink(var)

    def output(self, slot: int):
        return Handle(1, self.instance.id, self.index, 0)


@state_class("STUStatescriptStateGamePadVibration")
class GamePadVibration(State):
    """Ends itself; with no gamepad at once."""

    def on_enter(self) -> None:
        self.request_finish()


@state_class("STUStatescriptStateTrackTargets")
class TrackTargets(State):
    """A target search the server cannot run: it finds nothing."""

    family = "targets"

    def payload(self) -> dict:
        return {"targets": ()}


@state_class("STU_691BFA55", "STU_7CA2FBAF")
class Effect(State):
    """An effect (StEC wire form): a plain state whose payload is its activation counter & 3 (reader
    0x7FF789894540; the client keeps it as its own counter when the state is on)."""

    def payload(self) -> dict:
        return {"counter": self.counter & 3}


@state_class("STU_B9898052")
class ClientOnlyPulser(State):
    """Pulses its client-only subgraph (vtable 0x7FF78B549DE8): OnEnter and every begin while active
    (0x7FF78990D6D0) pulse once, and the client follows its plug m_B6A27F9A once per pulse
    (0x7FF78990D872). Its payload (reader 0x7FF78990D760) is a bit and the 8-bit count of pulses: a count
    that moved on pulses the client as often (mod 256); a state the client reads for the first time pulses
    once only with the bit set (or the node's m_C85E2CBA, node +0xF0), else it takes the count as seen.
    The server sends its own pulse count, and the bit while a pulse is FRESH_MS old or younger and came
    FRESH_MS or more after the instance began, so that a pulser a later frame begins (01CF st108 at a death)
    pulses on the client and the ones on since the spawn do not (unverified: the server's writer is not in the
    client)."""

    FRESH_MS = 500

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.pulses = 0
        self.pulsed_at: int | None = None

    def on_enter(self) -> None:
        self.pulse()

    def reentry(self) -> None:
        self.pulse()

    def pulse(self) -> None:
        self.pulses = (self.pulses + 1) & 0xFF
        if self.now - self.instance.start >= self.FRESH_MS:
            self.pulsed_at = self.now

    def payload(self) -> dict:
        fresh = self.pulsed_at is not None and self.now - self.pulsed_at <= self.FRESH_MS
        return {"fresh": fresh, "count": self.pulses}


@state_class("STU_01178155")
class ActiveQuery(State):
    """A link into its m_inPlug (an STU_A1AAB8C4 input plug) does not begin it: it follows m_7273C81B when
    the state is on and m_C6F3744E when it is off (vt+0x198 0x7FF78AAA8920, called by the base plug enter
    0x7FF78AA83D80 for every input plug that is not the abort plug). 01CF st70 is one: on while 01CF waits
    for 0A43.025, it pulses st108 (the kill feed line) when 003E.025 comes."""

    def plug_enter(self, input_path: str) -> bool:
        if input_path == "m_inPlug":
            self.instance.follow(self.node, "m_7273C81B" if self.active else "m_C6F3744E", self)
            return True
        return super().plug_enter(input_path)


# --- actions and conditions ------------------------------------------------------------------------


@handler("STUStatescriptActionSetVar")
def set_var(instance: Instance, node, caller) -> bool:
    """SetVar (0x7FF78AA7D300): the value (failure: int 0), then a keyed insert, an element write or a
    plain assignment with the value's own type; then m_outPlug."""
    value = instance.evaluate(node.fields.get("m_value"))
    store(instance, node.fields.get("m_out_Var"), value, node.fields.get("m_index"), node.fields.get("m_key"))
    instance.follow(node, "m_outPlug", None)
    return True


def store(instance: Instance, out_cfg, value, index_cfg=None, key_cfg=None) -> None:
    target = lvalue(out_cfg)
    if target is None:
        return
    if key_cfg is not None:
        key = instance.evaluate(key_cfg)
        current = instance.find_var(*target).value()
        items = list(current.items) if isinstance(current, expr.Map) else []
        items = [(k, v) for k, v in items if not equal(k, key)] + [(key, value)]
        instance.write(target, expr.Map(tuple(items)))
        return
    if index_cfg is not None:
        index = to_int(instance.evaluate(index_cfg)) if isinstance(index_cfg, dict) else int(index_cfg)
        if index >= 0:
            current = instance.find_var(*target).value()
            items = list(current) if isinstance(current, tuple) else ([] if current is None else [current])
            while len(items) <= index:
                items.append(None)
            items[index] = value
            instance.write(target, tuple(items))
            return
    instance.write(target, value)


@handler("STU_12F5D52D")
def set_vars(instance: Instance, node, caller) -> bool:
    """SetVars: every {m_out_Var, m_value} pair in order."""
    for pair in node.fields.get("m_143D4C5B") or []:
        if isinstance(pair, dict):
            store(instance, pair.get("m_out_Var"), instance.evaluate(pair.get("m_value")))
    instance.follow(node, "m_outPlug", None)
    return True


@handler("STU_BC5E4622")
def cycle_index(instance: Instance, node, caller) -> bool:
    """CycleIndex: from the current index (m_339A63DB) step forward or back (m_C78954CB) through
    [0, count) and keep the first candidate whose condition holds; the candidate is the parameter the
    condition reads. None: the fail index (m_A53FE9DA, -1)."""
    count = to_int(instance.evaluate(node.fields.get("m_D10618D1")))
    target = lvalue(node.fields.get("m_339A63DB"))
    backwards = truthy(instance.evaluate(node.fields.get("m_C78954CB")))
    fail = node.fields.get("m_A53FE9DA")
    result = to_int(instance.evaluate(fail)) if fail is not None else -1
    if count > 0 and target is not None:
        current = to_int(instance.find_var(*target).value())
        current = min(max(current, 0), count - 1)
        step = -1 if backwards else 1
        for k in range(1, count + 1):
            candidate = (current + step * k) % count
            instance.params.append({None: candidate})
            try:
                ok = truthy(instance.evaluate(node.fields.get("m_condition")))
            finally:
                instance.params.pop()
            if ok:
                result = candidate
                break
    if target is not None:
        instance.write(target, result)
    instance.follow(node, "m_outPlug", None)
    return True


def branch(instance: Instance, node, caller, result: bool) -> bool:
    """Condition execute (0x7FF78AA83CB0): a false result is taken when a false link is; a true one when a
    true link is, or when neither plug has links."""
    if not result:
        return instance.follow(node, "m_falsePlug", caller)
    if instance.follow(node, "m_truePlug", caller):
        return True
    return not node.links("m_truePlug") and not node.links("m_falsePlug")


@handler("STU_3387AB5D")
def condition_bool(instance: Instance, node, caller) -> bool:
    """Condition_Bool: m_condition, evaluated with the calling state as the watcher."""
    return branch(instance, node, caller, truthy(instance.evaluate(node.fields.get("m_condition"), caller)))


@handler("STU_821116A6")
def contains(instance: Instance, node, caller) -> bool:
    """The container m_814F8EC3 has an element equal to m_value (eval 0x7FF78AA85A30)."""
    container = instance.evaluate(node.fields.get("m_814F8EC3"), caller)
    value = instance.evaluate(node.fields.get("m_value"), caller)
    items = container if isinstance(container, tuple) else (() if container is None else (container,))
    return branch(instance, node, caller, any(equal(item, value) for item in items))


@handler("STU_7DEB522D")
def button_held(instance: Instance, node, caller) -> bool:
    """A logical-button condition: the button is held."""
    button = to_int(instance.evaluate(node.fields.get("m_logicalButton")))
    return branch(instance, node, caller, button in instance.component.held)


@handler("STU_4E2BCAAB")
def unknown_condition(instance: Instance, node, caller) -> bool:
    expr.warn_once(node.cls, "condition %s is not modelled: false", node.cls)
    return branch(instance, node, caller, False)


@handler("STUStatescriptActionSwitch")
def action_switch(instance: Instance, node, caller) -> bool:
    """Follow the case (m_FDD34C25) whose value equals the switch value, else the default (m_CD38D5DB)."""
    value = instance.evaluate(node.fields.get("m_262AE982"))
    for number, case in enumerate(node.fields.get("m_FDD34C25") or []):
        if isinstance(case, dict) and equal(value, instance.evaluate(case.get("m_value"))):
            instance.follow(node, f"m_FDD34C25[{number}]", None)
            break
    else:
        instance.follow(node, "m_CD38D5DB", None)
    instance.follow(node, "m_outPlug", None)
    return True


def message_params(instance: Instance, node) -> dict:
    params = {}
    for item in node.fields.get("m_params") or []:
        if isinstance(item, dict):
            params[expr.guid_of(item.get("m_B5051BCE")) & 0xFFFF] = instance.evaluate(item.get("m_value"))
    return params


def send(instance: Instance, node, caller) -> None:
    """Send a game message: to this entity's own instances, or through the world to another entity."""
    cfg = node.fields.get("m_gameMessage")
    message = expr.guid_of(cfg.get("m_gameMessage")) if isinstance(cfg, dict) else 0
    if not message:
        return
    params = message_params(instance, node)
    component: Component = instance.component
    targets = node.fields.get("m_targets", node.fields.get("m_target"))
    target = instance.evaluate(targets) if targets is not None else expr.Entity(component.entity)
    entity = expr.to_entity(target)
    if entity == component.entity:
        send_message(component, message, params, component.entity)
    elif entity:
        component.world.send_message(entity, message, params, component.entity)


@handler("STU_187D5125")
def stop_ability(instance: Instance, node, caller) -> bool:
    """ActionStopAbility: the running abilities of the instance stop (one per graph in the hero
    data)."""
    for state in list(instance.states.values()):
        if isinstance(state, Ability) and state.active:
            state.stop()
    instance.follow(node, "m_outPlug", None)
    return True


@handler("STUStatescriptActionSendGameMessage")
def send_game_message(instance: Instance, node, caller) -> bool:
    send(instance, node, caller)
    instance.follow(node, "m_outPlug", None)
    return True


def no_op(instance: Instance, node, caller) -> bool:
    """Actions with no effect on the server's graph state (effects, stats, client-only messages)."""
    instance.follow(node, "m_outPlug", None)
    return True


for _name in (
    "STUStatescriptActionEffect",
    "STUStatescriptActionCriteria",
    "STU_CEBDAF17",  # TrackStat
    "STU_100EF8C2",  # SendClientOnlyGameMessage
    "STUStatescriptActionSendClientGlobalGameMessage",
    "STUStatescriptActionPlayScript",
    "STU_7106C356",
    "STU_C43F9944",
):
    handler(_name)(no_op)


__all__ = ["ABORT", "Asset"]
