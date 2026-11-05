"""Metadata mappings and refusal of incomplete or ambiguous state snapshots."""

import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
