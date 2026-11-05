"""Predicted projectiles: what the server sends so that the owner's own Helix Rocket and Biotic Field canister
live, explode and leave their field (addresses are the PC client's, build 104319).

The lease:
- The owner's client shoots a projectile volley itself only under a lease on its body: component 90
  (MECPredictor, accessor 0x7FF789AE5B90: word 1 bit 26), field m_leaseBlocks (type 0x7FF78BFE9200, +120,
  owner-only flag 0x10), an array of LeaseBlock {u64 definition, u32 start, u32 stop, u32 startEntityID,
  u8 perFrame} (type 0x7FF78BFE9140). The allocator (0x7FF789AE59D0) takes the first block of the
  projectile's definition with start <= f < stop for command frame f and gives the id 0xA0000000 |
  startEntityID + (f - start) * perFrame (one more per further projectile of that frame). No block, no
  projectile. On the wire the array is an aligned u32 count and 21 bytes per block.
- Each body gets its own id range per definition (slot, definition), so no two bodies ever share an id, and
  ids move on with the frame, so a new body never repeats a recent one (the client ignores a destroyed id
  for 30 s).

Confirmation (0x7FF78969D4CD):
- The predicted projectile stays only if the server confirms it: a create record of the same id sets its
  component 91 +28 = 1 and changes nothing else (a create for an id the client does not have yet makes an
  unflown entity at the record's place, and the client's own shot of that id is then refused). So the
  creates go out only once the client has acknowledged the owner frame that reported the volley (the
  client shoots the rocket at the frame of that owner frame, K, or at the press, f): lease(K) and
  lease(f).

The release (reader chain: ch2 0x7FF789A4A490 -> state 0x7FF789A34600 -> record 0x7FF789C78980):
- The client holds a networked rocket's explosion back for the server (072C, 1C86:
  m_delayCollisionEffectsBasedOnLatency). A ch2 projectile state for a frame ahead of the rocket's, alive,
  with an impact count, in the same entity block as the destroy, makes the destructor play it at the
  client's own impact point. So the server destroys a rocket a little after it would hit (combat.py flies
  it) with that state.
"""

import logging
import math
from dataclasses import dataclass, field

from ow174.game import world
from ow174.game.bits import BitWriter
from ow174.game.script import graph as graphs_data
from ow174.game.statescript import InstanceWire, StateWire, Stream, remote_full_frame
from ow174.game.world import OP_CREATE, OP_DESTROY, WITH_MOVEMENT, WITH_SCALE, WITH_TRANSFORM, EntityUpdate

log = logging.getLogger("ow174.game")

NO_BASELINE = 0xFFFFFFFF  # the state's baseline frame: none, a blank state (0x7FF789A348DD)
RELEASE_AHEAD = 4096  # frames: the release's frame, far past the rocket's own
RELEASE_POS = (0.0, -1000.0, 0.0)  # where a count above the client's would play one more explosion


def _count(out: BitWriter, value: int) -> None:
    """1 + 4 bits, 01 + 8 bits, 00 + 32 bits."""
    if value < 16:
        out.bit(1)
        out.bits(value, 4)
    elif value < 256:
        out.bit(0)
        out.bit(1)
        out.bits(value, 8)
    else:
        out.bit(0)
        out.bit(0)
        out.bits(value & 0xFFFFFFFF, 32)


def state_record(out: BitWriter, position=RELEASE_POS, count: int = 1) -> None:
    """A projectile state record against a blank baseline (0x7FF789C78980): alive (+4 = 0), no +260, the
    position (+16), then every optional part the blank baseline's, but the impact count (+256)."""
    out.bit(0)  # +4: alive
    out.bit(0)  # no +260
    for value in position:
        out.f32(value)
    out.bit(0)  # velocity: the baseline's
    out.bit(0)  # +88
    out.bit(0)  # list +96
    out.bit(0)  # list +176
    out.bit(0)  # list +208
    out.bit(1)  # the impact count
    _count(out, count)
    out.bit(0)  # hits +264
    out.bit(0)  # S
    out.bit(0)  # +368 / +372
    out.bit(0)  # +432
    out.bit(0)  # +436
    out.bit(0)  # no tail


