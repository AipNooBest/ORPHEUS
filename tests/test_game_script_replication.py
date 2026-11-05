"""State snapshot validation and delivery through the encrypted game link."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_game_repeated_lifecycles import ProtocolHarness
from test_game_server import pick

from ow174.game import bodytrace, movelog
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.script import expr
from ow174.game.script.graph import graph
from ow174.game.script.replication import ReplicationError, from_runtime, wire_value
from ow174.game.script.research_runtime import RuntimeValue
from ow174.game.statescript import Asset, Bool, Entity, Float, Int


def instance(index, metadata, *, variables=None, active=(), children=None, parent=None):
    return SimpleNamespace(
        index=index, graph=metadata, variables=variables or {}, active=set(active),
        children=children or {}, parent=parent,
    )


def runtime(instances, *, entity_vars=None, complete=True):
    return SimpleNamespace(
        instances=instances, entity_vars=entity_vars or {},
        snapshot=lambda: {"complete": complete, "diagnostics": []},
    )


class ReplicationTests(unittest.TestCase):
    def test_owner_bits_use_filtered_metadata_not_raw_state_indexes(self):
        controller = graph(0x13C1)
        item = instance(1, controller, variables={8419: True, 123456: 9}, active=(25, 43), children={43: 2})
        child = instance(2, graph(0x288A), parent=(1, 43))
        replica = from_runtime(runtime([child, item]), state_payloads={(0x13C1, 25): None})
        first = replica.instances[0]
        self.assertEqual(set(first.active), {controller.owner_slot(25), controller.owner_slot(43)})
        self.assertNotEqual(controller.owner_slot(43), 43)
        self.assertEqual(first.presence[controller.presence_bit(8419)], Bool(True))
        self.assertEqual(first.extra[123456], Int(9))
        self.assertGreater(replica.frame(100).count, 0)

    def test_unresolved_runtime_paths_are_not_sent(self):
        with self.assertRaisesRegex(ReplicationError, "unresolved"):
            from_runtime(runtime([], complete=False))

    def test_unknown_native_state_payload_is_not_silently_empty(self):
        with self.assertRaisesRegex(ReplicationError, "Unverified state payload"):
            from_runtime(runtime([instance(1, graph(0x13C1), active=(25,))]))

    def test_subscript_requires_the_actual_child_relationship(self):
        parent = instance(1, graph(0x13C1), active=(43,), children={43: 2})
        with self.assertRaisesRegex(ReplicationError, "Missing SubScript child"):
            from_runtime(runtime([parent]))

    def test_duplicate_or_cyclic_instance_ids_are_rejected(self):
        metadata = graph(0x288A)
        for items in (
            [instance(1, metadata), instance(1, metadata)],
            [instance(1, metadata, parent=(2, 0)), instance(2, metadata, parent=(1, 0))],
        ):
            with self.subTest(items=items), self.assertRaises(ReplicationError):
                from_runtime(runtime(items))

    def test_scalar_kinds_are_preserved_and_ambiguous_assets_refused(self):
        self.assertEqual(wire_value(True), Bool(True))
        self.assertEqual(wire_value(1), Int(1))
        self.assertEqual(wire_value(1.0), Float(1.0))
        asset = Asset(0x02E000000000006E)
        self.assertIs(wire_value(asset), asset)
        self.assertEqual(wire_value(RuntimeValue("asset", asset.guid)), asset)
        self.assertEqual(wire_value(RuntimeValue("entity", 0xA0000100)), Entity(0xA0000100))
        self.assertEqual(wire_value(RuntimeValue("bool", True)), Bool(True))
        self.assertEqual(wire_value(RuntimeValue("float", 1)), Float(1.0))
        for value in (asset.guid, None, float("nan"), float("inf"), 1e99, Float(float("nan"))):
            with self.subTest(value=value), self.assertRaises(ReplicationError):
                wire_value(value)

    def test_variable_zero_cannot_terminate_the_variable_list_early(self):
        with self.assertRaisesRegex(ReplicationError, "Invalid variable"):
            from_runtime(runtime([], entity_vars={0: True}))


class PlayerDeathReplicationTests(unittest.TestCase):
    def setUp(self):
        self.run = ProtocolHarness()
        self.addCleanup(self.run.close)
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)

    def test_observer_receives_death_without_another_victim_command(self):
        handoffs = self.run.server.create_match(
            PRACTICE_RANGE,
            [(1, "Killer", 0, 0, False), (2, "Victim", 0, 0, False), (3, "Observer", 0, 0, False)],
        )
        (killer_link, killer), (victim_link, victim), (observer_link, observer) = [
            self.run.connect(handoff) for handoff in handoffs
        ]
        for link, player in ((killer_link, killer), (victim_link, victim)):
            self.run.message(link, 21616, pick(player.entity, SOLDIER))
        fight = victim.match.combat
        body = victim.body
        self.run.until(lambda: body in observer_link.models and fight.observers.has(observer, body))
        for _ in range(40):
            self.run.command(victim_link)
            self.run.step()
        model = observer_link.models[body]
        feed_id = next(index for index, graph_id in model.graphs.items() if graph_id == 0x01CF)
        before = len(observer_link.chunks)
        last_command, script_frame = victim.commands.last_frame, victim.body_script.frame
        loadout = 0x09E000000000008D  # Soldier's rifle
        target = next(target for target in fight._targets() if target.owner is victim)

        fight._damage(killer, target, 200.0, True, loadout, victim.match.tick)
        self.assertTrue(fight.is_dead(victim))
        self.run.step(12)  # transport ACKs only, with no further application input

        self.assertEqual((victim.commands.last_frame, victim.body_script.frame), (last_command, script_frame))
        pulses = []
        for _, entity, frame in observer_link.chunks[before:]:
            if entity == body and feed_id in frame.instances:
                feed = frame.instances[feed_id]
                if feed.states.get(108) == (True, {"fresh": True, "count": 1}):
                    self.assertFalse(frame.owner)
                    self.assertFalse(frame.full)
                    pulses.append(feed)
        self.assertEqual(len(pulses), 1)
        feed, = pulses
        self.assertEqual(feed.vars[392], (expr.Entity(killer.body), ()))
        self.assertEqual(feed.vars[1821], (True, ()))
        self.assertEqual(feed.vars[9300], (expr.Asset(loadout), ()))
        self.assertEqual(feed.vars[9296], ((), ()))


if __name__ == "__main__":
    unittest.main()
