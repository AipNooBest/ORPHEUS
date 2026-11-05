"""Moves a player's body from its commands the way the client's own mover does, on the map's collision, so
other players see the body where its owner's client has it.

The client never sends its position, and it moves no body but its own, so the server runs the
same mover for every player. This is the PC client's character mover (image base 0x7FF788F10000), one tick per
command frame (dt = 16 ms), main tick 0x7FF789B4B1D0:
 1. the command; the body turns to its yaw (0x7FF789B4C6A0), the pitch is held to +-89 degrees (that sets
    flag 0x200000 for good).
 2. a held crouch button does not crouch again within 0.15 s of standing up (0x7FF789B73CA0).
 3. the ground probe (0x7FF789B48990), at the feet before anything moves: a ray from the capsule's lower
    sphere centre (feet + pogo + spring offset + radius) down to the feet, or on the ground to step_down
    below them (more when fast: tan(max slope) * dt * min(|v|, top stance speed) * 1.1). Ground is a hit
    whose normal is within the max slope (and whose surface has no NOT_GROUND bit). If the centre misses,
    a disc of ground_circle_rays points round ground_circle_radius is swept along the same segment through
    the engine's shapes (contacts.scene_sweep); a mesh hit takes the face normal of the nearest triangle,
    a polytope hit five short rays round it, and the hit's plane is met under the feet. The sweep's point
    is 1 cm out of the surface, so feet held by the disc stand 1 cm up, as the client's. No ground
    without gravity.
 4. transitions (0x7FF789B4F780): in the air, ground and v.n < 0.004 lands, else the air ticks count up;
    on the ground, no ground is a fall. The jump button jumps when on the ground without the latch:
    the vertical speed becomes jump_speed (0x7FF789B48880), flags 0x4 | 0x400 | 0x1. The latch clears on
    the ground once the button is up. The crouch button crouches on the ground (0x7FF789B4C3D0) if the
    crouched capsule fits, and standing up again needs the standing capsule to fit.
 5. the move context (0x7FF789B49FA0): the ground normal (or up), the horizontal velocity, the axes of the
    yaw (forward (sin yaw, 0, cos yaw), right (-cos yaw, 0, sin yaw)), the throttles s8 / 127.
 6. the pogo spring (0x7FF789B4F110): on the ground the feet snap to the ground; the capsule does not
    follow at once but springs to pogo above the feet (an implicit damped spring, pogo_frequency and
    pogo_damping; offsets within 1/512 m rest). A second offset starts at the first one's at a crouch or
    a stand and wears off by as much as the first moves. In float32, as the client.
 7. velocity: friction (0x7FF789B43090; in the air with the air values, 0x7FF789B416A0, plus a quadratic
    drag toward terminal_velocity while falling), acceleration toward the throttles' wish (0x7FF789B434A0),
    the speed cap of the direction (0x7FF789B44090), gravity -10 * gravity_scale in the air (0x7FF789B42100).
 8. the move (0x7FF789B4D9C0): on the ground the velocity follows the ground plane at its horizontal
    speed; the physics engine moves the capsule by (velocity + spring speed) * dt (contacts.py: its
    contacts with the map's shapes, rounds of Gauss-Seidel over their planes and sweeps, all float32);
    the feet follow the capsule; the velocity loses what goes into the contact planes (0x7FF789CCAD50)
    and stays horizontal on the ground.
 9. gravity's part of the velocity (state +832, 0x7FF789B4F530); positions and velocities on the 1/1024 m
    grid, the spring offsets as 1/32768 steps of their range (0x7FF789B745E0).
The hero's values are its body's STUCharacterMoverComponent (data/game_movers_174.json), changed each tick
by the movement mods the body's statescript has on (0x7FF789B4CBA0, see `modded`): Soldier: 76's Sprint
adds 0.5 to the speed scalar (run speeds x 1.5) and sets the crouch intent to 0. Not modelled: mods of
heroes whose statescript the server does not run, the throttle mods (17-19), velocity mods (STU_FF3471E9
states) and knockback, abilities that take over the move (the tick's phase hooks), moving platforms,
other bodies.
The capsule rests 5 mm out of a wall (the engine's contact skin), not touching it.

Besides the pose it keeps what other clients animate the body from, as the client's own mover writes it
into a movement state (0x7FF789B4DA26): this command's throttles, and the throttles and tick of the last
command that had any; and for each command frame the state the owner's client predicted (`history`),
field for field as the client compares it with the server's (0x7FF789B73E00, see correction.py).
"""

import json
import math
import struct
from collections import deque
from dataclasses import dataclass, fields, replace
from functools import cache

from ow174.game import contacts
from ow174.game.collision import MOVER, NOT_GROUND, Ground, World, flat_world
from ow174.game.commands import CROUCH, JUMP, Command
from ow174.paths import DATA_DIR

MOVERS_PATH = DATA_DIR / "game_movers_174.json"  # tools/extract_game_movers.py
SOLDIER_BODY = 0x04000000000003CF

RUN_SPEED = 5.5  # m/s, most heroes
CROUCH_SPEED = 3.0
JUMP_SPEED = 6.0  # m/s up at the take-off, every hero
GRAVITY_SCALE = 1.75  # every hero's; the world's gravity is 10 m/s^2 (0x7FF78AA63BD0)
WORLD_GRAVITY = 10.0
TURN = 2 * math.pi / 65536


_FLOAT = struct.Struct("<f")


def f32(value: float) -> float:
    """The float32 nearest to a value, as the client's arithmetic keeps it."""
    return _FLOAT.unpack(_FLOAT.pack(value))[0]


# The pitch (0x7FF789B4C6A0, 0x7FF789B763B0): the command's s16 in radians (dword_7FF78B524F6C), held to
# +-89 degrees (dword_7FF78B523F94), back to s16 units (dword_7FF78B524F70) rounded half away from zero.
PITCH_RADIANS = f32(9.58738019e-05)
PITCH_LIMIT = f32(1.55334306)
PITCH_UNITS = f32(10430.3779)
THROTTLE_UNIT = f32(1.0 / 127)  # dword_7FF78B523B58
TWO_PI = f32(6.28318548)  # dword_7FF78B51FCAC


def turned_pitch(units: int) -> tuple[int, bool]:
    """(the state's pitch, whether it was held to the limit) of a command's pitch."""
    radians = f32(units * PITCH_RADIANS)
    held = max(-PITCH_LIMIT, min(PITCH_LIMIT, radians))
    scaled = f32(held * PITCH_UNITS)
    return int(f32(scaled - 0.5) if scaled < 0.0 else f32(scaled + 0.5)), held != radians


