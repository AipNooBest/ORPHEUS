"""Offline timeline clocks, serial frames, evidence-preserving gaps and repeatable replay."""

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.commands import Command
from ow174.game.simulation import (
    BUILD,
    FRAME_HALF,
    FRAME_MASK,
    CommandRecording,
    CommandTimeline,
    replay_document,
)


def recording(*batches, quanta=20000):
    capture = CommandRecording(quanta, {"p1": {"position": [0, 1, 0], "yaw": 0}})
    for server_tick, epoch, frames in batches:
        capture.record("p1", epoch, server_tick, [Command(frame, forward=127) for frame in frames])
    return capture.to_document()


class TimelineTests(unittest.TestCase):
    def test_u32_wrap_is_consecutive_without_restart_or_missing_input(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 8, [Command(frame) for frame in (FRAME_MASK - 1, FRAME_MASK, 0, 1)])
        self.assertEqual([item.unwrapped_frame for item in timeline.ordered("p1", 0)],
                         [FRAME_MASK - 1, FRAME_MASK, FRAME_MASK + 1, FRAME_MASK + 2])
        self.assertEqual(timeline.summary("p1", 0, 20000)["missing_ranges"], [])
        self.assertEqual(timeline.diagnostics, [])

    def test_overlap_and_out_of_order_commands_are_collected_once_then_sorted(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 10, [Command(102), Command(103)])
        self.assertEqual(timeline.ingest("p1", 0, 11, [Command(100), Command(101), Command(102)]), 2)
        self.assertEqual([item.command.frame for item in timeline.ordered("p1", 0)], [100, 101, 102, 103])
        summary = timeline.summary("p1", 0, 20000)
        self.assertEqual((summary["duplicates"], summary["out_of_order"]), (1, 2))
        self.assertEqual(summary["missing_ranges"], [])

    def test_late_command_before_wrap_maps_to_the_previous_cycle(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 1, [Command(0), Command(1)])
        timeline.ingest("p1", 0, 2, [Command(FRAME_MASK)])
        self.assertEqual([item.unwrapped_frame for item in timeline.ordered("p1", 0)],
                         [FRAME_MASK, FRAME_MASK + 1, FRAME_MASK + 2])
        self.assertEqual(timeline.diagnostics, [])

    def test_half_cycle_is_rejected_instead_of_guessing_a_cycle(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 1, [Command(0)])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            timeline.ingest("p1", 0, 2, [Command(FRAME_HALF)])

    def test_same_frame_with_different_payload_is_an_error(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 1, [Command(7)])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            timeline.ingest("p1", 0, 2, [Command(7, forward=127)])

    def test_backwards_jump_reports_candidate_and_does_not_guess_a_new_epoch(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 1, [Command(5000)])
        timeline.ingest("p1", 0, 2, [Command(1)])
        self.assertEqual(set(timeline.tracks), {("p1", 0)})
        self.assertEqual([item.command.frame for item in timeline.ordered("p1", 0)], [1, 5000])
        self.assertEqual(timeline.diagnostics[0]["kind"], "restart_candidate")
        self.assertEqual(timeline.diagnostics[0]["resolution"], "kept_in_declared_epoch")

    def test_explicit_epochs_allow_frame_reuse_and_late_old_epoch_packets(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 1, [Command(5000)])
        timeline.ingest("p1", 1, 2, [Command(1)])
        timeline.ingest("p1", 0, 3, [Command(5001)])
        self.assertEqual(len(timeline.ordered("p1", 0)), 2)
        self.assertEqual(len(timeline.ordered("p1", 1)), 1)
        self.assertEqual(timeline.diagnostics, [])

    def test_recorded_server_ticks_may_repeat_but_must_not_go_backwards(self):
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 10, [Command(100)])
        timeline.ingest("p1", 0, 10, [Command(101)])
        with self.assertRaisesRegex(ValueError, "recording order"):
            timeline.ingest("p1", 0, 9, [Command(102)])


