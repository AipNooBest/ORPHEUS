"""The ultimate's charge rule (ult_charge.py): rates as meter fills, lines per hero, the fallback."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import ult_charge
from ow174.paths import ROOT

SOLDIER = 0x02E000000000006E
OTHER = 0x02E0000000000061


def table(contents: dict):
    """Point the loader at a table of the test's own (the shipped path belongs to the runtime pack)."""
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "game_ult_charge_174.json"
    path.write_text(json.dumps(contents), encoding="utf-8")
    original = ult_charge.TABLE_PATH
    ult_charge.TABLE_PATH = path
    ult_charge._table.cache_clear()

    def restore():
        ult_charge.TABLE_PATH = original
        ult_charge._table.cache_clear()
        directory.cleanup()

    return restore


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(
            table(
                {
                    "status": "test",
                    "defaults": {
                        "damage_to_full": 400.0,
                        "heal_to_full": 1000.0,
                        "kill_bonus_percent": 10.0,
                    },
                    "heroes": {f"{SOLDIER:016X}": {"damage_to_full": 380.0, "heal_to_full": None}},
                }
            )
        )

    def test_the_amount_is_the_meters_own_fraction_of_what_filled_it(self):
        self.assertEqual(ult_charge.points("damage", SOLDIER, 190.0, 1000.0), 500.0)
        self.assertEqual(ult_charge.points("damage", SOLDIER, 95.0, 2000.0), 500.0)

    def test_a_hero_without_a_line_takes_the_defaults(self):
        self.assertEqual(ult_charge.points("damage", OTHER, 100.0, 400.0), 100.0)
        self.assertEqual(ult_charge.rule(OTHER), ult_charge.Rule(400.0, 1000.0, 10.0, known=False))

    def test_a_line_can_close_a_way_of_charging(self):
        self.assertEqual(ult_charge.points("healing", SOLDIER, 500.0, 1000.0), 0.0)  # its line says None
        self.assertEqual(ult_charge.points("healing", OTHER, 100.0, 1000.0), 100.0)

    def test_an_elimination_is_a_percent_of_a_full_meter(self):
        self.assertEqual(ult_charge.points("kill", SOLDIER, 0.0, 1000.0), 100.0)  # defaults' 10 %
        self.assertEqual(ult_charge.points("kill", OTHER, 0.0, 400.0), 40.0)

    def test_a_meter_without_a_cost_or_an_unknown_event_charges_nothing_or_raises(self):
        self.assertEqual(ult_charge.points("damage", SOLDIER, 380.0, 0.0), 0.0)
        with self.assertRaises(ValueError):
            ult_charge.points("experience", SOLDIER, 100.0, 1000.0)

    def test_no_table_at_all_leaves_only_the_passive_rule_and_says_so(self):
        self.addCleanup(self._restore)
        ult_charge.TABLE_PATH = Path(tempfile.gettempdir()) / "absent-charge-table.json"
        ult_charge._table.cache_clear()
        self.assertEqual(ult_charge.points("damage", SOLDIER, 380.0, 1000.0), 1000.0)  # the fallback
        self.assertEqual(ult_charge.rule(SOLDIER).known, False)

    @staticmethod
    def _restore():
        import ow174.paths

        ult_charge.TABLE_PATH = ow174.paths.DATA_DIR / "game_ult_charge_174.json"
        ult_charge._table.cache_clear()


class ShippedTableTests(unittest.TestCase):
    def test_the_shipped_table_is_placeholder_data_of_the_stated_shape(self):
        path = ROOT / "data" / "game_ult_charge_174.json"
        if not path.is_file():
            self.skipTest("the table is pack data (tools/extract_game_ult_charge.py)")
        table = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(table["status"], "placeholder")  # replace the numbers with the server's own
        for line in [table["defaults"], *table["heroes"].values()]:
            for field in ("damage_to_full", "heal_to_full", "kill_bonus_percent"):
                self.assertTrue(field not in line or line[field] is None or line[field] > 0)
            for field in ("damage_to_full", "kill_bonus_percent"):
                self.assertGreater(line.get(field, 1), 0)
        for guid in table["heroes"]:
            self.assertRegex(guid, r"^[0-9A-F]{16}$")


if __name__ == "__main__":
    unittest.main()
