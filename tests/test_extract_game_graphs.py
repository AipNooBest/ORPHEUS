"""Unread DragonML nodes remain explicit instead of inventing input-plug ownership."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import extract_game_graphs as extractor
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


class EntityRootTests(unittest.TestCase):
    def test_optional_entity_roots_preserve_initial_order_and_follow_shared_graph_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "003").mkdir()
            (root / "01B").mkdir()
            heroes = root / "heroes.json"
            heroes.write_text("{}", encoding="utf-8")
            namespace = 'xmlns:dragon="https://yretenai.com/dragonml/v1" xmlns:tank="urn:test:tank"'
            (root / "003/000000001403.003.xml").write_text(f"""
                <STUEntityDefinition {namespace}>
                  <STUStatescriptComponent>
                    <dragon:array dragon:name="m_B634821A">
                      <GraphDefinition>
                        <tank:ref dragon:name="m_graph" GUID="000000000BE2.01B" />
                      </GraphDefinition>
                      <GraphDefinition>
                        <tank:ref dragon:name="m_graph" GUID="000000000033.01B" />
                      </GraphDefinition>
                    </dragon:array>
                  </STUStatescriptComponent>
                </STUEntityDefinition>
                """, encoding="utf-8")
            (root / "01B/000000000BE2.01B.xml").write_text(f"""
                <STUStatescript {namespace}>
                  <tank:ref dragon:name="m_child" GUID="000000000C18.01B" />
                </STUStatescript>
                """, encoding="utf-8")
            for index in (0x0033, 0x0C18):
                (root / f"01B/{index:012X}.01B.xml").write_text("<STUStatescript />", encoding="utf-8")

            with (
                patch.object(extractor, "HEROES", heroes),
                patch.object(extractor, "ROOT_GRAPHS", {0x33: "base"}),
            ):
                baseline = extractor.collect(root, EnumTables(root))
                extended = extractor.collect(root, EnumTables(root), (0x1403, 0x1403))

        self.assertNotIn("entities", baseline)
        self.assertEqual(extended["bodies"], baseline["bodies"])
        self.assertEqual(extended["roots"], baseline["roots"])
        self.assertEqual([item["index"] for item in extended["graphs"]], [0x0033, 0x0BE2, 0x0C18])
        self.assertEqual(extended["missing"], [])
        entity, = extended["entities"].values()
        self.assertEqual([item["graph"] for item in entity["graphs"]], [0x0BE2, 0x0033])
        self.assertIsNone(entity["hero"])


class SpawnerRootTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.spawners = self.root / "ai_spawners_174.json"

    def write_table(self, table):
        self.spawners.write_text(json.dumps(table), encoding="utf-8")

    def test_optional_map_roots_deduplicate_and_follow_graph_references(self):
        self.write_table({"maps": {
            "0688": {"spawners": [{"graphs": ["0x0580000000000B92", "0x0580000000000033"]}, {"graphs": []}]},
            "tutorial": {"spawners": [{"identifier": "0x0D80000000000001",
                                       "graphs": ["0x0580000000000B92", "0x0580000000000BE5"]}]},
        }})
        heroes = self.root / "heroes.json"
        heroes.write_text("{}", encoding="utf-8")
        folder = self.root / "01B"
        folder.mkdir()
        for index in (0x0033, 0x0C18):
            (folder / f"{index:012X}.01B.xml").write_text("<STUStatescript />", encoding="utf-8")
        (folder / "000000000B92.01B.xml").write_text("""
            <STUStatescript xmlns:dragon="https://yretenai.com/dragonml/v1" xmlns:tank="urn:test:tank">
              <tank:ref dragon:name="m_child" GUID="000000000C18.01B" />
            </STUStatescript>
            """, encoding="utf-8")
        with (
            patch.object(extractor, "HEROES", heroes),
            patch.object(extractor, "ROOT_GRAPHS", {0x0033: "base"}),
        ):
            baseline = extractor.collect(self.root, EnumTables(self.root))
            extended = extractor.collect(self.root, EnumTables(self.root), ai_spawners=self.spawners)

        self.assertEqual(baseline["roots"]["graphs"], [0x0033])
        self.assertEqual(extended["roots"]["graphs"], [0x0033, 0x0B92, 0x0BE5])
        self.assertEqual([item["index"] for item in extended["graphs"]], [0x0033, 0x0B92, 0x0C18])
        self.assertEqual(extended["missing"], [0x0BE5])

    def test_malformed_tables_and_wrong_asset_types_are_rejected(self):
        cases = (
            ([], "expected a map table"),
            ({"maps": {"0688": {}}}, "map 0688 must contain a spawner list"),
            ({"maps": {"0688": {"spawners": [{}]}}}, "spawner 0 must contain a graph list"),
            ({"maps": {"0688": {"spawners": [{"graphs": [None]}]}}}, "spawner 0 has an invalid graph GUID"),
            ({"maps": {"0688": {"spawners": [{"graphs": ["0x0400000000001403"]}]}}}, "nonzero 16-bit index"),
            ({"maps": {"0688": {"spawners": [{"graphs": ["0x0580000000010B92"]}]}}}, "nonzero 16-bit index"),
        )
        for table, message in cases:
            with self.subTest(message=message):
                self.write_table(table)
                with self.assertRaisesRegex(ValueError, message):
                    extractor.ai_graph_roots(self.spawners)

    def test_cli_requires_a_destination_outside_the_repository(self):
        output = self.root / "bundle.gz"
        argv = ["extract_game_graphs.py", "--xml", str(self.root), "--owlib", str(self.root),
                "--ai-spawners", str(self.spawners), "--output", str(output)]
        errors = io.StringIO()
        with (
            patch.object(extractor, "ROOT", self.root),
            patch.object(sys, "argv", argv),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as stopped,
        ):
            extractor.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("--ai-spawners requires --output outside the repository", errors.getvalue())
        self.assertFalse(output.exists())


class AIWeaponButtonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "003").mkdir()
        (self.root / "Types.cs").write_text("""
    public class STUAIWeaponFireControlInfo : STUInstance
    {
        public STULogicalButton m_4F5E7D4C;
    }
    public enum STULogicalButton : int
    {
        [STUField(0xD128396E)] xD128396E = 0x14,
        [STUField(0xB42D90B7)] xB42D90B7 = 0x1D,
        [STUField(0xADC2B02C)] xADC2B02C = -1,
    }
        """, encoding="utf-8")
        self.enums = EnumTables(self.root)
        self.body = self.root / "003/000000001403.003.xml"
        self.associated = self.root / "003/000000001404.003.xml"
        self.write_body()

    def write_body(self, association='<dragon:ref dragon:name="m_8B9A461F" GUID="000000001404.003" />'):
        self.body.write_text(f"""
          <STUEntityDefinition xmlns:dragon="https://yretenai.com/dragonml/v1">
            {association}
          </STUEntityDefinition>
        """, encoding="utf-8")

    def write_controls(self, rows):
        self.associated.write_text(f"""
          <STUEntityDefinition xmlns:dragon="https://yretenai.com/dragonml/v1">
            <dragon:map dragon:name="m_componentMap">
              <STU_993B1B10 dragon:key="2570787600">
                <STU_993B1B10 dragon:name="dragon:value">
                  <dragon:array dragon:name="m_CD486A71">
                    {rows}
                  </dragon:array>
                </STU_993B1B10>
              </STU_993B1B10>
            </dragon:map>
          </STUEntityDefinition>
        """, encoding="utf-8")

    def body_record(self):
        return extractor.body_record(self.body, self.enums, None, "entity 1403", include_ai_weapons=True)[0]

    def test_only_opted_in_additional_bodies_get_associated_weapon_buttons(self):
        self.write_controls("""
          <STUAIWeaponFireControlInfo m_ED6D601D="3" m_A60150E2="0" m_4F5E7D4C="xD128396E" />
          <STUAIWeaponFireControlInfo m_ED6D601D="0" m_A60150E2="2" m_4F5E7D4C="xB42D90B7" />
        """)
        hero = "0x02E000000000016B"
        body = "0x0400000000001403"
        heroes = self.root / "heroes.json"
        heroes.write_text(json.dumps({hero: {"body": body, "name": "control"}}), encoding="utf-8")
        spawners = self.root / "spawners.json"
        spawners.write_text('{"maps": {}}', encoding="utf-8")
        with patch.object(extractor, "HEROES", heroes), patch.object(extractor, "ROOT_GRAPHS", {}):
            baseline = extractor.collect(self.root, self.enums, (0x1403,))
            opted_in = extractor.collect(self.root, self.enums, (0x1403,), ai_spawners=spawners)
        self.assertNotIn("ai_weapon_buttons", baseline["entities"][body])
        self.assertEqual(opted_in["entities"][body]["ai_weapon_buttons"], {"0": 20, "2": 29})
        self.assertEqual(opted_in["bodies"], baseline["bodies"])
        self.assertNotIn("ai_weapon_buttons", opted_in["bodies"][body])

    def test_unassociated_or_empty_controls_do_not_supply_defaults(self):
        self.write_body("")
        self.assertNotIn("ai_weapon_buttons", self.body_record())
        self.write_body()
        self.write_controls("")
        self.assertNotIn("ai_weapon_buttons", self.body_record())

    def test_ambiguous_slots_and_malformed_fields_are_rejected(self):
        primary = '<STUAIWeaponFireControlInfo m_A60150E2="0" m_4F5E7D4C="xD128396E" />'
        alternate = '<STUAIWeaponFireControlInfo m_A60150E2="0" m_4F5E7D4C="xB42D90B7" />'
        cases = (
            (primary + alternate, "duplicate AI weapon slot 0"),
            (primary.replace('m_A60150E2="0"', 'm_ED6D601D="0"'), "invalid AI weapon slot"),
            (primary.replace('m_A60150E2="0"', 'm_A60150E2="-1"'), "invalid AI weapon slot"),
            (primary.replace("xD128396E", "x00000000"), "invalid AI weapon logical button"),
            (primary.replace("xD128396E", "xADC2B02C"), "invalid AI weapon logical button"),
            ("<STUWeaponComponent />", "invalid AI weapon control row type"),
        )
        for rows, message in cases:
            with self.subTest(message=message, rows=rows):
                self.write_controls(rows)
                with self.assertRaisesRegex(ValueError, message):
                    self.body_record()

    def test_non_entity_association_is_rejected(self):
        self.write_body('<dragon:ref dragon:name="m_8B9A461F" GUID="000000001404.01B" />')
        with self.assertRaisesRegex(ValueError, r"m_8B9A461F must reference a \.003"):
            self.body_record()


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
