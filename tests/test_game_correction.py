"""The owner's own body's state (ow174/game/correction.py): the frame it carries, the fields the client
compares, one record per frame, and the switch."""

import logging
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_world import read_movement

from ow174.game import correction, world
from ow174.game.bits import BitReader
from ow174.game.commands import CROUCH, JUMP, Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.match import Match
from ow174.game.mover import Mover, f32, mover_data
from ow174.game.world import NO_INPUT, OP_CREATE, EntityUpdate, Movement

TICK = 0.016
SOLDIER_BODY = 0x04000000000003CF
TRACER = 0x02E0000000000003
SHIFT = 0x08  # ability 1: Sprint


def read(state: Movement, tick: int) -> dict:
    """A ch3 record read back as the client reads it in packet frame `tick`."""
    record = world.movement_record(state, tick)
    reader = BitReader(record.getvalue())
    length = reader.bits(12)
    assert reader.bit() == 0  # against the reference state
    fields = read_movement(reader, tick)
    assert reader.pos == length == record.count
    return fields


class FrameTests(unittest.TestCase):
    def test_a_record_carries_the_state_at_its_own_command_frame(self):
        # The client reads the frame as the packet frame minus an unsigned count (0x7FF789B76D00) and
        # compares the state with its prediction of that frame.
        for back in (0, 1, 15, 16, 1023, 1024, 70000):
            fields = read(Movement((1.0, 2.0, 3.0), frame=200000 - back), 200000)
            self.assertEqual(fields["frame"], 200000 - back)
        self.assertEqual(read(Movement((1.0, 2.0, 3.0)), 5000)["frame"], 5000)  # no frame: the packet's

    def test_a_state_after_the_packet_frame_cannot_go(self):
        with self.assertRaises(ValueError):
            world.movement_record(Movement((0.0, 0.0, 0.0), frame=101), 100)


class FieldTests(unittest.TestCase):
    def test_every_field_the_client_compares_comes_back_as_the_mover_has_it(self):
        mover = Mover((0.0, 1.0, 0.0), 1000, None, mover_data(SOLDIER_BODY))
        script = [(20, 127, 0, 2000), (12, 0, CROUCH, -3000), (14, 104, 0, 17000), (1, -127, JUMP, 0)]
        script += [(45, 9, 0, 0), (10, 0, 0, 0)]
        frame, seen = 5000, set()
        for count, forward, buttons, pitch in script:
            for _ in range(count):
                mover.step(Command(frame, forward=forward, buttons=buttons, pitch=pitch, yaw=-20000), TICK, 0)
                snapshot = mover.history[-1]
                fields = read(correction.movement(snapshot), frame + 3)
                expected = {
                    "frame": frame,
                    "flags": snapshot.flags,
                    "pitch": snapshot.pitch,
                    "yaw": snapshot.yaw,
                    "air_ticks": snapshot.air_ticks,
                    "gravity": snapshot.gravity,
                    "input_frame": NO_INPUT if snapshot.input_frame is None else snapshot.input_frame,
                    "+40": 0xFFFFFFFF if snapshot.crouch_end is None else snapshot.crouch_end,
                    "throttles": snapshot.throttles,
                    "input_throttles": snapshot.input_throttles,
                    "position": snapshot.position,
                    "velocity": snapshot.velocity,
                    "spring": snapshot.spring,
                    "spring_speed": snapshot.spring_speed,
                    "fall": (0.0, snapshot.fall, 0.0),
                }
                self.assertEqual({key: fields[key] for key in expected}, expected, f"frame {frame}")
                tried = {
                    "+40": fields["+40"] != 0xFFFFFFFF,
                    "fall": fields["fall"][1] != 0.0,
                    "spring speed": fields["spring_speed"] != 0.0,
                    "older input": fields["input_frame"] not in (frame, NO_INPUT),
                    "second spring": fields["spring"][0] != fields["spring"][1],
                }
                seen.update(key for key, value in tried.items() if value)
                frame += 1
        self.assertEqual(len(seen), 5)  # each field went with a value of its own somewhere
        self.assertEqual(f32(mover.fall), mover.fall)


class CadenceTests(unittest.TestCase):
    """A Soldier: 76 in the Practice Range, whose body statescript the server runs."""

    def setUp(self):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        flat = mock.patch("ow174.game.match.world_when_ready", return_value=None)  # a flat floor
        flat.start()
        self.addCleanup(flat.stop)
        self.match = Match(PRACTICE_RANGE)
        self.match.tick = 6999
        self.player = self.match.add_player(1, "Alpha", SOLDIER, 0, False)
        self.player.has_body = True
        self.match.start_body_script(self.player, EntityUpdate(self.player.body, OP_CREATE))
        self.player.body_script.create_arrived = True  # the client acknowledged the body's create

    def own(self, tick: int) -> list[dict]:
        """The frame's records of the player's own body, read back."""
        self.match.tick = tick
        updates = self.match.remote_movements(self.player)
        return [read(update.movement, tick) for update in updates if update.entity == self.player.body]

    def test_one_record_per_frame_of_the_newest_state_not_after_the_packet_frame(self):
        self.player.take_commands([Command(7000 + n, forward=127) for n in range(10)])
        history = {snapshot.frame: snapshot for snapshot in self.player.mover.history}
        (record,) = self.own(7005)
        self.assertEqual((record["frame"], record["position"]), (7005, history[7005].position))
        self.assertEqual(self.own(7005), [])  # sent: not again
        self.assertEqual([record["frame"] for record in self.own(7006)], [7006])
        self.assertEqual([record["frame"] for record in self.own(7020)], [7009])  # the newest it has
        self.assertEqual(self.own(7021), [])
        self.player.take_commands([Command(7010 + n, forward=127) for n in range(3)])
        (record,) = self.own(7022)
        self.assertEqual((record["frame"], record["position"]), (7012, tuple(self.player.mover.position)))

    def test_nothing_before_the_client_has_the_body_or_with_the_switch_off(self):
        self.player.take_commands([Command(7000 + n, forward=127) for n in range(10)])
        self.player.body_script.create_arrived = False
        self.assertEqual(self.own(7005), [])
        self.player.body_script.create_arrived = True
        with mock.patch.object(correction, "CORRECTIONS", False):
            self.assertEqual(self.own(7006), [])
        self.assertEqual(len(self.own(7007)), 1)

    def test_nothing_for_a_hero_whose_statescript_the_server_does_not_run(self):
        other = self.match.add_player(2, "Beta", heroes()[TRACER], 1, False)
        other.has_body = True
        other.take_commands([Command(7000 + n, forward=127) for n in range(10)])
        self.match.tick = 7005
        self.assertIsNone(other.body_script)
        self.assertEqual([u for u in self.match.remote_movements(other) if u.entity == other.body], [])

    def test_sprint_from_the_statescript_moves_the_body_and_its_record(self):
        commands = [Command(7000 + n) for n in range(30)]
        commands += [Command(7030 + n, forward=127, buttons=SHIFT if n == 10 else 0) for n in range(20)]
        self.player.take_commands(commands)
        speeds = {snapshot.frame: math.hypot(snapshot.velocity[0], snapshot.velocity[2])
                  for snapshot in self.player.mover.history}  # fmt: skip
        self.assertEqual([speeds[frame] for frame in range(7039, 7043)], [5.5, 5.5, 8.1396484375, 8.25])
        (record,) = self.own(7049)
        self.assertEqual(math.hypot(record["velocity"][0], record["velocity"][2]), 8.25)


if __name__ == "__main__":
    unittest.main()
