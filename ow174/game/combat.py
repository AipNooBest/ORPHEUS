"""Shots, damage, death and respawn: what the server decides and what it tells the clients.

What the client needs (read in the client unless marked):
- Health is component 51 (world.pool): an op-2 update of a pool's field changes the bar on every client
  that has the body. A body whose health pools (type 0) add up to 0 with a maximum above 0 is dead
  (0x7FF789ADBF00): the client then sets flag 0x2000 on its movement state (0x7FF7896A4F9A) and the 3P
  animation takes the dead pose 004.015 (0x7FF789B24889); flag 0x2000 in the records does the same.
- ClientInCombat 25001 {entity, u32 damage, u8 flags} u32 frame (handler 0x7FF7895D2D40) tells the shooter
  of a hit: it goes in his component 59's hit list (+0x12D0, records in frame order, not on his own
  body), which shows the target's health bar for 5 s (0x7FF7895D8520: int(1e6 / quanta) * 5 frames) and
  feeds the hit marker and its sound (event 7). Flag bit 0 counts at +0x124, else +0x120; bit 1 at +0x128.
  Unverified: bit 0 = critical, bit 1 = kill (the OW2 cross-check: 19 / 38 damage with 0 / 1, the last hit 2 /
  3).
- 25005 {u64, entity, entity victim, u8 score, bool assist} (0x7FF7895D2B50) and 25004 {u8, entity}
  (0x7FF7895D2A70) post 0F76.025 and 0F7B.025 / 0F7C.025 to the game mode entity's statescript
  (0x7FF7895D7AF0, the first component-114 entity): its graph 1FC0 (a client-only child of 0CB6) shows
  "ELIMINATED: <name>" (1126.07C), "YOU WERE KILLED BY <name>" (1127.07C) or "YOU DIED" (1128.07C). So
  the mode entity carries 0CB6 (mode_frame); the kill feed 0DEA is its child too.
- The kill feed line: the dead body's 01CF takes game message 003E.025 (the body's "died": killer 0012,
  crit 0095, loadout 03E6, assists 03E7), pulses its ClientOnlyPulser st108, and every client's 01CF sends
  0765.025 to the mode entity's 0DEA (DATA). So the server gives 003E.025 to the dead player's body
  script; its frames carry the line.

What the server decides here:
- A shot is a volley's shot in the shooter's body script (nodes.WeaponVolley.report): hitscan along the
  command's aim from the eye (STUFirstPersonComponent m_E29993A5 = 1.55 m, crouched m_816B830F, DATA;
  unverified as the eye height), or a projectile (m_projectileSpeed) flown each tick.
- Damage from the volley's hit definition (the graph data, evaluated by the body script): the
  ModifyHealth amount (Soldier 20, Helix 40 and 80 splash, quick melee 30), unverified: falloff from
  m_EAD7F104 to m_157E5BC5 metres down to m_72A81154 (30 m, 50 m, 30 %), critical times m_A872C70E (2), splash
  scaled between the explosion rings (1 m: 1.0, 3 m: 0.5). Shields, then armour (5 less a hit, at most half:
  unverified, OW1's rule; the client has no damage code), then health.
- Quick melee: 0043 st4 (ModifyHealth) on what its st5 TrackTargets would find (MELEE). The Biotic Field
  heals through heal_area (projectiles.py). A dead body's statescript takes no presses (the VM's
  owner_dead, as the client's LogicalButton 0x7FF78AAAC570), so the dead do not shoot.
- Bodies are capsules from the feet to the mover's height (radius the mover's, DATA; unverified as a hitbox),
  a hit at or above the eye height less HEAD_BELOW is critical (unverified). Shots stop at the map's collision
  (the mover's part of it, which the server keeps). Allies are not hit.
- Targets are where the shooter's client showed them: at the command's view time (commands.py: the
  frame's time less the view delay the command carries, 0x7FF7894BEE40 / 0x7FF7894BEEC0), from the
  server's own record of where each body was in every tick (the records the clients got, bots too),
  between two ticks by a straight line (unverified: as the client draws them), at most HISTORY ticks back.
- Respawn: heroes after the mode's time (Practice Range 3 s, PvP 10 s: the mode graphs' 06FD.025 to the
  body script 0C8E, DATA), at the spawn point as a new body (match.switch_hero); training bots after
  BOT_RESPAWN (unverified: 3 s in the OW2 cross-check) as new entities at their spawner.
"""

import logging
import math
from collections import deque
from dataclasses import dataclass

from ow174.game import bots, observers, pools, projectiles, stats, voicelines, world
from ow174.game.bits import BitWriter
from ow174.game.collision import AX, AY, AZ, BX, BY, BZ, CELL, CX, CY, CZ, MASK, MOVER, NUMBER
from ow174.game.mover import mover_data
from ow174.game.script import expr, graph, runtime
from ow174.game.statescript import InstanceWire, body_full_frame
from ow174.game.world import ARMOUR_POOL, HEALTH_POOL, OP_CREATE, OP_DESTROY, OP_UPDATE, POOLS, SHIELDS_POOL

