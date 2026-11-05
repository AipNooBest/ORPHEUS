"""The NPC body's initial and death statescript frames, shared by all observers."""

from ow174.game import observers
from ow174.game.bits import BitWriter
from ow174.game.script import graph, runtime
from ow174.game.script.driver import OWNER_HEALTH, BodyStream, ReplicatedBody, Snapshot, create_initial_graphs

DIED = 0x024000000000003E  # 003E.025, the body's authoritative death message


class BotScript(ReplicatedBody):
    """Run an NPC body's graph at creation and death. Movement and AI stay with the bot."""

    def __init__(self, body: int, hero: int, entity: int, frame: int, mode: int, health: float) -> None:
        record = graph.entity_body(body)
        if record is None:
            raise ValueError(f"no statescript data for entity body {body:016X}")
        self.frame = frame
        component = self.component = runtime.Component(entity, world=runtime.World(hero, mode))
        component.frame = frame
        component.now = frame * component.frame_ms
        component.set(OWNER_HEALTH, float(health))
        for instance in create_initial_graphs(component, record):
            instance.init()
        component.run_frame(frame)
        self.server_only = self.initial_exclusions()
        snapshot = observers.snapshot(self)
        self.initial = observers.full_frame(self, snapshot), snapshot

    def die(self, frame: int, params: dict, sender: int) -> tuple[BitWriter, Snapshot, set] | None:
        """Deliver 003E.025 at this server frame and encode its terminal change from the initial frame."""
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

        snapshot = observers.snapshot(self)
        base = BodyStream(component.entity)
        base.base = self.initial[1]
        delta, keys = observers.delta(self, base, snapshot)
        if delta is not None:
            return delta, snapshot, keys
