"""Explicit 21616 reselect after a terminal body script fault."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_combat import Run
from test_game_entity_lifecycle import client, sent
from test_server_journal import pick

from ow174.game import bodytrace, match, messages, movelog
from ow174.game.bits import BitWriter
from ow174.game.commands import FIRE, FRAME_MASK, Command
from ow174.game.script import runtime
from ow174.game.script.decode import read_chunk
from ow174.game.world import OP_CREATE, OP_DESTROY


def attach_client(player):
    connection = client()
    connection.player = player
    player.client = connection
    return connection


def reselect(connection, sequence=0):
    player = connection.player
    payload = BitWriter()
    message = messages.encode(21616, pick(player.entity, player.hero.guid))
    messages.write_channel(payload, [(sequence, message)])
    connection._payload_in(payload.getvalue(), 17.0)


class BodyRecoveryTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            recording = patch.object(module, "ENABLED", False)
            recording.start()
            self.addCleanup(recording.stop)

    def test_same_hero_pick_after_fault_replaces_body_and_waits_for_fresh_acks(self):
        run = Run()
        player = run.players[0]
        connection = attach_client(player)
        old_body, old_script, old_mover = player.body, player.body_script, player.mover
        commands, controller = player.commands, player.script
        (old_full,) = old_script.frames()
        connection.queue_entities([old_full])
        sent(connection, 0, [old_full])

        with patch.object(runtime, "MAX_SCRIPT_WORK", 1), self.assertLogs("ow174.game", level="ERROR"):
            player.take_commands([Command(1001, buttons=FIRE)])
        self.assertIsInstance(old_script.component.fault, runtime.ScriptBudgetError)
        self.assertTrue(old_script.retired)
        self.assertEqual(old_script.component.shots, [])
        self.assertIsNone(player.body_script)
        self.assertTrue(player.body_script_failed)

        reselect(connection)
        self.assertEqual(connection.expected_in, 1)
        script = player.body_script
        self.assertIsNotNone(script)
        self.assertIsNot(script, old_script)
        self.assertNotEqual(player.body, old_body)
        self.assertIsNot(player.mover, old_mover)
        self.assertIs(player.commands, commands)
        self.assertIs(player.script, controller)
        self.assertEqual((commands.last_frame, commands.epoch, script.frame), (1001, 0, 1001))
        self.assertFalse(player.body_script_failed)
        queued = [(item.entity, item.op) for item in connection.entities]
        self.assertIn((old_body, OP_DESTROY), queued)

        connection.acked(1, 1)  # A delayed positive ACK for the old full frame.
        self.assertIsNone(old_script.stream.base)
        self.assertFalse(script.create_arrived)
        self.assertEqual(script.frames(), [])
        (create,) = [
            item for item in connection.entities if item.entity == player.body and item.op == OP_CREATE
        ]
        sent(connection, 1, [create])
        connection.acked(2, 1)
        self.assertTrue(script.create_arrived)
        (full,) = script.frames()
        tree = read_chunk(full.chunk.getvalue(), full.chunk.count)
        connection.queue_entities([full])
        sent(connection, 2, [full])

        player.take_commands([Command(1002, buttons=FIRE)])
        self.assertEqual(script.frames(), [])  # Still waiting for this body's full frame ACK.
        connection.acked(3, 1)
        (delta,) = script.frames()
        known = {number: item.graph for number, item in tree.instances.items()}
        owner = read_chunk(delta.chunk.getvalue(), delta.chunk.count, known)
        self.assertTrue(owner.owner)
        self.assertFalse(owner.correction)
        self.assertEqual(owner.cmfd, 1002)
        self.assertEqual(script.component.instances[11].vars[53].value(), 29)

    def test_healthy_and_script_disabled_same_hero_picks_keep_the_body(self):
        for allowed in (None, set()):
            with self.subTest(allowed=allowed), patch.object(match, "BODY_SCRIPT_HEROES", allowed):
                run = Run()
                player = run.players[0]
                connection = attach_client(player)
                body, mover, script = player.body, player.mover, player.body_script
                self.assertEqual(script is not None, allowed is None)
                player.take_commands([Command(1001, buttons=FIRE)])
                if script is not None:
                    self.assertEqual(script.component.instances[11].vars[53].value(), 29)

                reselect(connection)
                self.assertEqual(connection.expected_in, 1)
                self.assertEqual(player.body, body)
                self.assertIs(player.mover, mover)
                self.assertIs(player.body_script, script)
                self.assertFalse(player.body_script_failed)
                self.assertFalse(any(item.op in (OP_CREATE, OP_DESTROY) for item in connection.entities))
                if script is not None:
                    self.assertEqual(script.component.instances[11].vars[53].value(), 29)

    def test_budget_fault_during_rebuild_leaves_later_explicit_reselect_available(self):
        run = Run()
        player = run.players[0]
        connection = attach_client(player)
        initial_body = player.body
        with patch.object(runtime, "MAX_SCRIPT_WORK", 1), self.assertLogs("ow174.game", level="ERROR"):
            player.take_commands([Command(1001, buttons=FIRE)])
            reselect(connection)
        failed_body = player.body
        self.assertNotEqual(failed_body, initial_body)
        self.assertIsNone(player.body_script)
        self.assertTrue(player.body_script_failed)

        reselect(connection, sequence=1)
        self.assertNotEqual(player.body, failed_body)
        self.assertIsNotNone(player.body_script)
        self.assertFalse(player.body_script_failed)

    def test_command_epoch_boundary_does_not_enable_fault_reselect(self):
        run = Run()
        player = run.players[0]
        player.commands.reset(last_frame=FRAME_MASK)
        run.match.switch_hero(player, player.hero)
        connection = attach_client(player)
        body, script = player.body, player.body_script
        with self.assertLogs("ow174.game", level="ERROR"):
            run.match.body_command(player, Command(0))
        self.assertTrue(script.retired)
        self.assertIsNone(player.body_script)
        self.assertFalse(player.body_script_failed)

        reselect(connection)
        self.assertEqual(player.body, body)
        self.assertIsNone(player.body_script)


if __name__ == "__main__":
    unittest.main()
