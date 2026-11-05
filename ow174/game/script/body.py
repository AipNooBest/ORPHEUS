"""An opt-in body runtime with a fail-closed replication boundary.

Diagnostic mode runs the extracted initialization without sending guessed wire
state. Experimental mode sends only fully supported snapshots with explicit
native state payloads. Neither mode promises retail weapon simulation fidelity.
"""

from ow174.game.script.replication import ReplicationError, from_runtime
from ow174.game.script.research_runtime import BodyRuntime
from ow174.game.statescript import Stream, owner_ack

MODES = ("off", "diagnostic", "experimental")


def _signature(replica) -> tuple:
    return (
        tuple(sorted(replica.entity_vars.items())),
        tuple(
            (
                item.index, item.graph, item.parent, tuple(sorted(item.presence.items())),
                tuple(sorted(item.extra.items())),
                tuple(
                    (state, None if payload is None else (payload.value, payload.count))
                    for state, payload in sorted(item.active.items())
                ),
            )
            for item in replica.instances
        ),
    )


class BodyScript:
    def __init__(
        self, entity: int, body_definition: int, quanta_us: int, *, generation: int,
        mode: str = "diagnostic", frame: int | None = None, state_payloads=None,
    ) -> None:
        if mode not in MODES or mode == "off":
            raise ValueError("A body script needs diagnostic or experimental mode")
        self.mode = mode
        self.stream = Stream(entity)
        self.runtime = BodyRuntime(body_definition, quanta_us=quanta_us, generation=generation)
        self.runtime.initialize(frame)
        self.state_payloads = state_payloads
        self.error: str | None = None
        self._sent_signature: tuple | None = None
        self.created = False

    def on_created(self) -> None:
        """The datagram carrying this lifecycle's create was acknowledged."""
        self.created = True

    def advance(self, command) -> None:
        self.runtime.advance(command)

    def pending(self, now: float):
        """Return reliable full data, or an ack only after data arrived and input ran."""
        if self.mode != "experimental" or not self.created:
            return None
        frame = self.runtime.frame
        if frame is None or frame < 1 or frame <= self.stream.cmfd:
            return None
        try:
            replica = from_runtime(self.runtime, state_payloads=self.state_payloads)
        except ReplicationError as error:
            self.error = str(error)
            return None
        self.error = None
        signature = _signature(replica)
        if signature != self._sent_signature:
            update = self.stream.full_frame(replica.frame(frame))
            self.stream.cmfd = frame
            self._sent_signature = signature
            self.stream.next_ack = now + 0.1
            return update
        if self.stream.data_in_flight or now < self.stream.next_ack:
            return None
        self.stream.cmfd = frame
        self.stream.next_ack = now + 0.1
        return self.stream.ack_delta(owner_ack(frame))

    def snapshot(self) -> dict:
        result = self.runtime.snapshot()
        result["mode"] = self.mode
        result["replication_error"] = self.error
        result["wire_data_sent"] = self.stream.data_last > 0
        return result
