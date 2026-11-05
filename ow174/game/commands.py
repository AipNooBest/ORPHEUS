"""The client's input record: what it pressed, frame by frame. It ends every client datagram.

The client never sends its position (only the message channel and this record have writers,
0x7FF7896D0930), so a server that shows a player to others has to move the player itself.

Layout (writer 0x7FF789B3DDC0, command writer 0x7FF789B3DD00):
    1 bit present, u32 length in bits, then u16 latency ms, u8 key flags, u8 count (1..32), and the
    commands oldest first. The first is complete:
        u32 frame | throttles | s16 yaw | s16 pitch | 24 bits buttons | u8 action |
        [buttons & 0x10000: u8 sub-frame, s16, s16] | u16 extra
    Each later one is a change against the one before:
        frame + 1 if bit 0, else + U(2, 6 bits or 32) | bit 0 = the same command again, else
        throttles | bit -> pitch += S(8, 10) | bit -> yaw += S(8, 10) | bit -> extra += S(5, 8) |
        bit -> buttons | bit -> action | [sub-frame data]
    Throttles: bit present; bit compact: 1 -> bit (0 = none, 1 = 3 bits direction), else bit ->
    s8 right, bit -> s8 forward.
Angles are s16 units of 2 pi / 65536; forward is (sin yaw, 0, cos yaw) and right (-cos yaw, 0,
sin yaw). The pitch is positive looking down. One command frame is one tick of the game.

The extra is the client's view delay in ms (clock +0x98 plus +0x9C, times 1000: how far in the past it
draws other bodies). The sub-frame data (command +0x12, 0xFF when not sent; the two angles +0x0E, +0x10)
says when in the frame a click came, as 255ths, and where it aimed then (unverified: yaw first, as in the main
pair). The client's own hit test runs at the view time of the frame (0x7FF7894BEE40): the frame's time
less (extra + round((1 - sub-frame / 255) * frame ms)) ms (0x7FF7894BEEC0).
"""

from dataclasses import dataclass, replace

from ow174.game.bits import BitReader

MAX_COMMANDS = 32
FRAME_MASK = 0xFFFFFFFF
FRAME_HALF = 1 << 31
SUBFRAME = 0x10000
# The compact directions as (forward, right).
DIRECTIONS = ((0, 127), (127, 127), (127, 0), (127, -127), (0, -127), (-127, -127), (-127, 0), (-127, 127))

FIRE, SECONDARY, JUMP, CROUCH = 0x01, 0x02, 0x40, 0x80


class InputError(ValueError):
    pass


def frame_distance(newer: int, older: int) -> int:
    """The forward uint32 distance; a distance >= FRAME_HALF is old or ambiguous."""
    return (newer - older) & FRAME_MASK


