"""The game server: one UDP port for every match, one thread.

The lobby sends each player of a match 20600 with this server's address, a connection id of their
own and two keys (lobby/matchmaker.py). The client then sends SYN datagrams with that id in the
header until it gets SYN|ACK, and the session is open with its first ACK. From then on the server
sends one world frame per tick; the first reliable message is 20300 (load the map).

Datagrams go to their game by that connection id, as in the client: it routes every datagram by the id
in its header and opens it with that connection's keys, whatever the address (0x7FF7893127F0). All
links of one game share one socket (0x7FF78930F920), so a game that goes from one match to the next
sends the old connection's FIN and the new one's SYN from the same address; the address of a client
follows its newest datagram.
"""

import ctypes
import logging
import math
import os
import socket
import struct
import threading
import weakref
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass, field, replace

from ow174.game import combat, correction, messages, movelog, observers, world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.commands import InputError, read_commands
from ow174.game.content import PRACTICE_RANGE_MODE
from ow174.game.environment import ExecutionEnvironment, SystemEnvironment
from ow174.game.link import (
    ACK,
    FIN,
    HEADER_SIZE,
    KIND_CONTROL,
    KIND_FRAME,
    MAX_DATAGRAM,
    SYN,
    LinkCipher,
    Packet,
    ReceiveWindow,
    delivery,
    peek_connection,
)
from ow174.game.match import SEND_EVERY, Match, Player, no_skin, pong, sends
from ow174.game.projectiles import MAX_LEASE_FRAME, _ObservedProjectile
from ow174.game.script.bot import practice_nameplate
from ow174.game.script.driver import BodyStream, Snapshot, warm_up
from ow174.jam.codec import DecodeError
from ow174.jam.values import to_jsonable

log = logging.getLogger("ow174.game")

TICK_SECONDS = 0.016
RECEIVE_BYTES = 4096
FIRST_TICK = 1000
SILENCE_SECONDS = 10.0  # the client sends at least every 250 ms; after this long it is gone
# A game that has not connected this long after its 20600 is not coming (for example it could not
# reach the game server's address): the player leaves the match, and the lobby takes them back.
HANDOFF_SECONDS = 60.0
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_SIZE
MAX_ENTITY_RECORDS = 127
MAX_PENDING_COMBAT_NOTICES = 256  # per body create, retain the newest notices during prolonged loss
BOT_DESTROY_GRACE_TICKS = round(SILENCE_SECONDS / TICK_SECONDS)  # bound waits by the transport silence budget
PLAYER_BODY_REUSE_SECONDS = 30.0  # the client retains destroyed entity IDs
PING = 21610
MAP_LOADED = (21601, 21602)
GAME_MESSAGE = 21616
LEAVE_GAME = 20304
# A game sent to the menu (20304) closes its link within ~30 ms; one that does
# not is dropped after this long, so that its match still ends.
LEAVE_GRACE_SECONDS = 3.0
# Leave room for the latest authenticated handoff and its reliable leave.
CLOCK_DRAIN_TICKS = math.ceil((HANDOFF_SECONDS + LEAVE_GRACE_SECONDS) / TICK_SECONDS) + SEND_EVERY
MAX_SIMULATION_TICK = MAX_LEASE_FRAME - CLOCK_DRAIN_TICKS
DROPPED_LOG_SECONDS = 5.0  # datagrams we drop: one line per address, connection and reason in this time
SIO_UDP_CONNRESET = 0x9800000C


