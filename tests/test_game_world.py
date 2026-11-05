"""World frames and entity records, read back the way the client reads them."""

import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.commands import CROUCH, JUMP, Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.match import Match
from ow174.game.mover import CROUCH_SPEED, CROUCHED, GRAVITY_SCALE, JUMP_LATCH, JUMPED, REAL_JUMP, Mover
from ow174.game.world import NO_INPUT, OP_CREATE, EntityUpdate, Movement

TICK = 0.016


def _field(reader: BitReader, widths, reference: int = 0) -> int:
    """A signed field as the client reads it: a presence bit, then one bit per width until a 1 picks it
    (none: 32 bits), the short forms sign-extended, added to the reference."""
    if not reader.bit():
        return reference
    for width in widths:
        if reader.bit():
            return reference + reader.signed(width)
    return reference + reader.signed(32)


def _frame_field(reader: BitReader, reference: int) -> int:
    """0x7FF789B71B60: 0 = the reference, 1 1 = reference + s8, 1 0 = 32 bits."""
    if not reader.bit():
        return reference
    if reader.bit():
        return (reference + reader.signed(8)) & 0xFFFFFFFF
    return reader.bits(32)


def _throttle(reader: BitReader) -> int:
    """0x7FF789B71C50: 1 and an s8, or 0 for the reference's 0."""
    return reader.signed(8) if reader.bit() else 0


def _frames_back(reader: BitReader) -> int:
    """0x7FF789B76D00: 0 = the base; 1, then a width bit for 4 and for 10 bits (none: 32), unsigned."""
    if not reader.bit():
        return 0
    for width in (4, 10):
        if reader.bit():
            return reader.bits(width)
    return reader.bits(32)


def _floats(reader: BitReader) -> tuple:
    """0x7FF789B71CE0: 1, then per float 1 and its 32 bits or 0 for the reference's 0.0."""
    if not reader.bit():
        return (0.0, 0.0, 0.0)
    return tuple(struct.unpack("<f", struct.pack("<I", reader.bits(32)))[0] if reader.bit() else 0.0
                 for _ in range(3))  # fmt: skip


def read_movement(reader: BitReader, frame: int) -> dict:
    """A movement-state block in packet frame `frame`, against the reference state, in the client's
    order (0x7FF789B6F680)."""
    state = {"flags": 0}
    if reader.bit():
        for k in range(4):
            if reader.bit():
                state["flags"] |= reader.bits(8) << (8 * k)
    assert reader.bit() == 0  # +4: 1/1024 m, the velocity on the wire
    state["frame"] = frame - _frames_back(reader)  # +8
    assert _frame_field(reader, 0) == 0  # +12
    state["pitch"] = _field(reader, (8, 10, 16))
    state["air_ticks"] = _field(reader, (4, 10))
    state["timer"] = _frame_field(reader, 0xFFFFFFFF)  # +20
    state["gravity"] = _field(reader, (8, 10, 24)) / 1024
    state["speed_scale"] = 1.0 + _field(reader, (8, 10, 24)) / 1024
    state["input_frame"] = _frame_field(reader, NO_INPUT) if reader.bit() else state["frame"]
    state["+40"] = _frame_field(reader, 0xFFFFFFFF)
    state["throttles"] = (_throttle(reader), _throttle(reader))
    if state["input_frame"] == state["frame"]:  # the client's test (0x7FF789B71DF6)
        state["input_throttles"] = state["throttles"]
    else:
        state["input_throttles"] = (_throttle(reader), _throttle(reader))
    state["yaw"] = _field(reader, (8, 10, 16))
    assert _field(reader, (8, 10, 16)) == 0 and _field(reader, (8, 10, 16)) == 0  # Euler pitch, roll
    state["position"] = tuple(_field(reader, (8, 10, 24)) / 1024 for _ in range(3))
    if reader.bit():
        state["velocity"] = tuple(_field(reader, (8, 10, 24)) / 1024 for _ in range(3))
    else:
        state["velocity"] = (0.0, 0.0, 0.0)
    assert reader.bits(9) == 0 and reader.bit() == 0  # no optional parts, no parent
    first = _field(reader, (8, 12, 17))  # +52
    state["spring_speed"] = _field(reader, (8, 10, 24)) / 1024  # +60
    state["spring"] = (first, _field(reader, (8, 12, 17)))  # +56
    assert reader.bit() == 0  # +384
    state["fall"] = _floats(reader)  # +832
    return state


