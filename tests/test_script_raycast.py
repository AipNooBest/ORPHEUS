"""Authored raycast actions write complete results before following their graph link."""

import unittest
from unittest.mock import patch

from test_script_runtime_budget import graph, node, plug

from ow174.game.script import runtime
from ow174.game.script.expr import INSTANCE, Entity, Vec3

OWNER = 0x80000101
OTHER = 0x80000201
ORIGIN = Vec3(0.0, 1.5, 0.0)
DIRECTION = Vec3(0.0, 0.0, 2.0)
FILTER = {
    "$": "STUConfigVarFilter",
    "m_C1903771": {"$": "STU_20DB8AB5", "m_C1903771": "0x0690000000000029"},
}
OLD = (True, Entity(0x80000301), Vec3(0.0, 1.0, 3.0), Vec3(0.0, 0.0, -1.0))
OUTPUTS = ("m_out_HitSomething", "m_out_HitEntity", "m_out_HitPointWS", "m_out_HitNormal")


def variable(number):
    return {"$": "STUConfigVarDynamic", "m_identifier": number, "m_60DB8F99": 0}


class ProviderWorld(runtime.World):
    def __init__(self, result):
        super().__init__()
        self.result = result
        self.requests = []

    def raycast(self, request):
        self.requests.append(request)
        return self.result


class RaycastActionTests(unittest.TestCase):
    def cast(self, world, **overrides):
        fields = {
            "m_type": 0,
            "m_startPosWS": ORIGIN,
            "m_dirWS": DIRECTION,
            "m_castLength": 15,
            "m_8CBE558B": {"$": "STUConfigVarBool", "m_value": 0},
            "m_ignoreEntities": [{"$": "STU_B5A0CAF0"}, Entity(OTHER), Entity(OTHER)],
            "m_castFilter": FILTER,
            "m_outPlug": plug((1, "m_inPlug")),
            **{name: variable(i) for i, name in enumerate(OUTPUTS, 1)},
            **overrides,
        }
        graph_data = graph(
            (
                node(0, "STU_08D6BA6E", **fields),
                node(
                    1,
                    "STU_12F5D52D",
                    m_inPlug={"$": "STUStatescriptInputPlug", "links": []},
                    m_143D4C5B=[{"m_out_Var": variable(10 + i), "m_value": variable(i)} for i in range(1, 5)],
                ),
            )
        )
        instance = runtime.Component(OWNER, world=world).create(graph_data, init=False)
        for i, value in enumerate(OLD, 1):
            instance.write((INSTANCE, i), value)

        cast = graph_data.by_rid(0)
        with patch.object(instance, "follow", wraps=instance.follow) as follow:
            self.assertTrue(instance.run_node(cast, None))
        self.assertEqual(
            [call.args for call in follow.call_args_list if call.args[0] is cast],
            [
                (cast, "m_outPlug", None),
            ],
        )
        outputs = tuple(instance.evaluate(variable(i)) for i in range(1, 5))
        copied = tuple(instance.evaluate(variable(i)) for i in range(11, 15))
        self.assertEqual(outputs, copied)
        return outputs

    def test_hit_uses_resolved_inputs_and_exposes_the_opaque_filter(self):
        result = (True, Entity(OTHER), Vec3(0.0, 1.5, 4.0), Vec3(0.0, 1.0, 0.0))
        world = ProviderWorld(runtime.RaycastResult(*result))
        self.assertEqual(self.cast(world), result)
        (request,) = world.requests
        self.assertEqual(
            request, runtime.RaycastRequest(ORIGIN, DIRECTION, 15.0, (OWNER, OTHER, OTHER), FILTER)
        )
        self.assertIs(request.filter_config, FILTER)

    def test_completed_miss_writes_the_providers_explicit_outputs(self):
        result = (False, None, Vec3(0.0, 1.5, 15.0), None)
        world = ProviderWorld(runtime.RaycastResult(*result))
        self.assertEqual(self.cast(world), result)
        self.assertEqual(len(world.requests), 1)

    def test_unavailable_keeps_outputs_and_follows_without_unneeded_evaluation(self):
        world = runtime.World()
        with patch.object(world, "native", side_effect=AssertionError("no provider")):
            self.assertEqual(self.cast(world, m_startPosWS={"$": "UnusedOrigin"}), OLD)

        provider = ProviderWorld(None)
        self.assertEqual(self.cast(provider), OLD)
        self.assertEqual(len(provider.requests), 1)

    def test_unsupported_shapes_and_invalid_inputs_do_not_reach_the_provider(self):
        for overrides in (
            {"m_type": 1},
            {"m_castRadius": 1},
            {"m_8CBE558B": {"$": "STUConfigVarBool", "m_value": 1}},
            {"m_2158DFDE": variable(9)},
            {"m_dirWS": Vec3(0.0, 0.0, 0.0)},
            {"m_castLength": float("inf")},
            {"m_ignoreEntities": [None]},
            {"m_out_HitNormal": {"$": "STUConfigVarInt", "m_value": 9}},
            {"m_out_HitNormal": variable(1)},
        ):
            with self.subTest(overrides=overrides):
                world = ProviderWorld(runtime.RaycastResult(False, None, None, None))
                self.assertEqual(self.cast(world, **overrides), OLD)
                self.assertEqual(world.requests, [])

    def test_invalid_result_does_not_partially_update_outputs(self):
        world = ProviderWorld(runtime.RaycastResult(False, None, Vec3(float("nan"), 0.0, 0.0), None))
        self.assertEqual(self.cast(world), OLD)
        self.assertEqual(len(world.requests), 1)
