"""Recipient-list routing through the ordinary target-build game message actions."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script import expr, graph, nodes, runtime

OWNER = 0xA0000101
FIRST = 0xA0000201
SECOND = 0xA0000301
OTHER = 0xA0000401
MESSAGE_BB = 0x02400000000000BB


class RecipientWorld(runtime.World):
    def __init__(self):
        super().__init__()
        self.targets = None
        self.dispatched = []

    def native(self, cfg, instance):
        if cfg["$"] == "STU_ED45C5E8":
            return self.targets
        return super().native(cfg, instance)

    def send_message(self, target, message, params, sender):
        self.dispatched.append((target, message, sender))


class MessageRecipientTests(unittest.TestCase):
    def setUp(self):
        self.world = RecipientWorld()
        self.component = runtime.Component(OWNER, world=self.world)
        self.sender = self.component.create(graph.graph(0x002B), init=False)
        self.action = self.sender.graph.node(28)  # 00BB.025 to STU_ED45C5E8(...)
        self.receiver = self.component.create(graph.graph(0x01CA), init=False)  # receives 00BB.025

    def test_00bb_routes_self_and_all_valid_direct_entities_in_order(self):
        self.world.targets = (
            expr.Entity(SECOND), expr.Entity(OWNER), None, OTHER, (expr.Entity(OTHER),),
            expr.Entity(0), expr.Entity(0xFFFFFFFF), expr.Entity(FIRST),
        )
        post = self.receiver.post

        def record_local(event):
            message, _, sender = event.message
            self.world.dispatched.append((OWNER, message, sender))
            post(event)

        with patch.object(self.receiver, "post", side_effect=record_local):
            self.sender.run_node(self.action, None)

        self.assertEqual(
            self.world.dispatched, [(target, MESSAGE_BB, OWNER) for target in (SECOND, OWNER, FIRST)]
        )
        (event,) = self.receiver.queue
        self.assertEqual((event.kind, event.node.pos), (runtime.MESSAGE, 25))

    def test_00ba_routes_multiple_external_entities(self):
        self.world.targets = (expr.Entity(FIRST), expr.Entity(SECOND))
        sender = self.component.create(graph.graph(0x0024), init=False)
        sender.run_node(sender.graph.node(47), None)  # 00BA.025 to the same selector
        self.assertEqual(
            self.world.dispatched, [(target, 0x02400000000000BA, OWNER) for target in (FIRST, SECOND)]
        )

    def test_null_and_empty_select_no_recipients(self):
        for targets in (None, ()):
            with self.subTest(targets=targets):
                self.world.targets = targets
                self.sender.run_node(self.action, None)
                self.assertEqual(self.world.dispatched, [])
                self.assertEqual(self.receiver.queue, [])

    def test_scalar_alias_and_default_self_target_keep_their_behavior(self):
        fields = {"m_gameMessage": self.action.field("m_gameMessage")}
        action = SimpleNamespace(fields=fields)
        self.world.targets = expr.Entity(FIRST)
        fields["m_target"] = self.action.field("m_targets")
        nodes.send(self.sender, action, None)
        self.assertEqual(self.world.dispatched, [(FIRST, MESSAGE_BB, OWNER)])

        del fields["m_target"]
        nodes.send(self.sender, action, None)
        (event,) = self.receiver.queue
        message, _, sender = event.message
        self.assertEqual((event.kind, message, sender), (runtime.MESSAGE, MESSAGE_BB, OWNER))