log = logging.getLogger("ow174.game")

HIT = 25001  # to the shooter: a hit
DEATH_NOTICE = 25004  # to the dead player: who killed him
KILL_NOTICE = 25005  # to the killer: whom he killed
KILLED_BY = 2  # 25004 +0x78: "you were killed by" (0F7B.025); anything else "you died" (0F7C.025)
CRITICAL, KILLING = 0x01, 0x02  # 25001 flags (unverified, see above)
DEAD = 0x2000  # movement-state flag: dead (the 3P anim's dead pose)
DIED = 0x024000000000003E  # game message 003E.025: the body's graphs' "died"
# 003E.025's parameters, by the low 16 bits of their 026 identifiers (01CF rid4).
KILLER, CRIT, LOADOUT, ASSISTS = 0x0012, 0x0095, 0x03E6, 0x03E7
MODE_UI = 0x0CB6  # the mode's client UI graph: the kill feed 0DEA and the notices 1FC0 (client-only)

EYE = 1.55  # m: STUFirstPersonComponent m_E29993A5, its default (OWLib) and Soldier's (DATA; unverified: eye)
EYE_CROUCHED = {0x04000000000003CF: 1.085}  # Soldier's m_816B830F (DATA); others the default
EYE_CROUCHED_DEFAULT = 1.15
HEAD_BELOW = 0.2  # m under the eye where the head starts (unverified)
HISTORY = 64  # ticks of body positions kept for the shots' view time (about 1 s; unverified: retail's limit)
RANGE = 200.0  # m a hitscan shot reaches when its volley names none (unverified)
RESPAWN = 10.0  # s a hero waits in the PvP modes (0F4A and 0EC2 send 06FD.025 {10}: DATA)
PRACTICE_RESPAWN = 3.0  # s in the Practice Range (13C0 sends 06FD.025 {3}: DATA)
BOT_RESPAWN = 3.0  # s a training bot lies dead (unverified: OW2 made two killed bots again after 2.98 s)
MELEE = {0x0043: (4, 1.5, 1.0)}  # graph -> (its ModifyHealth state, metres ahead of the eye, radius): DATA
ARMOUR_BLOCK = 5.0  # damage armour stops per hit (unverified: OW1's rule; the client has no damage code)
TICK = 0.016
EPSILON = 1e-9


# --- geometry -----------------------------------------------------------------------------------------


def aim(yaw: int, pitch: int) -> tuple[float, float, float]:
    """The unit vector a command looks along: forward is (sin yaw, 0, cos yaw), the pitch is positive
    looking down (commands.py)."""
    y = yaw * 2 * math.pi / 65536
    p = pitch * 2 * math.pi / 65536
    return (math.sin(y) * math.cos(p), -math.sin(p), math.cos(y) * math.cos(p))


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _along(origin, direction, t: float):
    return (origin[0] + direction[0] * t, origin[1] + direction[1] * t, origin[2] + direction[2] * t)


def ray_capsule(origin, direction, a, b, radius: float) -> float | None:
    """Where a ray (unit direction) first meets the capsule around segment a-b: its distance, 0 from
    inside, None when it misses."""
    ba, oa = _sub(b, a), _sub(origin, a)
    baba, bard, baoa = _dot(ba, ba), _dot(ba, direction), _dot(ba, oa)
    if segment_distance(origin, a, b) <= radius:
        return 0.0
    rdoa, oaoa = _dot(direction, oa), _dot(oa, oa)
    k2 = baba - bard * bard
    k1 = baba * rdoa - baoa * bard
    k0 = baba * oaoa - baoa * baoa - radius * radius * baba
    if abs(k2) > EPSILON:
        h = k1 * k1 - k2 * k0
        if h < 0.0:
            return None
        t = (-k1 - math.sqrt(h)) / k2
        y = baoa + t * bard
        if 0.0 < y < baba and t >= 0.0:
            return t
    best = None
    for centre in (a, b):  # the end spheres
        oc = _sub(origin, centre)
        half = _dot(direction, oc)
        h = half * half - (_dot(oc, oc) - radius * radius)
        if h >= 0.0:
            t = -half - math.sqrt(h)
            if t >= 0.0 and (best is None or t < best):
                best = t
    return best


def segment_distance(point, a, b) -> float:
    """The distance from a point to the segment a-b."""
    ab, ap = _sub(b, a), _sub(point, a)
    length = _dot(ab, ab)
    t = 0.0 if length <= EPSILON else max(0.0, min(1.0, _dot(ap, ab) / length))
    closest = _along(a, ab, t)
    d = _sub(point, closest)
    return math.sqrt(_dot(d, d))