def throttle_byte(value: int) -> tuple[float, int]:
    """(the throttle the move uses, the s8 the state keeps) of a command's s8 (0x7FF789B49FA0,
    0x7FF789B4DA30): the state keeps trunc(t * 127) in float32, which for 16 values is one less in size
    (9 -> 8, 13 -> 12, ... 104 -> 103)."""
    throttle = f32(max(-127, min(127, value)) * THROTTLE_UNIT)
    return throttle, max(-127, min(127, int(f32(throttle * 127.0))))


@cache
def disc_points(count: int, radius: float) -> tuple:
    """The probe's ground disc (0x7FF789B49610): `count` points round a circle of `radius`, 0.015 high,
    the first at +z, each next turned by the float32 sine and cosine of 2 pi / count (the client's sinf and
    cosf give the same, checked for 1..64), then the centre."""
    step = f32(TWO_PI / f32(count))
    sine, cosine = f32(math.sin(step)), f32(math.cos(step))
    s, c = 0.0, 1.0
    out = []
    for _ in range(count):
        out.append((f32(s * radius), DISC_RADIUS, f32(c * radius)))
        s, c = f32(f32(c * sine) + f32(s * cosine)), f32(f32(c * cosine) - f32(s * sine))
    out.append((0.0, 0.0, 0.0))
    return tuple(out)


def flat_unit(x: float, z: float):
    """(x, z) of the horizontal vector (x, 0, z) made unit as 0x7FF789503B30 does it (rsqrtps refined as
    (3 - r r l) r / 2), or None when it is too short."""
    length = f32(f32(x * x) + f32(z * z))
    if not length >= FLAT_TINY:
        return None
    r = contacts.rsqrt_estimate(length)
    k = f32(f32(3.0 - f32(f32(r * r) * length)) * f32(0.5 * r))
    return f32(x * k), f32(z * k)


def plane_height(start, end, point, normal) -> float:
    """Where the probe's segment meets the plane of its hit (0x7FF789B49B5A..0x7FF789B49BB7): the height at
    the fraction (n.start - n.point) / -n.dir, 1 / -n.dir from rcpss refined once (0x7FF7894D08B0); the
    hit's own height when the segment runs along the plane or meets it beyond its ends."""
    direction = contacts.sub(end, start)
    across = -contacts.dot(direction, normal)
    if across >= PLANE_STEEP:
        r = contacts.recip_estimate(across)
        inverse = f32(f32(r + r) - f32(f32(r * r) * across))
        t = f32(inverse * f32(contacts.dot(normal, start) - contacts.dot(point, normal)))
        if 0.0 <= t <= 1.0:
            return f32(f32(t * direction[1]) + start[1])
    return point[1]


# The movement state's flags word, as the client's mover sets it.
JUMP_LATCH = 0x1  # set at the take-off, cleared on the ground once the jump button is up
CROUCHED = 0x2
JUMPED = 0x4  # in the air after a jump, until the landing (0x7FF789AD03B0)
FELL = 0x8  # in the air after walking off (0x7FF789AD0370)
REAL_JUMP = 0x400  # the last take-off was a jump
TOUCHING = 0x20000  # the last move touched something (0x7FF789ACF8AC)
NOT_GROUND_UNDER = 0x80000  # the probe met a surface that is not ground
TURN_HELD = 0x200000  # a yaw or pitch was ever held to its limits (0x7FF789B4C8C0; never cleared)
IN_AIR = 0x80000C

# Movement mods (mover list +0x6E0, 0x38-byte entries): the op on the value of their index
# (0x7FF789B42ED0) and the indexes this mover applies (0x7FF789B4CBA0, jump table at RVA 0xC3D920).
ADD, MULTIPLY, SET, MINIMUM, MAXIMUM, CLAMP = range(6)
SPEED, ACCEL_GROUND, ACCEL_AIR, ACCEL_TOUCHING, DECEL_GROUND, DECEL_GROUND_BASE, DECEL_GROUND_SPEED = range(7)
DECEL_AIR, DECEL_AIR_BASE, DECEL_AIR_SPEED, JUMP_SPEED_MOD, GRAVITY_MOD = range(7, 12)
TERMINAL, TERMINAL_UP, GROUND_ACCEL, GROUND_DECEL = 13, 14, 15, 16
JUMP_INTENT, CROUCH_INTENT, COLLISION_MOD, LAND_LIMIT_MOD, STEP_DOWN_MOD = 20, 21, 22, 36, 38
FLT_MAX, FLT_MIN = 3.4028234663852886e38, 1.1754943508222875e-38
# The statescript's MovementMod state (STU_316CFEF2) and its mod classes (STU_6810506D: m_A9561CA0 index,
# m_9B7A63EA value): class -> (op, value of an empty config var) (0x7FF789C4C1E0, by their type objects).
MOVEMENT_MOD = "STU_316CFEF2"
MOD_OPS = {
    "STU_4CB0950F": (ADD, 0.0),
    "STU_D8CC6B10": (MULTIPLY, 1.0),
    "STU_56DEE1BB": (SET, 0.0),
    "STU_C61711C8": (MINIMUM, FLT_MAX),
    "STU_F197CFBF": (MAXIMUM, FLT_MIN),
}

LAND_LIMIT = 0.004  # m/s: landing needs v.n below this (dword_7FF78B509D70)
CROUCH_AGAIN = 0.15  # s after standing up before a held crouch button crouches again (0x7FF78B567978)
PROBE_FACTOR = 1.1  # dword_7FF78B522754
CIRCLE_SCALE = 0.35  # the ground circle shrinks with the body scale down to this (dword_7FF78B522748)
DEGREE = f32(0.0174532924)  # xmmword_7FF78B51FC98
SLOPE_LIMIT = f32(1.57078445)  # dword_7FF78B566E98
# The ground disc (0x7FF789B49515) and what the probe does with its hit.
DISC_RADIUS = f32(0.0149999997)  # the disc's points: this radius, this high, this far inside the circle
RAY_SPREAD = f32(0.0199999996)  # the five rays round a polytope hit (xmmword_7FF78B523558)
RAY_NUDGE = f32(0.00499999989)  # moved this much in under the hit (xmmword_7FF78B56A120)
RAY_BELOW = f32(0.0399999991)  # down to this far below it (dword_7FF78B569480)
RAY_TURN = (f32(0.95105654), f32(0.309016973))  # sin, cos of 72 degrees (dword_7FF78B56A110, A108)
FACE_REACH = f32(0.0500000007)  # a mesh hit's triangles within this (dword_7FF78BC27AD4)
FACE_SLACK = f32(1.1920929e-05)  # dword_7FF78B50B054
PLANE_STEEP = f32(1.1920929e-04)  # the probe meets the hit's plane when -n.dir is at least this
FLAT_TINY = f32(1.1754944e-36)  # a horizontal direction this short squared has none (0x7FF789503B30)
SPRING_REST = 1.0 / 512  # dword_7FF78B56A100
STEP = 1.0 / 1024  # positions and velocities (0x7FF789B77730)
SPRING_STEPS = 32768  # the spring offset's grid within its range (0x7FF789B77550)
EPSILON = 1.1754943508222875e-36  # the client's "is it zero" for squared lengths
TINY = 1.1920929e-07
CAP_TINY = 1.1920929e-05
ROUNDS = 20  # outer rounds of the physics move (0x7FF789B4B683)
SHAPE_SMALLEST = f32(0.0199999996)  # the capsule's radius is at least this (xmmword_7FF78B523558)
SEGMENT_PART = f32(0.100000001)  # its segment at least this part of the radius (dword_7FF78B506020)
ABYSS = 100.0  # m below the map's lowest surface where a falling body stops (positions stay encodable)
REPEAT = 32  # missing command frames run with the last command; as many again without its keys