def read_record(state: Movement, frame: int) -> dict:
    """A ch3 movement record of `state`, read back at packet frame `frame`."""
    record = world.movement_record(state, frame)
    reader = BitReader(record.getvalue())
    length = reader.bits(12)
    assert reader.bit() == 0  # against the reference state
    fields = read_movement(reader, frame)
    assert reader.pos == length == record.count
    return fields


def read_frame_start(payload: bytes):
    """The frame up to its entities: (reader, tick, ack, entity count)."""
    reader = BitReader(payload)
    tick = reader.var_a()
    assert reader.bit() == 0  # no messages
    ack = reader.var_a()
    assert reader.bits(4) == 0 and reader.bit() == 0  # flags, no ping
    assert reader.bit() == 1 and reader.bit() == 0  # ch4 keep, ch5 empty
    return reader, tick, ack, reader.bits(7)


class WorldTests(unittest.TestCase):
    def test_a_pose_record_is_as_long_as_a_live_one(self):
        # A live pose-only movement record (position and yaw) was 148 bits with its 13 framing bits.
        record = world.movement_record(Movement((54.56507, 1.0, 42.14788), yaw=-31000))
        self.assertEqual(record.count, 148)
        self.assertEqual(record.value & 0xFFF, 148)  # the length counts itself

    def test_a_frame_with_a_placeable_create(self):
        entity = 0x80000088
        payload = world.frame(
            1234,
            1234,
            (),
            (),
            [EntityUpdate(entity, OP_CREATE, lambda origin: world.create_placeable(origin, 0x88))],
        )
        reader, tick, ack, count = read_frame_start(payload)
        self.assertEqual((tick, ack, count), (1234, 1234, 1))
        self.assertEqual(reader.entity_id(), entity)
        self.assertEqual([reader.bit() for _ in range(7)], [1, 1, 0, 1, 0, 0, 1])  # ch0..ch3, empty
        self.assertEqual(reader.bit(), 0)  # ch3: no movement record
        self.assertEqual(reader.bit(), 1)  # ch4 runs
        self.assertEqual(reader.bits(2), OP_CREATE)
        length = reader.bits(14)
        start = reader.pos
        self.assertEqual((reader.bit(), reader.bits(32), reader.bit(), reader.bits(8)), (1, 0x88, 0, 0))
        self.assertEqual(reader.pos - start, length)
        self.assertEqual(reader.bit(), 0)  # ch5

    def test_component_values_start_on_a_byte_of_the_payload(self):
        payload = world.frame(
            7,
            7,
            (),
            (),
            [
                EntityUpdate(
                    0xA0000100,
                    OP_CREATE,
                    lambda origin: world.create(origin, 0x0400000000000001, {26: [0x800000]}),
                )
            ],
        )
        reader, _, _, _ = read_frame_start(payload)
        reader.entity_id()
        reader.bits(9)  # the channel bits, ch3's empty record bit and ch4's run bit
        self.assertEqual(reader.bits(2), OP_CREATE)
        reader.bits(14)
        self.assertEqual(reader.bit(), 0)  # not a placeable
        self.assertEqual((reader.bit(), reader.bits(12)), (1, 1))  # the definition's index, short form
        flags = reader.bits(8)
        self.assertEqual(flags, world.WITH_TRANSFORM | world.WITH_SCALE)
        floats = [struct.unpack("<f", struct.pack("<I", reader.bits(32)))[0] for _ in range(10)]
        self.assertEqual(floats[-3:], [1.0, 1.0, 1.0])
        self.assertEqual((reader.bits(8), reader.bits(8)), (1, 26))  # one component: 26
        reader.align()  # the pad is to the payload's byte
        self.assertEqual((reader.bits(8), reader.bits(8)), (1, 1))  # mask length, mask
        reader.align()  # the u64 is a byte mask with the non-zero bytes, aligned
        self.assertEqual((reader.bits(8), reader.bits(8)), (0b100, 0x80))

    def test_the_movement_state_keeps_the_position_on_the_1024_grid(self):
        out = BitWriter()
        world.write_movement(out, Movement((1.5, -2.25, 3.0)), None)
        reader = BitReader(out.getvalue())
        self.assertEqual([reader.bit() for _ in range(5)], [0, 0, 0, 0, 0])  # flags, +4, frame, +12, pitch
        reader.bits(8)
        self.assertEqual([reader.bit() for _ in range(3)], [0, 0, 0])  # no Euler angles
        axes = []
        for _ in range(3):
            self.assertEqual([reader.bit() for _ in range(4)], [1, 0, 0, 1])  # present, the 24-bit width
            axes.append(reader.signed(24) / 1024)
        self.assertEqual(axes, [1.5, -2.25, 3.0])
        self.assertTrue(math.isclose(axes[0], 1.5))

    def test_a_skin_theme_follows_the_flags_byte(self):
        # Doomfist (body 012F) in skin theme 467C with his golden gauntlet: flags 0x01 | 0x40 and the
        # transform flags, then 32 bits of the theme (bit 31, the remap, off) and the golden bit.
        record = world.create(0, 0x040000000000012F, {}, skin=(0x0A5000000000467C, True))
        reader = BitReader(record.getvalue())
        self.assertEqual(reader.bit(), 0)  # not a map placeable
        self.assertEqual((reader.bit(), reader.bits(12)), (1, 0x12F))  # the definition's 12-bit index
        self.assertEqual(reader.bits(8), world.WITH_SKIN | world.SKIN_THEME | world.WITH_TRANSFORM | 0x04)
        self.assertEqual((reader.bits(32), reader.bit()), (0x467C, 1))
        floats = [struct.unpack("<f", struct.pack("<I", reader.bits(32)))[0] for _ in range(10)]
        self.assertEqual(floats, [0.0] * 6 + [1.0] * 4)  # position, rotation (0, 0, 0, 1), scale


