"""The optional 0C5 export preserves the mode entity's typed initial variables."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tools import extract_game_graphs as extractor

NS = 'xmlns:dragon="https://yretenai.com/dragonml/v1"'
BOOL = '<STU_43CA1D94 dragon:name="m_value" m_value="0" />'
INT = '<STU_3ED2A372 dragon:name="m_value" m_value="-3" />'
IDENTIFIER = '''<STU_22BF1D21 dragon:name="m_value">
  <dragon:ref dragon:name="m_value" GUID="00000000195F.01C" />
</STU_22BF1D21>'''


def pair(key, value):
    return f'''<STUGameModeVarValuePair>
      <dragon:ref dragon:name="m_3E783677" GUID="{key}" />
      {value}
    </STUGameModeVarValuePair>'''


def mode(*pairs):
    return f'''<STUGameMode {NS}>
      <dragon:array dragon:name="m_3CE93B76">{''.join(pairs)}</dragon:array>
    </STUGameMode>'''


class ModeVariableExtractionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enums = extractor.EnumTables(self.root)

    def write(self, filename, xml):
        (self.root / filename).write_text(xml, encoding="utf-8")

    def read(self):
        return extractor.mode_variables(self.root, self.enums)

    def test_optional_bundle_preserves_mode_identity_and_typed_values(self):
        self.write("000000000018.0C5.xml", mode(
            pair("000000006B8E.01C", BOOL),
            pair("000000002295.01C", INT),
            pair("000000001A5F.01C", IDENTIFIER),
        ))
        self.write("00000000005A.0C5.xml", mode(pair("000000006B8E.01C", BOOL.replace('"0"', '"1"'))))
        self.write("000000000019.0C5.xml", '<STUGameMode m_3CE93B76="{null}" />')
        self.write("heroes.json", "{}")
        with (
            patch.object(extractor, "HEROES", self.root / "heroes.json"),
            patch.object(extractor, "ROOT_GRAPHS", {}),
        ):
            self.assertNotIn("mode_variables", extractor.collect(self.root, self.enums))
            variables = extractor.collect(self.root, self.enums, mode_xml=self.root)["mode_variables"]
        practice = variables["0x0230000000000018"]
        self.assertEqual(practice, {"27534": False, "8853": -3, "6751": "0x0D8000000000195F"})
        self.assertIs(practice["27534"], False)
        self.assertEqual(variables["0x0230000000000019"], {})
        self.assertIs(variables["0x023000000000005A"]["27534"], True)

    def test_invalid_pair_types_identifiers_and_values_fail(self):
        key = "000000006B8E.01C"
        for name, xml in (
            ("STUGameMode root", '<STUGameRulesetSchema />'),
            ("m_3CE93B76 array", '<STUGameMode m_3CE93B76="7" />'),
            ("pair type", mode("<STUGameRulesetSchemaEntry />")),
            ("m_3E783677 must reference", mode(pair("000000006B8E.0C5", BOOL))),
            ("outside the entity variable range", mode(pair("000000000000.01C", BOOL))),
            ("invalid STU_43CA1D94 m_value", mode(pair(key, BOOL.replace('"0"', '"2"')))),
            ("unsupported mode variable value type", mode(pair(key, INT.replace("3ED2A372", "73715F2E")))),
            ("duplicate mode variable identifier", mode(pair(key, BOOL), pair(key, INT))),
        ):
            with self.subTest(error=name), self.assertRaisesRegex(ValueError, name):
                self.write("000000000018.0C5.xml", xml)
                self.read()

    def test_duplicate_mode_aliases_fail(self):
        self.write("000000000018.0C5.xml", mode())
        self.write("0018.0C5.xml", mode())
        with self.assertRaisesRegex(ValueError, "duplicate mode asset 0x0230000000000018"):
            self.read()

    def test_mode_export_requires_an_explicit_output_outside_the_repository(self):
        args = [
            "extract_game_graphs.py", "--xml", "unused", "--owlib", "unused", "--mode-xml", str(self.root),
        ]
        for output in ([], ["--output", str(extractor.ROOT / "another-bundle.json.gz")]):
            error = io.StringIO()
            with (
                self.subTest(output=output),
                patch("sys.argv", args + output),
                redirect_stderr(error),
                self.assertRaises(SystemExit),
            ):
                extractor.main()
            self.assertIn("--mode-xml requires --output outside the repository", error.getvalue())
