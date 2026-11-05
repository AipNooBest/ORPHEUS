"""A failed weapon simulation cannot keep dealing damage through the live command route."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_game_combat as combat_tests

from ow174.game import combat
from ow174.game.commands import FIRE, Command
from ow174.game.script import runtime


class ScriptFaultIntegrationTests(unittest.TestCase):
    def test_exhausted_weapon_stops_the_body_before_combat_consumes_its_shots(self):
        run = combat_tests.Run(combat_tests.KANEZAKA_DM, ("A", "B"))
        shooter, other = run.players
        run.step({shooter: (0, 0, FIRE)})
        script = shooter.body_script
        rifle = script.component.instances[11]
        self.assertTrue(rifle.state(10).active)
        health = run.match.combat.vitals[other.body].total
        hits = len(shooter.client.messages(combat.HIT))

        # Harmless, valid edges overload one instance before its real release edge.
        # The old runtime stopped only the instance and returned success, leaving
        # the active volley available to the driver's shot reporter.
        frame = run.frame + 1
        for _ in range(runtime.EVENTS_PER_TIME + 2):
            rifle.post(runtime.Event(frame * script.frame_ms, 0, runtime.BUTTON, button=runtime.NO_BUTTON))
        with self.assertLogs("ow174.game", level="ERROR"):
            shooter.take_commands([Command(frame)])

        self.assertIsNone(shooter.body_script)
        self.assertTrue(script.retired)
        self.assertEqual(script.component.shots, [])
        self.assertEqual(run.match.combat.vitals[other.body].total, health)
        self.assertEqual(len(shooter.client.messages(combat.HIT)), hits)

        # A graph failure belongs to that body; it does not abort the match tick
        # or prevent another player's valid commands from running.
        other_script = other.body_script
        other.take_commands([Command(frame)])
        self.assertIs(other.body_script, other_script)
        self.assertEqual(other_script.frame, frame)
        shooter.take_commands([Command(frame + 1, buttons=FIRE)])
        self.assertEqual(run.match.combat.vitals[other.body].total, health)


if __name__ == "__main__":
    unittest.main()
