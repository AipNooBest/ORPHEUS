"""Statescript values and expressions, against the client's rules (expr.py) and real expressions of the
graph data."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script import expr, rulesets
from ow174.game.script import graph as graphs
from ow174.game.script.expr import Asset, Context, Entity, Vec3, equal, f32, to_float, to_int, truthy

SOLDIER = 0x02E000000000006E
PRACTICE_RANGE = 0x0230000000000018


def expression(opcodes, dynamics=(), floats=(), configs=None) -> dict:
    """An expression config var as the graph data writes one."""
    return {
        "$": "STU_919FD47C",
        "m_configVars": list(configs) if configs else None,
        "m_expression": {
            "$": "STUConfigVarExpressionData",
            "m_opcodes": list(opcodes),
            "m_dynamicVars": [dynamic(var, scope) for var, scope in dynamics] or None,
            "m_D99EF254": list(floats) or None,
        },
    }


def dynamic(var: int, scope: int = 0) -> dict:
    return {"$": "STUConfigVarDynamic", "m_identifier": var, "m_60DB8F99": scope}


class Vars(Context):
    """Variables from dicts, with a log of what was read."""

    def __init__(self, instance=None, entity=None, rules=None) -> None:
        super().__init__()
        self.instance = instance or {}
        self.entity = entity or {}
        self.rules = rules or {}
        self.reads = []

    def var(self, scope, var):
        self.reads.append((scope, var))
        return (self.entity if scope else self.instance).get(var)

    def ruleset(self, key):
        found = self.var(1, key)
        return found if found is not None else self.rules.get(key, 0)


class ValueTests(unittest.TestCase):
    def test_truthiness(self):
        for value, expected in [
            (None, False), (False, False), (True, True), (0, False), (3, True), (0.0, False), (0.5, True),
            (Entity(0), False), (Entity(0xA0000101), True), (Entity(0xBFFFFFFF), False), ((), False),
            ((False, True), False), ((True,), True), (Asset(0), False), (Asset(5), True),
            (Vec3(0.0, 0.0, 0.0), False), (Vec3(0.0, 1.0, 0.0), True),
        ]:  # fmt: skip
            self.assertEqual(truthy(value), expected, value)

    def test_numbers(self):
        self.assertEqual(to_float(None), 0.0)
        self.assertEqual(to_float(True), 1.0)
        self.assertEqual(to_int(2.5), 3)
        self.assertEqual(to_int(-2.5), -3)
        self.assertEqual(to_int(2.4999), 2)
        self.assertEqual(to_int((7, 8)), 7)
        self.assertEqual(to_int(None), 0)
        self.assertEqual(to_int(Asset(4), 9), 9)

    def test_equality(self):
        self.assertTrue(equal(None, 0))
        self.assertTrue(equal(0.0, None))
        self.assertFalse(equal(None, 1))
        self.assertTrue(equal(None, None))
        self.assertTrue(equal(True, 5))
        self.assertTrue(equal(1, 1.000001))
        self.assertFalse(equal(1, 1.001))
        self.assertTrue(equal((4,), 4))
        self.assertFalse(equal((4, 5), 4))
        self.assertTrue(equal(Asset(0x0D80000000002908), Asset(0x0D80000000002908)))
        self.assertFalse(equal(Asset(1), 1))
        self.assertTrue(equal(Entity(3), Entity(3)))

    def test_float32(self):
        self.assertEqual(f32(0.1), 0.10000000149011612)
        self.assertEqual(expr.round_half_away(f32(f32(0.7665) * 1000.0)), 767)


class BytecodeTests(unittest.TestCase):
    def run_code(self, opcodes, dynamics=(), floats=(), ctx=None, configs=None):
        return expr.evaluate(expression(opcodes, dynamics, floats, configs), ctx or Vars())

    def test_arithmetic_is_float32_and_division_by_zero_is_zero(self):
        self.assertEqual(self.run_code([6, 0, 6, 1, 23, 0], floats=[0.1, 0.2]), f32(f32(0.1) + f32(0.2)))
        self.assertEqual(self.run_code([6, 0, 6, 1, 26, 0], floats=[3.0, 0.0]), 0.0)
        self.assertIsInstance(self.run_code([6, 0, 6, 1, 25, 0], floats=[2.0, 3.0]), float)

    def test_round_gives_an_int_and_compares_a_bool(self):
        self.assertEqual(self.run_code([6, 0, 68, 0], floats=[29.5]), 30)
        self.assertIsInstance(self.run_code([6, 0, 68, 0], floats=[29.5]), int)
        self.assertIs(self.run_code([6, 0, 6, 1, 34, 0], floats=[1.0, 1.0]), False)  # 1 < 1 - eps
        self.assertIs(self.run_code([6, 0, 6, 1, 33, 0], floats=[1.0, 1.0]), True)  # 1 - eps <= 1

    def test_and_or_give_the_deciding_operand(self):
        ctx = Vars({1: 0.0, 2: 7})
        self.assertEqual(self.run_code([8, 0, 2, 4, 8, 1, 0], [(1, 0), (2, 0)], ctx=ctx), 0.0)
        self.assertEqual(self.run_code([8, 0, 3, 4, 8, 1, 0], [(1, 0), (2, 0)], ctx=ctx), 7)

    def test_unset_variables_and_indexes(self):
        self.assertIsNone(self.run_code([8, 0, 0], [(5, 0)]))
        ctx = Vars(entity={29: (False, True)})
        self.assertIs(self.run_code([8, 0, 6, 0, 17, 0], [(29, 1)], [1.0], ctx), True)
        self.assertIsNone(self.run_code([8, 0, 6, 0, 17, 0], [(29, 1)], [3.0], ctx))
        self.assertEqual(self.run_code([8, 0, 18, 0], [(29, 1)], ctx=ctx), 2)
        self.assertEqual(ctx.reads, [(1, 29)] * 3)

    def test_a_soldier_reload_time(self):
        # 0254 st51's timeout: (#842e ? 0.5 * #6885 : #581 * #6884)
        node = graphs.graph(0x0254).state(51)
        ctx = Vars({581: f32(1.5), 6884: f32(0.511), 6885: f32(0.1)})
        timeout = expr.evaluate(node.fields["m_timeout"], ctx)
        self.assertEqual(timeout, f32(f32(1.5) * f32(0.511)))
        self.assertEqual(expr.round_half_away(f32(timeout * 1000.0)), 767)
        self.assertEqual(expr.source(node.fields["m_timeout"]), "(#842e ? (0.5 * #6885) : (#581 * #6884))")

    def test_the_magazine_size_from_the_ruleset(self):
        # 0254 rid 8: #198 = round(30 * GetGameRulesetValue(#10265e))
        node = graphs.graph(0x0254).by_rid(8)
        value = node.fields["m_value"]
        self.assertEqual(expr.evaluate(value, Vars(rules={10265: 1.0})), 30)
        self.assertEqual(expr.evaluate(value, Vars(entity={10265: 2.0})), 60)
        self.assertEqual(expr.evaluate(value, Vars()), 0)
        self.assertEqual(expr.lvalue(node.fields["m_out_Var"]), (0, 198))

    def test_the_weapon_check(self):
        # 0254 st0: #31e == #28, entity and instance scope
        cfg = graphs.graph(0x0254).state(0).fields["m_condition"]
        self.assertTrue(truthy(expr.evaluate(cfg, Vars({28: 1}, {31: 1}))))
        self.assertFalse(truthy(expr.evaluate(cfg, Vars({28: 1}, {31: 2}))))
        self.assertTrue(truthy(expr.evaluate(cfg, Vars())))  # null == null

    def test_ability_cooldown_durations(self):
        # max(base * scalar, least): Biotic Field's 15 s (0257 st12 #472), its scalar from rules if set.
        stack = next(node for node in graphs.graph(0x0257).states if node and node.state == 12)
        cfg = stack.fields["m_value"]
        self.assertEqual(expr.evaluate(cfg, Vars()), 15.0)
        self.assertEqual(expr.evaluate(cfg, Vars(entity={9537: 0.5})), 7.5)
        self.assertEqual(expr.evaluate(cfg, Vars(entity={9528: 0.0})), 0.0)
        least = {**cfg, "m_EA70ACC2": {"$": "STUConfigVarFloat", "m_value": 0.5}}
        self.assertEqual(expr.evaluate(least, Vars(entity={9537: 0.0})), 0.5)
        self.assertEqual(expr.source(cfg), "cooldown(15, 0, valueOr(#9537e, valueOr(#9528e, 1)))")


class RulesetTests(unittest.TestCase):
    def test_soldier_in_the_practice_range(self):
        self.assertEqual(rulesets.default(10265, SOLDIER, PRACTICE_RANGE), 1.0)  # clip size scalar
        self.assertIs(rulesets.default(10266, SOLDIER, PRACTICE_RANGE), False)  # no ammo use
        self.assertIsNone(rulesets.default(1, SOLDIER, PRACTICE_RANGE))

    def test_stack_priorities(self):
        self.assertEqual(expr.stack_priority({"m_priority": "0x0C40000000000002"}), (-10.0, False))
        self.assertEqual(expr.stack_priority({"m_priority": "0x0C4000000000003E"}), (-5.0, True))
        self.assertEqual(expr.stack_priority(None), (0.0, True))


if __name__ == "__main__":
    unittest.main()