def release(frame: int, count: int = 1) -> BitWriter:
    """ch2 after its channel bit: no shot records, one projectile state for `frame` from no baseline."""
    out = BitWriter()
    out.bit(0)  # no events
    out.bit(1)  # a projectile state
    out.var_b(NO_BASELINE)
    out.var_b((frame + 1) & 0xFFFFFFFF)  # the frame is baseline + this (u32)
    state_record(out, RELEASE_POS, count)
    return out


# --- the field entity's create (the Biotic Field) ---------------------------------------------------------

KEEP_ROTATION = 0x01000000  # movement state +0 bit 24: the block's own rotation, not the parent's


def parented_movement(out: BitWriter, parent: int, flags: int = KEEP_ROTATION) -> None:
    """A create's movement-state block that puts the entity at its parent (world.write_movement's layout
    for a state at the origin with no motion, frame CREATE_BASE - 1, then the parent part: a bit, the
    parent's id +0x360 and the mode +0x364, 0 = the parent's scene-node matrix; reader
    0x7FF789B6F680)."""
    out.bit(1)  # +0: four optional bytes, low first
    for k in range(4):
        byte = (flags >> (8 * k)) & 0xFF
        out.bit(1 if byte else 0)
        if byte:
            out.bits(byte, 8)
    out.bit(0)  # +4
    out.bit(1)  # +8: one frame back from the create's base
    out.bit(1)
    out.bits(1, 4)
    out.bit(0)  # +12
    out.bit(0)  # pitch
    out.bits(0, 4)  # +16, +20, +24, +28
    out.bit(0)  # +36: the state's own frame
    out.bit(0)  # +40
    out.bits(0, 2)  # throttles
    out.bits(0, 3)  # Euler angles
    for _ in range(3):  # the position, (0, 0, 0) from the parent
        out.bit(1)
        out.bits(0, 2)
        out.bit(1)
        out.bits(0, 24)
    out.bit(0)  # no velocity
    out.bits(0, 9)  # nine optional parts
    out.bit(1)  # a parent
    out.bits(parent & 0xFFFFFFFF, 32)
    out.bits(0, 32)
    out.bits(0, 3)  # +52, +60, +56
    out.bit(0)  # +384
    out.bit(0)  # +832


# --- the server's part ------------------------------------------------------------------------------------

PREDICTED = 1  # a volley's sync after LET_GAME_DECIDE: SHOTS_AND_PROJECTILES, the lease's kind
LEASE_FIRST = 0x00100000  # the first lease id; the server's own ids stay below
LEASE_SPAN = 1 << 22  # frames (= ids) of one window: 18.6 hours
DEFINITIONS_PER_BODY = 4  # leased definitions per slot (Soldier: 072C, 1C86, 072D)
REPORT_WINDOW = 64  # frames after the shot in which an owner frame may report it
CONFIRM_TICKS = 3  # at least this after the report (0.05 s)
UNREPORTED_TICKS = 32  # a shot no owner frame reported gets lease(f) after this
RELEASE_MARGIN = (
    6  # frames after the server's rocket ended before the release (unverified: the client's flies the same)
)
CREATE_AT = (0.0, -1000.0, 0.0)  # an id the client did not shoot becomes an unflown entity here
FIELD_ENTITY = 0x04000000000003DC  # 03DC.003, the Biotic Field (0257 st1's m_projectileSpawn)
FIELD_GRAPH = 0x0258  # its initial graph
FIELD_FIRST, FIELD_SPAN = 0xA0080000, 0x00040000  # the field entities' ids
SEEN_FIRST, SEEN_SPAN = 0xA00C0000, 0x00040000  # the server projectiles the other clients see
FIELD_DELAY = 12  # ticks after the canister's create (0.2 s; it lands in 1-2 frames)
FIELD_SECONDS = 5.0  # 0258: #567 = 5, st1 Wait #567 - 0.5 then st4 Wait 0.5, then DestroyEntity (DATA)
FADE_SECONDS = 4.5  # 0258 st1 ends: st5 (its DataFlowMapping) goes off
HEAL_PER_SECOND = 35.0  # 0258 #420 = 35 a player, ApplyAura 025F.053's 002A (DATA; per second: unverified)
HEAL_RADIUS = 5.0  # 0258 #422 = 5: st0 TrackTargets m_radius (DATA)
HEAL_UP = 1.0  # TrackTargets' offset (0, 1, 0) from the field (DATA; unverified: the sphere's centre)
FIELD_STATES = {
    2: ("STU_691BFA55", {"counter": 1}),
    3: ("STUStatescriptStateCosmeticEntity", {}),
    5: ("STUStatescriptStateDataFlowMapping", {}),
}
# 0258's remote states on: st2 Effect 0B06 (the ring), st3 CosmeticEntity 068A (the canister on the ground),
# st5 DataFlowMapping 011E (until 4.5 s); st8 SendGameMessage and st9 Effect (under a switch) stay off.


