"""Static reference analysis must not silently become an execution/coverage claim."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.content import SOLDIER, heroes
from ow174.game.script.analysis import analyze_body, analyze_graph, reference_closure, render_text
from ow174.game.script.graph import Graph, graph


def node(
    position, kind="STUStatescriptState", *, state=None, remote=-1, client=False, server=False, fields=None,
):
    return {
        "pos": position, "class": kind, "uid": position, "rid": position, "remote": remote,
        "state": state, "client_only": client, "server_only": server,
        "fields": {"m_2BBEEAB8": state is not None, **(fields or {})},
    }


def make_graph(index, nodes=(), *, refs=(), states=(), remote=(), entries=(), variables=(), unknown=()):
    return Graph({
        "index": index, "header": {}, "items": len(nodes), "containers": {}, "nodes": list(nodes),
        "unknown_nodes": list(unknown), "states": list(states), "remote": list(remote),
        "entries": list(entries), "sync_vars": list(variables),
        "fields": {"assets": [f"0x0580{ref:012X}" for ref in refs]},
    })


def output(*links):
    return {"$": "STUStatescriptOutputPlug", "links": list(links)}


class ReferenceClosureTests(unittest.TestCase):
    def test_cycles_missing_and_dormant_references_do_not_imply_execution(self):
        table = {1: make_graph(1, refs=(2, 9)), 2: make_graph(2, refs=(1,))}
        calls = []

        def load(index):
            calls.append(index)
            return table.get(index)

        result = reference_closure([1, 1], load)
        self.assertEqual(result["found"], [1, 2])
        self.assertEqual(result["missing"], [9])
        self.assertEqual(result["edges"], [[1, 2], [1, 9], [2, 1]])
        self.assertEqual(sorted(calls), [1, 2, 9])
        self.assertIn("not executable", result["meaning"])

    def test_body_keeps_order_overrides_empty_weapon_slots_and_missing_root(self):
        definitions = [{"graph": 2, "m_1EB5A024": [{"schema": "uninterpreted"}]}, {"graph": 1}]
        table = {7: {"name": "Example", "graphs": definitions, "manager": 8, "weapons": [None, 3, None]}}
        graphs = {1: make_graph(1), 2: make_graph(2), 3: make_graph(3)}
        report = analyze_body(7, body_table=table, loader=graphs.get)
        self.assertEqual(report["initial_graphs"], definitions)
        self.assertEqual(report["weapons"], [None, 3, None])
        self.assertEqual(report["closure"]["roots"], [1, 2, 3, 8])
        self.assertEqual(report["closure"]["missing"], [8])
        self.assertEqual(report["graphs"][0]["unknowns"]["execution_reachability"], "not evaluated")
        with self.assertRaisesRegex(ValueError, "No extracted body"):
            analyze_body(99, body_table=table, loader=graphs.get)

    def test_body_schema_override_graph_assets_are_also_in_reference_closure(self):
        table = {7: {"graphs": [{"graph": 1, "override": "0x0580000000000002"}]}}
        report = analyze_body(7, body_table=table, loader={1: make_graph(1)}.get)
        self.assertEqual(report["closure"]["roots"], [1, 2])
        self.assertEqual(report["closure"]["missing"], [2])
        self.assertEqual(report["body_asset_references"], [{"path": "graphs[0].override", "target": 2}])


class LayoutAndEntryTests(unittest.TestCase):
    def test_filtered_wire_slots_keep_original_state_and_variable_indexes(self):
        item = make_graph(
            1,
            [node(10, state=0, remote=2), node(11, state=1, client=True),
             node(12, state=2, server=True), node(13, state=3, remote=0),
             node(14, "STUStatescriptActionSetVar", remote=1)],
            states=[10, 11, 12, 13, None], remote=[13, 14, 10, None],
            variables=[(100, 1, 0), (200, 0, 0), (None, 0, 0), (300, 0, 1)],
        )
        report = analyze_graph(item)
        self.assertEqual([row["owner_slot"] for row in report["state_layout"]], [0, None, None, 1, None])
        self.assertEqual([row["remote_slot"] for row in report["remote_layout"]], [0, None, 1, None])
        self.assertEqual([row["presence_bit"] for row in report["variables"]], [None, 0, None, 1])
        self.assertEqual(report["variables"][1]["source_index"], 1)
        self.assertEqual(report["unknowns"]["unread_state_slots"], [4])
        self.assertEqual(report["unknowns"]["unread_remote_slots"], [3])

    def test_entry_candidates_include_conditional_links_without_declaring_execution(self):
        item = make_graph(
            1,
            [node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
             node(1, "STUStatescriptStateBooleanSwitch", state=0,
                  fields={"m_whenTrue": output([2, "m_inPlug"], [None, "m_inPlug"])}),
             node(2, "STU_DEADBEEF", fields={"m_outPlug": output([1, "m_beginPlug"])}),
             node(8, "STUStatescriptEntryGameMessage", fields={"m_outPlug": output([9, "m_inPlug"])})],
            entries=[0, 8, None], states=[1], unknown=[9],
        )
        report = analyze_graph(item)
        first, message, missing = report["entries"]
        self.assertEqual(first["plug_link_candidate_positions"], [0, 1, 2])
        self.assertEqual(message["plug_link_candidate_positions"], [8])
        self.assertIsNone(missing["entry"])
        self.assertEqual(message["unresolved_links"][0]["target"], 9)
        self.assertEqual(first["unresolved_links"][0]["target"], None)
        self.assertIn("not evaluated", first["trigger_semantics"])
        self.assertEqual(report["unknowns"]["unnamed_node_types"], ["STU_DEADBEEF"])
        self.assertIsNone(report["declared_handler_coverage"])
        declared = analyze_graph(item, supported_nodes={"STUStatescriptEntry"})["declared_handler_coverage"]
        self.assertEqual(declared["supported_node_count"], 1)
        self.assertIn("not verified", declared["meaning"])

    def test_field_evidence_retains_raw_opcodes_scope_and_source_paths(self):
        fields = {
            "m_out_Var": {"$": "STUConfigVarDynamic", "m_identifier": 53, "m_60DB8F99": 0},
            "m_value": {"$": "STUConfigVarExpressionData", "m_opcodes": [6, 0, 0], "m_D99EF254": [30]},
            "m_duration": {"$": "STUConfigVarFloat", "m_value": 1.5},
            "m_reference": {"node": 77}, "m_script": "0x0580000000000002",
        }
        item = make_graph(1, [node(5, "STUStatescriptActionSetVar", fields=fields)])
        report = analyze_graph(item, include_fields=True)
        detail = report["nodes"][0]
        self.assertEqual(detail["fields"]["m_value"]["m_opcodes"], [6, 0, 0])
        summary = detail["field_summary"]
        self.assertEqual(summary["dynamic_variables"], [{"path": "m_out_Var", "identifier": 53, "scope": 0}])
        self.assertEqual(summary["expressions"][0]["constants"], [30])
        self.assertEqual(summary["literals"][0]["value"], 1.5)
        self.assertEqual(summary["node_references"][0]["position"], 77)
        self.assertEqual(report["unknowns"]["unresolved_node_references"],
                         [{"source": 5, "path": "m_reference", "position": 77}])
        self.assertEqual(report["references"], [{"source": "node[5]", "path": "m_script", "target": 2}])
        json.dumps(report)


class TargetBundleTests(unittest.TestCase):
    def test_soldier_weapon_slots_and_critical_wire_mapping_match_target_bundle(self):
        hero = heroes()[SOLDIER]
        report = analyze_body(hero.body, detail_graphs=[0x254])
        self.assertEqual(report["manager"], 0x15)
        self.assertEqual(report["weapons"], [None, 0x254, None])
        self.assertIn(0x254, report["closure"]["found"])
        weapon = next(item for item in report["graphs"] if item["index"] == 0x254)
        volley = next(node for node in weapon["nodes"] if node["position"] == 25)
        self.assertEqual(volley["type"], "STUStatescriptStateWeaponVolley")
        self.assertEqual(volley["owner_slot"], graph(0x254).owner_slot(volley["state_index"]))
        self.assertEqual(volley["remote_slot"], graph(0x254).remote_slot(volley["remote_index"]))
        self.assertEqual(volley["fields"]["m_numShotsPerSecond"]["m_value"], 9)
        self.assertIn("NOT execution reachability", render_text(report, [0x254]))


if __name__ == "__main__":
    unittest.main()