def ray_world(collision, origin, direction, length: float, category: int = MOVER) -> float | None:
    """The distance along a ray to the first triangle of the map's collision within `length` (the grid
    columns the ray crosses, nearest first), None when nothing is in the way."""
    if collision is None or length <= 0.0:
        return None
    ox, oy, oz = origin
    dx, dy, dz = direction
    i, j = math.floor(ox / CELL), math.floor(oz / CELL)
    step_i, step_j = (1 if dx > 0 else -1), (1 if dz > 0 else -1)
    big = math.inf
    next_x = ((i + (step_i > 0)) * CELL - ox) / dx if abs(dx) > EPSILON else big
    next_z = ((j + (step_j > 0)) * CELL - oz) / dz if abs(dz) > EPSILON else big
    delta_x = CELL / abs(dx) if abs(dx) > EPSILON else big
    delta_z = CELL / abs(dz) if abs(dz) > EPSILON else big
    seen: set[int] = set()
    best = math.inf
    start = 0.0
    while start <= length:
        end = min(next_x, next_z, length)
        low, high = oy + dy * start, oy + dy * end
        low, high = min(low, high) - EPSILON, max(low, high) + EPSILON
        for t in collision.column(i, j):
            number = t[NUMBER]
            if number in seen or not t[MASK] & category or t[3] < low or t[2] > high:
                continue
            seen.add(number)
            hit = _ray_triangle(ox, oy, oz, dx, dy, dz, t)
            if hit is not None and hit < best:
                best = hit
        if best <= end:
            return best if best <= length else None
        if next_x < next_z:
            start, next_x, i = next_x, next_x + delta_x, i + step_i
        else:
            start, next_z, j = next_z, next_z + delta_z, j + step_j
        if start == big:
            break
    return best if best <= length else None


def _ray_triangle(ox, oy, oz, dx, dy, dz, t) -> float | None:
    """Moeller-Trumbore, both sides."""
    ax, ay, az = t[AX], t[AY], t[AZ]
    e1x, e1y, e1z = t[BX] - ax, t[BY] - ay, t[BZ] - az
    e2x, e2y, e2z = t[CX] - ax, t[CY] - ay, t[CZ] - az
    px, py, pz = dy * e2z - dz * e2y, dz * e2x - dx * e2z, dx * e2y - dy * e2x
    det = e1x * px + e1y * py + e1z * pz
    if -1e-12 < det < 1e-12:
        return None
    inv = 1.0 / det
    sx, sy, sz = ox - ax, oy - ay, oz - az
    u = (sx * px + sy * py + sz * pz) * inv
    if u < 0.0 or u > 1.0:
        return None
    qx, qy, qz = sy * e1z - sz * e1y, sz * e1x - sx * e1z, sx * e1y - sy * e1x
    v = (dx * qx + dy * qy + dz * qz) * inv
    if v < 0.0 or u + v > 1.0:
        return None
    hit = (e2x * qx + e2y * qy + e2z * qz) * inv
    return hit if hit > 1e-6 else None


# --- health -------------------------------------------------------------------------------------------


class Vitals:
    """A body's pools as component 51 has them: health, armour, shields (world.health's ids and fields)."""

    def __init__(self, entity: int, health: float, armour: float = 0.0, shields: float = 0.0) -> None:
        self.entity = entity
        self.most = {HEALTH_POOL: float(health), ARMOUR_POOL: float(armour), SHIELDS_POOL: float(shields)}
        self.now = dict(self.most)
        self.ids = {}
        pool_id = 1
        for kind in (SHIELDS_POOL, ARMOUR_POOL):  # world.health numbers them so
            if self.most[kind] > 0:
                self.ids[kind] = pool_id
                pool_id += 1
        self.ids[HEALTH_POOL] = pool_id
        self.changed: set[int] = set()  # pools that changed since the last update went out
        self.died_at: float | None = None
        self.killer: int | None = None

    @property
    def dead(self) -> bool:
        return self.died_at is not None

    @property
    def total(self) -> float:
        return sum(self.now.values())

    def damage(self, amount: float) -> float:
        """Take a hit from shields, then armour, then health (unverified, as OW1 did: what reaches armour
        while it has some is ARMOUR_BLOCK less, at most halved). Returns what the pools lost."""
        left = max(0.0, amount)
        taken = 0.0
        for kind in (SHIELDS_POOL, ARMOUR_POOL, HEALTH_POOL):
            if left <= 0.0:
                break
            if kind == ARMOUR_POOL and self.now[kind] > 0.0:
                left = max(left - ARMOUR_BLOCK, left / 2)
            take = min(left, self.now[kind])
            if take > 0.0:
                self.now[kind] -= take
                self.changed.add(kind)
                left -= take
                taken += take
        return taken

    def heal(self, amount: float) -> float:
        """Heal health, then armour, then shields (unverified: the order). Returns what it gave."""
        left = max(0.0, amount)
        for kind in (HEALTH_POOL, ARMOUR_POOL, SHIELDS_POOL):
            give = min(left, self.most[kind] - self.now[kind])
            if give > 0.0:
                self.now[kind] += give
                self.changed.add(kind)
                left -= give
        return max(0.0, amount) - left

    def component(self, kinds) -> dict:
        """Component 51 with these pools' fields set (the others are left as they are)."""
        parts: list = [None] * (3 * POOLS)
        for kind in kinds:
            if kind in self.ids:
                parts[kind * POOLS] = world.pool(self.ids[kind], kind, self.most[kind], self.now[kind])
        return {51: parts}