def _sync(state) -> int:
    """A volley's resolved sync (as the PS4 build resolves it): LET_GAME_DECIDE (0) is NONE
    (3) for a volley of 2 or more shots at 5.5 a second or more, SHOTS_ONLY (2) without a lifetime or with
    more than 7 projectiles a shot, else SHOTS_AND_PROJECTILES (1)."""
    node = state.node

    def number(name, default=0.0):
        cfg = node.field(name)
        value = state.evaluate(cfg, watch=False) if cfg is not None else None
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    sync = int(number("m_5E4A4E85"))
    if sync:
        return sync
    if number("m_A17BE89B", 1) >= 2 and number("m_numShotsPerSecond") >= 5.5:
        return 3
    if number("m_projectileLifetime") < 1.19e-5 or number("m_numProjectilesPerShot", 1) > 7:
        return 2
    return 1


def _defs(cfg) -> list[int]:
    """Every 003 GUID a projectile entity field names (both sides of a condition)."""
    out = []
    if isinstance(cfg, dict):
        if cfg.get("$") == "STU_8556841E" and isinstance(cfg.get("m_entityDef"), str):
            out.append(int(cfg["m_entityDef"], 16))
        for value in cfg.values():
            out += _defs(value)
    elif isinstance(cfg, list):
        for value in cfg:
            out += _defs(value)
    return out


def definitions(script) -> list[int]:
    """The projectile definitions the body's client predicts under a lease: every definition a predicted
    (m_localPrediction) SHOTS_AND_PROJECTILES volley of the body's graphs names, in graph order."""
    found: list[int] = []
    for instance in script.component.instances.values():
        for node in instance.graph.states:
            if node is None or node.cls != "STUStatescriptStateWeaponVolley" or not node.field("m_45271E8C"):
                continue
            if _sync(instance.state(node.state)) != PREDICTED:
                continue
            for guid in _defs(node.field("m_projectileEntity")):
                if guid not in found:
                    found.append(guid)
    return found


@dataclass
class Shot:
    """One projectile the owner's client shoots itself."""

    player: object
    body: int
    definition: int
    frame: int  # f: the command frame the VM fired it in
    instance: int
    state: int
    spawn: int | None = None  # the entity it leaves where it lands (03DC.003), else None
    where: tuple = (0.0, 0.0, 0.0)  # the shooter's feet when it fired
    bound: int = 0  # frames the projectile lives at most (lifetime, range / speed)
    seen_reports: int = 0  # the number of the last owner frame looked at
    reports: list = field(default_factory=list)  # (K, chunk last) of the owner frames that carried it
    created: list = field(default_factory=list)  # the ids confirmed
    created_tick: int | None = None
    release_at: int | None = None  # the tick of the release and destroy
    field_ids: list = field(default_factory=list)  # (viewer slot, field entity id)
    seen_ids: list = field(default_factory=list)  # (viewer slot, server projectile id) on the other clients
    field_tick: int | None = None
    faded: bool = False
    arrivals: list = field(default_factory=list)  # the field entities whose create arrived


