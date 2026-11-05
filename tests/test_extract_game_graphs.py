"""Unread DragonML nodes remain explicit instead of inventing input-plug ownership."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.extract_game_graphs import EnumTables, GraphXml, scalar

INPUT = (
    '<STUStatescriptInputPlug dragon:id="1494" dragon:name="m_inputPlug" '
    'm_links="{null}" m_parentNode="{null}" />'
)
FIXTURE = '''<STUStatescript xmlns:dragon="https://yretenai.com/dragonml/v1">
  <STUGraph dragon:name="m_graph">
    <dragon:array dragon:name="m_items">
      <STUStatescriptStateUXPresenter dragon:id="415" m_871A8203="38">
        <STU_904BFCEC dragon:id="486" dragon:name="m_subgraphPlug">
          <dragon:array dragon:name="m_links">
            <STUGraphLink dragon:id="1491">
              <dragon:ref dragon:id="486" dragon:name="m_outputPlug" />
              INPUT
            </STUGraphLink>
          </dragon:array>
          <dragon:ref dragon:id="415" dragon:name="m_parentNode" />
        </STU_904BFCEC>
      </STUStatescriptStateUXPresenter>
      <dragon:null />
      OWNER
    </dragon:array>
  </STUGraph>
  <dragon:array dragon:name="m_nodes">
    <dragon:ref dragon:id="415" /><dragon:null />OWNER_REF
  </dragon:array>
  <dragon:array dragon:name="m_states">
    <dragon:ref dragon:id="415" /><dragon:null />OWNER_REF
  </dragon:array>
  <dragon:array dragon:name="m_entries" />
  <dragon:array dragon:name="m_remoteSyncNodes" />
  <dragon:array dragon:name="m_syncVars" />
</STUStatescript>'''


class LinkEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def read(self, input_xml=INPUT, owner=""):
        owner_ref = '<dragon:ref dragon:id="600" />' if owner else ""
        text = FIXTURE.replace("INPUT", input_xml).replace("OWNER_REF", owner_ref).replace("OWNER", owner)
        path = self.root / "000000000D24.01B.xml"
        path.write_text(text, encoding="utf-8")
        graph = GraphXml(path, EnumTables(self.root))
        return graph, graph.record(0x0D24)

    def test_null_parent_reports_raw_link_and_source_evidence_without_guessing_from_null_slots(self):
        graph, record = self.read()
        self.assertEqual(record["nodes"][0]["fields"]["m_subgraphPlug"]["links"], [[None, "m_inputPlug"]])
        self.assertEqual(record["unknown_nodes"], [1])
        self.assertEqual(record["states"], [0, None])
        self.assertEqual(len(graph.problems), 1)
        problem = graph.problems[0]
        for evidence in (
            "link_id=1491", "input_plug_id=1494", "m_parentNode=null",
            "output_plug_id=486", "output_parent_id=415",
            "output_parent_class=STUStatescriptStateUXPresenter", "output_parent_pos=0",
            "output_parent_rid=38", "output_owners=[(0, 'm_subgraphPlug')]",
            "'m_nodes': [1]", "'m_states': [1]", "'m_items': [1]",
            "ownership/node schema cannot be recovered from these nulls",
        ):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, problem)

    def test_missing_parent_is_distinguished_from_an_explicit_null(self):
        graph, _ = self.read(INPUT.replace(' m_parentNode="{null}"', ""))
        self.assertIn("m_parentNode=missing", graph.problems[0])
        self.assertNotIn("m_parentNode=null", graph.problems[0])

    def test_an_actual_node_field_can_establish_ownership_even_with_null_parent(self):
        owner = '''<STUStatescriptState dragon:id="600" m_871A8203="2">
          <dragon:ref dragon:id="1494" dragon:name="m_beginPlug" />
        </STUStatescriptState>'''
        graph, record = self.read(owner=owner)
        self.assertEqual(record["nodes"][0]["fields"]["m_subgraphPlug"]["links"], [[2, "m_beginPlug"]])
        self.assertEqual(graph.problems, [])

    def test_a_link_without_input_preserves_its_id_and_unknown_target(self):
        graph, record = self.read(input_xml="")
        self.assertEqual(record["nodes"][0]["fields"]["m_subgraphPlug"]["links"], [[None, None]])
        self.assertEqual(graph.problems, ["a link without an input plug (link_id=1491)"])


class NumericScalarTests(unittest.TestCase):
    def test_locales_and_exponents_produce_numeric_values(self):
        cases = {
            "0.5": 0.5, "2.25": 2.25, "0.05": 0.05, "0.3": 0.3, "0.75": 0.75,
            "0,5": 0.5, "-1,25": -1.25, "+.25": 0.25, "-.5": -0.5,
            "2.5e-2": 0.025, "2,5E-2": 0.025, "1e3": 1000.0, "1E+3": 1000.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = scalar(text)
                self.assertIs(type(result), float)
                self.assertEqual(result, expected)

    def test_integers_nulls_and_enum_text_keep_their_types(self):
        for text in ("0", "-2", "+3"):
            with self.subTest(text=text):
                self.assertIs(type(scalar(text)), int)
        self.assertIsNone(scalar(None))
        self.assertIsNone(scalar("{null}"))
        self.assertEqual(scalar("TeamBlue"), "TeamBlue")
        self.assertEqual(scalar("x1F0B0C4D"), "x1F0B0C4D")

    def test_overflow_cannot_enter_the_bundle_as_infinity(self):
        for text in ("1e9999", "-1E9999", "2,5E+9999"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "nonfinite numeric XML"):
                scalar(text)


if __name__ == "__main__":
    unittest.main()
