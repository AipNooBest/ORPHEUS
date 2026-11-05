"""The game server's packet frame stays on real time (ow174/game/server.py next_tick_after): ticks that fell
behind are skipped, not run late, as retail's frame counts 62.5 per second."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.server import TICK_SECONDS, GameServer, next_tick_after


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

    def test_world_sends_once_on_crossed_boundary_after_skipped_ticks(self):
        server = GameServer()
        server.tick = 1001
        client = SimpleNamespace(heard=100.0, leaving_since=None, open=True, send_frame=Mock())
        match = SimpleNamespace(ended=False, update=Mock())
        server.clients[1] = client
        server.matches.append(match)

        # Miss the send phase repeatedly, then cross two boundaries in one step.
        # A late frame can be followed by the next regular send in 1 or 2 ticks.
        for missed in (0, 3, 2, 2, 0, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0):
            tick = server.tick + missed + 1
            server._tick(100.0 + (tick - 1001) * TICK_SECONDS, missed=missed)

        sent = [call.args[0] for call in client.send_frame.call_args_list]
        self.assertEqual(sent, [1002, 1006, 1009, 1012, 1014, 1022, 1023, 1026, 1029])
        prepared = [call.args[1] for call in match.update.call_args_list if call.kwargs["frames"]]
        self.assertEqual(prepared, sent)


if __name__ == "__main__":
    unittest.main()
