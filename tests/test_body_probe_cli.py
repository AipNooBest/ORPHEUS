"""The real extracted graph's blockers remain visible in the offline probe."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("body_probe", ROOT / "tools" / "probe_body_script.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_partial_weapon_experiment_does_not_claim_a_complete_hero(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "probe.json"
            result = probe.main(["--only-graph", "0254", "--output", str(output)])
            record = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertEqual(record["scope"], "single-graph")
        self.assertFalse(record["runtime"]["complete"])
        self.assertFalse(record["replication"]["ready"])
        self.assertFalse(record["real_client_verified"])
        self.assertTrue(record["runtime"]["diagnostics"])
        self.assertEqual(len(record["bundle_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