class MovementStateTests(unittest.TestCase):
    """The fields another client animates a body from, read back in the client's order."""

    def test_a_pose_keeps_the_reference_values(self):
        fields = read_record(Movement((1.0, 2.0, 3.0), yaw=100), 5000)
        self.assertEqual(fields["input_frame"], 5000)  # the codec's default: the state's own frame
        self.assertEqual((fields["throttles"], fields["air_ticks"], fields["gravity"]), ((0, 0), 0, 0.0))
        self.assertEqual((fields["timer"], fields["+40"]), (0xFFFFFFFF, 0xFFFFFFFF))
        self.assertEqual(fields["speed_scale"], 1.0)

    def test_a_body_that_never_moved_has_no_input_frame(self):
        fields = read_record(Movement((1.0, 2.0, 3.0), input_frame=NO_INPUT), 5000)
        self.assertEqual(fields["input_frame"], NO_INPUT)
        self.assertEqual((fields["throttles"], fields["input_throttles"]), ((0, 0), (0, 0)))

    def test_a_moving_body_has_input_in_its_own_frame(self):
        state = Movement((1.0, 2.0, 3.0), velocity=(0.0, 0.0, 5.5), throttles=(-127, 127))
        fields = read_record(state, 5000)
        self.assertEqual(fields["input_frame"], 5000)
        self.assertEqual((fields["throttles"], fields["input_throttles"]), ((-127, 127), (-127, 127)))
        self.assertEqual(fields["velocity"], (0.0, 0.0, 5.5))

    def test_a_body_that_stopped_keeps_its_last_input(self):
        state = Movement((1.0, 2.0, 3.0), input_frame=4990, input_throttles=(0, -127))
        fields = read_record(state, 5000)
        self.assertEqual((fields["input_frame"], fields["input_throttles"]), (4990, (0, -127)))
        self.assertEqual(fields["throttles"], (0, 0))

    def test_a_jump_carries_its_flags_ticks_and_gravity(self):
        state = Movement(
            (1.0, 2.5, 3.0),
            velocity=(0.0, 2.0, 0.0),
            flags=JUMP_LATCH | JUMPED | REAL_JUMP,
            input_frame=NO_INPUT,
            air_ticks=9,
            gravity=GRAVITY_SCALE,
        )
        fields = read_record(state, 5000)
        self.assertEqual(fields["flags"], 0x405)
        self.assertEqual((fields["air_ticks"], fields["gravity"]), (9, 1.75))
        self.assertEqual(fields["velocity"], (0.0, 2.0, 0.0))

    def test_an_input_frame_equal_to_the_record_frame_goes_as_its_own_frame(self):
        # A bot that stopped in this very frame (the crashes of 2026-10-03): written as 32 bits, +36 = +8
        # made the client skip +34/+35 (0x7FF789B71DF6) and read them as the Euler angles, every later
        # field out of step. The record must be the one of the 0 bit, the throttles taken for +34/+35.
        where = (93.75, -4.0, 43.5)
        stopped = Movement(
            where, yaw=9950, input_frame=14133, input_throttles=(0, 127), gravity=GRAVITY_SCALE
        )
        fields = read_record(stopped, 14133)
        self.assertEqual((fields["input_frame"], fields["input_throttles"]), (14133, (0, 0)))
        self.assertEqual((fields["yaw"], fields["position"]), (9950, where))
        own = Movement(where, yaw=9950, gravity=GRAVITY_SCALE)
        self.assertEqual(
            world.movement_record(stopped, 14133).getvalue(), world.movement_record(own, 14133).getvalue()
        )

    def test_a_correction_with_input_in_its_own_frame_stays_in_step(self):
        # The record's frame is the state's (frame_back 3), not the packet frame.
        state = Movement(
            (1.0, 2.0, 3.0), 300, throttles=(0, 127), input_frame=4997, input_throttles=(5, 6), frame=4997
        )
        fields = read_record(state, 5000)
        self.assertEqual(
            (fields["frame"], fields["input_frame"], fields["input_throttles"]), (4997, 4997, (0, 127))
        )
        self.assertEqual((fields["yaw"], fields["position"]), (300, (1.0, 2.0, 3.0)))
        stopped = Movement((1.0, 2.0, 3.0), input_frame=5000, input_throttles=(0, 127), frame=4997)
        fields = read_record(stopped, 5000)  # the packet frame is not the record's
        self.assertEqual((fields["input_frame"], fields["input_throttles"]), (5000, (0, 127)))

    def test_a_create_without_input_at_the_create_base_stays_in_step(self):
        # On the create path +8 counts back from 0xFFFFFFFF (0x7FF78969DD3C): with frame_back 0, "never"
        # is +8 itself.
        out = BitWriter()
        state = Movement((1.0, 2.0, 3.0), input_frame=NO_INPUT, input_throttles=(0, 127))
        world.write_movement(out, state, 0, world.CREATE_BASE)
        reader = BitReader(out.getvalue())
        fields = read_movement(reader, world.CREATE_BASE)
        self.assertEqual((fields["input_frame"], fields["input_throttles"]), (NO_INPUT, (0, 0)))
        self.assertEqual(reader.pos, out.count)

    def test_an_input_frame_needs_the_record_frame(self):
        with self.assertRaises(ValueError):
            world.movement_record(Movement((0.0, 0.0, 0.0), input_frame=4990))


