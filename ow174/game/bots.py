"""The training bots: what a map's AI spawners make (data/ai_spawners_174.json, made by
tools/extract_ai_spawners.py from each map's ENTITY chunk).

The Practice Range has 15 spawners: 11 Training Bots on the red team and 4 Friendly Bots on the blue one
(retail's OW2 Practice Range showed 15 bots too, each with a movement record in every frame, idle or not). A
spawner names a hero, a team and the graphs that drive the bot:

- 0B8E walks a route. Its variable 6947 lists AI hint points, variable 39 is the route index (2 to start).
  It waits 2 s, then 1 s (two StateWaits), sets #39 = (#39 + 1) % count(#6947), and walks to the point
  #6947[#39] (STU_ECAEE401 with the move-to 194D744E and the face-to AB9B30EF); after a walk it waits 1 s
  (the second wait) and goes on.
- 0B92 shoots ahead every 1.75 to 2.5 s while it stands within 0.4 m of where it spawned (its weapon,
  STU_D7F9E1C3), stops for 2.75 s after a game message (a hit), and walks back to its place when it left
  it. Its shots need the bot body's own statescript on the server (its weapon), which the server does not
  run yet, so these bots only stand.
- No graph: the bot stands.

A bot is a hero body without a player: the hero's m_gameplayEntity, its health (the body's override of
the common body graph 0033's variable 199: Training Bots 200, Friendly Bots 225), the team and the hero, as
a player's body (match.Match._bot_create). The server moves it itself and sends its state as another
player's body's. The client's code for the AI's move-to, face-to and fire states is a stub (one shared
class, vtable 0x7FF78B6BC800): they ran on the server only.

How it walks was measured on the retail OW2 Practice Range (its live traffic), whose four
walking bots walk the far routes of these spawners (MEASURED unless marked):
- It sets off when its wait ends, whichever way it faces (backward or sideways at first), its keys held
  all the way (throttles of 127), and turns toward the way it walks at 60 degrees a second (TURN_RATE)
  until 0.5 s after it lets go of the keys (TURN_AFTER).
- It speeds up at its body's ground acceleration times run speed to its run speed, and lets go of the
  keys where its ground friction (base times run speed) stops it on its point: 4.5 m/s^2, 4.5 m/s and
  67.5 m/s^2, the Training Bot body's values (DATA: data/game_movers_174.json, 1403). On a ramp its speed
  is along the floor.
- The next walk starts WALK_GAP after the last tick with keys (the graph's 1 s wait and its state
  changes); the first one FIRST_WAIT after it spawns (DATA, not in the captures).
- unverified: it stops on its point. Retail stopped 0.05-0.45 m short of the 1.74 points, but its own points
  may be as far from them.
- The far runners (hero 016E) walk with their own body 14ED's values (DATA, 1.74): 6 m/s, up at 66 m/s^2
  (ground acceleration 11), down at 39 m/s^2 (friction base 6.5), the fast targets 1.74 made them. OW2
  put ordinary Training Bots on these routes (4.5 m/s); the server keeps 1.74's. The routes are 1.74's
  (DATA); OW2 changed three of them.
It stands on the floor under it: each tick on the highest surface of the map's collision from STEP_UP
above its feet down (ramps and steps), or, while the collision loads or where there is none, at the
height between its last point and the next one in step with the way it went.
"""

import json
import math
from functools import cache

from ow174.game.mover import GRAVITY_SCALE, mover_data
from ow174.game.world import NO_INPUT, Movement
from ow174.paths import DATA_DIR

