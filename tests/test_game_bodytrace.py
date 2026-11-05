"""The live command path records observational traces without changing gameplay."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_game_combat as combat_tests

from ow174.game import bodytrace, native_replay
from ow174.game.commands import FIRE, Command


class BodyTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "traces"
        for key, value in (("ENABLED", True), ("FOLDER", self.folder)):
            patch = mock.patch.object(bodytrace, key, value)
            patch.start()
            self.addCleanup(patch.stop)

    def read(self):
        paths = sorted(self.folder.glob("*.jsonl"))
        return [
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] for path in paths
        ]

    def test_creation_commands_and_retirement_have_a_continuous_trace(self):
        run = combat_tests.Run()
        player = run.players[0]
        script = player.body_script
        self.addCleanup(run.match.stop_body_script, player)
        for number in range(1, 11):
            run.match.tick = run.frame + number
            click = number <= 5
            command = Command(
                run.frame + number, buttons=(FIRE | 0x10000) if click else 0,
                extra=313, subframe=7 if click else 255,
                click_yaw=-192 if click else None, click_pitch=233 if click else None,
            )
            player.take_commands([command])
        run.match.stop_body_script(player)
        (lines,) = self.read()
        header = lines[0]["header"]
        events = lines[1:]
        self.assertEqual(header["schema"], "ow174.native-body-replay.v2")
        self.assertEqual(header["start_frame"], run.frame)
        self.assertEqual([event["seq"] for event in events], list(range(1, 12)))
        self.assertEqual([event["kind"] for event in events], ["command"] * 10 + ["end"])
        self.assertEqual(events[0]["before_hash"], header["initial_hash"])
        self.assertEqual(events[-1]["final_hash"], events[-2]["after_hash"])
        self.assertEqual(events[0]["command"]["click_yaw"], -192)
        self.assertEqual(events[0]["command"]["extra"], 313)
        self.assertEqual(len(events[0]["command"]), 11)
        self.assertTrue(script.retired)
        self.assertFalse(any(event["kind"] == "fault" for event in events))
        result = native_replay.replay_document({"header": header, "events": events})
        self.assertEqual(result["status"], "matched_observed_body_trace", result["diagnostics"])
        self.assertEqual(result["validated_commands"], 10)
        self.assertFalse(result["complete_native_capture"])

    def test_disabled_capture_creates_no_files(self):
        with mock.patch.object(bodytrace, "ENABLED", False):
            run = combat_tests.Run()
            run.step({})
            run.match.stop_body_script(run.players[0])
        self.assertFalse(self.folder.exists())

    def test_recording_io_error_does_not_prevent_body_creation_or_valid_commands(self):
        with (
            mock.patch.object(bodytrace.Writer, "write", side_effect=OSError("test disk failure")),
            self.assertLogs("ow174.game", level="WARNING"),
        ):
            run = combat_tests.Run()
        player = run.players[0]
        self.assertIsNotNone(player.body_script)
        script = player.body_script
        run.step({player: (0, 0, FIRE)})
        self.assertIs(player.body_script, script)
        self.assertIsNone(script.component.fault)
        self.assertTrue(script.component.instances[11].state(10).active)
        run.match.stop_body_script(player)

    def test_command_fault_closes_capture_with_an_explicit_incomplete_result(self):
        run = combat_tests.Run()
        player = run.players[0]
        script = player.body_script
        with (
            mock.patch.object(script, "command", side_effect=RuntimeError("test script failure")),
            self.assertLogs("ow174.game", level="WARNING"),
        ):
            player.take_commands([Command(run.frame + 1)])
        self.assertIsNone(player.body_script)
        (path,) = self.folder.glob("*.jsonl")
        result = native_replay.replay_document(native_replay.parse_trace(path.read_text(encoding="utf-8")))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["diagnostics"][0]["kind"], "capture_fault")


if __name__ == "__main__":
    unittest.main()