class MoverTests(unittest.TestCase):
    """The movement fields the records carry (tests/test_game_mover.py has the mover itself)."""

    def test_it_keeps_the_last_command_with_throttles(self):
        mover = Mover((0.0, 1.0, 0.0))
        self.assertIsNone(mover.input_tick)
        mover.step(Command(1, forward=127, right=-64), TICK, tick=10)
        for frame in range(2, 9):
            mover.step(Command(frame), TICK, tick=9 + frame)
        self.assertEqual((mover.throttles, mover.input_throttles, mover.input_tick), ((0, 0), (-64, 127), 10))
        self.assertEqual(mover.velocity, (0.0, 0.0, 0.0))  # it slows to a stop in a few ticks

    def test_crouching_is_slower_and_flagged(self):
        mover = Mover((0.0, 1.0, 0.0))
        for frame in range(60):
            mover.step(Command(frame, forward=127, buttons=CROUCH), TICK, tick=frame)
        self.assertEqual(mover.flags, CROUCHED)
        self.assertEqual(mover.velocity, (0.0, 0.0, CROUCH_SPEED))

    def test_a_jump_goes_up_and_lands_and_holding_does_not_jump_again(self):
        mover = Mover((0.0, 1.0, 0.0))
        mover.step(Command(1, buttons=JUMP), TICK)
        self.assertEqual(mover.flags, JUMP_LATCH | JUMPED | REAL_JUMP)
        self.assertGreater(mover.velocity[1], 0.0)
        top, ticks = mover.position[1], 1
        while mover.airborne and ticks < 200:
            mover.step(Command(1 + ticks, buttons=JUMP | CROUCH), TICK)  # no crouching in the air
            self.assertFalse(mover.crouched and mover.airborne)
            top, ticks = max(top, mover.position[1]), ticks + 1
        self.assertAlmostEqual(top, 2.0, delta=0.1)  # about 6^2 / (2 * 17.5) = 1.03 m up
        self.assertTrue(40 <= ticks <= 46)  # 2 * 6 / 17.5 = 0.69 s
        self.assertEqual((mover.position[1], mover.velocity[1], mover.air_ticks), (1.0, 0.0, 0))
        mover.step(Command(300, buttons=JUMP), TICK)
        self.assertFalse(mover.airborne)  # the button was never let go
        self.assertEqual(mover.flags, JUMP_LATCH | REAL_JUMP)  # the client keeps 0x400 until another take-off
        mover.step(Command(301), TICK)
        mover.step(Command(302, buttons=JUMP), TICK)
        self.assertTrue(mover.airborne)