SPAWNERS_PATH = DATA_DIR / "ai_spawners_174.json"
PATROL = 0x0580000000000B8E
ROUTE = "0x0D80000000001B23"  # #6947, the route's hint points
INDEX = "0x0D80000000000027"  # #39, the route index
FIRST_WAIT = 2.0 + 1.0  # seconds: the patrol graph's two waits before its first walk (DATA)
WALK_GAP = 1.2  # seconds from the last tick with keys held to the next walk (75 ticks)
ARRIVED = 0.05  # metres from the point at which a walk needs no keys
STEP_UP = 0.6  # metres above its feet the floor under a walking bot may be (a step, a ramp)
DROP = 4.0  # metres below its feet the floor may be
MAX_SLOPE = 1.0  # rise per metre up to which the speed is along the floor (the bodies' max_slope, 45 deg)
BOT_ENTITY = 0xC0001000  # bot n is BOT_ENTITY + n: entity id type 2, like the game mode entity
FORWARD = 127  # the throttle byte of a key held all the way
YAW_UNITS = 65536  # s16 angle units per turn
TURN_RATE = YAW_UNITS / 6  # yaw units per second it turns: 60 degrees (174.76 units per tick)
TURN_AFTER = 0.5  # seconds it turns on after it lets go of the keys (28 to 33 ticks)
FAR_RUNNER = 0x02E000000000016E  # the far runners' hero (body 14ED)
CATCH_UP = 625  # ticks a late step walks at most (10 s)
EPSILON = 1e-9
# A walking bot's state goes in every frame, as retail sent them. A standing one changes nothing, so its
# record goes once a second (staggered by bot), and in every frame for a second after it stopped.
SETTLE_TICKS = 63  # about a second of 16 ms ticks


@cache
def table() -> dict:
    if not SPAWNERS_PATH.is_file():
        return {"heroes": {}, "maps": {}}
    return json.loads(SPAWNERS_PATH.read_text(encoding="utf-8"))


def s16(units):
    return (units + 0x8000) % YAW_UNITS - 0x8000


def yaw_of(rotation) -> int:
    """A rotation about +Y (x, y, z, w) as the commands' yaw units (match._rotation's inverse)."""
    _, y, _, w = rotation
    return s16(round(math.atan2(y, w) * YAW_UNITS / math.pi))


def heading(dx: float, dz: float) -> int:
    """The yaw that faces (dx, dz): the mover's forward is (sin, cos) of the yaw (x, z)."""
    return s16(round(math.atan2(dx, dz) * YAW_UNITS / (2 * math.pi)))


def turned(yaw, toward, units):
    """`yaw` turned toward `toward` by at most `units`, the short way."""
    left = s16(toward - yaw)
    return s16(yaw + max(-units, min(units, left)))


def throttles(yaw, direction: tuple) -> tuple[int, int]:
    """(right, forward) bytes of a walk along `direction` (x, z) by a body facing `yaw`: forward is
    (sin, cos) of the yaw, right (-cos, sin), as in the mover."""
    angle = yaw * 2 * math.pi / YAW_UNITS
    dx, dz = direction
    forward = dx * math.sin(angle) + dz * math.cos(angle)
    right = -dx * math.cos(angle) + dz * math.sin(angle)
    return round(right * FORWARD), round(forward * FORWARD)


DEAD = 0x2000  # the movement state's dead flag (combat.py): the 3P animation's dead pose