class Projectiles:
    """The match's predicted projectiles: leases, confirmations, releases and the Biotic Field."""

    def __init__(self, combat) -> None:
        self.combat = combat
        self.match = combat.match
        self.shots: list[Shot] = []
        self.next_field = 0
        self.next_seen = 0

    # --- the lease ------------------------------------------------------------------------------------

    def _base(self, player, definition: int) -> int | None:
        script = player.body_script
        if script is None:
            return None
        defs = definitions(script)[:DEFINITIONS_PER_BODY]
        if definition not in defs:
            return None
        return LEASE_FIRST + (player.slot * DEFINITIONS_PER_BODY + defs.index(definition)) * LEASE_SPAN

    def lease(self, player) -> list | None:
        """The body's lease blocks (component 90): two windows from the current frame for each predicted
        definition, with the slot's own ids."""
        script = player.body_script
        if script is None:
            return None
        frame = player.commands.last_frame or self.match.tick
        start = frame - frame % LEASE_SPAN
        blocks = []
        for guid in definitions(script)[:DEFINITIONS_PER_BODY]:
            base = self._base(player, guid)
            for window in (start, start + LEASE_SPAN):
                blocks.append((guid, window, window + LEASE_SPAN, base, 1))
        return blocks or None

    def lease_id(self, player, definition: int, frame: int) -> int | None:
        """The id the client's allocator gives the first projectile of `definition` in `frame`."""
        base = self._base(player, definition)
        return None if base is None else 0xA0000000 | (base + frame % LEASE_SPAN)

    # --- shots ----------------------------------------------------------------------------------------

    def fired(
        self, player, state, frame: int, definition: int, bound_seconds: float, origin=None, direction=None
    ) -> Shot | None:
        """A predicted projectile the VM fired in command frame `frame`; returns its Shot (combat.py tells it
        when the server's projectile ended), None when it is not leased. A rocket also goes to the clients
        that have the shooter's body: a server projectile and its shot record (unverified: they start it)."""
        if _sync(state) != PREDICTED or self.lease_id(player, definition, frame) is None:
            return None
        spawned = _defs(state.node.field("m_49CBA3BC"))
        shot = Shot(
            player,
            player.body,
            definition,
            frame,
            state.instance.id,
            state.index,
            spawn=spawned[0] if spawned else None,
            where=tuple(player.mover.position),
            bound=math.ceil(bound_seconds * 1000 / FRAME_MS),
            seen_reports=player.body_script.report_count,
        )
        self.shots.append(shot)
        if shot.spawn is None and origin is not None and direction is not None:
            self._show_others(shot, state, origin, direction)
        return shot

    def _show_others(self, shot: Shot, state, origin, direction) -> None:
        """Every other client with the shooter's body: a server entity of the projectile's definition (under
        the map until a record starts it) and the shot record in the shooter's ch2 naming it."""
        observers = self.combat.observers
        for viewer in self.match.players:
            if viewer is shot.player or viewer.client is None or not viewer.seen.get(shot.body):
                continue
            if not observers.has(viewer, shot.body):
                continue  # its client has no statescript of the body, so no volley record of it
            entity = SEEN_FIRST + self.next_seen % SEEN_SPAN
            self.next_seen += 1
            shot.seen_ids.append((viewer.slot, entity))
            record = ShotRecord(
                shot.frame,
                shot.instance,
                shot.state,
                state.counter,
                tuple(origin),
                tuple(direction),
                (entity,),
                volleys=state.volleys & 0xFF,
            )
            events = EntityUpdate(shot.body)
            events.ch2 = shot_events([record])
            viewer.client.queue_entities(
                [EntityUpdate(entity, OP_CREATE, confirm_record(shot.definition)), events]
            )

    def flown(self, shot: Shot, seconds: float) -> None:
        """The server's rocket ended after `seconds`: the release goes a little after the client's would."""
        if shot.spawn is not None or shot.release_at is not None:
            return
        start = max([shot.frame] + [k for k, _ in shot.reports[:1]])
        shot.release_at = start + math.ceil(seconds * 1000 / FRAME_MS) + RELEASE_MARGIN

    def update(self, tick: int) -> None:
        for shot in list(self.shots):
            try:
                self._step(shot, tick)
            except Exception:
                log.exception("[game] %s: projectile %X failed", self.match.label(), shot.definition & 0xFFFF)
                if shot in self.shots:
                    self.shots.remove(shot)

    def _step(self, shot: Shot, tick: int) -> None:
        player = shot.player
        script = player.body_script
        alive = player.client is not None and player.has_body and player.body == shot.body
        if not alive or script is None:
            self._end(shot, tick, explode=alive)
            return
        for number, cmfd, last, keys in script.reports:
            if number <= shot.seen_reports:
                continue
            if (
                "state",
                shot.instance,
                shot.state,
            ) in keys and shot.frame <= cmfd <= shot.frame + REPORT_WINDOW:
                shot.reports.append((cmfd, last))
        shot.seen_reports = script.report_count
        self._confirm(shot, script, tick)
        if shot.spawn is not None:
            self._field(shot, tick)
            return
        due = shot.release_at
        if due is None and shot.created_tick is not None:
            due = shot.created_tick + shot.bound + RELEASE_MARGIN  # no flight from the server: the bound
        if due is not None and tick >= due and shot.created:
            self._end(shot, tick, explode=True)

    def _confirm(self, shot: Shot, script, tick: int) -> None:
        acked = script.stream.base_last
        if shot.reports:
            ready = [k for k, last in shot.reports if last <= acked]
            if not ready:
                return
            frames = [shot.frame, *ready]
        elif tick >= shot.frame + UNREPORTED_TICKS and not shot.created:
            frames = [shot.frame]
        else:
            return
        new = []
        for frame in frames:
            entity = self.lease_id(shot.player, shot.definition, frame)
            if entity is not None and entity not in shot.created and entity not in new:
                new.append(entity)
        if not new:
            return
        shot.created += new
        if shot.created_tick is None:
            shot.created_tick = tick
        build = confirm_record(shot.definition)
        shot.player.client.queue_entities([EntityUpdate(entity, OP_CREATE, build) for entity in new])
        log.info(
            "[game] %s: %s's projectile %04X (frame %d, reported at %s) confirmed as %s",
            self.match.label(),
            shot.player.name,
            shot.definition & 0xFFFF,
            shot.frame,
            [k for k, _ in shot.reports],
            " ".join(f"{entity:08X}" for entity in new),
        )

    def _end(self, shot: Shot, tick: int, explode: bool) -> None:
        """Destroy the projectile's ids on its owner's client (with the release when it holds an
        explosion) and its field entities on every client."""
        if shot in self.shots:
            self.shots.remove(shot)
        for slot, entity in shot.field_ids + shot.seen_ids:
            viewer = next((player for player in self.match.players if player.slot == slot), None)
            if viewer is not None and viewer.client is not None:
                update = EntityUpdate(entity, OP_DESTROY)
                if explode and (slot, entity) in shot.seen_ids:
                    update.ch2 = release(tick + RELEASE_AHEAD)
                viewer.client.queue_entities([update])
        client = shot.player.client
        if client is None or not shot.created:
            return
        updates = []
        for entity in shot.created:
            update = EntityUpdate(entity, OP_DESTROY)
            if explode and shot.spawn is None:
                update.ch2 = release(tick + RELEASE_AHEAD)
            updates.append(update)
        client.queue_entities(updates)

    # --- the Biotic Field -----------------------------------------------------------------------------

    def _field(self, shot: Shot, tick: int) -> None:
        if shot.created_tick is None:
            return
        if shot.field_tick is None:
            if tick >= shot.created_tick + FIELD_DELAY:
                shot.field_tick = tick
                self._open_field(shot)
            return
        seconds = (tick - shot.field_tick) * FRAME_MS / 1000
        if seconds >= FIELD_SECONDS:
            self._end(shot, tick, explode=False)
            return
        if not shot.faded and seconds >= FADE_SECONDS:
            shot.faded = True
            for arrival in shot.arrivals:
                arrival.send()
        self.combat.heal_area(shot.player, field_centre(shot), HEAL_RADIUS, HEAL_PER_SECOND * FRAME_MS / 1000)

    def _open_field(self, shot: Shot) -> None:
        """The field entity on every client: on the owner's parented to each canister id (its client puts it
        on its own canister), on the others at the shooter's feet (unverified: the canister falls there)."""
        for viewer in self.match.players:
            if viewer.client is None or not viewer.spawned:
                continue
            parents = shot.created if viewer is shot.player else [None]
            for parent in parents:
                entity = FIELD_FIRST + self.next_field % FIELD_SPAN
                self.next_field += 1
                shot.field_ids.append((viewer.slot, entity))
                create = EntityUpdate(entity, OP_CREATE, field_create(parent, shot.where))
                create.stream = FieldArrived(shot, viewer, Stream(entity))
                viewer.client.queue_entities([create])
        log.info("[game] %s: %s's Biotic Field opened", self.match.label(), shot.player.name)


