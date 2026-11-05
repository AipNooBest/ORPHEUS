"""Mode-entity member reads, independent of hero and mode ruleset defaults."""

import unittest
from unittest.mock import patch

from test_game_combat import Run

from ow174.game import bodytrace, movelog
from ow174.game.content import PRACTICE_RANGE, SOLDIER, game_map, heroes
from ow174.game.match import GAME_MODE_ENTITY
from ow174.game.script import expr, graph, runtime
from ow174.game.script.bot import DIED

ASSAULT = 0x0230000000000014
ALT_ASSAULT = 0x0230000000000067
HANAMURA = 0x0800000000000165
TRACER = 0x02E0000000000003
TYPED_MODE = 0x023000000000FFFF
BODY = 0xA0000101
OTHER_BODY = 0xA0000201
MODE_ENTITY = {"$": "STU_A3DEF368"}
SETTINGS_KEY = 0x4504
OBJECTIVE_EVENT = 0x0240000000000C3F


class ModeVariableTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)
        graph.mode_variables.cache_clear()
        self.addCleanup(graph.mode_variables.cache_clear)

    def install(self, modes):
        data = {key: value for key, value in graph._data().items() if key != "mode_variables"}
        if modes is not None:
            data["mode_variables"] = modes
        edit = patch.object(graph, "_data", return_value=data)
        edit.start()
        self.addCleanup(edit.stop)

    def test_assault_mode_entity_gates_the_real_0ba4_objective_path(self):
        # These keys and values come from the target-build 0014, 0067 and 0018 mode assets.
        self.install({
            f"0x{ASSAULT:016X}": {"17668": True},
            f"0x{ALT_ASSAULT:016X}": {"17668": True},
            f"0x{PRACTICE_RANGE.mode_guid:016X}": {"27534": False, "9525": True, "9539": True},
        })
        cases = (
            (game_map(HANAMURA, ASSAULT), SOLDIER, (1.0, 2.0)),
            (game_map(HANAMURA, ALT_ASSAULT), TRACER, (1.0, 2.0)),
            (PRACTICE_RANGE, SOLDIER, (None, None)),
        )
        for location, hero, expected in cases:
            self.assertIsNotNone(location)
            with self.subTest(mode=location.mode_guid, hero=hero):
                run = Run(location)
                (player,) = run.players
                if hero != SOLDIER:
                    run.match.switch_hero(player, heroes()[hero])
                component = player.body_script.component
                # Isolate the objective branch from 004B's separate native startup filter.
                instance = component.create(graph.graph(0x0BA4))
                component.set(7059, True)
                count = instance.find_var(runtime.INSTANCE_SCOPE, 17679)
                got = []
                for _ in range(2):
                    self.assertEqual(runtime.send_message(component, OBJECTIVE_EVENT, {}, player.body), 1)
                    component.run_now()
                    got.append(count.value())
                self.assertEqual(tuple(got), expected)
                self.assertIsNone(component.world.ruleset(SETTINGS_KEY))
                runtime.send_message(component, DIED, {0x12: expr.Entity(OTHER_BODY)}, OTHER_BODY)
                component.run_now()
                self.assertEqual(count.value(), 0.0)

    def test_types_are_preserved_and_lookup_is_scoped_to_the_mode_entity(self):
        self.install({
            f"0x{TYPED_MODE:016X}": {"10": False, "11": 2, "12": "0x0D800000000025BD"},
        })
        world = runtime.World(SOLDIER, TYPED_MODE)
        self.assertEqual(world.native(MODE_ENTITY, None), expr.Entity(GAME_MODE_ENTITY))
        self.assertIs(world.entity_var(GAME_MODE_ENTITY, 10), False)
        count = world.entity_var(GAME_MODE_ENTITY, 11)
        self.assertIs(type(count), int)
        self.assertEqual(count, 2)
        self.assertEqual(world.entity_var(GAME_MODE_ENTITY, 12), expr.Asset(0x0D800000000025BD))
        self.assertIsNone(world.entity_var(GAME_MODE_ENTITY, 13))
        self.assertIsNone(world.entity_var(BODY, 10))
        self.assertIsNone(runtime.World(SOLDIER, ASSAULT).native(MODE_ENTITY, None))

    def test_old_pack_keeps_the_mode_entity_unknown_and_keeps_schema_defaults(self):
        self.install(None)
        world = runtime.World(SOLDIER, PRACTICE_RANGE.mode_guid)
        self.assertIsNone(world.native(MODE_ENTITY, None))
        self.assertIsNone(world.entity_var(GAME_MODE_ENTITY, SETTINGS_KEY))
        self.assertEqual(world.ruleset(10265), 1.0)
        self.assertIs(world.ruleset(10266), False)


if __name__ == "__main__":
    unittest.main()