class ReplayTests(unittest.TestCase):
    def test_v1_recording_keeps_exactly_the_original_seven_command_fields(self):
        document = recording((1, 0, [1]))
        command = document["packets"][0]["commands"][0]
        self.assertEqual(set(command), {"frame", "forward", "right", "yaw", "pitch", "buttons", "action"})
        timeline = CommandTimeline()
        timeline.ingest("p1", 0, 1, [Command(**command)])
        decoded = timeline.ordered("p1", 0)[0].command
        self.assertEqual((decoded.extra, decoded.subframe, decoded.click_yaw, decoded.click_pitch),
                         (0, 0xFF, None, None))
        self.assertEqual(len(replay_document(document)["states"]), 1)

    def test_recording_nondefault_view_delay_or_subframe_data_is_explicitly_unsupported(self):
        for extension in ({"extra": 1}, {"subframe": 0}, {"click_yaw": 0}, {"click_pitch": 0}):
            with self.subTest(extension=extension):
                capture = CommandRecording(20000, {"p1": {"position": [0, 1, 0], "yaw": 0}})
                with self.assertRaisesRegex(ValueError, "v1 cannot record nondefault"):
                    capture.record("p1", 0, 1, [Command(1, **extension)])
                self.assertEqual(capture.to_document()["packets"], [])
                timeline = CommandTimeline()
                with self.assertRaisesRegex(ValueError, "v1 cannot record nondefault"):
                    timeline.ingest("p1", 0, 1, [Command(1, **extension)])
                self.assertEqual(timeline.tracks, {})

    def test_json_v1_rejects_extension_fields_even_when_their_values_are_defaults(self):
        for extension in ({"extra": 0}, {"subframe": 0xFF}, {"click_yaw": None}, {"click_pitch": None}):
            document = recording((1, 0, [1]))
            document["packets"][0]["commands"][0].update(extension)
            with (
                self.subTest(extension=extension),
                self.assertRaisesRegex(ValueError, "exactly these fields"),
            ):
                replay_document(document)

    def test_basic_fixture_keeps_its_pre_import_state_and_recording_hashes(self):
        path = Path(__file__).resolve().parents[1] / "data" / "replays" / "basic.json"
        result = replay_document(json.loads(path.read_text(encoding="utf-8-sig")))
        self.assertEqual(
            result["state_hash"], "4c2d8324e24235864e14428ea83122e899269a4a8e7a77f4077d376687a6ea04"
        )
        self.assertEqual(
            result["recording_hash"], "0ce43846f0fee0e18c33cd9da2f41054a3ab0045be28d288f946218449ac06ba"
        )

    def test_missing_frames_report_source_span_without_fabricating_steps(self):
        result = replay_document(recording((700, 0, [100, 103])))
        states, summary = result["states"], result["summaries"][0]
        self.assertEqual(len(states), 2)
        self.assertAlmostEqual(states[-1]["position"][2], 0.22)
        self.assertEqual(summary["applied_time_us"], 40000)
        self.assertEqual(summary["source_frame_span"], 3)
        self.assertEqual(summary["source_span_us"], 60000)
        self.assertEqual(summary["missing_ranges"], [
            {"first_unwrapped_frame": 101, "last_unwrapped_frame": 102, "count": 2},
        ])
        self.assertEqual([state["source_offset_us"] for state in states], [0, 60000])
        self.assertEqual([state["applied_time_us"] for state in states], [20000, 40000])

    def test_identical_input_has_identical_states_and_hashes(self):
        document = recording((900, 0, [20, 21]), (950, 0, [21, 22]))
        first, second = replay_document(document), replay_document(copy.deepcopy(document))
        self.assertEqual(first, second)
        self.assertEqual(len(first["state_hash"]), 64)

    def test_duplicate_arrival_ticks_and_late_delivery_do_not_change_state_hash(self):
        ordered = replay_document(recording((4, 0, [100, 101, 102, 103])))
        reordered = replay_document(recording((500, 0, [102, 103]), (600, 0, [100, 101, 102])))
        self.assertEqual(ordered["states"], reordered["states"])
        self.assertEqual(ordered["state_hash"], reordered["state_hash"])
        self.assertNotEqual(ordered["recording_hash"], reordered["recording_hash"])

    def test_explicit_epoch_resets_model_state_and_reuses_source_frames(self):
        result = replay_document(recording((1, 0, [10]), (2, 1, [10])))
        self.assertEqual([state["epoch"] for state in result["states"]], [0, 1])
        self.assertEqual([state["position"] for state in result["states"]], [[0, 1, 0.11]] * 2)
        self.assertEqual(result["diagnostics"], [])

    def test_reordered_packets_across_wrap_have_the_same_canonical_state_hash(self):
        ordered = replay_document(recording((1, 0, [FRAME_MASK, 0, 1])))
        late = replay_document(recording((1, 0, [0, 1]), (2, 0, [FRAME_MASK])))
        self.assertEqual(ordered["states"], late["states"])
        self.assertEqual(ordered["state_hash"], late["state_hash"])

    def test_decoded_angle_deltas_are_preserved_without_inventing_additional_wraps(self):
        capture = CommandRecording(20000, {"p1": {"position": [0, 1, 0], "yaw": 0}})
        capture.record("p1", 0, 1, [Command(1, forward=127, yaw=32780)])
        result = replay_document(capture.to_document())
        self.assertEqual(result["states"][0]["yaw"], 32780)

    def test_quanta_is_the_explicit_model_step_and_not_server_tick_delta(self):
        first = replay_document(recording((1, 0, [2]), (99999, 0, [3]), quanta=10000))
        second = replay_document(recording((1, 0, [2]), (1, 0, [3]), quanta=20000))
        self.assertEqual(first["states"][-1]["position"][2], 0.11)
        self.assertEqual(second["states"][-1]["position"][2], 0.22)

    def test_players_have_independent_timelines_and_initial_states(self):
        capture = CommandRecording(20000, {
            "a": {"position": [0, 1, 0], "yaw": 0},
            "b": {"position": [10, 2, 20], "yaw": 0},
        })
        capture.record("b", 0, 1, [Command(5, forward=127)])
        capture.record("a", 0, 1, [Command(5, forward=127)])
        states = replay_document(capture.to_document())["states"]
        self.assertEqual([state["player"] for state in states], ["a", "b"])
        self.assertEqual(states[0]["position"], [0, 1, 0.11])
        self.assertEqual(states[1]["position"], [10, 2, 20.11])

    def test_exported_recording_is_independent_of_later_records(self):
        capture = CommandRecording(20000, {"p1": {"position": [0, 1, 0], "yaw": 0}})
        before = capture.to_document()
        capture.record("p1", 0, 1, [Command(1)])
        self.assertEqual(before["packets"], [])
        self.assertEqual(len(capture.to_document()["packets"]), 1)

    def test_wrong_build_schema_fields_and_invalid_numbers_are_rejected(self):
        original = recording((1, 0, [1]))
        for change in ({"build": "1.68"}, {"schema": "unknown"}, {"quanta_us": 0}, {"quanta_us": True},
                       {"unknown": 1}):
            bad = copy.deepcopy(original)
            bad.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                replay_document(bad)
        self.assertEqual(original["build"], BUILD)

    def test_empty_recording_has_stable_empty_state_hash(self):
        result = replay_document(recording())
        self.assertEqual(result["states"], [])
        self.assertEqual(result["summaries"], [])
        self.assertEqual(result["state_hash"], replay_document(recording())["state_hash"])

    def test_offline_cli_writes_result_and_detects_hash_mismatch(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "replay_game_commands.py"
        spec = importlib.util.spec_from_file_location("replay_game_commands_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "capture.json", Path(temporary) / "result.json"
            source.write_text(json.dumps(recording((1, 0, [1]))), encoding="utf-8")
            args = [str(source), "--output", str(output)]
            self.assertEqual(module.main(args), 0)
            expected = json.loads(output.read_text(encoding="utf-8"))["state_hash"]
            self.assertEqual(module.main([*args, "--expect-state-hash", expected]), 0)
            with patch("sys.stderr"):
                self.assertEqual(module.main([*args, "--expect-state-hash", "0" * 64]), 2)


if __name__ == "__main__":
    unittest.main()
