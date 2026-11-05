"""Full VM storage observations preserve causality without executing the VM."""

import copy
import hashlib
import math
import random
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import replay_values
from ow174.game.body_vm_snapshot import (
    BodyVMSnapshotError,
    export_body_vm,
    export_component_vm,
    snapshot_digest,
)
from ow174.game.content import SOLDIER
from ow174.game.script import expr, graph, nodes, runtime
from ow174.game.script.driver import BodyScript

STATE_MODELS = (
    ("STU_TestPlainState", runtime.State),
    ("STUStatescriptStateBooleanSwitch", nodes.BooleanSwitch),
    ("STUStatescriptStateStack", nodes.Stack),
    ("STUStatescriptStateLogicalButton", nodes.LogicalButton),
    ("STU_E4A30BCF", nodes.DisableLogicalButton),
    ("STUStatescriptStateWait", nodes.Wait),
    ("STU_E3CEF6B9", nodes.WaitForServerFrames),
    ("STU_D74D4F47", nodes.WaitFrames),
    ("STUStatescriptStateChaseVar", nodes.ChaseVar),
    ("STUStatescriptStateWeaponVolley", nodes.WeaponVolley),
    ("STUStatescriptStateAnim", nodes.Anim),
    ("STUStatescriptStateAbility", nodes.Ability),
    ("STU_9C9A422A", nodes.Defer),
    ("STU_2F4E2E3F", nodes.Watch),
    ("STU_DEBD057B", nodes.Switch),
    ("STU_37D754C8", nodes.SubScript),
    ("STUStatescriptStateGameMessageEntry", nodes.GameMessageEntry),
    ("STU_38EE1100", nodes.SendGameMessageState),
    ("STUStatescriptStateCosmeticEntity", nodes.CosmeticEntity),
    ("STUStatescriptStateGamePadVibration", nodes.GamePadVibration),
    ("STUStatescriptStateTrackTargets", nodes.TrackTargets),
    ("STU_691BFA55", nodes.Effect),
    ("STU_B9898052", nodes.ClientOnlyPulser),
    ("STU_01178155", nodes.ActiveQuery),
)
COMPONENT_FIELDS = {
    "entity", "frame_ms", "world", "rng", "frame", "now", "instances", "vars", "held", "button",
    "disabled", "seq", "next_id", "destroyed", "shots", "owner_dead", "fault", "_operation_depth",
    "_follow_depth", "_work_left",
}
INSTANCE_FIELDS = {
    "component", "id", "graph", "parent", "parent_state", "vars", "states", "queue", "ticking",
    "deferred", "params", "stopped", "teardown", "initialized", "start",
}
STATE_FIELDS = {
    "instance", "node", "index", "active", "ending", "counter", "start", "ended_at", "ticked_at",
    "watched", "flags_changed", "flags_check", "polled", "natives",
}
EVENT_FIELDS = {
    "time", "seq", "kind", "state", "op", "param", "node", "button", "pressed", "message", "transient",
}
TRANSPORT_FIELDS = {"stream", "create_arrived", "first_owner_frame", "reports", "report_count"}


def minimal_graph(classes=("STU_TestPlainState",), index=1):
    return graph.Graph({
        "index": index, "header": {}, "fields": {}, "items": len(classes), "containers": {},
        "nodes": [{
            "pos": number, "class": cls, "uid": number + 10, "rid": number, "remote": -1,
            "state": number, "client_only": 0, "server_only": 0, "fields": {"m_2BBEEAB8": 1},
        } for number, cls in enumerate(classes)],
        "unknown_nodes": [], "states": list(range(len(classes))), "entries": [], "remote": [],
        "sync_vars": [],
    })


def fixture(classes=("STU_TestPlainState",), allocation=None):
    component = runtime.Component(123)
    instance = component.create(minimal_graph(classes), init=False)
    for index in range(len(classes)) if allocation is None else allocation:
        instance.state(index)
    return component, instance


def raw(document):
    return replay_values.decode(document["value"])


def record(data, reference):
    return next(item for item in data["objects"] if item["ref"] == reference)


def fields(data, reference):
    return dict(record(data, reference)["fields"])


def ref(*identity):
    return {"ref": list(identity)}


def elements(value):
    return value["items"]


def digest(component):
    return snapshot_digest(export_component_vm(component))


