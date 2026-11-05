"""Observed native Soldier traces: exact inputs, read-only observation and honest incomplete results."""

import copy
import importlib.util
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.commands import FIRE, FRAME_MASK, Command
from ow174.game.content import SOLDIER
from ow174.game.native_replay import (
    COMMAND_FIELDS,
    make_command_event,
    make_end_event,
    make_header,
    parse_trace,
    replay_document,
    snapshot_hash,
    stored_snapshot,
)
from ow174.game.script import graph, runtime
from ow174.game.script.driver import BodyScript


def body(frame=1000):
    return BodyScript(SOLDIER, 0xA0000101, frame)


def header(script):
    return make_header(script, quanta_us=16000, command_epoch=0, body_epoch=1, server_tick=1000)


def command_event(script, command, seq):
    before = snapshot_hash(script)
    script.command(command.frame, command.buttons, command.action, command.forward, command.right)
    event = make_command_event(script, command, seq=seq + 1, server_tick=1001 + seq,
                               command_epoch=0, body_epoch=1, before_hash=before)
    script.component.shots.clear()  # the live combat consumer runs after the observation
    return event


def ending(script, seq):
    return make_end_event(script, seq=seq + 1, server_tick=1001 + seq, command_epoch=0, body_epoch=1)


def trace(commands=None):
    script = body()
    document = {"header": header(script), "events": []}
    for seq, command in enumerate(commands or [Command(1001, buttons=FIRE), Command(1002)]):
        document["events"].append(command_event(script, command, seq))
    document["events"].append(ending(script, len(document["events"])))
    return document


class NativeReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)

    def test_recording_helpers_never_evaluate_values_payloads_or_consume_shots_rng_or_sequences(self):
        script = body()
        script.command(1001, FIRE)
        shots = list(script.component.shots)
        rng, sequence = script.component.rng.getstate(), repr(script.component.seq)
        with (
            patch.object(runtime.Var, "value", side_effect=AssertionError("value evaluation")),
            patch.object(runtime.State, "payload", side_effect=AssertionError("payload evaluation")),
        ):
            first = snapshot_hash(script)
            self.assertEqual(snapshot_hash(script), first)
            context = header(script)
            event = make_command_event(script, Command(1001, buttons=FIRE), seq=1, server_tick=1001,
                                       before_hash=first, command_epoch=0, body_epoch=1)
            self.assertEqual(context["initial_hash"], event["after_hash"])
        self.assertEqual(script.component.shots, shots)
        self.assertEqual(script.component.rng.getstate(), rng)
        self.assertEqual(repr(script.component.seq), sequence)

    def test_full_native_command_fields_survive_and_replay_is_repeatable(self):
        command = Command(1001, forward=127, right=-10, yaw=200, pitch=-300, buttons=FIRE | 0x10000,
                          action=0, extra=105, subframe=128, click_yaw=90, click_pitch=-210)
        document = trace([command, Command(1002)])
        encoded = document["events"][0]["command"]
        self.assertEqual(set(encoded), set(COMMAND_FIELDS))
        self.assertEqual((encoded["extra"], encoded["subframe"], encoded["click_yaw"]), (105, 128, 90))
        first, second = replay_document(document), replay_document(copy.deepcopy(document))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "matched_observed_body_trace")
        self.assertFalse(first["complete_native_capture"])
        self.assertTrue(first["observed_trace_closed"])
        self.assertTrue(first["commands"][0]["shots"])

    def test_hash_distinguishes_allocation_history_that_changes_shot_order(self):
        # Fixed entry/activation order, different first access: identical active attributes,
        # but the real driver later reports the two volleys in different causal order.
        def with_volleys(order):
            script = body()
            nodes = [
                {"pos": index, "class": "STUStatescriptStateWeaponVolley", "state": index,
                 "uid": index, "rid": index, "remote": -1,
                 "client_only": False, "server_only": False,
                 "fields": {"m_2BBEEAB8": True, "m_ADEB6E05": False,
                            "m_A17BE89B": {"$": "STUConfigVarInt", "m_value": 10}}}
                for index in (0, 1)
            ]
            nodes.append({
                "pos": 2, "class": "STUStatescriptEntry", "state": None,
                "uid": 2, "rid": 2, "remote": -1,
                "client_only": False, "server_only": False,
                "fields": {"m_outPlug": {"$": "STUStatescriptOutputPlug",
                                         "links": [[0, "m_inPlug"], [1, "m_inPlug"]]}},
            })
            metadata = graph.Graph({
                "index": 0xFFFF, "header": {}, "fields": {}, "items": 3,
                "containers": {}, "nodes": nodes, "unknown_nodes": [],
                "states": [0, 1], "entries": [2], "remote": [], "sync_vars": [],
            })
            instance = script.component.create(metadata, init=False)
            for index in order:
                instance.state(index)
            instance.init()
            script.component.run_now()
            return script, instance

        forward, first = with_volleys((0, 1))
        reverse, second = with_volleys((1, 0))
        snapshots = [stored_snapshot(script) for script in (forward, reverse)]
        for snapshot in snapshots:
            for instance in snapshot["instances"]:
                instance.pop("state_order")
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertNotEqual(snapshot_hash(forward), snapshot_hash(reverse))
        orders = []
        for script, instance in ((forward, first), (reverse, second)):
            script.component.now += 15
            script._report_shots()
            orders.append([state.index for state, _, _ in script.component.shots
                           if state.instance is instance])
        self.assertEqual(orders, [[0, 1], [1, 0]])

    def test_unrecorded_first_state_access_is_detected_before_next_command(self):
        script = body()
        document = {"header": header(script), "events": []}
        instance, index = next(
            (instance, node.state)
            for instance in script.component.instances.values()
            for node in instance.graph.states
            if node is not None and node.state not in instance.states
        )
        state = instance.state(index)
        self.assertFalse(state.active)
        self.assertEqual(state.counter, 0)
        document["events"].append(command_event(script, Command(1001), 0))
        document["events"].append(ending(script, 1))
        result = replay_document(document)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["validated_commands"], 0)
        self.assertEqual(result["diagnostics"][0]["kind"], "before_state_mismatch")

    def test_unrecorded_external_health_change_is_not_called_a_complete_replay(self):
        script = body()
        document = {"header": header(script), "events": [command_event(script, Command(1001), 0)]}
        script.set_health(100, 200)
        document["events"].append(command_event(script, Command(1002), 1))
        document["events"].append(ending(script, 2))
        result = replay_document(document)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["validated_commands"], 1)
        self.assertEqual(result["diagnostics"][0]["kind"], "before_state_mismatch")

    def test_native_driver_gap_fill_is_never_silently_used(self):
        document = trace([Command(1001), Command(1004)])  # source native driver did run 1002/1003
        result = replay_document(document)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["validated_commands"], 1)
        self.assertEqual(result["diagnostics"][0],
                         {"kind": "source_gap", "seq": 2, "first": 1002, "last": 1003, "count": 2})

    def test_old_and_duplicate_commands_do_not_run_a_frame_again(self):
        result = replay_document(trace([Command(1001, buttons=FIRE), Command(1001, buttons=FIRE),
                                        Command(900), Command(1002)]))
        self.assertEqual(result["status"], "matched_observed_body_trace")
        self.assertEqual([row["disposition"] for row in result["commands"]],
                         ["applied", "ignored_old_or_duplicate", "ignored_old_or_duplicate", "applied"])

    def test_model_and_initial_state_mismatches_are_explicit(self):
        for name, expected in (("model_hash", "model_mismatch"), ("initial_hash", "initial_state_mismatch")):
            document = trace()
            document["header"][name] = "0" * 64
            result = replay_document(document)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["diagnostics"][0]["kind"], expected)

    def test_hash_or_shot_tampering_is_detected(self):
        for field in ("before_hash", "after_hash", "shots"):
            document = trace()
            document["events"][0][field] = [] if field == "shots" else "0" * 64
            result = replay_document(document)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["validated_commands"], 0)

    def test_jsonl_and_document_forms_match_and_open_or_truncated_tails_are_incomplete(self):
        document = trace()
        lines = [json.dumps({"header": document["header"]})]
        lines.extend(json.dumps(event) for event in document["events"])
        self.assertEqual(replay_document(parse_trace("\n".join(lines))), replay_document(document))
        self.assertEqual(parse_trace(json.dumps(document)), document)
        for text in ("\n".join(lines[:-1]), "\n".join([*lines[:-1], '{"kind":'])):
            result = replay_document(parse_trace(text))
            self.assertEqual(result["status"], "incomplete")
            self.assertFalse(result["observed_trace_closed"])

    def test_unknown_fault_missing_sequence_and_events_after_end_are_incomplete(self):
        variants = []
        unknown, fault, sequence, after = (copy.deepcopy(trace()) for _ in range(4))
        unknown["events"][0]["kind"] = "unrecorded_world_action"
        fault["events"][0] = {"kind": "fault", "error": "native body stopped"}
        sequence["events"][1]["seq"] = 5
        after["events"].append(copy.deepcopy(after["events"][-1]))
        variants.extend((unknown, fault, sequence, after))
        for document in variants:
            self.assertEqual(replay_document(document)["status"], "incomplete")

    def test_native_uint32_rollover_requires_a_new_body_lifecycle(self):
        script = body(FRAME_MASK)
        context = header(script)
        event = make_command_event(script, Command(0), seq=1, server_tick=1001,
                                   before_hash=snapshot_hash(script), command_epoch=0, body_epoch=1)
        result = replay_document({"header": context, "events": [event, ending(script, 1)]})
        self.assertEqual(result["diagnostics"][0]["kind"], "epoch_boundary")
        self.assertEqual(result["validated_commands"], 0)

    def test_unsupported_seed_policy_and_missing_native_fields_are_errors(self):
        for key, value in (("seed", 2), ("gap_policy", "guess_held_input")):
            document = trace()
            document["header"][key] = value
            with self.assertRaises(ValueError):
                replay_document(document)
        document = trace()
        del document["events"][0]["command"]["extra"]
        with self.assertRaises(ValueError):
            replay_document(document)
        document = trace()
        document["header"]["maximum_health"] = document["header"]["health"] + 1
        with self.assertRaisesRegex(ValueError, "health == maximum_health"):
            replay_document(document)
        document = trace()
        document["header"]["body_definition"] += 1
        with self.assertRaisesRegex(ValueError, "Body definition"):
            replay_document(document)

    def test_match_external_first_access_is_not_mistaken_for_a_constructor_only_trace(self):
        from test_game_combat import Run

        run = Run()
        player = run.players[0]
        document = {"header": header(player.body_script), "events": []}
        context = document["header"]
        initial = BodyScript(
            context["hero"], context["entity"], context["start_frame"],
            quanta=context["quanta_us"], mode=context["mode"], health=context["health"],
            ult_seconds=context["ult_seconds"],
        )
        # Match hooks, including projectile-lease discovery, can allocate inactive states.
        # Those reads affect later traversal order; the body-only trace does not replay them.
        actual_snapshot, initial_snapshot = (
            stored_snapshot(script) for script in (player.body_script, initial)
        )
        actual_order, initial_order = [], []
        for snapshot, orders in ((actual_snapshot, actual_order), (initial_snapshot, initial_order)):
            for instance in snapshot["instances"]:
                orders.append(instance.pop("state_order"))
        self.assertEqual(actual_snapshot, initial_snapshot)
        self.assertNotEqual(actual_order, initial_order)
        original = run.match.body_command

        def observe(current, command):
            before = snapshot_hash(current.body_script)
            original(current, command)
            seq = len(document["events"])
            document["events"].append(make_command_event(
                current.body_script, command, seq=seq + 1, server_tick=run.match.tick,
                before_hash=before, command_epoch=0, body_epoch=1,
            ))

        with patch.object(run.match, "body_command", side_effect=observe):
            for frame in range(12):
                run.step({player: (0, 0, FIRE if frame < 8 else 0)})
        document["events"].append(make_end_event(
            player.body_script, seq=len(document["events"]) + 1, server_tick=run.match.tick,
            command_epoch=0, body_epoch=1,
        ))
        result = replay_document(document)
        self.assertEqual(len(document["events"]), 13)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["validated_commands"], 0)
        self.assertEqual(result["diagnostics"], [{"kind": "initial_state_mismatch"}])

    def test_cli_accepts_jsonl_and_distinguishes_incomplete_and_hash_mismatch(self):
        path = Path(__file__).resolve().parents[1] / "tools" / "replay_native_body.py"
        spec = importlib.util.spec_from_file_location("test_replay_native_body_tool", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        document = trace()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "trace.jsonl", Path(directory) / "replay.json"
            source.write_text(
                "\n".join([json.dumps({"header": document["header"]}),
                           *(json.dumps(event) for event in document["events"])]), encoding="utf-8",
            )
            args = [str(source), "--output", str(output)]
            self.assertEqual(module.main(args), 0)
            self.assertEqual(module.main([*args, "--expect-state-hash", "0" * 64]), 2)
            source.write_text(json.dumps({"header": document["header"]}), encoding="utf-8")
            self.assertEqual(module.main(args), 3)

    def test_state_hash_matches_in_fresh_processes_with_different_hash_seeds(self):
        code = (
            "import sys;sys.path.insert(0,'tests');"
            "from test_native_replay import trace;"
            "from ow174.game.native_replay import replay_document;"
            "print(replay_document(trace())['state_hash'])"
        )
        results = [subprocess.check_output([sys.executable, "-B", "-c", code],
                                          env={**os.environ, "PYTHONHASHSEED": seed}, text=True)
                   for seed in ("1", "99")]
        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
