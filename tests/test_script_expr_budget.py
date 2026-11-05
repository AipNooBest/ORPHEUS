"""Malformed expression paths terminate and report an error instead of hanging the server."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script import expr


def expression(code, floats=()):
    return {"m_expression": {"m_opcodes": code, "m_D99EF254": list(floats)}}


class ExpressionBudgetTests(unittest.TestCase):
    def rejected(self, code, reason, *, floats=(), **options):
        with mock.patch.object(expr, "warn_once") as report:
            result = expr.run(expression(code, floats), expr.Context(), **options)
        self.assertIsNone(result)
        report.assert_called_once()
        self.assertIn(reason, str(report.call_args.args[-1]))

    def test_zero_offset_jump_does_not_hang(self):
        self.rejected([1, 0], "non-progressing jump")

    def test_taken_conditional_zero_offset_jumps_are_rejected(self):
        for opcode, value in ((2, 0.0), (3, 1.0), (4, 0.0), (5, 1.0)):
            with self.subTest(opcode=opcode):
                self.rejected([6, 0, opcode, 0, 0], "non-progressing jump", floats=[value])

    def test_jump_past_or_exactly_at_the_end_is_rejected(self):
        for offset in (2, 3, 255):
            with self.subTest(offset=offset):
                self.rejected([1, offset], "leaves the stream")
        self.rejected([6, 0, 4, 255, 0], "leaves the stream", floats=[0.0])

    def test_truncated_operand_bytes_are_not_synthesized_as_zero(self):
        for code in ([1], [2], [4], [6], [10], [10, 1], [14], [20]):
            with self.subTest(code=code):
                self.rejected(code, "truncated opcode")

    def test_instruction_budget_counts_the_exit_instruction(self):
        code = [6, 0, 6, 1, 23, 0]
        self.rejected(code, "instruction budget 3 exhausted", floats=[2.0, 3.0], max_steps=3)
        self.assertEqual(expr.run(expression(code, [2.0, 3.0]), expr.Context(), max_steps=4), 5.0)

    def test_bad_budget_or_nonbyte_inputs_report_an_expression_error(self):
        for budget in (0, -1, True, 1.5):
            with self.subTest(budget=budget):
                self.rejected([0], "max_steps must be", max_steps=budget)
        self.rejected([256], "invalid opcode byte")
        self.rejected([1, -1], "invalid operand byte")

    def test_an_untaken_bad_jump_does_not_invalidate_the_executed_path(self):
        self.assertEqual(expr.run(expression([6, 0, 2, 255, 6, 1, 0], [1.0, 7.0]), expr.Context()), 7.0)

    def test_real_ternary_paths_do_not_require_linear_instruction_boundaries(self):
        # The next branch begins at byte 11; a linear scan would incorrectly classify
        # byte 11 as the operand of byte 10 in the branch that already returned at 9.
        code = [14, 0, 14, 1, 38, 4, 6, 6, 0, 0, 4, 6, 1, 0]
        cfg = expression(code, [10.0, 20.0])
        cfg["m_configVars"] = [
            {"$": "STUConfigVarInt", "m_value": 1},
            {"$": "STUConfigVarInt", "m_value": 1},
        ]
        self.assertEqual(expr.run(cfg, expr.Context()), 20.0)
        cfg["m_configVars"][1]["m_value"] = 2
        self.assertEqual(expr.run(cfg, expr.Context()), 10.0)


if __name__ == "__main__":
    unittest.main()