class BodyVMSnapshotTests(unittest.TestCase):
    def test_all_allocated_states_and_component_storage_are_present_without_activation(self):
        component, instance = fixture(("STU_TestPlainState",) * 3, allocation=(2, 0, 1))
        data = raw(export_component_vm(component))
        self.assertEqual(set(dict(data["component"])), COMPONENT_FIELDS)
        self.assertEqual(set(fields(data, ["instance", 1])), INSTANCE_FIELDS)
        allocation = elements(fields(data, ["instance", 1])["states"])
        self.assertEqual([pair[0] for pair in allocation], [2, 0, 1])
        for index in (2, 0, 1):
            state = fields(data, ["state", 1, index])
            self.assertEqual(set(state), STATE_FIELDS)
            self.assertEqual((state["active"], state["counter"]), (False, 0))
            self.assertIs(state["ticked_at"], None)
        self.assertEqual(len(instance.states), 3)

    def test_every_current_concrete_state_class_exports_before_any_callback(self):
        component, instance = fixture(tuple(name for name, _ in STATE_MODELS))
        data = raw(export_component_vm(component))
        self.assertEqual(len(STATE_MODELS), 24)
        for index, (_, expected_type) in enumerate(STATE_MODELS):
            with self.subTest(state=expected_type.__name__):
                self.assertIs(type(instance.states[index]), expected_type)
                exported = record(data, ["state", 1, index])
                self.assertEqual(exported["class"], f"{expected_type.__module__}.{expected_type.__name__}")
                self.assertEqual(set(dict(exported["fields"])), set(vars(instance.states[index])))

    def test_optional_lifecycle_storage_distinguishes_missing_from_present_none(self):
        models = (
            ("STU_E4A30BCF", "button_id"), ("STUStatescriptStateWait", "ms"),
            ("STUStatescriptStateWeaponVolley", "max_shots"),
            ("STUStatescriptStateWeaponVolley", "min_shots"), ("STU_2F4E2E3F", "seen"),
            ("STUStatescriptStateGameMessageEntry", "received"),
            ("STUStatescriptStateCosmeticEntity", "var"),
        )
        for cls, optional in models:
            with self.subTest(cls=cls, optional=optional):
                component, instance = fixture((cls,))
                missing = export_component_vm(component)
                self.assertNotIn(optional, fields(raw(missing), ["state", 1, 0]))
                setattr(instance.states[0], optional, None)
                present = export_component_vm(component)
                self.assertIsNone(fields(raw(present), ["state", 1, 0])[optional])
                self.assertNotEqual(snapshot_digest(missing), snapshot_digest(present))

    def test_watchers_watched_links_and_causal_work_queues_keep_their_actual_order(self):
        component, instance = fixture(("STU_TestPlainState",) * 3, allocation=(2, 0, 1))
        first, second, third = (instance.states[i] for i in (2, 0, 1))
        entity_var = component.entity_var(44)
        local_var = instance.find_var(expr.INSTANCE, 55)
        entity_var.watchers = {third: None, first: None, second: None}
        first.watched = {local_var: None, entity_var: None}
        entity_var.links = [runtime.Link(second, 2, -0.0, False, True), runtime.Link(first, 1)]
        component.held = [29, 20]
        component.disabled = {20: [third, first]}
        instance.ticking = [second, first]
        instance.deferred = [(8.0, 2, third), (8.0, 1, first)]
        component.shots = [(second, 9, 30), (first, 0, 30)]
        data = raw(export_component_vm(component))
        variable = fields(data, ["var", 0, 44])
        self.assertEqual(elements(variable["watchers"]), [
            [ref("state", 1, 1), None], [ref("state", 1, 2), None], [ref("state", 1, 0), None],
        ])
        watched = fields(data, ["state", 1, 2])["watched"]
        self.assertEqual(elements(watched), [[ref("var", 1, 55), None], [ref("var", 0, 44), None]])
        links = [dict(item["fields"]) for item in elements(variable["links"])]
        self.assertEqual([item["state"] for item in links], [ref("state", 1, 0), ref("state", 1, 2)])
        self.assertEqual((links[0]["slot"], links[0]["above"], links[0]["stack"]), (2, False, True))
        self.assertEqual(math.copysign(1, links[0]["priority"]), -1)
        instance_data = fields(data, ["instance", 1])
        self.assertEqual(elements(instance_data["ticking"]), [ref("state", 1, 0), ref("state", 1, 2)])
        self.assertEqual([elements(item)[2] for item in elements(instance_data["deferred"])],
                         [ref("state", 1, 1), ref("state", 1, 2)])
        component_data = dict(data["component"])
        self.assertEqual(elements(component_data["held"]), [29, 20])
        ((button, disabled),) = elements(component_data["disabled"])
        self.assertEqual(button, 20)
        self.assertEqual(elements(disabled), [ref("state", 1, 1), ref("state", 1, 2)])
        self.assertEqual([elements(shot) for shot in elements(component_data["shots"])],
                         [[ref("state", 1, 0), 9, 30], [ref("state", 1, 2), 0, 30]])

    def test_complete_event_records_include_nonstate_and_transient_events_in_fifo_order(self):
        component, instance = fixture()
        kinds = (runtime.MESSAGE, runtime.ENTRY, runtime.BUTTON, runtime.CHECK, runtime.CHANGED,
                 runtime.TIMER, runtime.STATE_OP)
        for number, kind in enumerate(kinds):
            instance.queue.append(runtime.Event(
                30, number, kind, instance.states[0] if number > 2 else None, op=2, param=number + 9,
                node=instance.graph.nodes[0], button=20, pressed=True,
                message=(0x123, {None: b"\x00\xff"}, 7), transient=True,
            ))
        data = raw(export_component_vm(component))
        events = [dict(item["fields"]) for item in elements(fields(data, ["instance", 1])["queue"])]
        self.assertEqual([event["kind"] for event in events], list(kinds))
        self.assertEqual([event["seq"] for event in events], list(range(7)))
        for index, event in enumerate(events):
            self.assertEqual(set(event), EVENT_FIELDS)
            self.assertEqual((event["time"], event["op"], event["param"]), (30, 2, index + 9))
            self.assertEqual(event["node"], ref("node", 1, 0))
            self.assertEqual((event["button"], event["pressed"], event["transient"]), (20, True, True))
            self.assertEqual(event["state"], None if index < 3 else ref("state", 1, 0))
            self.assertEqual(elements(elements(event["message"])[1]), [[None, b"\x00\xff"]])

    def test_observation_does_not_consume_sequence_rng_shots_or_execute_graph_methods(self):
        component, instance = fixture(tuple(name for name, _ in STATE_MODELS))
        component.seq = runtime.EventSequence(41)
        component.rng.gauss_next = -0.0
        component.shots = [(instance.states[9], 3, 400)]
        rng_before = component.rng.getstate()
        shots_before = list(component.shots)
        forbidden = AssertionError("snapshot executed live behavior")
        with ExitStack() as patches:
            for target, names in (
                (runtime.Var, ("value",)),
                (runtime.Instance, ("evaluate", "update", "dispatch", "follow")),
                (runtime.Component, ("ensure_valid", "update", "run_frame")),
                (BodyScript, ("snapshot", "command", "frames", "delta")),
                (expr, ("evaluate", "run")),
                (random.Random, ("random", "getrandbits", "gauss", "uniform", "randint", "randrange")),
                (runtime.EventSequence, ("__next__",)),
            ):
                for name in names:
                    patches.enter_context(mock.patch.object(target, name, side_effect=forbidden))
            for _, state_type in STATE_MODELS:
                for name in ("output", "payload", "evaluate"):
                    if name in state_type.__dict__:
                        patches.enter_context(mock.patch.object(state_type, name, side_effect=forbidden))
            first = export_component_vm(component)
            second = export_component_vm(component)
        self.assertEqual(first, second)
        self.assertEqual(component.seq.next_value, 41)
        self.assertEqual(component.rng.getstate(), rng_before)
        self.assertEqual(component.shots, shots_before)
        stored = dict(raw(first)["component"])
        self.assertEqual(stored["seq"]["next_value"], 41)
        rng_state = stored["rng"]["state"]
        self.assertEqual(math.copysign(1, elements(rng_state)[2]), -1)

    def test_next_sequence_rng_gaussian_cache_and_undrained_shots_each_change_the_hash(self):
        component, instance = fixture()
        previous = digest(component)
        next(component.seq)
        sequence = digest(component)
        self.assertNotEqual(sequence, previous)
        component.rng.gauss_next = 0.25
        gaussian = digest(component)
        self.assertNotEqual(gaussian, sequence)
        component.rng.random()
        generator = digest(component)
        self.assertNotEqual(generator, gaussian)
        component.shots.append((instance.states[0], 0, 50))
        self.assertNotEqual(digest(component), generator)

    def test_destroyed_instances_and_explicit_orphan_roots_retain_their_complete_closure(self):
        component, instance = fixture()
        variable = instance.find_var(expr.INSTANCE, 17)
        variable.base = b"retained"
        instance.states[0].watched[variable] = None
        component.destroy(instance)
        retained = raw(export_component_vm(component))
        self.assertEqual(fields(retained, ["var", 1, 17])["base"], b"retained")
        self.assertTrue(fields(retained, ["instance", 1])["teardown"])
        self.assertEqual(set(fields(retained, ["state", 1, 0])), STATE_FIELDS)
        component.destroyed.clear()
        self.assertFalse(raw(export_component_vm(component))["objects"])
        explicit = raw(export_component_vm(component, extra_roots=(instance.states[0],)))
        self.assertEqual(fields(explicit, ["var", 1, 17])["base"], b"retained")
        self.assertEqual(fields(explicit, ["state", 1, 0])["instance"], ref("instance", 1))
        self.assertEqual(explicit["extra_roots"], [ref("state", 1, 0)])
        ordered = export_component_vm(component, extra_roots=(instance.states[0], variable))
        reversed_roots = export_component_vm(component, extra_roots=(variable, instance.states[0]))
        self.assertEqual(raw(reversed_roots)["extra_roots"], [ref("var", 1, 17), ref("state", 1, 0)])
        self.assertNotEqual(snapshot_digest(ordered), snapshot_digest(reversed_roots))

    def test_same_entity_components_have_isolated_local_namespaces_and_reject_foreign_references(self):
        first, _ = fixture()
        second, foreign = fixture()
        self.assertEqual(export_component_vm(first), export_component_vm(second))
        first.shots.append((foreign.states[0], 0, 1))
        with self.assertRaisesRegex(BodyVMSnapshotError, "foreign"):
            export_component_vm(first)
        with self.assertRaisesRegex(BodyVMSnapshotError, "foreign"):
            export_component_vm(runtime.Component(123), extra_roots=(foreign,))

    def test_reused_local_instance_ids_are_rejected_instead_of_collapsing_distinct_objects(self):
        component, old = fixture()
        component.destroy(old)
        component.create(old.graph, instance_id=old.id, init=False)
        with self.assertRaisesRegex(BodyVMSnapshotError, "reuse"):
            export_component_vm(component)

    def test_variable_aliases_are_references_and_unowned_or_duplicate_variables_are_rejected(self):
        component, instance = fixture()
        variable = component.entity_var(12)
        instance.params = [{"first": variable, "again": variable}]
        data = raw(export_component_vm(component))
        parameters = elements(fields(data, ["instance", 1])["params"])[0]
        self.assertEqual(elements(parameters), [["first", ref("var", 0, 12)],
                                                ["again", ref("var", 0, 12)]])
        self.assertEqual(sum(item["ref"] == ["var", 0, 12] for item in data["objects"]), 1)
        instance.params.append(runtime.Var((0, 12)))
        with self.assertRaisesRegex(BodyVMSnapshotError, "reuse"):
            export_component_vm(component)
        with self.assertRaisesRegex(BodyVMSnapshotError, "outside"):
            export_component_vm(runtime.Component(123), extra_roots=(runtime.Var((0, 4)),))

    def test_scalar_bits_array_types_expression_maps_and_none_parameter_keys_are_preserved(self):
        component, instance = fixture()
        component.entity_var(1).base = (
            -0.0, b"\x00\xff", [True, 1], expr.Map(((7, "first"), (7, "second"))),
            expr.Asset(1 << 63), expr.Entity(5), expr.Vec3(-0.0, 1.5, 2.0), expr.Handle(1, 1, 0, 9),
        )
        instance.params = [{None: ("fallback",), 8: [False]}]
        data = raw(export_component_vm(component))
        value = fields(data, ["var", 0, 1])["base"]
        self.assertEqual(value["kind"], "tuple")
        values = elements(value)
        self.assertEqual(math.copysign(1, values[0]), -1)
        self.assertEqual(values[1], b"\x00\xff")
        self.assertEqual((values[2]["kind"], elements(values[2])), ("list", [True, 1]))
        self.assertIn("identity", values[2])
        mapped = dict(values[3]["fields"])["items"]
        self.assertEqual([elements(pair) for pair in elements(mapped)], [[7, "first"], [7, "second"]])
        self.assertEqual(dict(values[4]["fields"])["guid"], 1 << 63)
        self.assertEqual(dict(values[5]["fields"])["id"], 5)
        self.assertEqual(math.copysign(1, dict(values[6]["fields"])["x"]), -1)
        self.assertEqual(dict(values[7]["fields"]), {"kind": 1, "instance": 1, "state": 0, "extra": 9})
        parameters = elements(fields(data, ["instance", 1])["params"])[0]
        self.assertEqual(elements(parameters)[0], [None, {"kind": "tuple", "items": ["fallback"]}])
        self.assertEqual(elements(parameters)[1][0], 8)
        self.assertEqual(elements(parameters)[1][1]["kind"], "list")
        self.assertEqual(elements(elements(parameters)[1][1]), [False])

    def test_graph_and_node_definition_mutations_change_the_definition_signature(self):
        component, instance = fixture()
        original = export_component_vm(component)
        original_graph = record(raw(original), ["graph", 1])
        source_hash = hashlib.sha256(graph.GRAPHS_PATH.read_bytes()).hexdigest()
        self.assertEqual(original["graph_data_sha256"], source_hash)
        instance.graph.fields["stored_definition"] = [1, 2]
        changed = export_component_vm(component)
        self.assertNotEqual(record(raw(changed), ["graph", 1])["definition_sha256"],
                            original_graph["definition_sha256"])
        instance.graph.nodes[0].fields["condition"] = {"$": "STUConfigVarBool", "m_value": True}
        changed_node = export_component_vm(component)
        self.assertNotEqual(record(raw(changed_node), ["graph", 1])["definition_sha256"],
                            record(raw(changed), ["graph", 1])["definition_sha256"])
        self.assertEqual(changed_node["graph_data_sha256"], original["graph_data_sha256"])

    def test_unknown_graph_or_node_fields_and_foreign_nodes_are_rejected(self):
        for target in ("graph", "node"):
            with self.subTest(target=target):
                component, instance = fixture()
                obj = instance.graph if target == "graph" else instance.graph.nodes[0]
                obj.unknown_definition_field = 1
                with self.assertRaises(BodyVMSnapshotError):
                    export_component_vm(component)
        component, instance = fixture()
        instance.queue.append(runtime.Event(0, 0, runtime.ENTRY, node=minimal_graph(index=2).nodes[0]))
        with self.assertRaisesRegex(BodyVMSnapshotError, "outside"):
            export_component_vm(component)

    def test_terminal_faulted_component_exports_without_ensure_valid(self):
        component, _ = fixture()
        component.fault = runtime.ScriptBudgetError("terminal probe")
        with mock.patch.object(runtime.Component, "ensure_valid", side_effect=AssertionError("called")):
            fault = dict(raw(export_component_vm(component))["component"])["fault"]
        self.assertTrue(fault["class"].endswith(".ScriptBudgetError"))
        self.assertEqual(fault["args"], {"kind": "tuple", "items": ["terminal probe"]})

    def test_known_state_fields_and_types_cannot_be_transplanted_to_another_node_model(self):
        component, instance = fixture()
        instance.states[0].cur = 1.0  # A known ChaseVar/Ability field is unknown on a plain State.
        with self.assertRaises(BodyVMSnapshotError):
            export_component_vm(component)
        del instance.states[0].cur
        instance.states[0] = nodes.Wait(instance, instance.graph.nodes[0], 0)
        with self.assertRaisesRegex(BodyVMSnapshotError, "does not match"):
            export_component_vm(component)

    def test_exported_documents_are_detached_from_later_plain_storage_mutations(self):
        component, _ = fixture()
        value = [b"before", {"nested": (1, 2)}]
        component.entity_var(9).base = value
        document = export_component_vm(component)
        observed = copy.deepcopy(document)
        value[0] = b"after"
        value[1]["nested"] = (3, 4)
        self.assertEqual(document, observed)
        self.assertNotEqual(snapshot_digest(document), digest(component))

    def test_unknown_classes_unknown_fields_and_missing_required_fields_fail_closed(self):
        class ExtendedComponent(runtime.Component):
            pass

        class ExtendedState(runtime.State):
            pass

        with self.assertRaises(BodyVMSnapshotError):
            export_component_vm(ExtendedComponent(123))
        component, instance = fixture()
        instance.states[0] = ExtendedState(instance, instance.graph.nodes[0], 0)
        with self.assertRaises(BodyVMSnapshotError):
            export_component_vm(component)
        for target_name in ("component", "instance", "state", "world", "event", "link"):
            for missing in (False, True):
                with self.subTest(target=target_name, missing=missing):
                    component, instance = fixture()
                    event = runtime.Event(0, 0, runtime.CHECK, state=instance.states[0])
                    link = runtime.Link(instance.states[0])
                    instance.queue.append(event)
                    component.entity_var(2).links.append(link)
                    target, required = {
                        "component": (component, "frame"), "instance": (instance, "start"),
                        "state": (instance.states[0], "active"), "world": (component.world, "hero"),
                        "event": (event, "seq"), "link": (link, "priority"),
                    }[target_name]
                    if missing:
                        delattr(target, required)
                    else:
                        target.unknown_storage = 1
                    with self.assertRaises(BodyVMSnapshotError):
                        export_component_vm(component)

    def test_nonfinite_unknown_values_and_unsafe_container_subclasses_are_rejected(self):
        class UnsafeList(list):
            def __iter__(self):
                raise AssertionError("must reject without calling replacement container")

        for value in (math.inf, math.nan, object(), UnsafeList()):
            with self.subTest(kind=type(value).__name__):
                component, _ = fixture()
                component.entity_var(3).base = value
                with self.assertRaises(BodyVMSnapshotError):
                    export_component_vm(component)
        component, _ = fixture()
        component.shots = UnsafeList()
        with self.assertRaises(BodyVMSnapshotError):
            export_component_vm(component)
        with self.assertRaises(BodyVMSnapshotError):
            export_component_vm(runtime.Component(1), extra_roots=iter(()))

    def test_soldier_body_storage_excludes_only_named_transport_fields(self):
        script = BodyScript(SOLDIER, 123, 1000)
        baseline = export_body_vm(script)
        body = dict(raw(baseline)["body_script"])
        self.assertEqual(set(body), set(vars(script)) - TRANSPORT_FIELDS)
        self.assertIn("ultimates", body)
        self.assertIn("retired", body)
        self.assertEqual(set(dict(body["world"]["fields"])), {"hero", "mode", "body", "throttles"})
        for name in TRANSPORT_FIELDS:
            setattr(script, name, object())
        with mock.patch.object(BodyScript, "snapshot", side_effect=AssertionError("driver snapshot")):
            ignored_transport = export_body_vm(script)
        self.assertEqual(snapshot_digest(baseline), snapshot_digest(ignored_transport))
        script.unknown_execution_state = 1
        with self.assertRaises(BodyVMSnapshotError):
            export_body_vm(script)

    def test_snapshot_envelope_rejects_unknown_missing_and_noncanonical_typed_fields(self):
        component, _ = fixture()
        baseline = export_component_vm(component)
        bad = []
        for name in baseline:
            variant = copy.deepcopy(baseline)
            del variant[name]
            bad.append(variant)
        variant = copy.deepcopy(baseline)
        variant["unknown"] = 1
        bad.append(variant)
        for name, value in (
            ("schema", "unknown"), ("scope", "body-vm-storage-excludes-transport"),
            ("graph_data_sha256", "A" * 64), ("graph_data_sha256", "0" * 63),
            ("value", ["int", "0x01"]), ("value", ["float", "7ff0000000000000"]),
        ):
            variant = copy.deepcopy(baseline)
            variant[name] = value
            bad.append(variant)
        for index, document in enumerate(bad):
            with self.subTest(index=index), self.assertRaises(BodyVMSnapshotError):
                snapshot_digest(document)
        self.assertEqual(snapshot_digest(baseline), snapshot_digest(copy.deepcopy(baseline)))


if __name__ == "__main__":
    unittest.main()