@dataclass(frozen=True)
class MoverData:
    """A body's STUCharacterMoverComponent (the client keeps it at mover +0x418 + the field's offset)."""

    stand_height: float = 2.0
    stand_radius: float = 0.5
    stand_pogo: float = 0.5
    crouch_height: float = 1.25
    crouch_radius: float = 0.5
    crouch_pogo: float = 0.25
    ground_circle_radius: float = 0.45
    ground_circle_rays: int = 8
    pogo_frequency: float = 10.0
    pogo_damping: float = 1.0
    max_slope: float = 45.0  # degrees
    step_down: float = 0.5
    run_forward: float = 5.5
    run_backward: float = 4.95
    run_strafe: float = 5.5
    crouch_forward: float = 3.0
    crouch_backward: float = 3.0
    crouch_strafe: float = 3.0
    accel_ground: float = 35.0  # times run_forward, m/s^2
    accel_air: float = 3.5
    accel_air_touching: float = 0.5
    decel_ground_base: float = 15.0  # times run_forward, m/s^2
    decel_ground_base_speed: float = 6.2
    decel_ground_per_speed: float = 2.0
    decel_air_base: float = 0.0
    decel_air_base_speed: float = 5.5
    decel_air_per_speed: float = 0.35
    jump_speed: float = 6.0
    gravity_scale: float = 1.75
    terminal_velocity: float = 30.0
    terminal_velocity_up: float = 0.0
    ground_gravity_accel: float = 0.0
    ground_gravity_decel: float = 0.0
    character_collision: int = 1


@cache
def _movers() -> dict[int, MoverData]:
    if not MOVERS_PATH.is_file():
        return {}
    names = {field.name for field in fields(MoverData)}
    table = json.loads(MOVERS_PATH.read_text(encoding="utf-8"))
    return {
        int(body, 16): MoverData(**{key: value for key, value in entry.items() if key in names})
        for body, entry in table.items()
    }


def mover_data(body: int | None) -> MoverData:
    """The body's mover values; Soldier: 76's (most heroes') when the data does not know it."""
    movers = _movers()
    return movers.get(body or 0) or movers.get(SOLDIER_BODY) or MoverData()


def quantise(value: float) -> float:
    """The client's 1/1024 m grid: floor(x * 1024 + 0.5) / 1024."""
    return math.floor(value * 1024.0 + 0.5) * STEP


@dataclass
class Mod:
    """A movement mod of the mover: index, op and value act on the ticks of command frames start..end - 1
    (end None: until the mod goes; 0x7FF789B42FFB). The mover keeps them sorted by priority, then start
    (0x7FF789B3FAA0), and applies them in that order."""

    index: int
    op: int
    value: float
    start: int
    end: int | None = None
    priority: float = 0.0
    key: tuple = ()

    def acts(self, frame: int) -> bool:
        return self.start <= frame and (self.end is None or frame < self.end)

    def order(self) -> tuple:
        return self.priority, self.start, self.key


def apply_op(op: int, value: float, operand: float) -> float:
    """One mod on a value (0x7FF789B42ED0): add, multiply, set, min, max (the clamp takes two operands)."""
    if op == ADD:
        return f32(value + operand)
    if op == MULTIPLY:
        return f32(value * operand)
    if op == SET:
        return operand
    if op == MINIMUM:
        return min(value, operand)
    if op == MAXIMUM:
        return max(value, operand)
    return value


def modded(data: MoverData, mods: list[Mod]) -> MoverData:
    """The values of a tick: the body's own changed by its mods (0x7FF789B4CBA0). Each index starts as a
    scalar of 1 (or the value it replaces: jump speed, terminal velocities, ground gravity, character
    collision); the mods act on it in their order; then the speeds, accelerations and decelerations are
    the body's times their scalars, most clamped to 0 or more first, as the client multiplies them."""
    if not mods:
        return data
    slots = dict.fromkeys(range(10), 1.0)
    slots.update({JUMP_SPEED_MOD: data.jump_speed, GRAVITY_MOD: 1.0, TERMINAL: data.terminal_velocity,
                  TERMINAL_UP: data.terminal_velocity_up, GROUND_ACCEL: data.ground_gravity_accel,
                  GROUND_DECEL: data.ground_gravity_decel, COLLISION_MOD: float(data.character_collision),
                  STEP_DOWN_MOD: 1.0})  # fmt: skip
    for mod in mods:
        if mod.index in slots:
            slots[mod.index] = apply_op(mod.op, slots[mod.index], mod.value)
    clamp = {index: max(0.0, slots[index]) for index in (0, 1, 2, 3, 4, 5, 7, 8)}
    speed = clamp[SPEED]
    return replace(
        data,
        run_forward=f32(data.run_forward * speed),
        run_backward=f32(data.run_backward * speed),
        run_strafe=f32(data.run_strafe * speed),
        crouch_forward=f32(data.crouch_forward * speed),
        crouch_backward=f32(data.crouch_backward * speed),
        crouch_strafe=f32(data.crouch_strafe * speed),
        accel_ground=f32(data.accel_ground * clamp[ACCEL_GROUND]),
        accel_air=f32(data.accel_air * clamp[ACCEL_AIR]),
        accel_air_touching=f32(data.accel_air_touching * clamp[ACCEL_TOUCHING]),
        decel_ground_base=f32(f32(clamp[DECEL_GROUND_BASE] * clamp[DECEL_GROUND]) * data.decel_ground_base),
        decel_ground_per_speed=f32(clamp[DECEL_GROUND] * data.decel_ground_per_speed),
        decel_ground_base_speed=f32(f32(speed * slots[DECEL_GROUND_SPEED]) * data.decel_ground_base_speed),
        decel_air_base=f32(f32(clamp[DECEL_AIR_BASE] * clamp[DECEL_AIR]) * data.decel_air_base),
        decel_air_per_speed=f32(clamp[DECEL_AIR] * data.decel_air_per_speed),
        decel_air_base_speed=f32(f32(speed * slots[DECEL_AIR_SPEED]) * data.decel_air_base_speed),
        jump_speed=max(0.0, slots[JUMP_SPEED_MOD]),
        gravity_scale=f32(slots[GRAVITY_MOD] * data.gravity_scale),
        terminal_velocity=max(0.0, slots[TERMINAL]),
        terminal_velocity_up=max(0.0, slots[TERMINAL_UP]),
        ground_gravity_accel=slots[GROUND_ACCEL],
        ground_gravity_decel=slots[GROUND_DECEL],
        character_collision=int(slots[COLLISION_MOD] > CAP_TINY),
        step_down=f32(data.step_down * max(0.0, slots[STEP_DOWN_MOD])),
    )


