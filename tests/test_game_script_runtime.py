"""Supported execution contracts and fail-closed behavior, plus real-data startup probes."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script.graph import ENTITY, INSTANCE, Graph
from ow174.game.script.research_runtime import BodyRuntime, RuntimeValue, Unsupported, Variables

BODY = 0x04000000000003CF


def dynamic(identifier, scope=INSTANCE):
    return {"$": "STUConfigVarDynamic", "m_identifier": identifier, "m_60DB8F99": scope}


def literal(value):
    cls = "Bool" if isinstance(value, bool) else "Float" if isinstance(value, float) else "Int"
    return {"$": f"STUConfigVar{cls}", "m_value": value}


def plug(*links):
    return {"$": "STUStatescriptOutputPlug", "links": [list(link) for link in links]}


def node(position, cls, *, state=None, client_only=False, **fields):
    return {
        "pos": position,
        "class": cls,
        "uid": position,
        "rid": position,
        "remote": -1,
        "state": state,
        "client_only": client_only,
        "server_only": False,
        "fields": {"m_2BBEEAB8": state is not None, **fields},
    }


def entry(*links, position=0):
    return node(position, "STUStatescriptEntry", m_outPlug=plug(*links))


def setter(position, identifier, value, *, scope=INSTANCE, links=(), client_only=False):
    return node(
        position,
        "STUStatescriptActionSetVar",
        client_only=client_only,
        m_out_Var=dynamic(identifier, scope),
        m_value=value,
        m_outPlug=plug(*links),
    )


def metadata(index, nodes):
    states = sorted((item for item in nodes if item["state"] is not None), key=lambda item: item["state"])
    return Graph(
        {
            "index": index,
            "header": {},
            "fields": {},
            "items": len(nodes),
            "containers": {},
            "nodes": nodes,
            "unknown_nodes": [],
            "states": [item["pos"] for item in states],
            "entries": [item["pos"] for item in nodes if item["class"] == "STUStatescriptEntry"],
            "remote": [],
            "sync_vars": [],
        }
    )


def model(nodes, *, extra_graphs=(), definitions=None, **options):
    graphs = {item.index: item for item in [metadata(1, nodes), *extra_graphs]}
    catalog = {BODY: {"graphs": definitions or [{"graph": 1}], "weapons": []}}
    return BodyRuntime(BODY, graph_loader=graphs.get, body_catalog=catalog, **options)


class ValueTests(unittest.TestCase):
    def test_scopes_are_isolated_and_entity_values_are_shared(self):
        other = metadata(2, [entry((1, "m_inPlug")), setter(1, 3, dynamic(2, ENTITY))])
        runtime = model(
            [entry((1, "m_inPlug")), setter(1, 2, literal(19), scope=ENTITY)],
            extra_graphs=[other],
            definitions=[{"graph": 1}, {"graph": 2}],
        )
        runtime.initialize()
        self.assertEqual(runtime.entity_vars[2], RuntimeValue("int", 19))
        self.assertEqual(runtime.instances[1].variables[3].value, 19)
        self.assertNotIn(2, runtime.instances[0].variables)
        runtime.set_variable(1, 3, 99)
        self.assertEqual(runtime.instances[1].variables[3].value, 19)

    def test_default_wrapper_only_defaults_unset_variables(self):
        runtime = model([entry()])
        runtime.initialize()
        wrapped = {"$": "STU_91EFD5B1", "m_value": dynamic(42), "m_default": literal(7)}
        self.assertEqual(runtime.evaluate(wrapped, 1).value, 7)
        runtime.set_variable(1, 42, 0)
        self.assertEqual(runtime.evaluate(wrapped, 1).value, 0)
        wrapped["m_value"] = {"$": "Unknown"}
        with self.assertRaisesRegex(Unsupported, "Unknown"):
            runtime.evaluate(wrapped, 1)

    def test_numeric_expression_opcodes_fail_closed_until_verified(self):
        runtime = model([entry()])
        runtime.initialize()
        expression = {"$": "STU_919FD47C", "m_expression": {"m_opcodes": [6, 0, 0]}}
        with self.assertRaisesRegex(Unsupported, "unverified expression"):
            runtime.evaluate(expression, 1)
        custom = model([entry()], expression_evaluator=lambda _value, _runtime, _instance: 12.0)
        custom.initialize()
        self.assertEqual(custom.evaluate(expression, 1), RuntimeValue("float", 12.0))

    def test_asset_and_entity_types_are_explicit_and_bad_scalars_are_rejected(self):
        runtime = model([entry()])
        runtime.initialize()
        asset = {"$": "STUConfigVarIdentifier", "m_identifier": "0x0D8000000000001F"}
        self.assertEqual(runtime.evaluate(asset, 1), RuntimeValue("asset", 0x0D8000000000001F))
        runtime.set_variable(1, 7, RuntimeValue("entity", 0xA0000101))
        self.assertEqual(runtime.instances[0].variables[7].kind, "entity")
        for kind, bad in (("int", 1 << 40), ("float", float("nan")), ("entity", -1)):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                RuntimeValue(kind, bad)

    def test_stack_layer_updates_keep_priority_and_restore_base(self):
        store = Variables({4: 1})
        store.push(4, (1, 10), RuntimeValue("int", 2))
        store.push(4, (1, 11), RuntimeValue("int", 3))
        store.push(4, (1, 10), RuntimeValue("int", 4))
        self.assertEqual(store.read(4).value, 3)
        store.pop((1, 11))
        self.assertEqual(store.read(4).value, 4)
        store.pop((1, 10))
        self.assertEqual(store.read(4).value, 1)


class ExecutionTests(unittest.TestCase):
    def test_plugs_follow_definition_order_depth_first(self):
        runtime = model(
            [
                entry((1, "m_inPlug"), (3, "m_inPlug")),
                setter(1, 8, literal(1), links=[(2, "m_inPlug")]),
                setter(2, 8, literal(2)),
                setter(3, 8, literal(3)),
            ]
        )
        self.assertTrue(runtime.initialize()["complete"])
        self.assertEqual(runtime.instances[0].variables[8].value, 3)

    def test_unknown_path_stops_but_independent_supported_sibling_runs(self):
        runtime = model(
            [
                entry((1, "m_inPlug"), (3, "m_inPlug")),
                node(1, "UnknownAction", m_outPlug=plug((2, "m_inPlug"))),
                setter(2, 9, literal(99)),
                setter(3, 8, literal(3)),
            ]
        )
        snapshot = runtime.initialize()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(runtime.instances[0].variables[8].value, 3)
        self.assertNotIn(9, runtime.instances[0].variables)
        self.assertEqual(snapshot["diagnostics"][0]["node"], 1)
        self.assertTrue(snapshot["diagnostics"][0]["path"])

    def test_client_only_actions_are_left_for_the_client(self):
        runtime = model([entry((1, "m_inPlug")), setter(1, 8, literal(3), client_only=True)])
        runtime.initialize()
        self.assertNotIn(8, runtime.instances[0].variables)

    def test_boolean_branch_lifetime_releases_its_stack(self):
        switch = node(
            1,
            "STUStatescriptStateBooleanSwitch",
            state=0,
            m_condition=dynamic(7, ENTITY),
            m_trueSubgraphPlug=plug((2, "m_beginPlug")),
        )
        stack = node(
            2, "STUStatescriptStateStack", state=1, m_out_Var=dynamic(8), m_value=literal(30)
        )
        runtime = model([entry((1, "m_beginPlug")), switch, stack], entity_vars={7: True})
        runtime.initialize(frame=10)
        self.assertEqual(runtime.instances[0].active, {0, 1})
        self.assertEqual(runtime.instances[0].variables[8].value, 30)
        runtime.set_variable(1, 7, False, scope=ENTITY)
        runtime.advance(SimpleNamespace(frame=11))
        self.assertEqual(runtime.instances[0].active, {0})
        self.assertNotIn(8, runtime.instances[0].variables)

    def test_wait_uses_command_time_and_duplicates_do_not_advance_it(self):
        wait = node(
            1,
            "STUStatescriptStateWait",
            state=0,
            m_timeout=literal(0.032),
            m_onFinishedPlug=plug((2, "m_inPlug")),
        )
        runtime = model([entry((1, "m_beginPlug")), wait, setter(2, 8, literal(42))])
        runtime.initialize(frame=100)
        runtime.advance(SimpleNamespace(frame=101))
        runtime.advance(SimpleNamespace(frame=101))
        self.assertEqual(runtime.elapsed_us, 16000)
        self.assertNotIn(8, runtime.instances[0].variables)
        runtime.advance(SimpleNamespace(frame=102))
        self.assertEqual(runtime.instances[0].variables[8].value, 42)
        self.assertEqual(runtime.instances[0].active, set())

    def test_a_missing_command_blocks_without_inventing_input_or_time(self):
        runtime = model([entry()])
        runtime.initialize(frame=100)
        snapshot = runtime.advance(SimpleNamespace(frame=102, buttons=1))
        self.assertEqual((snapshot["frame"], snapshot["elapsed_us"], runtime.buttons), (100, 0, 0))
        self.assertEqual(snapshot["diagnostics"][0]["feature"], "command-gap")

    def test_first_command_anchors_time_and_uint32_wrap_is_contiguous(self):
        runtime = model([entry()], quanta_us=7000)
        runtime.initialize()
        runtime.advance(SimpleNamespace(frame=0xFFFFFFFF))
        self.assertEqual(runtime.elapsed_us, 0)
        runtime.advance(SimpleNamespace(frame=0))
        self.assertEqual(runtime.elapsed_us, 7000)
        runtime.advance(SimpleNamespace(frame=0xFFFFFFFF))
        self.assertEqual(runtime.frame, 0)

    def test_cycles_are_bounded_and_diagnosed(self):
        runtime = model(
            [entry((1, "m_inPlug")), setter(1, 8, literal(1), links=[(1, "m_inPlug")])], max_steps=12
        )
        snapshot = runtime.initialize()
        self.assertFalse(snapshot["complete"])
        self.assertTrue(any(item["feature"] == "step-budget" for item in snapshot["diagnostics"]))

    def test_subscript_lifecycle_never_reuses_an_instance_id(self):
        child = metadata(2, [entry((1, "m_beginPlug")), node(1, "STUStatescriptState", state=0)])
        switch = node(
            1,
            "STUStatescriptStateBooleanSwitch",
            state=0,
            m_condition=dynamic(7, ENTITY),
            m_trueSubgraphPlug=plug((2, "m_beginPlug")),
        )
        subscript = node(
            2,
            "STU_37D754C8",
            state=1,
            m_36F7B64C={
                "m_36F7B64C": {"m_C974E369": {"m_graph": "0x0580000000000002"}}
            },
        )
        runtime = model(
            [entry((1, "m_beginPlug")), switch, subscript], extra_graphs=[child], entity_vars={7: True}
        )
        runtime.initialize(frame=10)
        first = runtime.instances[0].children[1]
        self.assertEqual(runtime.instances[1].parent, (1, 1))
        runtime.set_variable(1, 7, False, scope=ENTITY)
        runtime.advance(SimpleNamespace(frame=11))
        self.assertEqual(len(runtime.instances), 1)
        runtime.set_variable(1, 7, True, scope=ENTITY)
        runtime.advance(SimpleNamespace(frame=12))
        self.assertGreater(runtime.instances[0].children[1], first)
        self.assertEqual(len({item.index for item in runtime.instances}), len(runtime.instances))

    def test_snapshot_is_detached_and_replay_is_deterministic(self):
        def run():
            runtime = model([entry((1, "m_inPlug")), setter(1, 8, literal(4))])
            runtime.initialize(frame=10)
            runtime.advance(SimpleNamespace(frame=11))
            return runtime

        first, second = run(), run()
        self.assertEqual(
            json.dumps(first.snapshot(), sort_keys=True), json.dumps(second.snapshot(), sort_keys=True)
        )
        detached = first.snapshot()
        detached["instances"][0]["variables"][8]["value"] = 99
        self.assertEqual(first.instances[0].variables[8].value, 4)

    def test_other_entries_remain_pending_unless_selected(self):
        runtime = model([entry(), entry((2, "m_inPlug"), position=1), setter(2, 8, literal(42))])
        runtime.initialize()
        self.assertEqual(runtime.instances[0].pending_entries, [1])
        self.assertNotIn(8, runtime.instances[0].variables)


class RealGraphTests(unittest.TestCase):
    def test_soldier_startup_initializes_proven_literals_and_reports_the_ammo_blocker(self):
        runtime = BodyRuntime(BODY)
        snapshot = runtime.initialize(frame=10)
        weapon = next(item for item in runtime.instances if item.role == "weapon:1")
        self.assertEqual(weapon.graph.index, 0x254)
        self.assertEqual(weapon.variables[476], RuntimeValue("float", 20))
        self.assertEqual(weapon.variables[581], RuntimeValue("float", 1.5))
        self.assertNotIn(53, weapon.variables)
        blockers = [item for item in runtime.diagnostics if item.graph == 0x254 and item.node == 8]
        self.assertTrue(blockers)
        self.assertIn("unverified expression", blockers[0].message)
        self.assertFalse(snapshot["complete"])
        self.assertEqual(
            [item.graph.index for item in runtime.instances if item.role == "initial"],
            [0x33, 0x4B, 0x43, 0x251, 0x255, 0x257, 0x259, 0x91B, 0x8B6],
        )


if __name__ == "__main__":
    unittest.main()
