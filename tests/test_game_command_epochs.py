"""Late packets do not reset command consumers, and a real uint32 rollover is explicit."""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_game_combat as combat_tests
import test_game_commands as command_tests

from ow174.game.bits import BitReader
from ow174.game.commands import FRAME_HALF, FRAME_MASK, Command, CommandQueue, InputError, read_commands
from ow174.game.content import SOLDIER
from ow174.game.script.driver import BodyScript, CommandEpochError


class QueueEpochTests(unittest.TestCase):
    def test_a_far_older_packet_cannot_reset_the_queue(self):
        queue = CommandQueue()
        queue.new([Command(5000, buttons=1)])
        self.assertEqual(queue.new([Command(1, buttons=0)]), [])
        self.assertEqual((queue.last_frame, queue.epoch), (5000, 0))
        self.assertEqual([item.frame for item in queue.new([Command(5001)])], [5001])

    def test_overlap_duplicates_and_old_windows_are_delivered_once(self):
        queue = CommandQueue()
        self.assertEqual(
            [item.frame for item in queue.new([Command(10), Command(10), Command(11)])], [10, 11]
        )
        self.assertEqual([item.frame for item in queue.new([Command(9), Command(11), Command(12)])], [12])
        self.assertEqual(queue.new([Command(10), Command(11)]), [])
        self.assertEqual(queue.last_frame, 12)

    def test_uint32_rollover_and_a_late_pre_rollover_packet(self):
        queue = CommandQueue()
        queue.new([Command(FRAME_MASK - 1), Command(FRAME_MASK)])
        self.assertEqual([item.frame for item in queue.new([Command(0), Command(1)])], [0, 1])
        self.assertEqual((queue.last_frame, queue.wraps), (1, 1))
        self.assertEqual(queue.new([Command(FRAME_MASK)]), [])
        self.assertEqual(queue.last_frame, 1)

    def test_exactly_half_the_serial_space_is_ambiguous(self):
        queue = CommandQueue()
        queue.new([Command(10)])
        self.assertEqual(queue.new([Command(10 + FRAME_HALF)]), [])
        self.assertEqual(queue.last_frame, 10)

    def test_only_an_explicit_reset_starts_an_epoch(self):
        queue = CommandQueue()
        queue.new([Command(5000)])
        queue.reset()
        self.assertEqual(queue.epoch, 1)
        self.assertEqual([item.frame for item in queue.new([Command(1)])], [1])
        queue.reset(last_frame=20)
        self.assertEqual((queue.epoch, queue.last_frame), (2, 20))
        self.assertEqual(queue.new([Command(19)]), [])

    def test_malformed_windows_are_rejected_before_advancing(self):
        queue = CommandQueue()
        queue.new([Command(10)])
        for invalid in (-1, FRAME_MASK + 1, True):
            with self.subTest(invalid=invalid), self.assertRaises(InputError):
                queue.new([Command(11), Command(invalid)])
            self.assertEqual(queue.last_frame, 10)

    def test_compressed_command_frames_wrap_as_uint32(self):
        commands = read_commands(BitReader(command_tests.record(FRAME_MASK)))
        self.assertEqual([item.frame for item in commands], [FRAME_MASK, 0])

    def test_bounded_prefix_reports_zero_on_rollover_without_admitting_the_suffix(self):
        queue = CommandQueue()
        queue.reset(last_frame=FRAME_MASK - 1)
        admitted, boundary = queue.before_limit([Command(FRAME_MASK), Command(0), Command(1)], FRAME_MASK)
        self.assertEqual([item.frame for item in admitted], [FRAME_MASK])
        self.assertEqual((boundary, queue.last_frame, queue.wraps), (0, FRAME_MASK, 0))


class DriverEpochTests(unittest.TestCase):
    @staticmethod
    def body(frame=5000):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        return BodyScript(SOLDIER, 0xA0000101, frame)

    def test_delayed_old_release_does_not_hide_the_next_valid_release(self):
        body = self.body()
        queue = CommandQueue()
        for item in queue.new([Command(5001, buttons=1)]):
            body.command(item.frame, item.buttons)
        rifle = body.component.instances[11]
        self.assertTrue(rifle.state(10).active)
        self.assertEqual(queue.new([Command(1)]), [])
        for item in queue.new([Command(5002)]):
            body.command(item.frame, item.buttons)
        self.assertEqual(body.frame, 5002)
        self.assertFalse(rifle.state(10).active)
        self.assertEqual(body.buttons, 0)
        self.assertNotIn(20, body.component.held)

    def test_real_rollover_is_an_error_before_timers_or_input_change(self):
        body = self.body(FRAME_MASK - 1)
        body.command(FRAME_MASK, 1)
        before = (body.frame, body.buttons, body.component.now, body.snapshot())
        with self.assertRaises(CommandEpochError) as caught:
            body.command(0, 0)
        self.assertEqual((caught.exception.previous, caught.exception.incoming), (FRAME_MASK, 0))
        self.assertEqual((body.frame, body.buttons, body.component.now, body.snapshot()), before)

    def test_a_pre_rollover_packet_in_a_fresh_epoch_is_old(self):
        body = self.body(0)
        body.command(1, 0)
        before = body.snapshot()
        body.command(FRAME_MASK, 1)
        self.assertEqual(body.snapshot(), before)
        self.assertEqual(body.buttons, 0)

    def test_non_uint32_commands_are_rejected(self):
        body = self.body()
        for invalid in (-1, FRAME_MASK + 1, True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                body.command(invalid, 0)


class KillEpochRegressionTests(unittest.TestCase):
    def test_old_packet_after_a_kill_cannot_split_queue_and_weapon_frames(self):
        # Reproduce the real command route, including the damage/kill notices and the
        # body's owner-frame generation. A late old frame used to reset only the queue.
        case = combat_tests.DeathTests("test_the_killer_stops_when_he_lets_go_and_spends_ammo")
        case.setUp()
        case.kill_b()
        player = case.a
        while case.run.frame < 1100:
            case.run.step({player: (0, 0, 1)})
        script = player.body_script
        rifle = script.component.instances[11]
        self.assertTrue(rifle.state(10).active)
        player.take_commands([Command(1, buttons=0)])
        self.assertEqual((player.commands.last_frame, script.frame), (1100, 1100))
        player.take_commands([Command(1101, buttons=0)])
        self.assertIs(player.body_script, script)
        self.assertEqual((player.commands.last_frame, script.frame, script.buttons), (1101, 1101, 0))
        self.assertFalse(rifle.state(10).active)
        ammo = rifle.vars[53].value()
        for number in range(1102, 1122):
            player.take_commands([Command(number, buttons=1)])
        self.assertLess(rifle.vars[53].value(), ammo)


if __name__ == "__main__":
    unittest.main()
