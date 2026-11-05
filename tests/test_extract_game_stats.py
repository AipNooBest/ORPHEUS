"""Score-event metadata is extracted from typed DragonML without dropping evidence."""

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.extract_game_graphs import EnumTables
from tools.extract_game_stats import score_event_metadata

TYPES = """namespace Fixture {
    public enum STUStatEvent : uint {
        [EnumMember] x11111111 = 17,
        [EnumMember] NamedEvent = 32,
    }
    public class StatEventScoreScaler {
        public STUStatEvent m_B44A42A0;
        public float m_9A97C666;
        public byte m_20100AF4;
    }
}"""

SCALERS = """  <dragon:array dragon:name="m_AFED546B">
    <StatEventScoreScaler dragon:id="3" m_B44A42A0="NamedEvent" m_9A97C666="0,04" m_20100AF4="1" />
    <dragon:ref dragon:id="3" />
  </dragon:array>
"""

FIXTURE = """<STU_A1BCAB6E xmlns:dragon="https://yretenai.com/dragonml/v1">
  <dragon:array dragon:name="m_3ED74851">
    <STU_E2C36BB8 dragon:id="2" m_identifier="{null}" m_0385849C="0,5" m_C68DDECA="0" m_06CD1888="1">
      <dragon:array dragon:name="m_34A798D4">
        <STUStatEvent>x11111111</STUStatEvent>
        <STUStatEvent>NamedEvent</STUStatEvent>
        <STUStatEvent>x11111111</STUStatEvent>
      </dragon:array>
      <dragon:ref dragon:name="m_text" GUID="00000000C001.07C" />
    </STU_E2C36BB8>
    <dragon:ref dragon:id="2" />
    <STU_E2C36BB8 m_34A798D4="{null}" m_0385849C="0" m_C68DDECA="0" m_06CD1888="5">
      <dragon:ref dragon:name="m_identifier" GUID="0000000000A1.01C" />
      <dragon:ref dragon:name="m_text" GUID="00000000C001.07C" />
    </STU_E2C36BB8>
  </dragon:array>
""" + SCALERS + "</STU_A1BCAB6E>"


class ScoreEventMetadataTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        (folder / "Types.cs").write_text(TYPES, encoding="utf-8")
        self.enums = EnumTables(folder)
        self.path = folder / "00000000003F.054.xml"

    def extract(self, xml):
        self.path.write_bytes(xml.encode("utf-8"))
        return score_event_metadata(self.path, self.enums)

    def test_preserves_order_references_and_typed_fields(self):
        label = {
            "$": "STU_E2C36BB8",
            "m_identifier": None,
            "m_0385849C": 0.5,
            "m_C68DDECA": 0,
            "m_06CD1888": 1,
            "m_34A798D4": [17, 32, 17],
            "m_text": "0x0DE000000000C001",
        }
        identifier_label = {
            "$": "STU_E2C36BB8",
            "m_identifier": "0x0D800000000000A1",
            "m_0385849C": 0,
            "m_C68DDECA": 0,
            "m_06CD1888": 5,
            "m_34A798D4": None,
            "m_text": "0x0DE000000000C001",
        }
        scaler = {
            "$": "StatEventScoreScaler",
            "m_B44A42A0": 32,
            "m_9A97C666": 0.04,
            "m_20100AF4": 1,
        }
        self.assertEqual(self.extract(FIXTURE), {
            "asset": "003F.054",
            "sha256": hashlib.sha256(FIXTURE.encode("utf-8")).hexdigest(),
            "labels": [label, label, identifier_label],
            "scalers": [scaler, scaler],
        })

    def test_rejects_unresolved_enum_members(self):
        for name, xml in (
            ("label", FIXTURE.replace(">NamedEvent<", ">x22222222<")),
            ("scaler", FIXTURE.replace('m_B44A42A0="NamedEvent"', 'm_B44A42A0="UnlistedEvent"')),
        ):
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, rf"{name} enum"):
                self.extract(xml)

    def test_rejects_invalid_root_and_arrays(self):
        null_scalers = '<dragon:null dragon:name="m_AFED546B" />'
        wrong_entry = FIXTURE.replace("<StatEventScoreScaler ", "<STU_E2C36BB8 ")
        malformed_scaler = FIXTURE.replace('m_9A97C666="0,04"', 'm_9A97C666="bogus"')
        for name, diagnostic, xml in (
            ("wrong root", "STU_A1BCAB6E", FIXTURE.replace("STU_A1BCAB6E", "STU_13047F85")),
            ("missing labels", "m_3ED74851", FIXTURE.replace("m_3ED74851", "m_other")),
            ("wrong scalers field type", "m_AFED546B", FIXTURE.replace(SCALERS, null_scalers)),
            ("wrong scaler entry type", "m_AFED546B", wrong_entry),
            ("non-numeric scaler", "scaler fields", malformed_scaler),
        ):
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, diagnostic):
                self.extract(xml)
