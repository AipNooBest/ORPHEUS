"""Recipient-list routing through the ordinary target-build game message actions."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_combat import Run
from test_game_graph_analysis import make_graph, node, output
from test_script_abilities import Q

from ow174.game import bodytrace, movelog
from ow174.game.script import expr, graph, nodes, runtime
from ow174.game.script.driver import ULT_COST

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


class BodyMessageRoutingTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)
        self.run = Run(names=("Killer", "Victim", "Outsider"))
        self.killer, self.victim, self.outsider = self.run.players
        for _ in range(40):
            self.run.step({})

    @staticmethod
    def receiver(player):
        return next(
            item for item in player.body_script.component.instances.values() if item.graph.index == 0x0C7D
        )

    @staticmethod
    def award(receiver):
        return tuple(receiver.vars[key].value() if key in receiver.vars else None for key in (499, 7462))

    def eliminate(self, *, visor):
        if visor:
            script = self.victim.body_script
            script.set_ult_charge(script.component.vars[ULT_COST].value())
            self.run.step({self.victim: (0, 0, Q)})
            self.assertIs(script.component.entity_var(842).value(), True)
        fight = self.run.match.combat
        target = next(target for target in fight._targets() if target.owner is self.victim)
        fight._damage(self.killer, target, 200.0, False, 0x09E000000000008D, self.run.frame)
        self.assertTrue(fight.is_dead(self.victim))

    def update_without_commands(self):
        self.run.frame += 1
        self.run.match.update(self.run.frame * 0.016, self.run.frame)

    def test_visor_death_routes_064a_to_killer_at_existing_clock(self):
        script = self.killer.body_script
        killer, outsider = self.receiver(self.killer), self.receiver(self.outsider)
        self.eliminate(visor=True)
        before = (script.frame, script.component.now, self.killer.commands.last_frame)
        self.assertEqual(self.award(killer), (None, None))  # queued, outside the sending VM's call stack
        self.update_without_commands()
        self.assertEqual(self.award(killer), (50.0, expr.Asset(0x0DE000000000405F)))
        self.assertIs(type(killer.vars[499].value()), float)
        self.assertEqual(self.award(outsider), (None, None))
        self.assertEqual((script.frame, script.component.now, self.killer.commands.last_frame), before)

    def test_ordinary_death_does_not_send_the_visor_award(self):
        self.eliminate(visor=False)
        self.update_without_commands()
        self.assertEqual(self.award(self.receiver(self.killer)), (None, None))

    def test_earlier_delivery_can_activate_a_later_messages_listener(self):
        first, second = 0x024000000000F101, 0x024000000000F102

        def message(guid):
            return {"$": "STUConfigVarGameMessage", "m_gameMessage": f"0x{guid:016X}"}

        definition = make_graph(
            0xF101,
            [
                node(0, "STUStatescriptEntryGameMessage",
                     fields={"m_gameMessage": message(first), "m_outPlug": output([1, "m_beginPlug"])}),
                node(1, "STUStatescriptStateGameMessageEntry", state=0,
                     fields={"m_gameMessage": message(second)}),
            ],
            states=(1,), entries=(0,),
        )
        instance = self.killer.body_script.component.create(definition)
        listener = instance.state(0)
        world = self.victim.body_script.world
        world.send_message(self.killer.body, first, {}, self.victim.body)
        world.send_message(self.killer.body, second, {}, self.victim.body)
        self.assertFalse(listener.active)

        self.run.match.deliver_body_messages()
        self.assertTrue(listener.received)

    def test_queued_award_does_not_cross_the_killers_body_replacement(self):
        old_body, old_script = self.killer.body, self.killer.body_script
        old_receiver = self.receiver(self.killer)
        self.eliminate(visor=True)
        self.run.match.switch_hero(self.killer, self.killer.hero)
        self.assertNotEqual(self.killer.body, old_body)
        self.assertTrue(old_script.retired)
        self.update_without_commands()
        self.assertEqual(self.award(old_receiver), (None, None))
        self.assertEqual(self.award(self.receiver(self.killer)), (None, None))

    def test_queued_award_is_dropped_when_the_sending_body_retires(self):
        receiver = self.receiver(self.killer)
        self.eliminate(visor=True)
        self.run.match.leave(self.victim)
        self.update_without_commands()
        self.assertEqual(self.award(receiver), (None, None))

    def test_queued_award_is_dropped_on_leave_even_with_the_same_body_id_in_another_match(self):
        other = Run(names=("Other match",))
        other.step({})
        (unrelated,) = other.players
        self.assertEqual(unrelated.body, self.killer.body)
        unrelated_receiver = self.receiver(unrelated)
        departed_receiver = self.receiver(self.killer)
        match = self.run.match
        with patch.object(match, "queue_body_message", wraps=match.queue_body_message) as enqueue:
            self.eliminate(visor=True)
        enqueue.assert_any_call(
            self.killer.body, 0x024000000000064A,
            {0x2A: 50.0, 0x36A: expr.Asset(0x0DE000000000405F), 0x35F: None}, self.victim.body,
        )
        match.leave(self.killer)
        self.update_without_commands()
        other.match.update(self.run.frame * 0.016, self.run.frame)
        self.assertEqual(self.award(departed_receiver), (None, None))
        self.assertEqual(self.award(unrelated_receiver), (None, None))