@dataclass
class HitDef:
    """A hit definition evaluated: ModifyHealth's amount and loadout, and the volley's own fields."""

    amount: float = 0.0
    loadout: int | None = None
    falloff_start: float | None = None
    falloff_end: float | None = None
    falloff_floor: float | None = None
    critical: float = 1.0
    rings: tuple = ()  # (radius, scalar) of an explosion

    def scale(self, distance: float) -> float:
        """Unverified: full damage to falloff_start, falloff_floor from falloff_end on, a straight line
        between."""
        start, end, floor = self.falloff_start, self.falloff_end, self.falloff_floor
        if start is None or end is None or floor is None or end <= start or distance <= start:
            return 1.0
        if distance >= end:
            return floor
        return 1.0 + (floor - 1.0) * (distance - start) / (end - start)

    def splash(self, distance: float) -> float:
        """Unverified: an explosion's scalar at a distance: the first ring's inside it, a straight line from
        ring to ring, nothing past the last."""
        if not self.rings:
            return 0.0
        last_radius, last_scalar = self.rings[0]
        if distance <= last_radius:
            return last_scalar
        for radius, scalar in self.rings[1:]:
            if distance <= radius:
                share = (distance - last_radius) / max(EPSILON, radius - last_radius)
                return last_scalar + (scalar - last_scalar) * share
            last_radius, last_scalar = radius, scalar
        return 0.0


def _number(value, default=None):
    if value is None:
        return default
    try:
        return float(expr.to_float(value))
    except (TypeError, ValueError):
        return default


def hit_def(state, cfg) -> HitDef | None:
    """Evaluate a volley's hit definition (STU_00D9C7E1) or explosion (STU_4AA6CA44) with the shooter's
    body script."""
    if not isinstance(cfg, dict):
        return None

    def value(name, default=None):
        field = cfg.get(name)
        return default if field is None else _number(state.evaluate(field, watch=False), default)

    modify = cfg.get("m_modifyHealth") or {}
    amount = _number(state.evaluate(modify.get("m_amount"), watch=False), 0.0) if modify else 0.0
    loadout = state.evaluate(modify.get("m_loadout"), watch=False) if modify.get("m_loadout") else None
    rings = []
    for ring in cfg.get("m_0D30E40C") or []:  # WeaponProjectileExplosionRing {m_radius, m_B399CACB}
        if isinstance(ring, dict):
            radius = (
                state.evaluate(ring.get("m_radius"), watch=False) if ring.get("m_radius") is not None else 0.0
            )
            scalar = ring.get("m_B399CACB")
            scalar = state.evaluate(scalar, watch=False) if scalar is not None else 0.0
            rings.append((_number(radius, 0.0), _number(scalar, 0.0)))
    return HitDef(
        amount=amount,
        loadout=loadout.guid if isinstance(loadout, expr.Asset) else None,
        falloff_start=value("m_EAD7F104"),
        falloff_end=value("m_157E5BC5"),
        falloff_floor=value("m_72A81154"),
        critical=value("m_A872C70E", 1.0),
        rings=tuple(sorted(rings)),
    )


# --- who can be hit -------------------------------------------------------------------------------------


@dataclass
class Target:
    entity: int
    team: int
    feet: tuple
    height: float
    radius: float
    eye: float
    owner: object  # a match.Player or a bots.Bot

    def segment(self, feet=None):
        """The capsule's segment (from the feet to the top, a radius in from both ends)."""
        x, y, z = feet or self.feet
        return (x, y + self.radius, z), (x, y + max(self.radius, self.height - self.radius), z)


@dataclass
class Rocket:
    """A projectile in flight (Helix Rockets)."""

    shooter: object
    team: int
    position: tuple
    direction: tuple
    speed: float
    left: float  # seconds of life: its lifetime, or its range at its speed
    direct: HitDef | None
    blast: HitDef | None
    shot: object = None  # its projectiles.Shot: told when it ends
    flown: float = 0.0  # seconds in the air