FRAME_MS = 16


def field_centre(shot: Shot) -> tuple:
    x, y, z = shot.where
    return (x, y + HEAL_UP, z)


class FieldArrived:
    """The field entity's create arrived: its graph's frame goes, and again when it fades."""

    def __init__(self, shot: Shot, viewer, stream: Stream) -> None:
        self.shot, self.viewer, self.stream = shot, viewer, stream
        self.done = False

    def arrived(self, last: int) -> None:
        if self.done:
            return
        self.done = True
        self.shot.arrivals.append(self)
        self.send()

    def send(self) -> None:
        if self.viewer.client is not None:
            self.viewer.client.queue_entities([self.stream.full_frame(field_frame(self.shot.faded))])


def confirm_record(definition: int):
    """A plain create of the projectile's definition: for an id the client shot it only confirms it."""

    def build(origin: int):
        return world.create(origin, definition, {}, flags=WITH_TRANSFORM | WITH_SCALE, position=CREATE_AT)

    return build


def field_create(parent: int | None, where: tuple):
    """03DC.003's create: with the movement block that names its parent (the owner's canister), else placed
    at `where`."""

    def build(origin: int):
        if parent is None:
            return world.create(origin, FIELD_ENTITY, {}, flags=WITH_TRANSFORM | WITH_SCALE, position=where)
        out = world.RecordWriter(origin)
        out.bit(0)
        out.guid_index(FIELD_ENTITY)
        out.bits(WITH_TRANSFORM | WITH_SCALE | WITH_MOVEMENT, 8)
        for value in (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0):  # position, rotation, scale
            out.f32(value)
        parented_movement(out, parent)
        out.bits(0, 8)  # no components
        return out

    return build