def intent(mods: list[Mod], index: int, value: float) -> float:
    """A value the transitions read through the mods of its index (0x7FF789B42FB0): the jump and crouch
    intents (1 with the button, 0 without) and the landing's speed limit."""
    for mod in mods:
        if mod.index == index:
            value = apply_op(mod.op, value, mod.value)
    return value


def _number(instance, cfg, default: float) -> float:
    if cfg is None:
        return default
    value = instance.evaluate(cfg)
    return f32(float(value)) if isinstance(value, (bool, int, float)) else default


def statescript_mods(component) -> dict:
    """The movement mods of a body's statescript (a script.runtime.Component) that are on now:
    {(instance, state, slot): (index, op, value, priority)}. The client gives the mover each mod of a
    MovementMod state while it is on (0x7FF789C49590 begin, 0x7FF789C4C1E0 tick, 0x7FF789C499C0 end)."""
    found = {}
    for instance in component.instances.values():
        for state in instance.states.values():
            if not state.active or state.node.cls != MOVEMENT_MOD:
                continue
            for slot, mod in enumerate(state.node.fields.get("m_BC5E91CF") or ()):
                kind = MOD_OPS.get(mod.get("$")) if isinstance(mod, dict) else None
                if kind is None or mod.get("m_A9561CA0") is None:
                    continue
                op, empty = kind
                value = _number(instance, mod.get("m_9B7A63EA"), empty)
                priority = _number(instance, mod.get("m_priority"), 0.0)
                found[(instance.id, state.index, slot)] = (int(mod["m_A9561CA0"]), op, value, priority)
    return found


@dataclass(frozen=True)
class Snapshot:
    """The body's movement state after the tick of a command frame, as the owner's client predicts it."""

    frame: int
    position: tuple
    velocity: tuple
    yaw: int
    pitch: int
    flags: int
    air_ticks: int
    gravity: float
    throttles: tuple
    input_frame: int | None  # the command frame of the last command with throttles; None: never
    input_throttles: tuple
    crouch_end: int | None  # the command frame the body last stood up in (state +40); None: never
    spring: tuple  # the two spring offsets as 1/32768 steps of their range (state +52, +56)
    spring_speed: float  # state +60
    fall: float  # the up part of state +832


HISTORY = 64  # command frames of snapshots kept (the client keeps 64 predicted states)


