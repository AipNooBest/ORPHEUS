"""World frames: what the server sends in a kind-1 datagram after the message channel.

A frame, as the client reads it (0x7FF7896DE110):
    var_a packet frame
    ch0 the message channel (messages.py)
    ch3 var_a ack, 4 bits flags, 1 bit "ping follows"   the client drops its commands below ack - 1
    ch4 1 bit: 1 = keep, 0 resets the entity channel
    ch5 1 bit: the end of an empty list
    7 bits: how many entities follow, then per entity its id and one bit per channel in order; a 0
    ends the entity, a 1 runs that channel's reader, which has presence bits of its own.

The entity channel (ch4, reader 0x7FF78969BBC0) takes 2 bits op: 1 create, 2 update, 3 destroy; a
create or update carries 14 bits of length and that many bits.

A create (0x7FF78969C360, body 0x7FF78969DAB0):
    bit from map: 1 -> 32 bits placeable index, 1 bit transform override (0), then the update body
    else the 003 definition's GUID index, 8 bits flags,
         flags & 0x01: the skin: with 0x40 32 bits of a skin theme (0A6; bit 31 asks for a remap)
         and 1 bit for the theme's second (golden) weapon, else a skin (076) GUID index,
         flags & 0x08: 3 f32 position and a quaternion (x y z w); flags & 0x08 or 0x04: 3 f32 scale,
         flags & 0x02: a movement-state block, then the update body.
    Flag 0x10 is the player's own entity (the client adds its command components when 20302 named
    it), 0x20 adds component 74. The transform floats are thrown away on that path. The skin theme's
    overrides swap the body's models, looks and effects when the client loads it.

The update body (0x7FF78969E5D0 / 0x7FF78969B870): 8 bits count, then per component its index, a
pad to the next byte of the payload, a u8 mask length and the mask (one bit per replicated field in
order), then the values of the fields that are set. Scalars go as whole bytes; a u64 marked as an
id is a byte mask and its non-zero bytes.

The movement-state block (0x7FF789B6F680) is a delta against a reference state (mostly zeros, see
_input_fields). Its field order was read in the client; the command frame, the flags, the aim pitch, the
throttles and the last command that had any, the ticks in the air, the gravity scale, the frame of the
last stand-up, the Euler angles, the position, the velocity, the spring offsets and speed and gravity's
part of the velocity are sent here. The same block, after one bit (0 = against that reference
state), is a ch3 movement record, which is how a moving entity gets its position: the client only
interpolates between the records it gets, it never moves another player's body itself. A movement
state is only kept when its command frame is newer than the newest one the entity has (the gate at
0x7FF7896DA7D3).
"""

import math
import struct

from ow174.game import messages
from ow174.game.bits import BitWriter

OP_CREATE, OP_UPDATE, OP_DESTROY = 1, 2, 3

# Create flags.
WITH_SKIN = 0x01
WITH_MOVEMENT = 0x02
WITH_SCALE = 0x04
WITH_TRANSFORM = 0x08
PLAYER_PATH = 0x10
WITH_PARTICIPANT = 0x20
SKIN_THEME = 0x40  # the skin is a skin theme, not a skin

