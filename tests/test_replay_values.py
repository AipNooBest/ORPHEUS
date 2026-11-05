"""Replay values cannot silently coerce types or lose causal mapping order."""

import math
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import replay_values as values


class ReplayValueTests(unittest.TestCase):
    def test_roundtrip_preserves_exact_types_bits_and_mapping_order(self):
        original = {"second": (1, True, None, b"\x00\xff", -0.0), 7: [1 << 20000, "\u8bad\u7ec3"]}
        copied = values.clone(original)
        self.assertEqual(list(copied), ["second", 7])
        self.assertIs(type(copied["second"]), tuple)
        self.assertIs(type(copied[7]), list)
        self.assertIs(type(copied["second"][0]), int)
        self.assertIs(type(copied["second"][1]), bool)
        self.assertEqual(struct.pack(">d", copied["second"][-1]), b"\x80" + b"\x00" * 7)
        self.assertEqual(values.encode(copied), values.encode(original))
        copied[7].append("independent")
        self.assertEqual(len(original[7]), 2)

    def test_subnormal_is_not_rounded_and_nan_inf_are_rejected(self):
        smallest = float.fromhex("0x0.0000000000001p-1022")
        self.assertEqual(values.clone(smallest).hex(), smallest.hex())
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(values.ReplayValueError):
                values.encode(value)
        for raw in ("7ff0000000000000", "7ff8000000000000"):
            with self.assertRaises(values.ReplayValueError):
                values.decode(["float", raw])

    def test_noncanonical_and_coerced_scalars_are_rejected(self):
        for row in (["int", "1"], ["int", "0x01"], ["int", "0X1"], ["int", True],
                    ["float", "8000 000000000000"], ["bytes", "FF"], ["bytes", "00 00"],
                    ["bool", 1], ["str", b"value"], ["none", None], ["unknown", 0]):
            with self.subTest(row=row), self.assertRaises(values.ReplayValueError):
                values.decode(row)

    def test_unknown_objects_are_rejected_without_calling_user_hooks(self):
        class Opaque:
            def __repr__(self):
                raise AssertionError("must not inspect")

        with self.assertRaises(values.ReplayValueError):
            values.encode(Opaque())
        with self.assertRaises(values.ReplayValueError):
            values.encode({True: 1})

    def test_cycles_and_duplicate_keys_are_rejected(self):
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(values.ReplayValueError):
            values.encode(cyclic)
        encoded = ["list", []]
        encoded[1].append(encoded)
        with self.assertRaises(values.ReplayValueError):
            values.decode(encoded)
        duplicate = ["dict", [[["int", "0x1"], ["none"]], [["int", "0x1"], ["none"]]]]
        with self.assertRaises(values.ReplayValueError):
            values.decode(duplicate)

    def test_structural_budget_is_checked_on_both_sides(self):
        for name, limit in (("MAX_DEPTH", 1), ("MAX_VALUES", 2)):
            with mock.patch.object(values, name, limit):
                with self.assertRaises(values.ReplayValueError):
                    values.encode([[1, 2]])
                with self.assertRaises(values.ReplayValueError):
                    values.decode(["list", [["list", [["int", "0x1"], ["int", "0x2"]]]]])


if __name__ == "__main__":
    unittest.main()