class Mover:
    def __init__(
        self,
        position,
        yaw: int = 0,
        world: World | None = None,
        data: MoverData | None = None,
        category: int = MOVER,
    ) -> None:
        self.data = data or mover_data(None)
        self.values = self.data  # the data changed by the mods of the tick being run
        self.position = [quantise(axis) for axis in position]  # the feet
        self.on_map = world is not None  # else a flat floor at the start height, until use() gives the map
        self.world = world if world is not None else flat_world(self.position[1])
        self.category = category  # the query category: the team's proxy bit
        self.velocity = (0.0, 0.0, 0.0)
        self.yaw = int(yaw)
        self.pitch = 0  # the aim pitch (state +66), held to +-89 degrees
        self.flags_word = 0
        self.air_ticks = 0
        self.spring = 0.0  # the capsule's offset above its rest (pogo) height over the feet (state +44)
        self.spring2 = 0.0  # a second offset: the first's at a crouch or stand, wearing off (state +48)
        self.spring_steps = (0, 0)  # both as 1/32768 steps of their range (state +52, +56)
        self.spring_speed = 0.0
        self.fall = 0.0  # the speed gravity gave since the take-off (state +832, up only: see _fall_after)
        self.landed = False  # landed this tick (mover +0x14DD)
        self.crouch_end: int | None = None  # the command frame the body last stood up in (state +40)
        self.throttles = (0, 0)  # this command's (right, forward)
        self.input_throttles = (0, 0)  # those of the last command that had any
        self.input_tick: int | None = None  # the tick that command was applied in; None: never
        self.input_frame: int | None = None  # its command frame
        self.last_command: Command | None = None
        self.mods: dict = {}  # the statescript's movement mods by (instance, state, slot)
        self.history: deque[Snapshot] = deque(maxlen=HISTORY)
        self.corrected: int | None = None  # the newest frame whose state went to the owner (correction.py)
        self.body: contacts.CharacterBody | None = None  # the physics engine's body, made when first needed
        self._limits()

    def _limits(self) -> None:
        data = self.data
        self.slope = min(max(math.radians(data.max_slope), 0.0), 1.5707845)
        self.slope_cos = math.cos(self.slope)
        self.slope_tan = math.tan(self.slope)
        # The probe's own float32 cosine (0x7FF789ACEE40, cosf): the disc's ground test.
        self.slope_cos32 = f32(math.cos(max(min(f32(data.max_slope * DEGREE), SLOPE_LIMIT), 0.0)))
        # The spring offset's range (mover +0x4F8 / +0x4FC, set at 0x7FF789AD0BD1).
        lowest = max(data.stand_height + data.stand_pogo, data.crouch_height + data.crouch_pogo)
        self.spring_low = f32(-lowest)
        self.spring_high = f32(2.0 * data.step_down)

    def use(self, data: MoverData | None = None, world: World | None = None) -> None:
        """Take another hero's values (a hero switch), or the map's collision once it is loaded."""
        if data is not None and data is not self.data:
            self.data = self.values = data
            self._limits()
            self.body = None
        if world is not None and world is not self.world:
            self.world = world
            self.on_map = True
            self.body = None

    def track_mods(self, active: dict, frame: int) -> None:
        """Follow the statescript's movement mods after it ran command frame `frame` (`statescript_mods`):
        a mod that came on acts from the next frame on, one that went off acts up to this frame. The
        client starts a mod at the statescript's frame + 1 (0x7FF789C4C510..0x7FF789C4C52A) and ends it at
        the frame + 1 it went off in (0x7FF789C49CE1, 0x7FF789B456C0)."""
        for key, (index, op, value, priority) in active.items():
            mod = self.mods.get(key)
            if mod is None or mod.end is not None:
                self.mods[key] = Mod(index, op, value, frame + 1, None, priority, key)
            else:
                mod.value, mod.priority = value, priority
        for key, mod in list(self.mods.items()):
            if key not in active and mod.end is None:
                mod.end = frame + 1
            elif mod.end is not None and mod.end <= frame:
                del self.mods[key]  # the mover's next tick is frame + 1

    # ---- what the movement records and the match read

    @property
    def flags(self) -> int:
        return self.flags_word

    @property
    def crouched(self) -> bool:
        return bool(self.flags_word & CROUCHED)

    @property
    def airborne(self) -> bool:
        return bool(self.flags_word & IN_AIR)

    @property
    def gravity_scale(self) -> float:
        """The gravity scale of the last tick (state +24, written every tick: 0x7FF789B4D869)."""
        return self.values.gravity_scale

    def snapshot(self, frame: int) -> Snapshot:
        return Snapshot(
            frame,
            tuple(self.position),
            self.velocity,
            self.yaw,
            self.pitch,
            self.flags_word,
            self.air_ticks,
            self.values.gravity_scale,
            self.throttles,
            self.input_frame,
            self.input_throttles,
            self.crouch_end,
            self.spring_steps,
            self.spring_speed,
            self.fall,
        )

    # ---- the body's shape

    def _pogo(self, crouched: bool | None = None) -> float:
        crouched = self.crouched if crouched is None else crouched
        return self.data.crouch_pogo if crouched else self.data.stand_pogo

    def _capsule(self, crouched: bool | None = None) -> tuple[float, float]:
        """(radius, segment between the sphere centres) of a stance, float32 as the client sets the shape
        (0x7FF789B4C3D0, 0x7FF789B45822; body scale 1): r = max(0.02, radius), segment = max(0.1 r,
        height - (pogo + 2 radius))."""
        data = self.data
        crouched = self.crouched if crouched is None else crouched
        radius = f32(data.crouch_radius if crouched else data.stand_radius)
        height = f32(data.crouch_height if crouched else data.stand_height)
        pogo = f32(data.crouch_pogo if crouched else data.stand_pogo)
        r = SHAPE_SMALLEST if radius < SHAPE_SMALLEST else radius
        rest = f32(height - f32(pogo + f32(radius + radius)))
        part = f32(r * SEGMENT_PART)
        return r, (part if part > rest else rest)

    def _lift(self) -> float:
        """The capsule's lowest point above the feet: pogo * min(scale, 1) + the spring offset (float32)."""
        return f32(f32(self._pogo()) + self.spring)

    def _engine_body(self) -> contacts.CharacterBody:
        """The physics engine's body of this mover, made at the feet + the lift when first needed."""
        if self.body is None:
            radius, segment = self._capsule()
            body = contacts.CharacterBody(radius, f32(segment + radius), world=self.world.engine,
                                          category=self.category)  # fmt: skip
            x, y, z = self.position
            body.teleport((x, f32(y + self._lift()), z))
            self.body = body
        return self.body

    # ---- the tick

    def step(self, command: Command, seconds: float, tick: int = 0) -> None:
        """One tick per command frame. Frames that never arrived are run first: the client ran them, most
        likely with the same keys (the last command again, and from the 33rd
        missing one without throttles and buttons)."""
        last = self.last_command
        if last is not None and 1 < command.frame - last.frame <= 2 * REPEAT:
            for frame in range(last.frame + 1, command.frame):
                if frame - last.frame > REPEAT:
                    last = Command(last.frame, yaw=last.yaw, pitch=last.pitch)
                self._tick(replace(last, frame=frame), seconds, tick)
        self.last_command = command
        self._tick(command, seconds, tick)

    def _tick(self, command: Command, seconds: float, tick: int) -> None:
        dt = seconds
        frame = command.frame
        active = sorted((mod for mod in self.mods.values() if mod.acts(frame)), key=Mod.order)
        self.values = data = modded(self.data, active)
        self.yaw = command.yaw
        self.pitch, held = turned_pitch(command.pitch)
        if held:
            self.flags_word |= TURN_HELD
        right, right_byte = throttle_byte(command.right)
        forward, forward_byte = throttle_byte(command.forward)
        jump = intent(active, JUMP_INTENT, 1.0 if command.buttons & JUMP else 0.0) > 0.5
        crouch = bool(command.buttons & CROUCH)
        stood_up = self.crouch_end
        if crouch and not self.crouched and stood_up is not None:
            crouch = (frame - stood_up) * dt >= CROUCH_AGAIN
        y0 = self.position[1]
        start = (y0, self._pogo(), self.spring)
        was_crouched = self.crouched
        self.landed = False

        ground, under = self._probe(dt)
        self._transitions(ground, under, jump, crouch, active)
        if was_crouched and not self.crouched:
            self.crouch_end = frame

        # The move context.
        grounded = not self.flags_word & IN_AIR
        normal = ground.normal if (grounded and ground is not None) else (0.0, 1.0, 0.0)
        vx, vy, vz = self.velocity
        horizontal = (vx, 0.0, vz)
        angle = self.yaw * TURN
        axes = (math.sin(angle), math.cos(angle))  # forward (x, z); right is (-cos, sin)

        self._spring(f32(dt), ground if grounded else None, start)
        self._friction(dt, horizontal, vy, grounded)
        self._accelerate(dt, right, forward, axes, grounded)
        self._cap(horizontal, axes)
        if not grounded:
            vx, vy, vz = self.velocity
            self.velocity = (vx, vy - WORLD_GRAVITY * data.gravity_scale * dt, vz)
            self.fall = f32(self.fall + f32(f32(-WORLD_GRAVITY * data.gravity_scale) * f32(dt)))

        self.throttles = (right_byte, forward_byte)
        if right_byte or forward_byte:
            self.input_throttles = self.throttles
            self.input_tick = tick
            self.input_frame = frame
        self._fall_after(self._move(dt, normal))
        self.history.append(self.snapshot(frame))

    def _fall_after(self, vertical: float) -> None:
        """The state's +832 after the move (0x7FF789B4F530): gravity's part of the velocity, added up in the
        air (0x7FF789B42133), 0 at the landing (where the client hands it to the body: vt+0x88) and while
        going up, never more than the fall speed. The client normalises it with rsqrtps, so in the air it
        can differ from ours in the last bits (and between CPUs)."""
        fall = self.fall
        if abs(fall) <= TINY:
            return
        if self.landed:
            self.fall = 0.0
        elif self.flags_word & IN_AIR:
            if f32(-WORLD_GRAVITY * self.values.gravity_scale) * vertical < 0.0:
                self.fall = 0.0  # going up
            elif vertical * math.copysign(1.0, fall) < abs(fall):
                self.fall = f32(vertical)

    # ---- 3. the ground probe

    def _probe(self, dt: float):
        """(ground, under): the ground the feet stand on or land on (None if none), and whether the probe
        met a surface that is not ground (flag 0x80000)."""
        data = self.values
        x, y, z = self.position
        radius, _ = self._capsule()
        top = y + self._pogo() + self.spring + radius
        if self.flags_word & IN_AIR:
            reach = 0.0
        else:
            vx, vy, vz = self.velocity
            speed = min(math.sqrt(vx * vx + vy * vy + vz * vz), self._top_speed())
            reach = max(data.step_down, self.slope_tan * dt * speed * PROBE_FACTOR)
        bottom = y - reach
        hit = self.world.ground(x, z, top, bottom, self.category)
        under = bool(hit is not None and hit.surface & NOT_GROUND)
        if data.gravity_scale <= 0.0:
            return None, under
        if hit is not None and hit.normal[1] > self.slope_cos and not hit.surface & NOT_GROUND:
            return hit, under
        if data.ground_circle_rays <= 0:
            return None, under
        # The disc swept from the ray's start to its end (0x7FF789B4953D), in float32 as the client: the
        # start at the capsule's lower sphere centre (the body's height over the feet + the radius).
        start = (x, f32(y + f32(radius + f32(f32(y + self._lift()) - y))), z)
        end = (x, f32(y - f32(reach)), z)
        rim = f32(f32(max(1.0, CIRCLE_SCALE) * f32(data.ground_circle_radius)) - DISC_RADIUS)
        points = disc_points(data.ground_circle_rays, rim)
        found = contacts.scene_sweep(self.world.engine, start, end, points, DISC_RADIUS, self.category)
        if found is None:
            return None, under
        _fraction, point, normal, shape, number = found
        surface = shape.geometry.surfaces[number] if number >= 0 else 0
        if shape.kind == contacts.MESH:
            face = self._face(shape, point)
            if face is None:
                return None, under
            normal, surface = face
        elif shape.kind == contacts.HULL:
            near = self._rays(start, point, normal)
            if near is None:
                return None, under
            point, normal, surface = near
        under = under or bool(surface & NOT_GROUND)
        if surface & NOT_GROUND or not normal[1] > self.slope_cos32:
            return None, under
        return Ground.at((x, plane_height(start, end, point, normal), z), normal, surface), under

    def _face(self, shape, point):
        """The ground where the disc swept into a mesh (0x7FF789B506E0): the mesh's triangles within 0.05 of
        the hit (a box query, then each one's nearest point), the nearest by squared distance with 1.19e-5
        of slack toward ground: a later triangle takes over when it is no farther + the slack while the last
        one was no ground, or nearer - the slack while it was. (face normal, surface) when that one is
        ground, else None. The hit point stays (1 cm out of the surface: the cast's), and the face normal
        stays in the mesh's frame (as the client: a turned prop's is not turned back)."""
        mesh = shape.geometry
        if shape.frame is None:
            local, up = point, (0.0, 1.0, 0.0)
        else:
            t, q = shape.frame
            local, up = contacts.unrotate(q, contacts.sub(point, t)), contacts.unrotate(q, (0.0, 1.0, 0.0))
        r = FACE_REACH
        low = contacts.v3(local[0] - r, local[1] - r, local[2] - r)
        high = contacts.v3(local[0] + r, local[1] + r, local[2] + r)
        limit = f32(f32(r * r) * 3.0)
        found = None
        for number in mesh.query(low, high, self.category):
            if not mesh.masks[number] & MOVER:
                continue
            a, b, c = mesh.triangle(number).corners
            d = contacts.sub(contacts.closest_on_triangle(a, b, c, local), local)
            distance = contacts.dot(d, d)
            if distance > f32(limit - FACE_SLACK if found is not None else limit + FACE_SLACK):
                continue
            limit = distance
            surface = mesh.surfaces[number]
            found = None
            if not surface & NOT_GROUND:
                n = contacts.cross(contacts.sub(b, a), contacts.sub(c, a))
                length = contacts.dot(n, n)
                if length > contacts.TINY:
                    n = contacts.scale(n, contacts.rsqrt(length))
                    if contacts.dot(n, up) > self.slope_cos32:
                        found = (n, surface)
        return found

    def _rays(self, start, point, normal):
        """The ground where the disc swept into a polytope (0x7FF789B498AC): five vertical rays 0.02 m round
        the hit, 72 degrees apart starting from the hit's direction from the disc's centre, moved 5 mm in
        under the hit, from the disc's start height down to 0.04 below the hit; the first that finds ground,
        else the first that finds anything: ((x, y, z), normal, surface) or None. Unverified: the rays are
        World.ground's (the client casts them through the engine's shapes, like its centre ray)."""
        hx, hy, hz = point
        dx, dz = flat_unit(f32(hx - start[0]), f32(hz - start[2])) or (0.0, 1.0)
        nudge = flat_unit(-normal[0], -normal[2]) or (dx, dz)
        nx, nz = f32(nudge[0] * RAY_NUDGE), f32(nudge[1] * RAY_NUDGE)
        sine, cosine = RAY_TURN
        bottom = f32(hy - RAY_BELOW)
        found = None
        for _ in range(5):
            x = f32(hx + f32(f32(dx * RAY_SPREAD) + nx))
            z = f32(hz + f32(f32(dz * RAY_SPREAD) + nz))
            hit = self.world.ground(x, z, start[1], bottom, self.category)
            if hit is not None:
                ground = not hit.surface & NOT_GROUND and hit.normal[1] > self.slope_cos32
                if found is None or ground:
                    found = ((x, hit.y, z), hit.normal, hit.surface)
                    if ground:
                        break
            dx, dz = f32(f32(dz * sine) + f32(dx * cosine)), f32(f32(dz * cosine) - f32(dx * sine))
        return found

    def _top_speed(self) -> float:
        data = self.values
        if self.crouched:
            return max(data.crouch_forward, data.crouch_backward, data.crouch_strafe)
        return max(data.run_forward, data.run_backward, data.run_strafe)

    # ---- 4. land, fall, jump, crouch

    def _transitions(self, ground, under: bool, jump: bool, crouch: bool, active=()) -> None:
        flags = self.flags_word
        if flags & IN_AIR:
            vx, vy, vz = self.velocity
            nx, ny, nz = ground.normal if ground is not None else (0.0, 0.0, 0.0)
            limit = intent(active, LAND_LIMIT_MOD, LAND_LIMIT) if active else LAND_LIMIT
            if ground is not None and vx * nx + vy * ny + vz * nz < limit:
                if flags & (JUMPED | FELL):  # 0x7FF789AD0400
                    flags &= ~(JUMPED | FELL)
                    self.air_ticks = 0
                    self.landed = True
            else:
                self.air_ticks += 1
        elif ground is None:  # 0x7FF789AD0370
            self.air_ticks = 0
            flags = flags & ~JUMPED | FELL
        flags = flags & ~NOT_GROUND_UNDER | (NOT_GROUND_UNDER if under else 0)
        if jump:
            if not flags & (IN_AIR | JUMP_LATCH):
                vx, vy, vz = self.velocity
                self.velocity = (vx, self.values.jump_speed, vz)  # 0x7FF789B48880 on the ground
                self.air_ticks = 0  # 0x7FF789AD03B0
                flags = flags & ~(FELL | REAL_JUMP) | JUMPED | REAL_JUMP | JUMP_LATCH
        elif not flags & IN_AIR:
            flags &= ~JUMP_LATCH
        self.flags_word = flags
        crouch = 1.0 if crouch and not flags & IN_AIR else 0.0
        self._stance((intent(active, CROUCH_INTENT, crouch) if active else crouch) > 0.5)

    def _stance(self, crouch: bool) -> None:
        """Crouch or stand up (0x7FF789B4C3D0) if the new capsule fits where the capsule is now (the engine's
        fit test, 0x7FF789CCAC10, then the body takes the new shape). The capsule does not move: the spring
        offset takes the difference of the pogo heights, and the second offset starts from there
        (0x7FF789B4C5DF)."""
        if crouch == self.crouched:
            return
        data = self.data
        body = self._engine_body()
        radius, segment = self._capsule(crouch)
        top = f32(segment + radius)
        if not body.fits(radius, top):
            return
        body.set_capsule(radius, top)
        self.flags_word = self.flags_word & ~CROUCHED | (CROUCHED if crouch else 0)
        difference = f32(data.crouch_pogo - data.stand_pogo)
        if difference < 0.0:
            if crouch:
                self.spring = max(-data.crouch_pogo, f32(self.spring - difference))
            else:
                self.spring = max(-data.stand_pogo, f32(difference + self.spring))
            self.spring2 = self.spring

    # ---- 6. the pogo spring

    def _spring(self, dt: float, ground, start) -> None:
        """In float32 as the client (dt too): the offsets end up in the movement state as u16 steps."""
        data = self.data
        if ground is not None:
            self.position[1] = ground.y  # the feet stand on the ground (0x7FF789AD0150)
        y0, pogo0, spring0 = start  # the tick start's feet and pogo vector (0x7FF789ACF060)
        rest = f32(f32(y0 + f32(pogo0 + spring0)) - f32(self.position[1]))
        offset = f32(rest - self._pogo())
        second = self.spring2
        if abs(offset) > SPRING_REST:
            omega = f32(data.pogo_frequency * TWO_PI)
            h = f32(omega * dt)
            pull = f32(f32(f32(omega * omega) * dt) * offset)
            damping = f32(f32(f32(f32(data.pogo_damping + data.pogo_damping) + h) * h) + 1.0)
            speed = f32(f32(self.spring_speed - pull) / damping)
            spring = f32(f32(speed * dt) + offset)
            moved = abs(f32(spring - self.spring))
            if abs(spring) <= SPRING_REST:
                spring = 0.0
                speed = -f32(offset / dt) if dt > TINY else 0.0
            # The second offset wears off by as much as the first moved (0x7FF789B4F31D).
            if second != 0.0:
                if moved > abs(second):
                    second = 0.0
                else:
                    second = f32(second - moved) if second > 0.0 else f32(second + moved)
        else:
            spring = second = speed = 0.0
        self.spring = min(max(spring, self.spring_low), self.spring_high)
        self.spring2 = min(max(second, self.spring_low), self.spring_high)
        self._spring_grid()
        self.spring_speed = quantise(speed)

    def _spring_grid(self) -> None:
        """Both offsets as the client stores them: u16 steps of 1/32768 of their range, and the offsets
        back from those (0x7FF789B74E40, 0x7FF789B77550)."""
        low = self.spring_low
        span = f32(self.spring_high - low)
        if span <= TINY:
            self.spring = self.spring2 = 0.0
            self.spring_steps = (0, 0)
            return
        inverse = f32(1.0 / span)
        steps = []
        for value in (self.spring, self.spring2):
            fraction = min(1.0, max(0.0, f32(f32(value - low) * inverse)))
            steps.append(math.floor(f32(fraction * SPRING_STEPS + 0.5)) & 0xFFFF)
        self.spring_steps = (steps[0], steps[1])
        self.spring, self.spring2 = (f32(f32(span * (step / SPRING_STEPS)) + low) for step in steps)

    # ---- 7. velocity

    def _friction(self, dt: float, horizontal, vertical: float, grounded: bool) -> None:
        """Slow the horizontal velocity the context had (0x7FF789B43090), and in the air the quadratic drag
        toward the terminal velocity while falling (0x7FF789B416A0). The base part goes with the run speed of
        the tick, the part per speed with the body's own (the PC path, cvar word_7FF78BD3EE20)."""
        data = self.values
        if grounded:
            base, base_speed = data.decel_ground_base, data.decel_ground_base_speed
            per_speed = data.decel_ground_per_speed
        else:
            base, base_speed = data.decel_air_base, data.decel_air_base_speed
            per_speed = data.decel_air_per_speed
        hx, _, hz = horizontal
        speed = math.sqrt(hx * hx + hz * hz)
        rate = base * data.run_forward + max(0.0, speed - base_speed) * per_speed * self.data.run_forward
        slow = min(speed, dt * rate)
        if speed * speed > EPSILON and slow > 0.0:
            vx, vy, vz = self.velocity
            self.velocity = (vx - hx / speed * slow, vy, vz - hz / speed * slow)
        if grounded:
            return
        vx, vy, vz = self.velocity
        terminal = data.terminal_velocity_up if vertical > 0.0 else data.terminal_velocity
        if terminal <= CAP_TINY:
            return
        change = -WORLD_GRAVITY * data.gravity_scale * abs(vertical) / (terminal * terminal) * vertical * dt
        length = math.sqrt(vx * vx + vy * vy + vz * vz)
        if abs(change) > length:
            change = math.copysign(length, change)
        self.velocity = (vx, vy + change, vz)

    def _wish(self, right: float, forward: float, axes) -> tuple[float, float, float]:
        """(direction x, direction z, speed) the throttles ask for (0x7FF789B44340)."""
        data = self.values
        crouched = self.crouched
        strafe = data.crouch_strafe if crouched else data.run_strafe
        if forward > 0.0:
            ahead = data.crouch_forward if crouched else data.run_forward
        else:
            ahead = data.crouch_backward if crouched else data.run_backward
        fx, fz = axes
        wx = right * strafe * -fz + forward * ahead * fx
        wz = right * strafe * fx + forward * ahead * fz
        speed = math.sqrt(wx * wx + wz * wz)
        if speed <= 0.0:
            return 0.0, 0.0, 0.0
        wx, wz = wx / speed, wz / speed
        return wx, wz, min(speed, self._cap_of(wx, wz, 1.0, axes))

    def _cap_of(self, dx: float, dz: float, length: float, axes) -> float:
        """The top speed in a direction: strafe speed, blended toward the forward or backward one by the
        cosine to the facing (0x7FF789ACEF40)."""
        data = self.values
        crouched = self.crouched
        strafe = data.crouch_strafe if crouched else data.run_strafe
        if length < CAP_TINY:
            return strafe
        fx, fz = axes
        cosine = (fx * dx + fz * dz) / length
        if cosine > 0.0:
            ahead = data.crouch_forward if crouched else data.run_forward
            return strafe + cosine * (ahead - strafe)
        back = data.crouch_backward if crouched else data.run_backward
        return strafe - cosine * (back - strafe)

    def _accelerate(self, dt: float, right: float, forward: float, axes, grounded: bool) -> None:
        """Turn the horizontal velocity toward the wish (0x7FF789B434A0)."""
        data = self.values
        wx, wz, wish = self._wish(right, forward, axes)
        if grounded:
            rate = data.accel_ground
        elif self.flags_word & TOUCHING:
            rate = data.accel_air_touching
        else:
            rate = data.accel_air
        rate *= data.run_forward
        vx, vy, vz = self.velocity
        squared = vx * vx + vz * vz
        speed = math.sqrt(squared) if squared >= EPSILON else -1.0
        target = max(speed, wish) if wish > 0.0 else wish
        if speed > 0.0:
            cx, cz = vx, vz
        else:
            cx, cz = -vx, -vz  # the client's sentinel speed of -1 times the (tiny) velocity
        dx, dz = target * wx - cx, target * wz - cz
        delta = math.sqrt(dx * dx + dz * dz)
        if delta * delta <= EPSILON or delta <= 0.0 or target <= 0.0:
            return
        k = min(1.0, max(0.0, rate * dt / delta))
        nx, nz = cx + k * dx, cz + k * dz
        if speed > TINY and wx * cx + wz * cz > 0.0:
            length = speed + min(rate * dt, target - speed)
            norm = math.sqrt(nx * nx + nz * nz)
            if norm > 0.0:
                nx, nz = nx / norm * length, nz / norm * length
        self.velocity = (vx + (nx - cx), vy, vz + (nz - cz))

    def _cap(self, horizontal, axes) -> None:
        """Hold the horizontal speed to the direction's top speed, or to what it was before this tick if
        that was more (0x7FF789B44090)."""
        vx, vy, vz = self.velocity
        squared = vx * vx + vz * vz
        speed = math.sqrt(squared) if squared > EPSILON else 0.0
        cap = self._cap_of(vx, vz, speed, axes)
        before = math.sqrt(horizontal[0] ** 2 + horizontal[2] ** 2)
        allowed = min(speed, max(cap, before))
        if abs(speed - allowed) > LAND_LIMIT and squared >= EPSILON:
            self.velocity = (vx / speed * allowed, vy, vz / speed * allowed)

    # ---- 8. the move

    def _move(self, dt: float, normal) -> float:
        """Move the capsule and the feet (0x7FF789B4D9C0, 0x7FF789ACF640); the vertical speed before it is put
        on the grid."""
        vx, vy, vz = self.velocity
        grounded = not self.flags_word & IN_AIR
        if grounded and vx * vx + vy * vy + vz * vz >= EPSILON:
            vx, vy, vz = _along_ground(vx, vy, vz, normal)
        body = self._engine_body()
        step = f32(dt)
        velocity = contacts.v3(vx, vy, vz)
        rise = f32(self.spring_speed)
        displacement = contacts.v3(  # ((up * spring speed) + v) * dt + the ground's motion (none: + 0)
            f32(f32(f32(0.0 * rise) + velocity[0]) * step) + 0.0,
            f32(f32(rise + velocity[1]) * step) + 0.0,
            f32(f32(f32(0.0 * rise) + velocity[2]) * step) + 0.0,
        )
        final, planes, touching = body.move(displacement, step, ROUNDS)
        self.flags_word = self.flags_word & ~TOUCHING | (TOUCHING if touching else 0)
        vx, vy, vz = contacts.clip_velocity(velocity, planes)
        if grounded:
            squared = vx * vx + vy * vy + vz * vz
            flat = vx * vx + vz * vz
            if squared >= EPSILON and flat >= EPSILON:
                scale = math.sqrt(squared / flat)
                vx, vy, vz = vx * scale, 0.0, vz * scale
        lift = self._lift()
        feet = f32(final[1] - lift)
        floor = self.world.lowest - ABYSS
        if feet < floor:  # fell out of the map: the client's player is dead by now (a mode's kill volume)
            feet, vy = floor, 0.0
        self.position = [quantise(final[0]), quantise(feet), quantise(final[2])]
        self.velocity = (quantise(vx), quantise(vy), quantise(vz))
        x, y, z = self.position
        body.teleport((x, f32(y + lift), z))  # the end of the tick (0x7FF789D11B60): on the grid
        return vy


def _along_ground(vx: float, vy: float, vz: float, normal) -> tuple[float, float, float]:
    """The ground velocity: the horizontal speed along the line where the ground plane meets the
    vertical plane of the velocity (0x7FF789B4DD81)."""
    nx, ny, nz = normal
    speed = math.sqrt(vx * vx + vz * vz)
    along = vx * nx + vz * nz  # the horizontal velocity onto the ground plane
    proj = (vx - nx * along, -ny * along, vz - nz * along)
    px, pz = -vz, vx  # horizontal, square to the velocity
    length = math.sqrt(px * px + pz * pz)
    if length * length < EPSILON or speed <= 0.0:
        return proj
    px, pz = px / length, pz / length
    # (px, 0, pz) x normal: in the ground plane and in the velocity's vertical plane.
    dx, dy, dz = -pz * ny, pz * nx - px * nz, px * ny
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if norm <= 0.0:
        return proj
    if dx * proj[0] + dy * proj[1] + dz * proj[2] < 0.0:
        norm = -norm
    return dx / norm * speed, dy / norm * speed, dz / norm * speed
