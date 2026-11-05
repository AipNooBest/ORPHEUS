"""The NPC body's statescript, shared by all observers."""

from ow174.game import observers
from ow174.game.bits import BitWriter
from ow174.game.script import graph, runtime
from ow174.game.script.driver import OWNER_HEALTH, BodyStream, ReplicatedBody, Snapshot, create_initial_graphs

DIED = 0x024000000000003E  # 003E.025, the body's authoritative death message
_Payload = tuple[BitWriter, Snapshot, set]


class BotWorld(runtime.World):
    """Use the NPC body's associated fire-control slots as logical input bindings."""

    def __init__(self, hero: int, mode: int, weapon_buttons: dict[str, int]) -> None:
        super().__init__(hero, mode)
        self.weapon_buttons = weapon_buttons

    def ai_weapon_button(self, slot: int) -> int | None:
        return self.weapon_buttons.get(str(slot))


class BotScript(ReplicatedBody):
    """Run the NPC body's graphs on the server clock. Movement and AI stay with the bot."""

    def __init__(self, body: int, hero: int, entity: int, frame: int, mode: int, health: float) -> None:
        record = graph.entity_body(body)
        if record is None:
            raise ValueError(f"no statescript data for entity body {body:016X}")
        self.frame = frame
        buttons = record.get("ai_weapon_buttons")
        world = BotWorld(hero, mode, buttons) if buttons else runtime.World(hero, mode)
        component = self.component = runtime.Component(entity, world=world)
        component.frame = frame
        component.now = frame * component.frame_ms
        self.set_health(health, health)
        for instance in create_initial_graphs(component, record):
            instance.init()
        component.run_frame(frame)
        self.server_only = self.initial_exclusions()
        snapshot = observers.snapshot(self)
        self.initial = observers.full_frame(self, snapshot), snapshot
        self.death_frame: int | None = None
        self._produced = snapshot

    def advance_live(self, frame: int) -> Snapshot | None:
        """Advance a live weapon body and freeze its latest observer baseline."""
        if frame <= self.frame or self.component.owner_dead:
            return None
        self.frame = frame
        self.component.run_frame(frame)
        self._report_shots()
        snapshot = observers.snapshot(self)
        before = self._produced
        if (
            snapshot.instances == before.instances
            and snapshot.vars == before.vars
            and snapshot.states == before.states
        ):
            return None
        self.initial = observers.full_frame(self, snapshot), snapshot
        self._produced = snapshot
        return snapshot

    def die(self, frame: int, params: dict, sender: int) -> _Payload | None:
        """Deliver 003E.025 and encode the change that happens immediately."""
        component = self.component
        if component.owner_dead:
            return None
        if frame < self.frame:
            raise ValueError("bot death predates its script")
        # A bot can die in the tick that created it. Its initial frame is already
        # frozen, so run the terminal event after the bootstrap simulation frame.
        self.frame = max(frame, self.frame + 1)
        component.run_frame(self.frame)
        component.owner_dead = True
        component.set(OWNER_HEALTH, 0.0)
        if not runtime.send_message(component, DIED, params, sender):
            return None
        component.run_now()
        self.death_frame = frame
        return self._delta()

    def advance_death(self, frame: int, lifetime_ticks: int) -> list[_Payload]:
        """Run due graph events within this death's lifetime and retain each frame's change in order."""
        if self.death_frame is None:
            return []
        limit = min(frame, self.death_frame + lifetime_ticks)
        component = self.component
        payloads = []
        while (due := component.next_event_time()) is not None:
            due_frame = due // component.frame_ms
            if due_frame > limit:
                break
            self.frame = due_frame
            component.run_frame(due_frame)
            payload = self._delta()
            if payload is not None:
                payloads.append(payload)
        return payloads

    def _delta(self) -> _Payload | None:
        snapshot = observers.snapshot(self)
        base = BodyStream(self.component.entity)
        base.base = self._produced
        delta, keys = observers.delta(self, base, snapshot)
        if delta is not None:
            self._produced = snapshot
            return delta, snapshot, keys