# Replicated fields of the components we send, in order (the client's type objects; checked
# for 26, 29, 44, 74, 75 and 123). "id" is a u64 sent as a byte mask, "dbid" a 16-byte id (low, high),
# "entity" 4 bytes, "blob:N" N raw bytes of a nested struct.
COMPONENT_FIELDS = {
    26: ["id"],  # MFilterBitsComponentData: filter bits (team, spectator)
    # MPlayerComponentData (field records at 0x7FF78BFEAE50, names from the PS4 data): m_lobbyPlayerID (the
    # key of the client's player cards, 20308), m_battleTag (the entity's name in the chat, 0x7FF789CB3F80),
    # m_playerLevel, then s32 audio locale, group token, party state and platform, m_rankedLevel (skill
    # rating), m_heroicRank (Top 500 place), m_rankedLevelTier (0-6), m_moderator. All flags 0: raw bytes.
    29: ["dbid", "string", "u32", "s32", "s32", "s32", "s32", "s16", "s16", "s8", "u8"],
    44: ["entity", "entity", "entity"],  # MECPossessorData: current, view target, primary possessable
    # The health component (type 0x7FF78BFE8720): 16 health, 16 armour and 16 shield pools (see pool),
    # then u32, u32, entity, u8, u8.
    51: ["blob:20"] * 48 + ["u32", "u32", "entity", "u8", "u8"],
    # MECGameModeParticipant: portrait frame (the client loads its asset, 0x7FF7894C9D90), s16, slot,
    # lfg role, u8, role queue role, can select a hero, u8, u8.
    74: ["id", "s16", "s8", "u8", "u8", "s8", "u8", "u8", "u8"],
    75: ["id"] * 6,  # MECCharacterController: current hero, skin, ..., last valid hero
    # MECGameMode: u32[], the game mode GUID, ...
    114: ["u32[]", "id", "id", "u64", "u64", "u64", "u64", "s32", "f32", "f32", "u32"] + ["u8"] * 9,
    123: ["id"],  # MECCharacterBody: the body's hero
    90: ["lease[]"],  # MECPredictor: m_leaseBlocks, owner-only (projectiles.py)
}
SCALAR_FORMATS = {
    "u8": "<B",
    "s8": "<b",
    "s16": "<h",
    "u16": "<H",
    "u32": "<I",
    "s32": "<i",
    "u64": "<Q",
    "f32": "<f",
}

ANGLE_UNITS = 65536 / (2 * math.pi)  # s16 angle units per radian (the commands' yaw and pitch)
POSITION_UNITS = 1024  # movement states hold positions and velocities on a 1/1024 m grid


class RecordWriter(BitWriter):
    """A record that knows where it will sit in the frame payload, for its byte padding."""

    def __init__(self, origin: int) -> None:
        super().__init__()
        self.origin = origin

    def pad(self) -> None:
        missing = (-(self.origin + self.count)) & 7
        if missing:
            self.bits(0, missing)


def _byte_mask(value: int) -> bytes:
    """A u64 as a mask of its non-zero bytes followed by those bytes, the lowest first."""
    number = int(value) & 0xFFFFFFFFFFFFFFFF
    mask, body = 0, bytearray()
    for k in range(8):
        byte = (number >> (8 * k)) & 0xFF
        if byte:
            mask |= 1 << k
            body.append(byte)
    return bytes([mask]) + bytes(body)


def _write_value(out: RecordWriter, kind: str, value) -> None:
    if kind == "id":
        out.pad()
        out.raw(_byte_mask(value))
    elif kind == "dbid":  # (low, high), each a byte mask and its bytes, not padded (0x7FF78A76B500)
        for half in value:
            out.raw(_byte_mask(half))
    elif kind == "entity":
        out.raw(struct.pack("<I", int(value) & 0xFFFFFFFF))
    elif kind == "string":  # a teString: on a byte, its u32 byte count and the bytes (0x7FF78AAF0300)
        data = str(value).encode("utf-8")
        out.pad()
        out.raw(struct.pack("<I", len(data)) + data)
    elif kind.startswith("blob:"):
        data = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
        if len(data) != int(kind[5:]):
            raise ValueError(f"{kind} needs {kind[5:]} bytes, got {len(data)}")
        out.raw(data)
    elif kind == "lease[]":  # LeaseBlock {u64 definition, u32 start, u32 stop, u32 first id, u8 per frame}
        out.pad()
        out.raw(struct.pack("<I", len(value)))
        for block in value:
            out.raw(struct.pack("<QIIIB", *block))
    elif kind.endswith("[]"):
        items = list(value)
        out.pad()
        out.raw(struct.pack("<I", len(items)))
        for item in items:
            out.raw(struct.pack(SCALAR_FORMATS[kind[:-2]], item))
    else:
        out.raw(struct.pack(SCALAR_FORMATS[kind], value))


