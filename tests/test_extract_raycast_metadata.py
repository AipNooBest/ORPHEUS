"""The local raycast export preserves typed rules, ordered references and null results."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import extract_raycast_metadata as extractor
from tools.extract_game_graphs import EnumTables

NS = 'xmlns:dragon="https://yretenai.com/dragonml/v1"'
TYPES = """namespace Fixture {
    public enum Enum_FF4B83D8 : int {
        [EnumMember] xCED33456 = 0,
        [EnumMember] x489A8C68 = 2,
    }
    public class STU_ABCB02A5 {
        public Enum_FF4B83D8 m_BE02E348;
        public Enum_FF4B83D8 m_283D5F20;
    }
}"""
FILTER = f"""<STU_8EFC337B {NS} m_BC69BB40="281474976710656"
  m_5CE47069="20" m_0220680A="1" m_category="{{null}}">
  <STU_933AD0A9 dragon:name="m_CBCB2AB9" m_breakable="1" />
  <dragon:array dragon:name="m_BC131C03">
    <STU_1E8E0BC3>
      <STU_ABCB02A5 dragon:name="m_B5051BCE" m_DEB2C67E="18446744073709551615" m_E7094A1D="0"
        m_1C0BC578="1" m_ECAEDBE2="0" m_BE02E348="xCED33456" m_283D5F20="x489A8C68" />
    </STU_1E8E0BC3>
    <STU_1E8E0BC3 m_B5051BCE="{{null}}" />
  </dragon:array>
</STU_8EFC337B>"""
RECEIVER = f"""<STURayCastReceiver {NS} m_DC35ABD3="{{null}}">
  <STURayCastReceiverResult dragon:name="m_DE3392C3" m_flags="0" />
  <dragon:array dragon:name="m_6228373E">
    <STURayCastReceiverOverride m_8EB25F67="{{null}}" m_C258455A="{{null}}">
      <dragon:array dragon:name="m_354CDA7A">
        <dragon:ref GUID="00000000002C.097" />
        <dragon:ref GUID="000000000029.097" />
        <dragon:ref GUID="00000000002C.097" />
      </dragon:array>
    </STURayCastReceiverOverride>
    <STURayCastReceiverOverride m_354CDA7A="{{null}}" m_C258455A="{{null}}">
      <STURayCastReceiverResult dragon:name="m_8EB25F67" m_flags="13" />
    </STURayCastReceiverOverride>
  </dragon:array>
</STURayCastReceiver>"""


class RaycastMetadataTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "097").mkdir()
        (self.root / "098").mkdir()
        (self.root / "Types.cs").write_text(TYPES, encoding="utf-8")
        self.enums = EnumTables(self.root)
        self.filter_path = self.root / "097" / "000000000029.097.xml"
        self.filter_path.write_text(FILTER, encoding="utf-8")
        (self.root / "097" / "00000000002C.097.xml").write_text(FILTER, encoding="utf-8")
        self.receiver_path = self.root / "098" / "00000000002A.098.xml"
        self.receiver_path.write_text(RECEIVER, encoding="utf-8")

    def read(self):
        return extractor.raycast_metadata(self.root, self.enums)

    def test_preserves_typed_values_guids_order_null_and_source_hash(self):
        metadata = self.read()
        self.assertEqual(metadata["schema"], "orpheus-raycast-metadata-v1")
        self.assertEqual(metadata["build"], "1.74.0.0.104319")
        profile = metadata["filters"]["0x0690000000000029"]
        self.assertEqual(profile["sha256"], hashlib.sha256(self.filter_path.read_bytes()).hexdigest())
        self.assertIsNone(profile["fields"]["m_category"])
        self.assertEqual(profile["fields"]["m_CBCB2AB9"], {"$": "STU_933AD0A9", "m_breakable": 1})
        first, second = profile["fields"]["m_BC131C03"]
        rule = first["m_B5051BCE"]
        self.assertEqual(rule["m_DEB2C67E"], (1 << 64) - 1)
        self.assertEqual((rule["m_BE02E348"], rule["m_283D5F20"]), (0, 2))
        self.assertIsNone(second["m_B5051BCE"])
        receiver = metadata["receivers"]["0x0E9000000000002A"]
        self.assertEqual(receiver["sha256"], hashlib.sha256(self.receiver_path.read_bytes()).hexdigest())
        fields = receiver["fields"]
        self.assertIsNone(fields["m_DC35ABD3"])
        self.assertEqual(fields["m_DE3392C3"], {"$": "STURayCastReceiverResult", "m_flags": 0})
        self.assertEqual(
            fields["m_6228373E"],
            [
                {
                    "$": "STURayCastReceiverOverride",
                    "m_354CDA7A": ["0x069000000000002C", "0x0690000000000029", "0x069000000000002C"],
                    "m_8EB25F67": None,
                    "m_C258455A": None,
                },
                {
                    "$": "STURayCastReceiverOverride",
                    "m_354CDA7A": None,
                    "m_8EB25F67": {"$": "STURayCastReceiverResult", "m_flags": 13},
                    "m_C258455A": None,
                },
            ],
        )

    def test_rejects_bad_receiver_references_or_flags(self):
        for diagnostic, xml in (
            (
                "STURayCastReceiver root",
                RECEIVER.replace("STURayCastReceiver ", "Other ", 1).replace(
                    "</STURayCastReceiver>", "</Other>"
                ),
            ),
            (r"expected a \.097 profile reference", RECEIVER.replace("000000000029.097", "000000000029.098")),
            (
                r"expected a \.097 profile reference",
                RECEIVER.replace("000000000029.097", "0000000000029.097"),
            ),
            (
                "references absent profile 0x0690000000000099",
                RECEIVER.replace("000000000029.097", "000000000099.097"),
            ),
            (r"m_DE3392C3\.m_flags", RECEIVER.replace('m_flags="0"', 'm_flags="4294967296"')),
        ):
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(ValueError, diagnostic):
                self.receiver_path.write_text(xml, encoding="utf-8")
                self.read()

    def test_rejects_unresolved_filter_enum_and_duplicate_filename_alias(self):
        self.filter_path.write_text(FILTER.replace("xCED33456", "x11111111"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "m_BE02E348 integer"):
            self.read()
        self.filter_path.write_text(FILTER, encoding="utf-8")
        (self.root / "097" / "0029.097.xml").write_text(FILTER, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"duplicate \.097 asset 0x0690000000000029"):
            self.read()

    def test_cli_requires_an_output_outside_the_source_checkout(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit):
            extractor.main(
                [
                    str(self.root),
                    "--owlib",
                    str(self.root),
                    "--output",
                    str(extractor.ROOT / "raycast.json"),
                ]
            )
        self.assertIn("--output must be a JSON path outside the repository", error.getvalue())

    def test_cli_replaces_the_catalog_and_preserves_it_on_write_failure(self):
        types = self.root / "TankLib" / "STU" / "Types"
        types.mkdir(parents=True)
        (types / "Types.cs").write_text(TYPES, encoding="utf-8")
        output = self.root / "raycast.json"
        output.write_text("old catalog", encoding="utf-8")
        argv = [str(self.root), "--owlib", str(self.root), "--output", str(output)]
        with redirect_stdout(io.StringIO()):
            extractor.main(argv)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), self.read())
        before = output.read_bytes()
        with (
            patch.object(Path, "replace", side_effect=OSError("replacement unavailable")),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            extractor.main(argv)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