def next_tick_after(due: float, now: float) -> tuple[float, int]:
    """When the tick after the one due at `due` is due, and how many ticks fell behind `now` (they are
    skipped, not run late). So the packet frame stays on real time, like retail's (62.5 per second, OW2
    traffic): the client's command clock runs on real time and only ever jumps forward to the packet
    frame (0x7FF7896DB296, 0x7FF789AC7140), so a frame number that fell behind would leave the client's
    commands further ahead of it with every stall, past its 64 predicted states and 32 kept commands."""
    following = due + TICK_SECONDS
    if following >= now:
        return following, 0
    missed = int((now - following) // TICK_SECONDS) + 1
    return following + missed * TICK_SECONDS, missed


def _ignore_connection_resets(sock: socket.socket) -> None:
    """On Windows a closed game's ICMP "port unreachable" comes back as an error from the next
    recvfrom; SIO_UDP_CONNRESET turns that off. Python's sock.ioctl does not take it, so WSAIoctl."""
    if os.name != "nt":
        return
    ws2 = ctypes.WinDLL("ws2_32")
    ws2.WSAIoctl.argtypes = [
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    off = wintypes.BOOL(False)
    returned = wintypes.DWORD()
    ws2.WSAIoctl(
        sock.fileno(),
        SIO_UDP_CONNRESET,
        ctypes.byref(off),
        ctypes.sizeof(off),
        None,
        0,
        ctypes.byref(returned),
        None,
        None,
    )


@dataclass
class Handoff:
    """What the lobby puts in a player's 20600."""

    conn: int
    key_in: bytes  # the client encrypts with it (20600 +0xAE)
    key_out: bytes  # we encrypt with it (20600 +0xCE)
    match_id: tuple[int, int]


@dataclass
class OpenMatch:
    """A match that still has free places, as the matchmaker sees it."""

    match: Match
    waiting: bool  # still waiting for its players: hero select has not started counting down
    free: list[int]  # free places per team (one entry for free for all)
    roles: list[dict[int, int]]  # per team: role queue role -> players playing it
    started: float  # time.time() of its creation


@dataclass
class _BotScriptDelivery:
    stream: BodyStream
    source: object
    live: Snapshot | None = None  # newest snapshot waiting behind an unacknowledged frame
    volleys: deque[_ObservedProjectile] = field(default_factory=deque)
    death_frames: deque[Snapshot] | None = None  # None until this viewer is registered for death
    death_snapshot: Snapshot | None = None  # latest terminal state for a deferred feed pulse
    nameplate_update: world.EntityUpdate | None = None  # standalone presenter chunk awaiting ACK


class Client:
    """One connected game: the transport state and the queues of what to send."""

    def __init__(self, server: "GameServer", address, conn: int, cipher: LinkCipher, player: Player) -> None:
        self.server = server
        self.address = address
        self.conn = conn
        self.cipher = cipher
        self.player = player
        self.window = ReceiveWindow()
        self.seq = 0  # of our next datagram
        self.open = False
        self.closed = False
        self.heard = server.environment.wall_time(callsite="client.heard")
        self.next_reliable = 0
        self._ownership_ordinal: int | None = None
        self.reliable = []  # [(seq, encoded)] waiting to go out, oldest first
        self.unreliable = []  # [encoded]
        self.entities = []  # [EntityUpdate] waiting to go out
        self.in_flight = {}  # datagram seq -> (reliable messages, entity updates) it carried
        # Current combat body creates. None means the peer acknowledged this create.
        self._combat_notices: dict[int, deque[tuple[int, bytes]] | None] = {}
        self._body_health: dict[int, world.EntityUpdate] = {}  # latest snapshot awaiting delivery per body
        self._combat_notice_tail: dict[int, int] = {}  # last promoted reliable ordinal per body
        self._bot_scripts: dict[int, _BotScriptDelivery] = {}
        self._body_destroy_wait: dict[int, tuple[world.EntityUpdate, int]] = {}
        self._player_body_reuse_after: dict[int, float | None] = {}  # None until destroy ACK
        self._pending_possessions: dict[int, tuple[int, world.EntityUpdate]] = {}
        self._confirmed_possessions: dict[int, int] = {}  # remote controller -> body named by its ACKed write
        self._own_controller_acked = False
        self._mode_create_acked = False
        self._pending_mode_frame: BitWriter | None = None
        self._display_notices: deque[tuple[int | None, int, bytes]] = deque(maxlen=MAX_PENDING_COMBAT_NOTICES)
        self.expected_in = 0  # the client's next reliable message
        self.parked_in = {}
        self.leaving_since: float | None = None  # when go_home sent 20304

    def log(self, message: str, level: int = logging.INFO) -> None:
        log.log(level, "[game %s:%d %s] %s", self.address[0], self.address[1], self.player.name, message)

    def go_home(self, now: float) -> None:
        """20304 with 0 = leave for good (0x7FF7896E6830): the game goes back to the menu and closes its
        link. GameServer._tick drops it after LEAVE_GRACE_SECONDS if it does not."""
        if self.leaving_since is None:
            self._pending_mode_frame = None
            self._display_notices.clear()
            self.queue_reliable(LEAVE_GAME, {"+0x78": 0, "+0x79": False})
            self.leaving_since = now

    def drain_clock(self, now: float) -> None:
        """Discard obsolete world data while preserving reliable ordinals and packet ACKs."""
        self.unreliable.clear()
        self.entities.clear()
        self._pending_possessions.clear()
        for _, records in self.in_flight.values():
            records.clear()
        self.go_home(now)

    # --- queues --------------------------------------------------------------------------------

    def queue_reliable(self, msg_id: int, value: dict) -> int:
        return self._queue_encoded_reliable(msg_id, messages.encode(msg_id, value))

    def queue_ownership(self) -> None:
        self._ownership_ordinal = self.queue_reliable(
            20302, {"+0x78": {"+0x0": self.player.entity}, "+0x7C": False, "+0x80": 0},
        )

    def ownership_delivered(self) -> bool:
        """The client can apply 20302 once the entire reliable prefix has arrived."""
        ordinal = self._ownership_ordinal
        return ordinal is not None and self._oldest_pending_reliable() > ordinal

    @property
    def controller_ready(self) -> bool:
        """Whether this connection can receive updates for its own controller."""
        return self._own_controller_acked and self.leaving_since is None

    def _queue_encoded_reliable(self, msg_id: int, payload: bytes) -> int:
        ordinal = self.next_reliable
        self.reliable.append((ordinal, payload))
        self.next_reliable += 1
        if msg_id not in (20306,):
            self.log(f"[>>>] {msg_id} (reliable #{ordinal})")
        return ordinal

    def bot_create_acked(self, entity: int) -> bool:
        """Whether this connection acknowledged the current create for a combat body."""
        return entity in self._combat_notices and self._combat_notices[entity] is None

    def bot_stream(self, entity: int) -> BodyStream | None:
        """The current bot lifetime's registered stream, even before its baseline ACK."""
        script = self._bot_scripts.get(entity)
        return script.stream if script is not None and entity not in self._body_destroy_wait else None

    def queue_mode_frame(self, frame: BitWriter) -> None:
        """Send the mode UI only once its entity exists on the client."""
        if self.leaving_since is not None:
            return
        if self._mode_create_acked:
            self.queue_entities([self.player.mode_script.full_frame(frame)])
        else:
            self._pending_mode_frame = frame  # countdown updates replace earlier waiting values

    def queue_combat_notice(self, entity: int | None, msg_id: int, value: dict) -> None:
        """Wait for any referenced body before considering delivery of the notice."""
        if self.leaving_since is not None:
            return
        if entity is not None and (entity not in self._combat_notices or entity in self._body_destroy_wait):
            return
        payload = messages.encode(msg_id, value)
        pending = self._combat_notices.get(entity)
        if pending is None:
            self._promote_combat_notice(entity, msg_id, payload)
        else:
            pending.append((msg_id, payload))

    def _promote_combat_notice(self, entity: int | None, msg_id: int, payload: bytes) -> None:
        if self.leaving_since is not None:
            return
        if msg_id in (combat.KILL_NOTICE, combat.DEATH_NOTICE):
            identity_ready = self.combat_identity_ready(entity)
            destroy = self._body_destroy_wait.get(entity)
            if destroy is not None and (not identity_ready or self.player.match.tick >= destroy[1]):
                return
            if not identity_ready or not self.player.mode_script.delivered:
                self._display_notices.append((entity, msg_id, payload))
                return
        ordinal = self._queue_encoded_reliable(msg_id, payload)
        if entity is not None:
            self._combat_notice_tail[entity] = ordinal

    def combat_identity_ready(self, body: int | None) -> bool:
        """Remote human bodies acquire their names from an acknowledged possession."""
        if self.leaving_since is not None:
            return False
        if body is None:
            return True
        controller = body & ~0xFF  # player body IDs keep their controller's upper 24 bits
        if controller == self.player.entity:
            return self.player.body == body
        if not any(player.entity == controller for player in self.player.match.players):
            return True  # NPC names have a separate native path
        return self._confirmed_possessions.get(controller) == body

    def _flush_combat_ui(self) -> None:
        if self.leaving_since is not None or not self.player.mode_script.delivered:
            return
        for _ in range(len(self._display_notices)):
            self._promote_combat_notice(*self._display_notices.popleft())
        for script in self._bot_scripts.values():
            if script.death_frames is not None:
                self._flush_bot_frames(script)

    def queue_body_health(self, entity: int, components: dict) -> None:
        """Keep the latest complete pool snapshot for a current body, including before create ACK."""
        if entity not in self._combat_notices or entity in self._body_destroy_wait:
            return
        previous = self._body_health.get(entity)
        if previous is not None:
            # Each viewer owns its record. Supersession must not disable another client's resend.
            self.cancel_entity_record(previous)
        update = world.EntityUpdate(entity, world.OP_UPDATE, lambda origin: world.update(origin, components))
        self._body_health[entity] = update
        if self.bot_create_acked(entity):
            self.queue_entities([update])

    def queue_bot_live(self, entity: int, snapshot: Snapshot) -> None:
        script = self._bot_scripts.get(entity)
        if script is None or entity in self._body_destroy_wait or script.death_frames is not None:
            return
        script.live = snapshot
        self._flush_bot_frames(script)

    def retain_bot_volley(self, delivery: _ObservedProjectile) -> None:
        script = self._bot_scripts.get(delivery.stream.entity)
        if script is not None and script.stream is delivery.stream:
            script.volleys.append(delivery)

    def release_bot_volley(self, delivery: _ObservedProjectile) -> None:
        script = self._bot_scripts.get(delivery.stream.entity)
        if script is None or script.stream is not delivery.stream:
            return
        first = script.volleys[0] is delivery
        script.volleys.remove(delivery)
        if first and self.player.client is self:
            self._flush_bot_frames(script)

    def queue_bot_death(self, entity: int, payload: Snapshot | None) -> None:
        """Register this lifetime's death, even when the first statescript changes are delayed."""
        script = self._bot_scripts.get(entity)
        if script is None or entity in self._body_destroy_wait or script.death_frames is not None:
            return
        script.death_frames = deque() if payload is None else deque([self._project_bot_snapshot(payload)])
        self._flush_bot_frames(script)

    def queue_bot_death_update(self, entity: int, payload: Snapshot) -> None:
        """Queue a continuation for a viewer already registered for this death."""
        script = self._bot_scripts.get(entity)
        if script is None or entity in self._body_destroy_wait or script.death_frames is None:
            return
        # A later timer keeps the presenter's projection from the original death.
        known = script.death_frames[-1] if script.death_frames else script.death_snapshot
        if known is None:
            stream = script.stream
            known = stream.base or stream.pending[stream.full_update.chunk_last][0]
        script.death_frames.append(self._project_bot_snapshot(payload, known=known))
        self._flush_bot_frames(script)

    def _flush_bot_frames(self, script: _BotScriptDelivery, *, replace_nameplate: bool = False) -> None:
        stream = script.stream
        nameplate = script.nameplate_update
        if stream.base is None or (stream.pending and (nameplate is None or not replace_nameplate)):
            return
        if script.volleys:
            delivery = script.volleys[0]
            if stream.entity in self._body_destroy_wait or self.player.match.tick >= delivery.deadline:
                return  # projectile cleanup cancels its launch before advancing the stream
            if delivery.matching_volley(stream.base) is not None:
                return  # keep this activation installed until its ch2 launch is acknowledged
            base = stream.base if nameplate is None else stream.pending[nameplate.chunk_last][0]
            latest = script.live or base
            if delivery.record.instance not in latest.instances:
                return  # the old activation expires without recreating a removed instance
            states = dict(latest.states)
            # Later dependent states stay at their acknowledged value. Everything else
            # still comes from the latest snapshot, without replaying old live state.
            for later in script.volleys:
                key = later.volley_key
                if key in stream.base.states:
                    states[key] = stream.base.states[key]
                else:
                    states.pop(key, None)
            states[delivery.volley_key] = delivery.activation
            snapshot = replace(latest, states=states)
            self._queue_bot_snapshot(script, snapshot)
            return
        if script.live is not None:
            snapshot = script.live
            script.live = None
            if self._queue_bot_snapshot(script, snapshot):
                return
        if script.death_frames:
            while script.death_frames:
                snapshot = script.death_snapshot = script.death_frames.popleft()
                if self._queue_bot_death_snapshot(script, snapshot):
                    return
        elif script.death_snapshot is not None:
            # Rechecking feed eligibility must not install a new presenter on a corpse.
            self._queue_bot_death_snapshot(script, script.death_snapshot, known=stream.base)

    def _queue_bot_death_snapshot(
        self, script: _BotScriptDelivery, snapshot: Snapshot, *, known: Snapshot | None = None,
    ) -> bool:
        stream = script.stream
        allowed = self.player.match.combat.kill_feed_allowed(self.player, stream.entity)
        snapshot = stream.kill_feed_snapshot(snapshot, allowed=allowed)
        return self._queue_bot_snapshot(script, snapshot, known=known)

    def _project_bot_snapshot(self, snapshot: Snapshot, *, known: Snapshot | None = None) -> Snapshot:
        if self.player.match.game_map.mode_guid != PRACTICE_RANGE_MODE:
            return snapshot
        mode_ready = self.player.mode_script.delivered
        if mode_ready and known is None:
            return snapshot
        child = practice_nameplate(snapshot)
        if child is None or (mode_ready and known is not None
                             and known.instances.get(child) == snapshot.instances[child]):
            return snapshot
        # The client-only 0D24 presenter reads #14206 from the mode's 0CB6 graph.
        return replace(
            snapshot,
            instances={item: value for item, value in snapshot.instances.items() if item != child},
            vars={key: value for key, value in snapshot.vars.items() if key[0] != child},
            states={key: value for key, value in snapshot.states.items() if key[0] != child},
            events={item: value for item, value in snapshot.events.items() if item != child},
        )

    def _flush_bot_nameplates(self) -> None:
        if (not self._bot_scripts or self.leaving_since is not None or not self.player.mode_script.delivered
                or self.player.match.game_map.mode_guid != PRACTICE_RANGE_MODE):
            return
        for script in self._bot_scripts.values():
            stream = script.stream
            if script.nameplate_update is not None:
                # Match logic has queued this frame's health and combat updates.
                self._flush_bot_frames(script, replace_nameplate=True)
                continue
            base = stream.base
            if (base is None or stream.pending or script.live is not None or script.volleys
                    or script.death_frames is not None or stream.entity in self._body_destroy_wait):
                continue
            latest = script.source.latest_live
            child = practice_nameplate(latest)
            if child is None or child in base.instances:
                continue
            descriptor = latest.instances[child]
            parent = descriptor[1][0]
            if base.instances.get(parent) != latest.instances[parent]:
                continue
            # The acknowledged viewer snapshot owns everything outside the presenter.
            snapshot = replace(
                base,
                instances=base.instances | {child: descriptor},
                vars=base.vars | {key: value for key, value in latest.vars.items() if key[0] == child},
                states=base.states | {key: value for key, value in latest.states.items() if key[0] == child},
                events=base.events | {item: value for item, value in latest.events.items() if item == child},
            )
            self._queue_bot_snapshot(script, snapshot, nameplate_only=True)

    def _queue_bot_snapshot(
        self, script: _BotScriptDelivery, snapshot: Snapshot, *,
        known: Snapshot | None = None, nameplate_only: bool = False,
    ) -> bool:
        stream = script.stream
        snapshot = self._project_bot_snapshot(snapshot, known=known)
        old_nameplate = script.nameplate_update
        if old_nameplate is not None:
            previous, _ = stream.pending[old_nameplate.chunk_last]
            if (snapshot.instances == previous.instances and snapshot.vars == previous.vars
                    and snapshot.states == previous.states):
                return False  # only the already-queued presenter would be repeated
        frame, keys = observers.delta(script.source, stream, snapshot)
        if frame is None:
            return False
        # Authoritative updates normally wait for the preceding baseline. A
        # presenter alone can be replaced by a cumulative update from the same base.
        update = stream.delta(frame, snapshot, keys, 0)
        update.resend = True
        if old_nameplate is not None:
            child = practice_nameplate(snapshot)
            nameplate_only = child is not None and all(key[1] == child for key in keys)
            self.cancel_entity_record(old_nameplate)
            stream.pending.pop(old_nameplate.chunk_last, None)
        script.nameplate_update = update if nameplate_only else None
        self.queue_entities([update])
        return True

    def _ack_entity_create(self, entity: int) -> None:
        if entity == self.player.entity and not self._own_controller_acked:
            self._own_controller_acked = True
            possession = self._pending_possessions.get(entity)
            if possession is not None and self.controller_ready:
                self.queue_entities([possession[1]])
        pending = self._combat_notices.get(entity)
        if pending is not None:
            self._combat_notices[entity] = None
            health = self._body_health.get(entity)
            if health is not None:
                self.queue_entities([health])
            script = self._bot_scripts.get(entity)
            if script is not None:
                self.queue_entities([script.stream.full_update])
            for msg_id, payload in pending:
                self._promote_combat_notice(entity, msg_id, payload)

    def _oldest_pending_reliable(self) -> int:
        queued = self.reliable[0][0] if self.reliable else self.next_reliable
        in_flight = (batch[0][0] for batch, _ in self.in_flight.values() if batch)
        return min(queued, min(in_flight, default=queued))

    def body_id_available(self, entity: int) -> bool:
        """Whether a previous player body can no longer be referenced by this peer."""
        if entity not in self._player_body_reuse_after:
            return True
        ready = self._player_body_reuse_after[entity]
        tail = self._combat_notice_tail.get(entity)
        return (
            ready is not None
            and (tail is None or self._oldest_pending_reliable() > tail)
            and self.server.environment.monotonic(callsite="client.body_id_available") >= ready
        )

    def _body_destroy_pending(self, entity: int, oldest: int) -> bool:
        if any(body == entity for body, _, _ in self._display_notices):
            return True
        tail = self._combat_notice_tail.get(entity)
        health_pending = entity in self._body_health and entity not in self._player_body_reuse_after
        if health_pending and self.bot_create_acked(entity):
            return True  # bystanders also need the old body's terminal health
        if tail is not None and oldest <= tail:
            return True  # the receiver can park a notice behind an earlier reliable gap
        script = self._bot_scripts.get(entity)
        # A frozen bot script must not keep retrying the create of a retired body.
        if script is None or not self.bot_create_acked(entity):
            return False
        return bool(
            (script.stream.pending and script.nameplate_update is None)
            or script.live is not None
            or script.death_frames
            or (script.death_frames is not None and health_pending)
        )

    def _hold_body_destroy(self, item: world.EntityUpdate) -> bool:
        entity = item.entity
        if entity in self._body_destroy_wait:
            return True
        if entity not in self._combat_notices or not self._body_destroy_pending(
            entity, self._oldest_pending_reliable()
        ):
            return False
        # Keep the old generation eligible for delivery until its notices and any
        # pending bot health and statescript have arrived.
        expiry = self.player.match.tick + BOT_DESTROY_GRACE_TICKS
        self._body_destroy_wait[entity] = (item, expiry)
        return True

    def _release_body_destroys(self, tick: int) -> None:
        if not self._body_destroy_wait:
            return
        oldest = self._oldest_pending_reliable()
        ready = []
        for entity, (item, expiry) in list(self._body_destroy_wait.items()):
            if tick >= expiry or not self._body_destroy_pending(entity, oldest):
                self._forget_body(entity)
                ready.append(item)
        if ready:
            self.queue_entities(ready)

    def _forget_body(self, entity: int) -> None:
        self._combat_notices.pop(entity, None)
        self._discard_display_notices(entity)
        controller = entity & ~0xFF
        if self._confirmed_possessions.get(controller) == entity:
            del self._confirmed_possessions[controller]
        self._body_health.pop(entity, None)
        if entity not in self._player_body_reuse_after:
            self._combat_notice_tail.pop(entity, None)
        self._bot_scripts.pop(entity, None)
        self._body_destroy_wait.pop(entity, None)

    def _discard_display_notices(self, entity: int) -> None:
        if self._display_notices:
            self._display_notices = deque(
                (notice for notice in self._display_notices if notice[0] != entity),
                maxlen=MAX_PENDING_COMBAT_NOTICES,
            )

    def queue_unreliable(self, msg_id: int, value: dict) -> None:
        self.unreliable.append(messages.encode(msg_id, value))

    def cancel_entity_record(self, update: world.EntityUpdate) -> None:
        """Cancel a queued record and prevent an earlier send from being retried."""
        update.resend = False
        self.entities = [item for item in self.entities if item is not update]

    def cancel_possession(self, controller: int) -> None:
        previous = self._pending_possessions.pop(controller, None)
        if previous is not None:
            self.cancel_entity_record(previous[1])

    def queue_possession(self, update: world.EntityUpdate, body: int) -> None:
        """Replace an unacknowledged component 44/75 write for this controller."""
        self.cancel_possession(update.entity)
        old = self._confirmed_possessions.get(update.entity)
        if old is not None and old != body:
            del self._confirmed_possessions[update.entity]
            self._discard_display_notices(old)
        self._pending_possessions[update.entity] = (body, update)
        if update.entity != self.player.entity or self.controller_ready:
            self.queue_entities([update])

    def queue_entities(self, updates: list) -> None:
        # A destroy retires all older records of this entity for this client, including
        # reliable creates/chunks awaiting ACK. A later reuse of the id is a new generation.
        lifecycles = getattr(self, "_entity_lifecycles", None)
        if lifecycles is None:
            lifecycles = self._entity_lifecycles = {}
        generations = getattr(self, "_entity_record_generations", None)
        if generations is None:
            generations = self._entity_record_generations = weakref.WeakKeyDictionary()
        for item in updates:
            generation, alive = lifecycles.get(item.entity, (0, True))
            if item not in generations:
                if item.op == world.OP_DESTROY:
                    if self._display_notices and not self.combat_identity_ready(item.entity):
                        self._discard_display_notices(item.entity)
                    if self._hold_body_destroy(item):
                        continue
                if item.op in (world.OP_CREATE, world.OP_DESTROY):
                    # Preserve the generation of already-sent records, even for an older
                    # producer that did not put them through queue_entities first.
                    pending = [*self.entities]
                    for _reliable, records in self.in_flight.values():
                        pending.extend(records)
                    for old in pending:
                        if old.entity == item.entity and old not in generations:
                            generations[old] = generation
                    generation += 1
                    alive = item.op == world.OP_CREATE
                    lifecycles[item.entity] = (generation, alive)
                    if item.entity == self.player.entity:
                        self._own_controller_acked = False
                    self._pending_possessions.pop(item.entity, None)
                    old = self._confirmed_possessions.pop(item.entity, None)
                    if old is not None:
                        self._discard_display_notices(old)
                    self._forget_body(item.entity)
                    if alive:
                        self._track_body_create(item.entity)
                generations[item] = generation
            if generations[item] == generation and (alive or item.op == world.OP_DESTROY):
                self._queue_current_entity(item)
        self.entities = [
            item
            for item in self.entities
            if generations.get(item, 0) == lifecycles.get(item.entity, (0, True))[0]
            and (lifecycles.get(item.entity, (0, True))[1] or item.op == world.OP_DESTROY)
            and self._current_controller_close(item)
        ]

    def _current_controller_close(self, item: world.EntityUpdate) -> bool:
        # A newer full frame replaces a deferred close, including an explicit reopen.
        return not item.controller_close or item.chunk_last == self.player.script.data_last

    def _queue_current_entity(self, item: world.EntityUpdate) -> None:
        script = getattr(self.player, "body_script", None)
        if item.chunk is not None and not item.resend and script is not None and item.stream is script.stream:
            for index, queued in enumerate(self.entities):
                if queued.stream is item.stream and not queued.resend:
                    if item.chunk_last > queued.chunk_last:
                        # The replacement already includes the cumulative keys. The
                        # queued record never went to the wire or registered a report.
                        # Keep the replacement behind updates already queued for this body.
                        last = max(
                            (later for later in range(index + 1, len(self.entities))
                             if self.entities[later].entity == item.entity),
                            default=index,
                        )
                        self.entities.pop(index)
                        self.entities.insert(last, item)
                        item.stream.pending.pop(queued.chunk_last, None)
                    return
        self.entities.append(item)

    def _track_body_create(self, entity: int) -> None:
        match = self.player.match
        bot = next((bot for bot in match.bots if bot.entity == entity), None)
        if bot is None:
            if not any(player.has_body and player.body == entity for player in match.players):
                return
            self._player_body_reuse_after[entity] = None
            self._combat_notice_tail.pop(entity, None)
        self._combat_notices[entity] = deque(maxlen=MAX_PENDING_COMBAT_NOTICES)
        script = getattr(bot, "body_script", None)
        if script is not None:
            stream = BodyStream(entity)
            initial = script.latest_live
            snapshot = self._project_bot_snapshot(initial)
            frame = script.initial[0] if snapshot is initial else observers.full_frame(script, snapshot)
            stream.full(frame, snapshot)
            self._bot_scripts[entity] = _BotScriptDelivery(stream, script)

    # --- sending -------------------------------------------------------------------------------

    def send_control(self, flags: int) -> None:
        self._send(Packet(self.conn, KIND_CONTROL, flags=flags))

    def _send(self, packet: Packet) -> bool:
        packet.seq = self.seq
        packet.ack = self.window.expected
        packet.ack_bits = self.window.bits
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        try:
            self.server.sock.sendto(self.cipher.seal(packet), self.address)
        except OSError as error:
            self.log(f"[!] send failed: {error}", logging.WARNING)
            return False
        return True

    def _ready_entity_records(self) -> list[world.EntityUpdate]:
        if not self.player.has_body:
            return self.entities
        controller, body = self.player.entity, self.player.body
        pending = self._pending_possessions.get(controller)
        created = self.bot_create_acked(body)
        bound = pending is None and self.controller_ready
        if created and bound:
            return self.entities

        possession = pending[1] if pending is not None else None
        script = getattr(self.player, "body_script", None)
        stream = script.stream if script is not None else None
        records = []
        for item in self.entities:
            if item is possession:
                if not created:
                    continue
                bound = True
            elif not bound and (
                item.controller_close
                or (stream is not None and item.stream is stream and item is not stream.full_update)
            ):
                # The body's H0 tree is independent, but owner H1 starts after possession.
                continue
            elif item.op == world.OP_CREATE:
                if item.entity == body:
                    created = True
                elif item.entity == controller:
                    if not created:
                        continue
                    bound = possession is None  # initial controller create also carries 44/75
            elif (
                not bound and item.op == world.OP_DESTROY
                and item.entity not in (controller, body) and item.entity & ~0xFF == controller
            ):
                continue
            records.append(item)
        # Budget selection only removes a suffix, so a dependent record cannot
        # be sent without a prerequisite admitted earlier in this list.
        return records

    def send_frame(self, tick: int, now: float, *, reliable_only: bool = False) -> None:
        queued = []
        own = None
        if reliable_only:
            unreliable, entities = [], []
        else:
            self._release_body_destroys(tick)
            self._flush_bot_nameplates()
            unreliable = self.unreliable[: messages.MAX_PER_KIND]
            queued = self._ready_entity_records()
            movements = self.player.match.remote_movements(self.player)
            if movements and movements[-1].correction is not None:
                own = movements[-1]
            entities = (queued + movements)[:MAX_ENTITY_RECORDS]
        reliable = self.reliable[: messages.MAX_PER_KIND]
        try:
            payload = self._payload(tick, reliable, unreliable, entities)
            if len(payload) > MAX_PAYLOAD:
                # Fit each channel in priority order so a smaller reliable batch
                # leaves room for the other messages and the ordered entity prefix.
                if unreliable or entities:
                    payload = self._payload(tick, reliable, [], [])
                while len(payload) > MAX_PAYLOAD:
                    if len(reliable) <= 1:
                        self._frame_rejected("oldest reliable message exceeds datagram budget")
                        return
                    reliable = reliable[: len(reliable) // 2]
                    payload = self._payload(tick, reliable, [], [])
                while unreliable:
                    candidate = self._payload(tick, reliable, unreliable, [])
                    if len(candidate) <= MAX_PAYLOAD:
                        payload = candidate
                        break
                    unreliable = unreliable[: len(unreliable) // 2]
                while entities:
                    candidate = self._payload(tick, reliable, unreliable, entities)
                    if len(candidate) <= MAX_PAYLOAD:
                        payload = candidate
                        break
                    entities = entities[: len(entities) // 2]
            if own is not None and own not in entities and len(queued) <= len(entities):
                # Leave the reliable and queued entity prefix in place. Only
                # best-effort remote movement can give up space for the correction.
                prefix = len(queued)
                keep = min(len(entities), MAX_ENTITY_RECORDS - 1)
                while keep >= prefix:
                    records = [*entities[:keep], own]
                    try:
                        candidate = self._payload(tick, reliable, unreliable, records)
                    except (ValueError, OverflowError, struct.error):
                        break  # the already validated packet is still sendable
                    if len(candidate) <= MAX_PAYLOAD:
                        entities, payload = records, candidate
                        break
                    if keep == prefix:
                        break
                    keep = prefix + (keep - prefix) // 2
        except (ValueError, OverflowError, struct.error) as error:
            self._frame_rejected(f"frame encoding failed: {type(error).__name__}")
            return
        if not reliable_only and not reliable and not unreliable and not entities and (
            self.reliable or self.unreliable or queued
        ):
            self._frame_rejected("oldest queued entity exceeds datagram budget; fragmentation required")
            return
        sent_seq = self.seq
        if not self._send(Packet(self.conn, KIND_FRAME, flags=ACK, payload=payload)):
            return
        self._frame_budget_warning = None
        del self.reliable[: len(reliable)]
        del self.unreliable[: len(unreliable)]
        sent_records = [item for item in entities
                        if item.op or item.chunk is not None or item.ch2 is not None]
        self.entities = [item for item in self.entities if item not in entities]
        self.in_flight[sent_seq] = (reliable, sent_records)
        self.player.match.sent_updates(self.player, entities)

    def _frame_rejected(self, reason: str) -> None:
        # Do not repeatedly flood logs, consume queues or send an invalid oversized packet.
        if getattr(self, "_frame_budget_warning", None) != reason:
            self.log(f"[!] frame rejected: {reason}", logging.ERROR)
            self._frame_budget_warning = reason

    def _payload(self, tick: int, reliable, unreliable, entities) -> bytes:
        # The command ack: the newest command frame we have. The client stops
        # sending the frames before it again; our tick could make it drop frames we never got. While
        # the owner gets records of its body, it stays near them (correction.command_ack).
        ack = correction.command_ack(self.player, tick, entities)
        return world.frame(tick, ack, reliable, unreliable, entities)

    def acked(self, ack: int, ack_bits: int) -> None:
        """Take back what the client did not get: its reliable messages and entity records go out again."""
        lifecycles = getattr(self, "_entity_lifecycles", {})
        generations = getattr(self, "_entity_record_generations", {})

        def current(item) -> bool:
            generation, alive = lifecycles.get(item.entity, (0, True))
            return (
                generations.get(item, 0) == generation
                and (alive or item.op == world.OP_DESTROY)
                and self._current_controller_close(item)
            )

        lost_reliable, lost_records = [], []
        for seq in list(self.in_flight):
            state = delivery(seq, ack, ack_bits)
            if state is None:
                continue
            reliable, records = self.in_flight.pop(seq)
            if state:
                for item in records:
                    if not current(item):
                        continue
                    if item.op == world.OP_CREATE:
                        if item.entity == world.GAME_MODE_ENTITY:
                            self._mode_create_acked = True
                            if self._pending_mode_frame is not None:
                                self.queue_mode_frame(self._pending_mode_frame)
                                self._pending_mode_frame = None
                        else:
                            self._ack_entity_create(item.entity)
                    elif item.op == world.OP_DESTROY and item.entity in self._player_body_reuse_after:
                        # ACKs can arrive before the server catches up on missed simulation ticks.
                        now = self.server.environment.monotonic(callsite="client.body_destroy_ack")
                        self._player_body_reuse_after[item.entity] = now + PLAYER_BODY_REUSE_SECONDS
                    elif self._body_health.get(item.entity) is item:
                        del self._body_health[item.entity]
                    else:
                        pending = self._pending_possessions.get(item.entity)
                        if pending is not None and pending[1] is item:
                            del self._pending_possessions[item.entity]
                            if item.entity != self.player.entity:
                                self._confirmed_possessions[item.entity] = pending[0]
                                self._flush_combat_ui()
                    if item.stream is not None:
                        first_mode = (
                            item.entity == world.GAME_MODE_ENTITY
                            and item.stream is self.player.mode_script
                            and not item.stream.delivered
                        )
                        item.stream.arrived(item.chunk_last)
                        if first_mode and item.stream.delivered:
                            self._flush_combat_ui()
                        script = self._bot_scripts.get(item.entity)
                        if script is not None and item.stream is script.stream:
                            if item is script.nameplate_update:
                                script.nameplate_update = None
                            self._flush_bot_frames(script)
            else:
                lost_reliable += reliable
                script = getattr(self.player, "body_script", None)
                for item in records:
                    if not current(item):
                        continue
                    if item.resend:
                        lost_records.append(item)
                    elif script is not None and item.stream is script.stream:
                        script.stream.lost(item.chunk_last)
        if lost_reliable:
            self.reliable = sorted(lost_reliable + self.reliable)
            self.log(f"[!] resending {len(lost_reliable)} reliable message(s)")
        lost_records = [item for item in lost_records if item.resend and current(item)]
        if lost_records:
            self.entities = lost_records + self.entities
            self.log(f"[!] resending {len(lost_records)} entity record(s)")

    # --- receiving -----------------------------------------------------------------------------

    def received(self, packet: Packet, now: float) -> None:
        if not self.window.accept(packet.seq):
            return
        self.heard = now
        self.acked(packet.ack, packet.ack_bits)
        if packet.flags & FIN:
            self.log("[<<<] FIN: the game left")
            self.send_control(FIN | ACK)
            self.server.drop(self)
            return
        if packet.flags & SYN:
            self.send_control(SYN | ACK)
        if not self.open and packet.flags & ACK:
            self.open = True
            self.log("[+] connected")
            if self.player.gone or self.server.going_home(self.player):
                self.go_home(now)  # taken out of the match before its game connected (GameServer.take_out)
            else:
                self.queue_reliable(20300, self.player.match.instance_message(self.player))
                self.server.joined(self.player)
        if (
            packet.kind == KIND_FRAME
            and packet.payload
            and not self.player.gone
            and self.leaving_since is None
        ):
            self._payload_in(packet.payload, now)

    def _payload_in(self, payload: bytes, now: float) -> None:
        reader = BitReader(payload)
        try:
            reliable, unreliable = messages.read_channel(reader)
        except (DecodeError, KeyError, EOFError) as error:
            self.log(f"[<<<] undecodable messages: {error} {payload.hex()}", logging.WARNING)
            return
        for seq, msg_id, value in reliable:
            self._reliable_in(seq, msg_id, value, now)
        for msg_id, value in unreliable:
            self._message(msg_id, value, now)
        try:
            commands = read_commands(reader)
        except InputError as error:
            self.log(f"[<<<] undecodable input: {error}", logging.DEBUG)
            return
        if commands and self.player.spawned:
            boundary = self.player.take_commands(commands)
            if boundary is not None:
                self.log(f"[!] command clock boundary at {boundary:08X}; leaving", logging.WARNING)
                self.server.take_out(self.player, "command clock boundary", now=now)

    def _reliable_in(self, seq: int, msg_id: int, value: dict, now: float) -> None:
        if seq < self.expected_in:
            return  # a repeat
        self.parked_in[seq] = (msg_id, value)
        while self.expected_in in self.parked_in:
            msg_id, value = self.parked_in.pop(self.expected_in)
            self.expected_in += 1
            self._message(msg_id, value, now)

    def _message(self, msg_id: int, value: dict, now: float) -> None:
        if msg_id == PING:
            environment = self.server.environment
            self.queue_reliable(
                20306,
                pong(
                    value,
                    environment.wall_time_ns(callsite="client.ping.received_ns"),
                    environment=environment,
                ),
            )
            return
        if msg_id in MAP_LOADED:
            self.player.match.map_loaded(self.player, now)
        if msg_id == GAME_MESSAGE:
            self.player.match.game_message(self.player, value, now)
            return
        shown = str(to_jsonable(value))[:300]
        self.log(f"[<<<] {msg_id} {shown}")


class GameServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3730,
        on_leave=None,
        skin_of=no_skin,
        on_join=None,
        *,
        environment: ExecutionEnvironment | None = None,
        collision_provider_factory=None,
        on_clock_ready=None,
    ) -> None:
        self.environment = SystemEnvironment() if environment is None else environment
        # None preserves the default per-match asynchronous provider. Explicit factories support
        # controlled server replay and isolated scenes without patching global collision state.
        self.collision_provider_factory = collision_provider_factory
        self.host = host
        self.port = port
        self.on_leave = on_leave  # called with each player who leaves a match
        self.on_join = on_join  # called with each player whose game connected to its match
        self.on_clock_ready = on_clock_ready
        self.skin_of = skin_of  # (account, hero) -> the equipped (skin theme, golden weapon); no locks
        self.sock: socket.socket | None = None
        self.lock = threading.RLock()
        # connection id -> (the 20600 data, the waiting player, when to give up on them)
        self.handoffs: dict[int, tuple[Handoff, Player, float]] = {}
        self.clients: dict[int, Client] = {}  # connection id -> client
        self._dropped: dict[tuple, list] = {}  # (address, conn, why) -> [count not logged yet, last line]
        self.matches: list[Match] = []
        self.tick = FIRST_TICK
        self._next_conn = self.environment.randbelow(0x100000, callsite="server.next_conn") + 1
        self._left_players: list[Player] = []  # told to on_leave once the lock is released
        self._joined_players: list[Player] = []  # told to on_join once the lock is released
        self._reporting_players = 0
        self._clock_ticks_left: int | None = None
        self._clock_ready_pending = False
        self._sent_home: weakref.WeakSet[Player] = weakref.WeakSet()  # taken out by the server (take_out)
        self.running = False

    def start(self) -> None:
        """Bind the port and run the server thread. Raises OSError when the port is taken."""
        warm_up()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _ignore_connection_resets(self.sock)
        self.sock.bind((self.host, self.port))
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._run, name="game-server", daemon=True).start()
        log.info("[game] Game server on UDP %s:%d", self.host, self.port)

    def stop(self) -> None:
        self.running = False

    # --- matches -------------------------------------------------------------------------------

    def create_match(self, game_map, players, card: int = 0) -> list[Handoff] | None:
        """A match for these players: [(account_lo, name, hero GUID, team, tournament[, card])], card
        being the player's card as the lobby shows it (content.player.record). `card` is the queue card
        the match is for (0: none). Returns each 20600 in order, or None during clock rollover."""
        with self.lock:
            if self._clock_ticks_left is not None:
                return None
            match = Match(
                game_map,
                on_leave=self._left,
                skin_of=self.skin_of,
                card=card,
                environment=self.environment,
                collision_provider=(
                    None if self.collision_provider_factory is None else self.collision_provider_factory()
                ),
            )
            handoffs = [self._add_player(match, entry) for entry in players]
            self.matches.append(match)
        names = ", ".join(player.describe() for player in match.players)
        log.info("[game] %s on %s (%s): %s", match.label(), game_map.name, game_map.mode_name, names)
        return handoffs

    def open_matches(self, card: int) -> list[OpenMatch]:
        """The queue card's matches that still have free places, oldest first."""
        with self.lock:
            if self._clock_ticks_left is not None:
                return []
            rooms = []
            for match in self.matches:
                if not card or match.card != card or match.ended:
                    continue
                free = match.free_places()
                if any(places > 0 for places in free):
                    rooms.append(
                        OpenMatch(match, match.waiting_for_players, free, match.roles_taken(), match.started)
                    )
            return rooms

    def join_match(self, match: Match, players) -> list[Handoff] | None:
        """More players for a match that is on, as create_match takes them; None once it has ended."""
        with self.lock:
            if self._clock_ticks_left is not None or match.ended or match not in self.matches:
                return None
            handoffs = [self._add_player(match, entry) for entry in players]
            names = ", ".join(player.describe() for player in match.players[-len(players) :])
        log.info("[game] %s: %s join", match.label(), names)
        return handoffs

    def lineup(self, match_id: tuple[int, int]) -> list[tuple[int, int, int, dict]]:
        """(account_lo, team, role, card) of each player still in the match: the lobby's roster for its
        VERSUS loading screen (lobby/lineup.py)."""
        with self.lock:
            for match in self.matches:
                if match.id == match_id:
                    return [(p.account_lo, p.team, p.role, dict(p.card)) for p in match.players if not p.gone]
        return []

    def _add_player(self, match: Match, entry) -> Handoff:
        account_lo, name, hero, team, tournament, *card = entry
        player = match.add_player(account_lo, name, hero, team, tournament, card[0] if card else None)
        conn = self._next_conn
        self._next_conn = (self._next_conn % 0x0FFFFFFF) + 1
        handoff = Handoff(
            conn,
            self.environment.token_bytes(32, callsite="server.handoff.key_in"),
            self.environment.token_bytes(32, callsite="server.handoff.key_out"),
            match.id,
        )
        deadline = self.environment.wall_time(callsite="server.handoff.deadline") + HANDOFF_SECONDS
        self.handoffs[conn] = (handoff, player, deadline)
        return handoff

    def expire_handoff(self, conn: int) -> bool:
        """Reject a pending handoff immediately and release its seat on the next tick."""
        with self.lock:
            waiting = self.handoffs.get(conn)
            if waiting is None:
                return False
            handoff, player, _ = waiting
            self.handoffs[conn] = (handoff, player, 0.0)
            return True

    def handoff_pending(self, conn: int) -> bool:
        """Whether a reserved ticket can still authenticate, including during clock rollover."""
        with self.lock:
            waiting = self.handoffs.get(conn)
            if waiting is None:
                return False
            return self.environment.wall_time(callsite="server.handoff.pending") < waiting[2]

    def switch_hero(self, account_lo: int, hero) -> bool:
        with self.lock:
            if self._clock_ticks_left is not None:
                return False
            for match in self.matches:
                for player in match.players:
                    if player.account_lo == account_lo and player.client is not None:
                        return match.switch_hero(player, hero)
        return False

    def skin_changed(self, account_lo: int, hero_guid: int) -> bool:
        """The lobby equipped an item of a hero (24500): a player in a match who plays that hero gets the
        new skin (Match.skin_changed). Hero select equips skins through the lobby, even in a match, and
        picks nothing again."""
        with self.lock:
            if self._clock_ticks_left is not None:
                return False
            for match in self.matches:
                for player in match.players:
                    if player.account_lo == account_lo and player.client is not None and not player.gone:
                        return match.skin_changed(player, hero_guid)
        return False

    def send_home(self, account_lo: int | None = None) -> int:
        """Send a player's game (every game for None) back to the menu: 20304 with 0 = leave for good
        (0x7FF7896E6830). Returns how many games were told."""
        count = 0
        with self.lock:
            for client in list(self.clients.values()):
                if account_lo is None or client.player.account_lo == account_lo:
                    self._sent_home.add(client.player)
                    client.go_home(self.environment.wall_time(callsite="server.send_home"))
                    count += 1
        return count

    def player_of(self, account_lo: int) -> Player | None:
        """The account's player in a match it has not left, whether its game connected or not."""
        with self.lock:
            for match in self.matches:
                for player in match.players:
                    if player.account_lo == account_lo and not player.gone:
                        return player
        return None

    def elsewhere(self, account_lo: int, match_id: tuple[int, int]) -> Player | None:
        """The account's player in a match other than this one that it has not left: for example the
        Practice Range it plays while its queue match pops (lobby/matchmaker.py)."""
        with self.lock:
            for match in self.matches:
                for player in match.players:
                    if match.id != match_id and player.account_lo == account_lo and not player.gone:
                        return player
        return None

    def take_out(self, player: Player, reason: str, *, now: float | None = None) -> str:
        """Take a player out of its match from the server side: a connected game goes back to the menu
        (20304) and leaves the match when its link closes; a game that has not connected leaves the
        match now, and gets 20304 if it still connects. Returns what happened, for the log."""
        with self.lock:
            if player.gone:
                return "had left already"
            self._sent_home.add(player)
            client = player.client
            if client is not None and not client.closed:
                if now is None:
                    now = self.environment.wall_time(callsite="server.take_out")
                client.go_home(now)
                return "sent to the menu"
            player.match.leave(player, reason)
            return "left before its game connected"

    def going_home(self, player: Player) -> bool:
        """Whether the server took the player out of its match (take_out, send_home)."""
        with self.lock:
            return player in self._sent_home

    def joined(self, player: Player) -> None:
        self._joined_players.append(player)

    def in_match(self, account_lo: int) -> bool:
        with self.lock:
            return any(
                player.account_lo == account_lo and not player.gone
                for match in self.matches
                for player in match.players
            )

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [match.snapshot() for match in self.matches]

    def _left(self, player: Player) -> None:
        self._left_players.append(player)

    def _report_leaves(self) -> None:
        """Tell the lobby about games that connected and players who left, outside our lock: the lobby
        calls us under its own."""
        with self.lock:
            joined, self._joined_players = self._joined_players, []
            left, self._left_players = self._left_players, []
            reporting = bool(joined or left)
            if reporting:
                self._reporting_players += 1
            ready, self._clock_ready_pending = self._clock_ready_pending, False
        try:
            for callback, players in ((self.on_join, joined), (self.on_leave, left)):
                for player in players:
                    if callback is not None:
                        try:
                            callback(player)
                        except Exception:
                            log.exception("[game] the lobby's join or leave handler failed")
        finally:
            if reporting:
                with self.lock:
                    self._reporting_players -= 1
        if ready and self.on_clock_ready is not None:
            try:
                self.on_clock_ready()
            except Exception:
                log.exception("[game] the lobby's clock-ready handler failed")

    # --- the loop ------------------------------------------------------------------------------

    def _run(self, *, on_iteration_error=None) -> None:
        next_tick = self.environment.monotonic(callsite="server.run.start")
        while self.running:
            timeout = max(0.0, next_tick - self.environment.monotonic(callsite="server.run.timeout"))
            self.sock.settimeout(timeout or 0.0001)
            try:
                data, address = self.sock.recvfrom(RECEIVE_BYTES)
            except TimeoutError:
                data = None
            except OSError as error:
                log.warning("[game] receive failed: %s", error)
                data = None
            try:
                with self.lock:
                    if data is not None:
                        self._datagram(
                            data, address, self.environment.wall_time(callsite="server.run.datagram")
                        )
                    if self.environment.monotonic(callsite="server.run.due") >= next_tick:
                        next_tick, missed = next_tick_after(
                            next_tick, self.environment.monotonic(callsite="server.run.next_tick")
                        )
                        self._tick(self.environment.wall_time(callsite="server.run.tick"), missed=missed)
            except Exception as error:
                if on_iteration_error is not None:
                    on_iteration_error(error)
                log.exception("[game] tick failed")
            self._report_leaves()
        self.sock.close()

    def _datagram(self, data: bytes, address, now: float) -> None:
        conn = peek_connection(data)
        if conn is None:
            self._drop_datagram(address, None, "not a game link datagram", now)
            return
        client = self.clients.get(conn)
        if client is not None:
            packet = client.cipher.open(data)
            if packet is None:
                self._drop_datagram(address, conn, "does not open with that connection's keys", now)
                return
            if address != client.address:
                if not client.window.is_new(packet.seq):
                    self._drop_datagram(address, conn, "an old datagram from another address", now)
                    return
                client.log(f"[+] the game moved to {address[0]}:{address[1]}")
                client.address = address
            client.received(packet, now)
            return
        waiting = self.handoffs.get(conn)
        if waiting is None:
            self._drop_datagram(address, conn, "unknown connection", now)
            return
        handoff, player, deadline = waiting
        if now >= deadline:
            self._drop_datagram(address, conn, "expired handoff", now)
            return
        cipher = LinkCipher(handoff.key_in, handoff.key_out)
        packet = cipher.open(data)
        if packet is None or not packet.flags & SYN:
            self._drop_datagram(address, conn, "not a SYN with the keys of its 20600", now)
            return
        del self.handoffs[conn]
        client = Client(self, address, conn, cipher, player)
        if not player.gone:  # one taken out before it connected only gets its 20304
            player.client = client
        self.clients[conn] = client
        client.log(f"[+] SYN for {player.match.label()}, connection {conn}{self._others_at(address, client)}")
        if self._clock_ticks_left is not None:
            client.go_home(now)
        client.received(packet, now)

    def _others_at(self, address, client=None) -> str:
        """The other connections on this address, for the log: a game's links all share one socket."""
        at = [
            f"{other.conn} ({other.player.match.label()})"
            for other in self.clients.values()
            if other.address == address and other is not client
        ]
        return f"; this address also has connection {', '.join(at)}" if at else ""

    def _drop_datagram(self, address, conn, why: str, now: float) -> None:
        """Log a datagram we drop: the first at once, then one line with the count per address, connection
        and reason every DROPPED_LOG_SECONDS (_tick writes the last count)."""
        key = (address, conn, why)
        seen = self._dropped.get(key)
        if seen is not None:
            seen[0] += 1
            if now - seen[1] < DROPPED_LOG_SECONDS:
                return
        self._log_dropped(key, 1 if seen is None else seen[0], now)

    def _log_dropped(self, key, count: int, now: float) -> None:
        address, conn, why = key
        log.info(
            "[game %s:%d] [!] dropped %d datagram(s) of connection %s: %s%s",
            address[0],
            address[1],
            count,
            conn,
            why,
            self._others_at(address),
        )
        self._dropped[key] = [0, now]

    def _expire_handoffs(self, now: float, *, force: bool = False) -> None:
        for conn, (_, player, deadline) in list(self.handoffs.items()):
            if force or now >= deadline:
                del self.handoffs[conn]
                player.match.leave(player, "game clock rollover" if force else "did not connect")

    def _drain_clock(self, now: float) -> None:
        if self._clock_ticks_left is None:
            self._clock_ticks_left = min(CLOCK_DRAIN_TICKS, max(0, MAX_LEASE_FRAME - self.tick))
            for _, player, _ in self.handoffs.values():
                self._sent_home.add(player)
            for client in self.clients.values():
                self._sent_home.add(client.player)
                client.drain_clock(now)
        force = self._clock_ticks_left == 0
        if not force and (self.clients or self.handoffs):
            self._clock_ticks_left -= 1
            self.tick += 1
        self._expire_handoffs(now, force=force)
        for client in list(self.clients.values()):
            if force or now - client.leaving_since >= LEAVE_GRACE_SECONDS:
                self.drop(client)
            elif client.open and sends(self.tick):
                client.send_frame(self.tick, now, reliable_only=True)
        if self.clients or self.handoffs:
            return
        for match in self.matches:
            for player in match.players:
                match.leave(player, "game clock rollover")
        self.matches.clear()
        if not (self._joined_players or self._left_players or self._reporting_players):
            self.tick = FIRST_TICK
            self._clock_ticks_left = None
            self._clock_ready_pending = True
            log.info("[game] renewed the server frame clock")

    def _tick(self, now: float, *, missed: int = 0) -> None:
        candidate = self.tick + missed + 1
        if self._clock_ticks_left is not None or candidate > MAX_SIMULATION_TICK:
            self._drain_clock(now)
            return
        # Emit the current state once when a normal send boundary was crossed,
        # even when the scheduler skipped the boundary tick itself.
        frames = candidate // SEND_EVERY > self.tick // SEND_EVERY
        self.tick = candidate
        self._expire_handoffs(now)
        for match in list(self.matches):
            match.update(now, self.tick, frames=frames)
        for client in list(self.clients.values()):
            if now - client.heard > SILENCE_SECONDS:
                client.log("[!] no datagrams for 10 s, dropping the game")
                self.drop(client)
            elif client.leaving_since is not None and now - client.leaving_since >= LEAVE_GRACE_SECONDS:
                client.log("[!] still here after 20304, dropping the game")
                self.drop(client)
            elif client.open and frames:
                client.send_frame(self.tick, now)
        self.matches = [match for match in self.matches if not match.ended]
        for key, (count, last) in list(self._dropped.items()):
            if now - last >= DROPPED_LOG_SECONDS:
                if count:
                    self._log_dropped(key, count, now)
                else:
                    del self._dropped[key]

    def drop(self, client: Client) -> None:
        if client.closed:
            return
        client.closed = True
        client._combat_notices.clear()
        client._body_health.clear()
        client._combat_notice_tail.clear()
        client._bot_scripts.clear()
        client._body_destroy_wait.clear()
        client._player_body_reuse_after.clear()
        client._pending_possessions.clear()
        client._confirmed_possessions.clear()
        client._pending_mode_frame = None
        client._display_notices.clear()
        if self.clients.get(client.conn) is client:
            del self.clients[client.conn]
        movelog.close(client.player)
        client.player.match.leave(client.player)