class Combat:
    """The match's health, hits, deaths and respawns."""

    def __init__(self, match) -> None:
        self.match = match
        self.vitals: dict[int, Vitals] = {}  # body or bot entity -> its health
        self.history: dict[int, deque] = {}  # entity -> (tick, feet, height, eye) of the last HISTORY ticks
        self.rockets: list[Rocket] = []
        self.respawns: list[tuple[float, object]] = []  # (when, a player or a bot)
        self.bot_shown: dict[tuple[int, int], int] = {}  # (viewer slot, bot entity) -> tick of its create
        self.next_bot = bots.BOT_ENTITY + len(match.bots)
        self.now = 0.0
        self.tick = 0
        self.melee_seen: dict[tuple, int] = {}  # (body, instance, state) -> the melee state's last counter
        self.projectiles = projectiles.Projectiles(self)  # leases, confirmations, the Biotic Field
        self.observers = observers.Observers(match)  # the bodies' statescript for the other clients
        self.voice = voicelines.VoiceLines(match)  # the hero's spawn voice line (voicelines.py)

    # --- hooks (match.py) --------------------------------------------------------------------------

    def is_dead(self, player) -> bool:
        vitals = self.vitals.get(player.body) if player.has_body else None
        return vitals is not None and vitals.dead

    def spawned(self, player) -> None:
        """The player's client has the world: the game mode entity gets its UI graph (mode_frame). While
        heroes are assembled the countdown's frames carry it."""
        if player.client is not None and not self.match.assembling():
            player.client.queue_entities([player.mode_script.full_frame(mode_frame({}))])
        self.voice.spawned(player, self.now)

    def command(self, player, command) -> None:
        """One command frame the player's body script ran: its shots hit."""
        script = player.body_script
        if script is None:
            return
        shots, script.component.shots[:] = list(script.component.shots), []
        if self.is_dead(player):
            return
        for state, number, time in shots:
            try:
                self._shoot(player, command, state, number, time)
            except Exception:
                log.exception("[game] %s: %s's shot failed", self.match.label(), player.name)
        try:
            self._melee(player, command, script)
        except Exception:
            log.exception("[game] %s: %s's melee failed", self.match.label(), player.name)

    def _melee(self, player, command, script) -> None:
        """Quick melee: 0043 st4 (StateModifyHealth) hits what its st5 TrackTargets finds: a sphere of
        m_radius 1.0 at (0, 0, 1.5) from the eye (DATA; unverified: along the aim), #476 = 30 damage (DATA),
        once per activation of st4. The server cannot run TrackTargets itself (the VM finds nothing)."""
        for instance in script.component.instances.values():
            spec = MELEE.get(instance.graph.index)
            if spec is None:
                continue
            index, reach, radius = spec
            state = instance.states.get(index)
            if state is None:
                continue
            key = (script.entity, instance.id, index)
            seen = self.melee_seen.get(key, 0)
            self.melee_seen[key] = state.counter
            if state.counter == seen or not state.active:
                continue
            modify = state.node.field("m_modifyHealth") or {}
            amount = _number(state.evaluate(modify.get("m_amount"), watch=False), 0.0) if modify else 0.0
            loadout = (
                state.evaluate(modify.get("m_loadout"), watch=False) if modify.get("m_loadout") else None
            )
            loadout = loadout.guid if isinstance(loadout, expr.Asset) else None
            centre = _along(self._eye(player), aim(*command.aim()), reach)
            view_tick = command.frame - command.view_delay(player.quanta) * 1e6 / player.quanta
            for target in self._enemies(player):
                shown = self.seen(target, view_tick)
                a, b = shown.segment()
                if segment_distance(centre, a, b) - shown.radius <= radius and amount > 0.0:
                    self._damage(player, target, amount, False, loadout, command.frame)

    def update(self, now: float, tick: int, frames: bool = True) -> None:
        """Each tick: keep where the bodies are, fly the rockets, respawn, send health that changed. The
        other players' body frames only on the ticks that send (`frames`)."""
        self.now, self.tick = now, tick
        for target in self._targets():
            history = self.history.setdefault(target.entity, deque(maxlen=HISTORY))
            history.append((tick, target.feet, target.height, target.eye))
        for rocket in list(self.rockets):
            self._fly(rocket)
        self.projectiles.update(tick)
        if frames:
            self.observers.update()
        for when, who in list(self.respawns):
            if now >= when:
                self.respawns.remove((when, who))
                self._respawn(who)
        self.voice.expire(now)
        self._send_health()
        current = {target.entity for target in self._targets()}
        for table in (self.history, self.vitals):
            for entity in [key for key in table if key not in current]:
                del table[entity]

    # --- targets -----------------------------------------------------------------------------------

    def _targets(self) -> list[Target]:
        found = []
        for player in self.match.players:
            if not (player.has_body and player.spawned and player.client is not None):
                continue
            data = mover_data(player.hero.body)
            crouched = player.mover.crouched
            height = data.crouch_height if crouched else data.stand_height
            eye = EYE_CROUCHED.get(player.hero.body, EYE_CROUCHED_DEFAULT) if crouched else EYE
            found.append(
                Target(player.body, player.team, tuple(player.mover.position), height, data.stand_radius, eye,
                       player)
            )  # fmt: skip
        for bot in self.match.bots:
            data = mover_data(bot.body)
            found.append(
                Target(
                    bot.entity, bot.team, tuple(bot.position), data.stand_height, data.stand_radius, EYE, bot
                )
            )
        return found

    def vitals_of(self, target: Target) -> Vitals:
        found = self.vitals.get(target.entity)
        if found is None:
            if isinstance(target.owner, bots.Bot):
                found = Vitals(target.entity, target.owner.health)
            else:
                found = Vitals(target.entity, *pools.body_pools(target.owner.hero.guid))
            self.vitals[target.entity] = found
        return found

    def _enemies(self, shooter) -> list[Target]:
        free_for_all = self.match.game_map.free_for_all
        found = []
        for target in self._targets():
            if target.owner is shooter or target.entity == getattr(shooter, "body", None):
                continue
            if not free_for_all and target.team == shooter.team:
                continue
            if self.vitals_of(target).dead:
                continue
            found.append(target)
        return found

    def seen(self, target: Target, view_tick: float) -> Target:
        """The target as the shooter's client drew it at `view_tick` (a fraction between two ticks): between
        the two kept ticks around it, the oldest or newest one outside them."""
        history = self.history.get(target.entity)
        if not history:
            return target
        before = after = None
        for entry in history:
            if entry[0] <= view_tick:
                before = entry
            else:
                after = entry
                break
        if before is None:
            before = after
        if after is None or after is before:
            _, feet, height, eye = before
        else:
            share = (view_tick - before[0]) / (after[0] - before[0])
            feet = tuple(a + (b - a) * share for a, b in zip(before[1], after[1], strict=True))
            height, eye = (before[2], before[3]) if share < 0.5 else (after[2], after[3])
        return Target(target.entity, target.team, feet, height, target.radius, eye, target.owner)

    # --- shots -------------------------------------------------------------------------------------

    def _eye(self, player) -> tuple:
        x, y, z = player.mover.position
        crouched = player.mover.crouched
        height = EYE_CROUCHED.get(player.hero.body, EYE_CROUCHED_DEFAULT) if crouched else EYE
        return (x, y + height, z)

    def _shoot(self, player, command, state, number: int, time: int) -> None:
        node = state.node

        def field(name, default=None):
            cfg = node.field(name)
            return default if cfg is None else _number(state.evaluate(cfg, watch=False), default)

        origin = self._eye(player)
        direction = aim(*command.aim())
        direct = hit_def(state, node.field("m_6396149F"))
        speed = field("m_projectileSpeed")
        if speed:
            lifetime = field("m_projectileLifetime", 5.0) or 5.0
            reach = field("m_FEC435C6")
            if reach:
                lifetime = min(lifetime, reach / speed)
            blast = hit_def(state, node.field("m_D2D11CE9"))
            entity_def = (node.field("m_projectileEntity") or {}).get("m_entityDef")
            definition = state.evaluate(entity_def, watch=False) if entity_def is not None else None
            shot = None
            if isinstance(definition, expr.Asset):
                shot = self.projectiles.fired(
                    player, state, command.frame, definition.guid, lifetime, origin, direction
                )
            damaging = (direct is not None and direct.amount > 0.0) or (
                blast is not None and blast.amount > 0.0
            )
            if damaging or shot is not None:
                rocket = Rocket(player, player.team, origin, direction, speed, lifetime, direct, blast, shot)
                self.rockets.append(rocket)
            return
        if direct is None or direct.amount <= 0.0:
            return
        reach = field("m_FEC435C6", RANGE) or RANGE
        view_tick = command.frame - command.view_delay(player.quanta) * 1e6 / player.quanta
        best = None
        for target in self._enemies(player):
            shown = self.seen(target, view_tick)
            a, b = shown.segment()
            t = ray_capsule(origin, direction, a, b, shown.radius)
            if t is not None and t <= reach and (best is None or t < best[0]):
                best = (t, target, shown)
        if best is None:
            return
        t, target, shown = best
        wall = ray_world(self._collision(), origin, direction, t)
        if wall is not None and wall < t:
            return
        hit_y = origin[1] + direction[1] * t
        critical = hit_y >= shown.feet[1] + shown.eye - HEAD_BELOW
        amount = direct.amount * direct.scale(t) * (direct.critical if critical else 1.0)
        self._damage(
            player, target, amount, critical and direct.critical > 1.0, direct.loadout, command.frame
        )

    def _fly(self, rocket: Rocket) -> None:
        """One tick of a projectile: it hits the first enemy body or wall on its way and explodes there."""
        rocket.left -= TICK
        if rocket.left <= 0.0:
            self._landed(rocket)
            return
        rocket.flown += TICK
        step = rocket.speed * TICK
        best = None
        for target in self._enemies(rocket.shooter):
            a, b = target.segment()
            t = ray_capsule(rocket.position, rocket.direction, a, b, target.radius)
            if t is not None and t <= step and (best is None or t < best[0]):
                best = (t, target)
        wall = ray_world(self._collision(), rocket.position, rocket.direction, step)
        if wall is not None and (best is None or wall < best[0]):
            best = (wall, None)
        if best is None:
            rocket.position = _along(rocket.position, rocket.direction, step)
            return
        t, struck = best
        rocket.flown += t / rocket.speed - TICK
        self._landed(rocket)
        point = _along(rocket.position, rocket.direction, t)
        if struck is not None and rocket.direct is not None and rocket.direct.amount > 0.0:
            self._damage(
                rocket.shooter, struck, rocket.direct.amount, False, rocket.direct.loadout, self.tick
            )
        blast = rocket.blast
        if blast is None or blast.amount <= 0.0:
            return
        for target in self._enemies(rocket.shooter):
            a, b = target.segment()
            distance = max(0.0, segment_distance(point, a, b) - target.radius)
            scalar = blast.splash(distance)
            if scalar > 0.0:
                self._damage(rocket.shooter, target, blast.amount * scalar, False, blast.loadout, self.tick)

    def _landed(self, rocket: Rocket) -> None:
        """The projectile ended (hit, range, lifetime): the predicted one's release follows."""
        if rocket in self.rockets:
            self.rockets.remove(rocket)
        if rocket.shot is not None:
            self.projectiles.flown(rocket.shot, rocket.flown)

    def heal_area(self, healer, centre, radius: float, amount: float) -> None:
        """Heal the healer and his teammates (everyone's own team in a free-for-all is his alone) whose body
        is within `radius` of `centre` (unverified: to the body's capsule)."""
        for target in self._targets():
            ally = not self.match.game_map.free_for_all and target.team == healer.team
            if target.owner is not healer and not ally:
                continue
            vitals = self.vitals_of(target)
            if vitals.dead:
                continue
            a, b = target.segment()
            if segment_distance(centre, a, b) - target.radius > radius:
                continue
            given = vitals.heal(amount)
            if given <= 0.0:
                continue
            healer.stats.add(stats.HEALING, given)
            script = getattr(target.owner, "body_script", None)
            if script is not None:
                script.set_health(vitals.total, sum(vitals.most.values()))

    def _collision(self):
        from ow174.game.collision import world_when_ready

        game_map = self.match.game_map
        return world_when_ready(game_map.map_guid, game_map.name, game_map.mode_guid)

    # --- damage and death --------------------------------------------------------------------------

    def _damage(self, shooter, target: Target, amount: float, critical: bool, loadout, frame: int) -> None:
        vitals = self.vitals_of(target)
        if vitals.dead or amount <= 0.0:
            return
        dealt = vitals.damage(amount)
        if dealt <= 0.0:
            return
        killed = vitals.now[HEALTH_POOL] <= EPSILON
        if killed:
            vitals.now[HEALTH_POOL] = 0.0
            vitals.died_at = self.now
            vitals.killer = getattr(shooter, "body", None)
        flags = (CRITICAL if critical else 0) | (KILLING if killed else 0)
        self._tell_hit(shooter, target, dealt, flags, frame)
        shooter.stats.add(stats.HERO_DAMAGE, dealt)
        if isinstance(target.owner, bots.Bot):
            if killed:
                target.owner.died(self.tick)
                self.respawns.append((self.now + BOT_RESPAWN, target.owner))
                shooter.stats.add(stats.ELIMINATIONS)
                self._kill_notice(shooter, target.entity)
                log.info("[game] %s: %s killed %s", self.match.label(), shooter.name, target.owner.name)
            return
        victim = target.owner
        script = victim.body_script
        if script is not None:
            script.set_health(vitals.total, sum(vitals.most.values()))
        if killed:
            self._died(shooter, victim, critical, loadout)

    def _tell_hit(self, shooter, target: Target, dealt: float, flags: int, frame: int) -> None:
        """25001 to the shooter: the hit marker and the target's health bar (frame: the shot's)."""
        client = getattr(shooter, "client", None)
        if client is None:
            return
        value = {
            "+0x78": {"+0x0": {"+0x0": target.entity}, "+0x4": max(1, round(dealt)), "+0x8": flags},
            "+0x84": frame & 0xFFFFFFFF,
        }
        client.queue_reliable(HIT, value)

    def _kill_notice(self, killer, victim: int) -> None:
        """25005 to the killer: "ELIMINATED: <name>" (1126.07C; +0x89 assist would be 01F0.07C, +0x88 a
        score shown when above 0). Its handler (0x7FF7895D2B50) reads only +0x84, +0x88 and +0x89."""
        if killer.client is None:
            return
        notice = {
            "+0x78": 0,
            "+0x80": {"+0x0": killer.body},
            "+0x84": {"+0x0": victim},
            "+0x88": 0,
            "+0x89": False,
        }
        killer.client.queue_reliable(KILL_NOTICE, notice)

    def _died(self, killer, victim, critical: bool, loadout) -> None:
        """A player died: the notices, his body script's "died" (the kill feed line), and the respawn."""
        label = self.match.label()
        log.info("[game] %s: %s killed %s", label, killer.name, victim.name)
        victim.stats.add(stats.DEATHS)
        if killer is not victim:
            killer.stats.add(stats.ELIMINATIONS)
            self._kill_notice(killer, victim.body)
        if victim.client is not None:
            how = KILLED_BY if killer is not victim else 0
            victim.client.queue_reliable(DEATH_NOTICE, {"+0x78": how, "+0x7C": {"+0x0": killer.body}})
        script = victim.body_script
        if script is not None:
            params = {
                KILLER: expr.Entity(killer.body),
                CRIT: bool(critical),
                LOADOUT: expr.Asset(loadout) if loadout else None,
                ASSISTS: (),
            }
            try:
                runtime.send_message(script.component, DIED, params, killer.body)
            except Exception:
                log.exception("[game] %s: %s's body script failed on %X", label, victim.name, DIED)
        self.respawns.append((self.now + respawn_seconds(self.match), victim))

    def _respawn(self, who) -> None:
        if isinstance(who, bots.Bot):
            self._respawn_bot(who)
            return
        player = who
        if player.gone or not player.has_body or not self.is_dead(player):
            return
        old = player.body
        self.match.switch_hero(player, player.hero)  # a new body at the spawn point, the old one goes
        self.vitals.pop(old, None)
        self.voice.spawned(player, self.now)
        log.info("[game] %s: %s respawned", self.match.label(), player.name)

    def _respawn_bot(self, bot) -> None:
        """A new bot from the same spawner under a new entity (the client ignores a destroyed id for 30 s);
        the dead one goes."""
        match = self.match
        if bot not in match.bots:
            return
        entity = self.next_bot
        self.next_bot += 1
        fresh = bots.respawned(bot, entity, match.tick + match_record_delay())
        match.bots[match.bots.index(bot)] = fresh
        self.vitals.pop(bot.entity, None)
        for viewer in match.players:
            if viewer.client is None or match.bots_shown.get(viewer.slot) is None:
                continue
            viewer.client.queue_entities(
                [
                    world.EntityUpdate(bot.entity, OP_DESTROY),
                    world.EntityUpdate(
                        fresh.entity, OP_CREATE, lambda origin, b=fresh: match._bot_create(origin, b)
                    ),
                ]
            )
            self.bot_shown[(viewer.slot, fresh.entity)] = match.tick
        log.info("[game] %s: %s respawned as %08X", match.label(), fresh.name, fresh.entity)

    # --- what the clients get ----------------------------------------------------------------------

    def _send_health(self) -> None:
        """An update of component 51 to every client that has the body, for the pools that changed."""
        match = self.match
        for entity, vitals in list(self.vitals.items()):
            if not vitals.changed:
                continue
            components = vitals.component(sorted(vitals.changed))
            vitals.changed.clear()
            for viewer in match.players:
                if viewer.client is None or not viewer.spawned or not self._has(viewer, entity):
                    continue
                viewer.client.queue_entities(
                    [
                        world.EntityUpdate(
                            entity, OP_UPDATE, lambda origin, c=components: world.update(origin, c)
                        )
                    ]
                )

    def _has(self, viewer, entity: int) -> bool:
        """Whether the viewer's client has the entity from an earlier frame."""
        match = self.match
        if viewer.has_body and viewer.body == entity:
            return True
        if viewer.seen.get(entity):
            return True
        if any(bot.entity == entity for bot in match.bots):
            shown = self.bot_shown.get((viewer.slot, entity), match.bots_shown.get(viewer.slot))
            return shown is not None and shown < match.tick
        return False


def match_record_delay() -> int:
    from ow174.game import match as match_module

    return match_module.BOT_RECORD_DELAY


def respawn_seconds(match) -> float:
    """The mode's respawn time for heroes (DATA: the mode graphs' 06FD.025 to the body script 0C8E)."""
    from ow174.game import heroselect

    return PRACTICE_RESPAWN if match.controller is heroselect.PRACTICE else RESPAWN


def mode_frame(entity_vars: dict) -> BitWriter:
    """A plain full frame of the game mode entity: the mode's client UI graph 0CB6 as instance 1 (its
    client-only Entries make the kill feed 0DEA and the notices 1FC0) and the entity variables given (the
    PvP countdown). Every full frame of this entity has to list 0CB6, or it would destroy it."""
    ui = graph.graph(MODE_UI)
    values = {var: (value, ()) for var, value in entity_vars.items()}
    return body_full_frame([InstanceWire(1, ui)], values, {1: ui})