class Bot:
    def __init__(self, entity: int, spawner: dict, hero: dict, hints: dict) -> None:
        self.entity = entity
        self.spawner, self.hero_data, self.hints = spawner, hero, hints  # for a respawn (respawned)
        self.dead = False  # killed (combat.py): it stands still with the dead flag until it is made again
        self.records_from: int | None = None  # a respawned bot's first record tick (after its create)
        self.hero = int(spawner["hero"], 16)
        self.name = hero["name"]
        self.body = int(hero["body"], 16)
        self.team = spawner["team"]
        self.health = hero["health"]
        values = mover_data(self.body)
        self.speed = values.run_forward
        self.accel = values.accel_ground * values.run_forward  # m/s^2 up to full speed
        self.brake = values.decel_ground_base * values.run_forward  # m/s^2 down to a stop
        self.pace = 0.0  # its speed now
        self.direction = (0.0, 1.0)  # the way it walks now (x, z)
        self.position = tuple(spawner["position"])
        self.yaw = float(yaw_of(spawner["rotation"]))
        self.velocity = (0.0, 0.0, 0.0)
        variables = spawner.get("vars", {})
        route = variables.get(ROUTE, []) if PATROL in {int(g, 16) for g in spawner["graphs"]} else []
        self.route = [tuple(hints[point]) for point in route if point in hints]
        self.index = variables.get(INDEX, 0)
        self.wait = FIRST_WAIT  # seconds to the next walk
        self.target: tuple | None = None
        self.start = self.position  # where the walk to the target began
        self.keys = False  # it holds its movement keys
        self.turning = 0.0  # seconds it still turns after it let go of the keys
        self.held = (0, 0)  # the throttles of its last tick
        self.last_held = (0, 0)  # the throttles of the last tick it held the keys in
        self.input_tick: int | None = None  # the last tick it held the keys in
        self.moved_tick: int | None = None  # the last tick it moved or turned in
        self.last_tick: int | None = None

    def step(self, tick: int, seconds: float, collision=None) -> None:
        """Walk the route up to `tick`, a tick being `seconds` long, on the map's collision if loaded."""
        if not self.route or self.dead:
            return
        ticks = 1 if self.last_tick is None else tick - self.last_tick
        if ticks <= 0:
            return
        self.last_tick = tick
        for at in range(tick - min(ticks, CATCH_UP) + 1, tick + 1):
            self._tick(at, seconds, collision)

    def _tick(self, tick: int, dt: float, collision) -> None:
        before = (self.position, round(self.yaw))
        self.held = (0, 0)
        if not self.keys:
            self.wait -= dt
            if self.target is None and self.wait <= EPSILON:
                self.index = (self.index + 1) % len(self.route)
                self.target = self.route[self.index]
                self.start = self.position
                self.keys = True
                self.turning = 0.0
        if self.keys:
            self._walk(tick, dt, collision)
        else:
            if self.target is not None:
                self._slide(dt, collision)
            if self.turning > EPSILON:
                self.turning -= dt
                self._turn(dt)
        if (self.position, round(self.yaw)) != before:
            self.moved_tick = tick

    def _walk(self, tick: int, dt: float, collision) -> None:
        x, _, z = self.position
        tx, _, tz = self.target
        distance = math.hypot(tx - x, tz - z)
        if distance <= ARRIVED:
            self.keys = False
            self.wait = WALK_GAP
            self._stand()
            return
        self.direction = ((tx - x) / distance, (tz - z) / distance)
        self._turn(dt)
        self.pace = min(self.speed, self.pace + self.accel * dt)
        self.held = self.last_held = throttles(self.yaw, self.direction)
        self.input_tick = tick
        stride, slide = self.pace * dt, self._slide_length(dt)
        if distance - stride <= slide:
            # its last tick with keys: from here its friction slides it onto the point
            stride = max(0.0, distance - slide)
            self.keys = False
            self.wait = WALK_GAP
            self.turning = TURN_AFTER
        self._advance(stride, distance, dt, collision)

    def _slide(self, dt: float, collision) -> None:
        """No keys: its friction stops it, never past its point."""
        self.pace = max(0.0, self.pace - self.brake * dt)
        x, _, z = self.position
        tx, _, tz = self.target
        distance = math.hypot(tx - x, tz - z)
        moving = self.pace > 0.0 and distance > EPSILON
        if moving and self._advance(self.pace * dt, distance, dt, collision) < distance - EPSILON:
            return
        self._stand()

    def _stand(self) -> None:
        self.pace = 0.0
        self.velocity = (0.0, 0.0, 0.0)
        self.target = None

    def _slide_length(self, dt: float) -> float:
        """How far its friction slides it from its speed now, tick by tick."""
        slow = self.brake * dt
        length, pace = 0.0, self.pace - slow
        while pace > 0.0:
            length += pace * dt
            pace -= slow
        return length

    def _turn(self, dt: float) -> None:
        self.yaw = turned(self.yaw, heading(*self.direction), TURN_RATE * dt)

    def _advance(self, stride: float, distance: float, dt: float, collision) -> float:
        """Move `stride` along the floor the way it walks, not past its point (`distance` away); the
        ground distance gone."""
        x, y, z = self.position
        dx, dz = self.direction
        probe = min(stride, distance)
        rise = self._floor(x + dx * probe, y, z + dz * probe, collision) - y
        if 0.0 < abs(rise) <= MAX_SLOPE * probe:
            stride *= probe / math.hypot(probe, rise)
        if stride >= distance:
            stride = distance
            nx, nz = self.target[0], self.target[2]
        else:
            nx, nz = x + dx * stride, z + dz * stride
        ny = self._floor(nx, y, nz, collision)
        self.position = (nx, ny, nz)
        self.velocity = ((nx - x) / dt, (ny - y) / dt, (nz - z) / dt)
        return stride

    def _floor(self, x: float, y: float, z: float, collision) -> float:
        """The height its feet take at (x, z): the floor of the collision, else the height between the
        walk's start and its target at the share of the way it has gone."""
        if collision is not None:
            ground = collision.ground(x, z, y + STEP_UP, y - DROP)
            if ground is not None:
                return ground.y
        sx, sy, sz = self.start
        tx, ty, tz = self.target
        whole = math.hypot(tx - sx, tz - sz)
        gone = 1.0 if whole <= 0.0 else min(1.0, math.hypot(x - sx, z - sz) / whole)
        return sy + (ty - sy) * gone

    def walking(self) -> bool:
        return self.target is not None

    def died(self, tick: int) -> None:
        """Killed: it stops where it is, and its records carry the dead flag (every frame for a while)."""
        self.dead = True
        self.keys = False
        self.held = (0, 0)
        self._stand()
        self.moved_tick = tick

    def record_due(self, tick: int, every: int = 1) -> bool:
        """Whether this frame carries its state: each frame while it walks and SETTLE_TICKS after, else
        about once a second. `every` is the ticks between the frames sent (match.SEND_EVERY), so that a
        resting bot's turn falls on a tick that sends."""
        if self.records_from is not None and tick < self.records_from:
            return False
        if self.walking() or (self.moved_tick is not None and tick - self.moved_tick < SETTLE_TICKS):
            return True
        period = max(1, SETTLE_TICKS // every)
        return (tick // every) % period == self.entity % period

    def movement(self, tick: int) -> Movement:
        """Its state for the clients, as a player body's (match.Player.movement). The 3P animation takes
        the throttles as the input direction against the body's yaw and its speed from the velocity:
        while it walks they point the way it walks, seen from its yaw, at full throttle
        (retail sent 126-127 from the first tick of a walk to its last); after, they are 0 and +36
        (`input_frame`) names the last tick with keys held, its throttles in +34/+35."""
        if self.held != (0, 0):
            input_frame = None  # keys held now: the state's own frame
        elif self.input_tick is None:
            input_frame = NO_INPUT
        elif self.input_tick >= tick:
            # +36 must go as the state's own frame (one 0 bit): the client reads +34/+35 only when the
            # decoded +36 differs from +8, and the same frame written out would leave them in the bits,
            # the rest of the record read out of step (the bot jumped away and back; the client crashed
            # in the movement reader).
            input_frame = None
        else:
            input_frame = self.input_tick
        return Movement(
            self.position,
            s16(round(self.yaw)),
            0,
            self.velocity,
            DEAD if self.dead else 0,
            throttles=self.held,
            input_frame=input_frame,
            input_throttles=self.last_held,
            gravity=GRAVITY_SCALE,
        )


def respawned(bot: Bot, entity: int, records_from: int) -> Bot:
    """The bot made again at its spawner under a new entity id, its records from `records_from` on."""
    fresh = Bot(entity, bot.spawner, bot.hero_data, bot.hints)
    fresh.records_from = records_from
    return fresh


def make(map_guid: int) -> list[Bot]:
    """The bots of a map: one per AI spawner that needs no mode identifier (the Practice Range's need
    none; the Tutorial's do)."""
    data = table()
    entry = data["maps"].get(f"0x{map_guid:016X}")
    if entry is None:
        return []
    bots = []
    for spawner in entry["spawners"]:
        hero = data["heroes"].get(spawner["hero"])
        if hero is None or spawner["identifier"] is not None:
            continue
        bots.append(Bot(BOT_ENTITY + len(bots), spawner, hero, entry["hints"]))
    return bots