@dataclass(frozen=True)
class Command:
    frame: int
    forward: int = 0
    right: int = 0
    yaw: int = 0
    pitch: int = 0
    buttons: int = 0
    action: int = 0
    extra: int = 0  # the view delay, ms
    subframe: int = 0xFF  # when in the frame the click came, 255ths
    click_yaw: int | None = None  # the aim at that moment
    click_pitch: int | None = None

    def view_delay(self, quanta: int) -> float:
        """Seconds before the frame's time at which the client drew the bodies it aimed at
        (0x7FF7894BEEC0; quanta in microseconds)."""
        lead = (1.0 - self.subframe / 255) * (quanta // 1000)
        lead = int(lead + 0.5) if lead >= 0 else int(lead - 0.5)
        return ((self.extra + lead) & 0xFFFF) * 0.001

    def aim(self) -> tuple[int, int]:
        """(yaw, pitch) of a shot: the aim at the click when the command has one."""
        if self.click_yaw is None:
            return self.yaw, self.pitch
        return self.click_yaw, self.click_pitch


def _unsigned(reader: BitReader, widths, full: int) -> int:
    for width in widths:
        if reader.bit():
            return reader.bits(width)
    return reader.bits(full)


def _signed(reader: BitReader, widths) -> int:
    for width in widths:
        if reader.bit():
            return reader.signed(width)
    return reader.signed(16)


def _throttles(reader: BitReader, forward: int, right: int) -> tuple[int, int]:
    if not reader.bit():
        return forward, right
    if reader.bit():
        if not reader.bit():
            return 0, 0
        return DIRECTIONS[reader.bits(3)]
    if reader.bit():
        right = reader.signed(8)
    if reader.bit():
        forward = reader.signed(8)
    return forward, right


def _subframe(reader: BitReader, buttons: int) -> tuple[int, int | None, int | None]:
    if not buttons & SUBFRAME:
        return 0xFF, None, None
    return reader.bits(8), reader.signed(16), reader.signed(16)


def read_commands(reader: BitReader) -> list[Command]:
    """The commands of the record at the reader, oldest first; [] when there is none."""
    try:
        if not reader.left() or not reader.bit():
            return []
        length = reader.bits(32)
        if length > reader.left():
            raise InputError(f"the record says {length} bits, {reader.left()} are left")
        reader.bits(16 + 8)  # latency, key flags
        count = reader.bits(8)
        if not 1 <= count <= MAX_COMMANDS:
            raise InputError(f"{count} commands")
        frame = reader.bits(32)
        forward, right = _throttles(reader, 0, 0)
        yaw, pitch = reader.signed(16), reader.signed(16)
        buttons, action = reader.bits(24), reader.bits(8)
        click = _subframe(reader, buttons)
        extra = reader.bits(16)
        commands = [Command(frame, forward, right, yaw, pitch, buttons, action, extra, *click)]
        for _ in range(count - 1):
            last = commands[-1]
            frame = (last.frame + (_unsigned(reader, (2, 6), 32) if reader.bit() else 1)) & FRAME_MASK
            if not reader.bit():
                commands.append(replace(last, frame=frame))
                continue
            forward, right = _throttles(reader, last.forward, last.right)
            pitch, yaw, buttons, action, extra = last.pitch, last.yaw, last.buttons, last.action, last.extra
            if reader.bit():
                pitch += _signed(reader, (8, 10))
            if reader.bit():
                yaw += _signed(reader, (8, 10))
            if reader.bit():
                extra = (extra + _signed(reader, (5, 8))) & 0xFFFF
            if reader.bit():
                buttons = reader.bits(24)
            if reader.bit():
                action = reader.bits(8)
            click = _subframe(reader, buttons)
            commands.append(Command(frame, forward, right, yaw, pitch, buttons, action, extra, *click))
        return commands
    except EOFError:
        raise InputError("the record runs past the datagram") from None


class CommandQueue:
    """Hand forward uint32 command frames over once, in their window order.

    A late old packet cannot establish a new command epoch. ``reset`` is explicit: the
    caller must also replace the old body's simulation, timers and owner-frame stream at
    an established lifecycle boundary. A queue reset alone cannot reset those consumers.
    """

    def __init__(self) -> None:
        self.last_frame: int | None = None
        self.epoch = 0
        self.wraps = 0

    def reset(self, *, last_frame: int | None = None) -> None:
        """Start an explicitly established epoch; never called merely because an old packet arrived."""
        if last_frame is not None and (type(last_frame) is not int or not 0 <= last_frame <= FRAME_MASK):
            raise InputError("last_frame must be uint32 or None")
        self.last_frame = last_frame
        self.epoch += 1
        self.wraps = 0

    def new(self, commands: list[Command]) -> list[Command]:
        fresh, _ = self._consume(commands)
        return fresh

    def before_limit(
        self, commands: list[Command], limit: int, *, anchor: int | None = None
    ) -> tuple[list[Command], int | None]:
        """Admit the fresh prefix that fits the current body epoch, and report its first boundary."""
        return self._consume(commands, limit, anchor)

    def _consume(
        self, commands: list[Command], limit: int | None = None, anchor: int | None = None
    ) -> tuple[list[Command], int | None]:
        # Validate the whole window first so malformed input cannot partly advance the queue.
        if any(type(item.frame) is not int or not 0 <= item.frame <= FRAME_MASK for item in commands):
            raise InputError("command frame must be uint32")
        fresh = []
        previous = self.last_frame if self.last_frame is not None else anchor
        for command in commands:
            if previous is not None:
                distance = frame_distance(command.frame, previous)
                if distance == 0 or distance >= FRAME_HALF:
                    continue
            wrapped = previous is not None and command.frame < previous
            if limit is not None and (wrapped or command.frame > limit):
                return fresh, command.frame
            if wrapped:
                self.wraps += 1
            fresh.append(command)
            previous = self.last_frame = command.frame
        return fresh, None