def field_frame(faded: bool = False) -> BitWriter:
    """0258's H = 0 full frame as instance 1 (the client builds it itself from 03DC's initial graphs and the
    frame adopts it): the ring, the canister on the ground and, until it fades, st5."""
    field_graph = graphs_data.graph(FIELD_GRAPH)
    item = InstanceWire(1, field_graph)
    for index, (cls, payload) in FIELD_STATES.items():
        if not (faded and index == 5):
            item.states[index] = StateWire(cls, True, dict(payload))
    return remote_full_frame([item], {}, {1: field_graph})


# --- the shot record: how the other clients see a predicted projectile -------------------------------------
#
# A SHOTS_AND_PROJECTILES volley's projectile on the other clients is a server entity that a shot record
# starts (the PC reader 0x7FF789C91F80): the record goes in ch2 of the shooter's entity block (events bit,
# then per event a bit, the frame (the first a var_b, then steps), the record; a 0 bit; then no projectile
# state). The record: the shot counter, a spread bit, form 2 (bit 0): instance w16, state w16, the activation
# counter (6 bits), the volley counter (a 1 bit for 1), a w16; the origin (3 raw f32, world), yaw16 and
# pitch16 (value ^ 0x8000, pitch up, 0x7FF789CC1360), and the projectile ids (bit, w16 count, the first id,
# then steps). The client starts the projectile only while it holds the shooter's volley record of that key:
# entity, instance id, state index and volley counter (key 0x7FF789C95180; the volley's begin 0x7FF789C71950
# takes the instance's id +0x18 and the state's +0x70, the m_states index the state constructor 0x7FF78AAA1460
# stores). Unverified: the other client holds that record from the volley state the H = 0 frames begin.

YAW16 = 32768 / math.pi  # 10430.378: radians to the record's units
PITCH16 = 65536 / math.pi  # 20860.756


def _w16(out: BitWriter, value: int) -> None:
    """0x7FF78A76B9E0: 0 + 4 bits, 10 + 8, 11 + 16."""
    value &= 0xFFFF
    if value < 16:
        out.bit(0)
        out.bits(value, 4)
    elif value < 256:
        out.bit(1)
        out.bit(0)
        out.bits(value, 8)
    else:
        out.bit(1)
        out.bit(1)
        out.bits(value, 16)


