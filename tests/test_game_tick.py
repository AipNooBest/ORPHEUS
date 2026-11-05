"""The game server's packet frame stays on real time (ow174/game/server.py next_tick_after): ticks that fell
behind are skipped, not run late, as retail's frame counts 62.5 per second."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.server import TICK_SECONDS, next_tick_after


class TickTests(unittest.TestCase):
    def test_on_time_the_next_tick_is_one_period_on(self):
        due, missed = next_tick_after(10.0, 10.001)
        self.assertAlmostEqual(due, 10.0 + TICK_SECONDS)
        self.assertEqual(missed, 0)

    def test_after_a_stall_the_missed_ticks_are_skipped(self):
        # due at 10.0, the loop got there 50 ms late: the ticks of 10.016, 10.032 and 10.048 are gone
        due, missed = next_tick_after(10.0, 10.050)
        self.assertEqual(missed, 3)
        self.assertAlmostEqual(due, 10.0 + 4 * TICK_SECONDS)
        self.assertGreater(due, 10.050)

    def test_the_frame_count_follows_the_clock(self):
        # a loop that runs late now and then still counts one frame per 16 ms of real time
        now, due, frames = 0.0, 0.0, 0
        for step in range(2000):
            now += 0.016 if step % 50 else 0.300  # a 300 ms stall every 50 ticks
            if now >= due:
                due, missed = next_tick_after(due, now)
                frames += missed + 1
        self.assertLessEqual(abs(frames - now / TICK_SECONDS), 2)


if __name__ == "__main__":
    unittest.main()