def _write_components(out: RecordWriter, components: dict) -> None:
    """The update body. `components` maps a component index to its field values; None skips a field."""
    out.bits(len(components), 8)
    for index, values in components.items():
        fields = COMPONENT_FIELDS[index]
        values = list(values) + [None] * (len(fields) - len(values))
        mask = bytearray((len(fields) + 7) // 8)
        for k, value in enumerate(values):
            if value is not None:
                mask[k >> 3] |= 1 << (k & 7)
        out.bits(index, 8)
        out.pad()
        out.bits(len(mask), 8)
        out.raw(bytes(mask))
        for kind, value in zip(fields, values, strict=True):
            if value is not None:
                _write_value(out, kind, value)


# A health pool (type 0x7FF78BFE8660, 20 bytes): u32 id (0 = none), f32 max, f32 current, u32 type (0
# health, 1 armour, 2 shields: the client reads array t for type t, 0x7FF7894D0530), then four bytes. The
# HUD bar (0x7FF7894CCEE0) draws a pool as health, armour or shields only when byte 17 is 1 and byte 18 is
# 0, else as over-health in another colour; 1 is the client's own default for byte 17 (0x7FF78A1FFBD0).
# Bytes 16 and 18 are 0, as 0033's pool states leave them; byte 19 is 1 (unverified: no client reader found).
POOLS = 16  # pools per type
HEALTH_POOL, ARMOUR_POOL, SHIELDS_POOL = 0, 1, 2


def pool(pool_id: int, kind: int, most: float, now: float) -> bytes:
    return struct.pack("<IffI4B", pool_id, most, now, kind, 0, 1, 0, 1)


def health(now: float, most: float, armour: float = 0.0, shields: float = 0.0) -> dict:
    """Component 51 with a body's pools: health (now of most) and, when the body has them, full armour
    and shields. The ids follow the order 0033 begins the pools in, shields, armour, health (unverified: the
    client only tests an id for 0)."""
    parts: list = [None] * (3 * POOLS)
    pool_id = 1
    for kind, top in ((SHIELDS_POOL, shields), (ARMOUR_POOL, armour)):
        if top > 0:
            parts[kind * POOLS] = pool(pool_id, kind, top, top)
            pool_id += 1
    parts[HEALTH_POOL * POOLS] = pool(pool_id, HEALTH_POOL, most, now)
    return {51: parts}


NO_INPUT = 0xFFFFFFFF  # input_frame of a body that never had movement input: the reference state's value
CREATE_BASE = 0xFFFFFFFF  # the frame a create's movement state counts back from (0x7FF78969DD3C)


class Movement:
    """One movement state. yaw and pitch are the commands' s16 angle units.

    Another client animates a body from more than its pose (its 3P animation, 0x7FF789B1EB10): the flags
    (crouched, in the air, dead), throttles = this frame's (right, forward) as s8, input_frame = the
    frame of the last command that had throttles with input_throttles its throttles (None: this state's
    own frame, right for a body that moves now; NO_INPUT: never; the record's own frame goes as None, and
    the client then takes the throttles for input_throttles), air_ticks = ticks since the take-off and
    gravity = the gravity scale its air animation uses. The defaults are the reference state's.

    The owner's client compares the state of its own body with the one it predicted for that frame
    (correction.py), so that one also has frame (its command frame; None: the packet frame), crouch_frame
    (+40, the frame it last stood up in; None: never), spring (+52, +56: the two spring offsets as steps
    of 1/32768 of their range), spring_speed (+60) and fall (the up part of +832)."""

    def __init__(
        self,
        position,
        yaw=0,
        pitch=0,
        velocity=(0.0, 0.0, 0.0),
        flags=0,
        throttles=(0, 0),
        input_frame: int | None = None,
        input_throttles=(0, 0),
        air_ticks=0,
        gravity=0.0,
        frame: int | None = None,
        crouch_frame: int | None = None,
        spring=(0, 0),
        spring_speed=0.0,
        fall=0.0,
    ) -> None:
        self.position = tuple(position)
        self.yaw = int(yaw)
        self.pitch = int(pitch)
        self.velocity = tuple(velocity)
        self.flags = int(flags)
        self.throttles = tuple(throttles)
        self.input_frame = input_frame
        self.input_throttles = tuple(input_throttles)
        self.air_ticks = int(air_ticks)
        self.gravity = float(gravity)
        self.frame = frame
        self.crouch_frame = crouch_frame
        self.spring = tuple(spring)
        self.spring_speed = float(spring_speed)
        self.fall = float(fall)


def _selector(out: BitWriter, value: int, width_index: int, width: int) -> None:
    """A present value at the width that a unary selector picks: 1 = the first width, 01 the second,
    001 the third."""
    out.bit(1)
    for _ in range(width_index):
        out.bit(0)
    out.bit(1)
    out.bits(value & ((1 << width) - 1), width)


def _delta(out: BitWriter, value: int, widths: tuple[int, ...]) -> None:
    """A present signed value at the smallest of the field's widths it fits, else in 32 bits after a 0
    for every width. The client sign-extends the short forms."""
    out.bit(1)
    for width in widths:
        if -(1 << (width - 1)) <= value < 1 << (width - 1):
            out.bit(1)
            out.bits(value & ((1 << width) - 1), width)
            return
        out.bit(0)
    out.bits(value & 0xFFFFFFFF, 32)


def _throttle(out: BitWriter, value: int) -> None:
    """An s8 throttle (0x7FF789B71C50): 1 and its byte, or 0 for the reference state's 0."""
    if value:
        out.bit(1)
        out.bits(value & 0xFF, 8)
    else:
        out.bit(0)


def _input_frame(state: Movement, frame: int | None) -> int | None:
    """+36 as the record carries it: None for the record's own frame (+8, `frame`; None if not known)."""
    if state.input_frame is None:
        return None
    if frame is None:
        raise ValueError("a state with an input frame needs the record's frame")
    value = state.input_frame & 0xFFFFFFFF
    return None if value == frame & 0xFFFFFFFF else value


def _input_fields(out: BitWriter, state: Movement, frame: int | None) -> None:
    """+16 to +40 (0x7FF789B6F80E to 0x7FF789B71E50) of a record whose command frame (+8) is `frame`.
    Their reference values: +16 = 0, +20 = +40 = 0xFFFFFFFF, +24 = 0.0, +28 = 1.0, +32 to +35 = 0,
    +36 = 0xFFFFFFFF."""
    if state.air_ticks:
        _delta(out, state.air_ticks, (4, 10))  # +16 the ticks in the air
    else:
        out.bit(0)
    out.bit(0)  # +20: no timed movement event
    gravity = round(state.gravity * POSITION_UNITS)
    if gravity:
        _delta(out, gravity, (8, 10, 24))  # +24 the gravity scale, at 1/1024
    else:
        out.bit(0)
    out.bit(0)  # +28: the move-speed scale stays 1
    # +36, the frame of the last command with throttles: a 0 bit makes it the record's frame (+8); else 1
    # and 0 keep the reference's "never", 1 1 0 and 32 bits set it. The client reads +34 and +35 only when
    # +36 is not +8 (0x7FF789B71DF6), else it copies +32 and +33: so the record's own frame always goes as
    # the 0 bit, without +34/+35 (written, they were read as the next fields: the crashes of 2026-10-03).
    input_frame = _input_frame(state, frame)
    if input_frame is None:
        out.bit(0)
    elif input_frame == NO_INPUT:
        out.bit(1)
        out.bit(0)
    else:
        out.bit(1)
        out.bit(1)
        out.bit(0)
        out.bits(input_frame, 32)
    if state.crouch_frame is None:
        out.bit(0)  # +40: the reference's "never"
    else:
        out.bit(1)  # +40, the frame it last stood up in: 1 0 and 32 bits
        out.bit(0)
        out.bits(state.crouch_frame & 0xFFFFFFFF, 32)
    for throttle in state.throttles:  # +32 right, +33 forward
        _throttle(out, throttle)
    if input_frame is not None:
        for throttle in state.input_throttles:  # +34, +35: the throttles of command +36
            _throttle(out, throttle)


def _steps(out: BitWriter, steps: int) -> None:
    """A spring offset (+52, +56; 0x7FF789B770B0): the reference's steps (0) plus a signed delta."""
    if steps:
        _delta(out, steps, (8, 12, 17))
    else:
        out.bit(0)


def write_movement(out: BitWriter, state: Movement, frame_back: int | None, base: int | None = None) -> None:
    """The movement-state block. Its frame is `frame_back` frames before the base (0x7FF789B76D00: the
    packet frame on ch3, CREATE_BASE on the create path; None: not known); None or 0 is the base itself."""
    frame = None if base is None else (base - (frame_back or 0)) & 0xFFFFFFFF
    flags = state.flags & 0xFFFFFFFF
    if flags:
        out.bit(1)
        for k in range(4):  # four optional bytes, low first
            byte = (flags >> (8 * k)) & 0xFF
            out.bit(1 if byte else 0)
            if byte:
                out.bits(byte, 8)
    else:
        out.bit(0)
    out.bit(0)  # +4: positions and velocity at 1/1024 m
    if not frame_back:
        out.bit(0)  # +8: the command frame is the base
    elif 1 <= frame_back < 16:
        out.bit(1)
        out.bit(1)
        out.bits(frame_back, 4)
    elif 16 <= frame_back < 1024:
        out.bit(1)
        out.bit(0)  # not the 4-bit width: the 10-bit one
        out.bit(1)
        out.bits(frame_back, 10)
    else:
        out.bit(1)
        out.bits(0, 2)  # no width picked: 32 bits follow
        out.bits(frame_back & 0xFFFFFFFF, 32)
    out.bit(0)  # +12
    pitch = state.pitch & 0xFFFF
    if pitch:
        _selector(out, pitch, 2, 16)  # +66 the aim pitch; widths 8, 10, 16
    else:
        out.bit(0)
    _input_fields(out, state, frame)
    for angle in (state.yaw & 0xFFFF, 0, 0):  # Euler yaw, pitch, roll; widths 8, 10, 16
        if angle:
            _selector(out, angle, 2, 16)
        else:
            out.bit(0)
    for axis in state.position:  # always three; widths 8, 10, 24
        _selector(out, round(axis * POSITION_UNITS), 2, 24)
    velocity = [round(axis * POSITION_UNITS) for axis in state.velocity]
    if any(velocity):
        out.bit(1)
        for axis in velocity:
            if axis:
                _selector(out, axis, 2, 24)
            else:
                out.bit(0)
    else:
        out.bit(0)
    out.bits(0, 9)  # nine optional parts
    out.bit(0)  # no parent: world space
    _steps(out, state.spring[0])  # +52
    speed = round(state.spring_speed * POSITION_UNITS)
    if speed:
        _delta(out, speed, (8, 10, 24))  # +60 the spring speed, at 1/1024
    else:
        out.bit(0)
    _steps(out, state.spring[1])  # +56
    out.bit(0)  # +384
    if state.fall:  # +832: three optional raw floats (0x7FF789B71CE0), gravity's part of the velocity
        out.bit(1)
        out.bit(0)
        out.bit(1)
        out.f32(state.fall)
        out.bit(0)
    else:
        out.bit(0)


def create(
    origin: int,
    definition: int,
    components: dict,
    flags: int = WITH_TRANSFORM | WITH_SCALE,
    position=(0.0, 0.0, 0.0),
    rotation=(0.0, 0.0, 0.0, 1.0),
    movement: Movement | None = None,
    frame_back: int = 1,
    skin: tuple[int, bool] = (0, False),
) -> RecordWriter:
    """A create record for an entity of a 003 definition. `skin` is (skin theme GUID, golden weapon);
    theme 0 leaves the definition's own look."""
    if movement is not None:
        flags |= WITH_MOVEMENT
    theme, golden = skin
    if theme:
        flags |= WITH_SKIN | SKIN_THEME
    out = RecordWriter(origin)
    out.bit(0)
    out.guid_index(definition)
    out.bits(flags, 8)
    if theme:
        out.bits(theme & 0x7FFFFFFF, 32)  # bit 31 off: no remap
        out.bit(golden)
    if flags & WITH_TRANSFORM:
        for value in (*position, *rotation):
            out.f32(value)
    if flags & (WITH_TRANSFORM | WITH_SCALE):
        for _ in range(3):
            out.f32(1.0)
    if movement is not None:
        write_movement(out, movement, frame_back, CREATE_BASE)
    _write_components(out, components)
    return out


def create_placeable(origin: int, index: int) -> RecordWriter:
    """A create record for a map placeable the server owns: the client takes the definition and the
    transform from its map data. The entity id must be 0x80000000 | n."""
    out = RecordWriter(origin)
    out.bit(1)
    out.bits(index, 32)
    out.bit(0)  # keep the map's transform
    _write_components(out, {})
    return out


def update(origin: int, components: dict) -> RecordWriter:
    out = RecordWriter(origin)
    _write_components(out, components)
    return out


def movement_record(state: Movement, tick: int | None = None) -> BitWriter:
    """A ch3 record: 12 bits of length (counting themselves), a 0 bit, the movement state at the packet
    frame `tick`, or at the state's own frame (which must not be after the packet frame)."""
    frame_back = None
    if state.frame is not None and tick is not None:
        frame_back = tick - state.frame
        if frame_back < 0:
            raise ValueError(f"a state of frame {state.frame} cannot go in packet frame {tick}")
    body = BitWriter()
    body.bit(0)
    write_movement(body, state, frame_back, tick)
    out = BitWriter()
    out.bits(12 + body.count, 12)
    out.append(body)
    return out


class EntityUpdate:
    """What one frame says about one entity: a statescript chunk (ch1), a movement record (ch3) and/or
    an entity-channel record (ch4)."""

    def __init__(self, entity: int, op: int = 0, build=None, movement: Movement | None = None, chunk=None):
        self.entity = entity
        self.op = op
        self.build = build  # origin -> RecordWriter, for create and update
        self.movement = movement
        self.chunk = chunk  # a statescript.chunk()
        self.chunk_last = 0  # the chunk's last frame, to know when the client has it
        self.ch2 = None  # the projectile channel's bits after its own bit (projectiles.release), or None
        self.stream = None  # the statescript.Stream told when the chunk arrives
        self.resend = True  # send it again when its datagram is lost


def _write_entity(out: BitWriter, item: EntityUpdate, tick: int | None = None) -> None:
    out.entity_id(item.entity)
    out.bit(1)  # ch0 reads nothing but takes its bit
    if not item.op and item.movement is None and item.chunk is None and item.ch2 is None:
        out.bit(0)
        return
    out.bit(1)  # ch1 runs
    if item.chunk is not None:
        out.bit(1)  # a chunk follows
        out.bit(0)  # a bit the reader skips
        out.append(item.chunk)
    else:
        out.bit(0)
    if not item.op and item.movement is None and item.ch2 is None:
        out.bit(0)
        return
    if item.ch2 is None:
        out.bits(0b001, 3)  # ch2 runs, no event, no state
    else:
        out.bit(1)  # ch2 runs
        out.append(item.ch2)
    out.bit(1)  # ch3 runs
    if item.movement is not None:
        out.bit(1)
        out.append(movement_record(item.movement, tick))
    else:
        out.bit(0)
    if not item.op:
        out.bit(0)
        return
    out.bit(1)  # ch4 runs
    out.bits(item.op, 2)
    if item.op != OP_DESTROY:
        record = item.build(out.count + 14)
        if record.count >= 1 << 14:
            raise ValueError("an entity record is at most 16383 bits")
        out.bits(record.count, 14)
        out.append(record)
    out.bit(0)  # ch5: nothing, the end of this entity


def frame(tick: int, ack: int, reliable=(), unreliable=(), entities=()) -> bytes:
    """A kind-1 payload. The messages are messages.write_channel's; they are written into the frame
    itself, because their padding is to a byte of the payload."""
    out = BitWriter()
    out.var_a(tick)
    messages.write_channel(out, reliable, unreliable)
    out.var_a(ack)
    out.bits(0, 4)
    out.bit(0)  # no ping time
    out.bit(1)  # ch4: keep
    out.bit(0)  # ch5: empty list
    entities = list(entities)
    if len(entities) > 127:
        raise ValueError("at most 127 entities per frame")
    out.bits(len(entities), 7)
    for item in entities:
        _write_entity(out, item, tick)
    return out.getvalue()