def _w_counter(out: BitWriter, value: int) -> None:
    """0x7FF78A76BB90: 0 + 4 bits, 1 + 8 bits (below 0xFF), 1 + 0xFF + 32 bits."""
    value &= 0xFFFFFFFF
    if value < 16:
        out.bit(0)
        out.bits(value, 4)
    elif value < 0xFF:
        out.bit(1)
        out.bits(value, 8)
    else:
        out.bit(1)
        out.bits(0xFF, 8)
        out.bits(value, 32)


def _w_entity(out: BitWriter, entity: int) -> None:
    """0x7FF789CC0F00: 2 type bits, then 0 + 8 bits, 1 + 16 bits (below 0xFFFF), 1 + 0xFFFF + 32 bits."""
    out.bits((entity >> 29) & 3, 2)
    value = entity & 0x1FFFFFFF
    if value < 256:
        out.bit(0)
        out.bits(value, 8)
    elif value < 0xFFFF:
        out.bit(1)
        out.bits(value, 16)
    else:
        out.bit(1)
        out.bits(0xFFFF, 16)
        out.bits(value, 32)


def direction16(direction) -> tuple[int, int]:
    """A unit direction as the record's (yaw16, pitch16): angles rounded half away from zero, value ^ 0x8000;
    the pitch is up-positive (0x7FF789CC1360 reads back (sin a cos b, sin b, cos a cos b))."""
    x, y, z = direction
    yaw = math.atan2(x, z) * YAW16 if (x or z) else 0.0
    pitch = math.asin(max(-1.0, min(1.0, y))) * PITCH16
    out = []
    for value in (yaw, pitch):
        number = int(value + (0.5 if value >= 0 else -0.5))
        number = max(-0x8000, min(0x7FFF, number))
        out.append((number & 0xFFFF) ^ 0x8000)
    return out[0], out[1]


@dataclass(frozen=True)
class ShotRecord:
    frame: int  # the shot's command frame
    instance: int  # the volley's instance
    state: int  # its m_states index (unverified: the client's state +104)
    activation: int  # the volley state's activation counter (6 bits)
    origin: tuple  # world position
    direction: tuple  # unit direction
    ids: tuple  # the projectile entities it starts
    shot: int = 0
    volleys: int = 1


def write_shot(out: BitWriter, shot: ShotRecord) -> None:
    """One record after its frame (0x7FF789C91F80, form 2)."""
    _w_counter(out, shot.shot)
    out.bit(0)  # no spread
    out.bit(0)  # form 2
    _w16(out, shot.instance)
    _w16(out, shot.state)
    out.bits(shot.activation & 63, 6)
    if shot.volleys == 1:
        out.bit(1)
    else:
        out.bit(0)
        _w16(out, shot.volleys)
    _w16(out, 0)
    for value in shot.origin:
        out.f32(value)
    yaw16, pitch16 = direction16(shot.direction)
    out.bits(yaw16, 16)
    out.bits(pitch16, 16)
    if not shot.ids:
        out.bit(0)
        return
    out.bit(1)
    _w16(out, len(shot.ids))
    _w_entity(out, shot.ids[0])
    for before, entity in zip(shot.ids, shot.ids[1:], strict=False):
        step = entity - before
        if step == 1:
            out.bit(1)
        else:
            out.bit(0)
            out.bit(step < 0)
            _w_counter(out, abs(step))


def shot_events(shots: list) -> BitWriter:
    """ch2 after its channel bit in the shooter's entity block: the shot records in frame order, then no
    projectile state (0x7FF789A4A490 -> 0x7FF789A50130)."""
    out = BitWriter()
    out.bit(1)  # events
    last = None
    for shot in sorted(shots, key=lambda item: item.frame):
        out.bit(1)
        if last is None:
            out.var_b(shot.frame & 0xFFFFFFFF)
        else:
            step = shot.frame - last
            if step < 16:
                out.bit(0)
                out.bits(step, 4)
            elif step < 256:
                out.bit(1)
                out.bit(0)
                out.bits(step, 8)
            else:
                out.bit(1)
                out.bit(1)
                out.bits(step & 0xFFFFFFFF, 32)
        write_shot(out, shot)
        last = shot.frame
    out.bit(0)  # the end of the events
    out.bit(0)  # no projectile state
    return out