class RemoteMovementTests(unittest.TestCase):
    """What another player's client gets about a body from frame to frame."""

    def setUp(self):
        self.match = Match(PRACTICE_RANGE)
        self.runner = self.match.add_player(1, "Alpha", SOLDIER, 0, False)
        self.viewer = self.match.add_player(2, "Beta", SOLDIER, 1, False)
        self.runner.has_body = True
        self.viewer.seen[self.runner.body] = True  # Beta's client has Alpha's body

    def record_at(self, tick: int) -> dict:
        self.match.tick = tick
        (update,) = self.match.remote_movements(self.viewer)
        return read_record(update.movement, tick)

    def test_a_body_that_never_moved_stands(self):
        fields = self.record_at(2000)
        self.assertEqual((fields["input_frame"], fields["throttles"]), (NO_INPUT, (0, 0)))
        self.assertEqual(fields["gravity"], GRAVITY_SCALE)

    def test_a_running_body_then_a_stopped_one(self):
        self.match.tick = 2000
        self.runner.take_commands([Command(7000 + frame, forward=127) for frame in range(10)])
        fields = self.record_at(2001)
        self.assertEqual((fields["input_frame"], fields["throttles"]), (2001, (0, 127)))
        self.assertAlmostEqual(fields["velocity"][2] ** 2 + fields["velocity"][0] ** 2, 5.5**2, delta=0.01)
        self.runner.take_commands([Command(7010 + frame) for frame in range(8)])
        fields = self.record_at(2010)
        # The keys came up after tick 2000: the animation stops once that is 0.1 s old.
        self.assertEqual((fields["input_frame"], fields["input_throttles"]), (2000, (0, 127)))
        self.assertEqual((fields["throttles"], fields["velocity"]), ((0, 0), (0.0, 0.0, 0.0)))

    def test_the_own_body_keeps_its_old_state(self):
        self.runner.take_commands([Command(7000, forward=127, buttons=JUMP)])
        state = self.runner.pose()
        self.assertEqual((state.throttles, state.input_frame), ((0, 0), None))
        self.assertEqual((state.air_ticks, state.gravity, state.flags), (0, 0.0, 0))  # its client predicts it


if __name__ == "__main__":
    unittest.main()
