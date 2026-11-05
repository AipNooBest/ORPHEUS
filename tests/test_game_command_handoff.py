"""Command-clock boundary handling through the encrypted game link."""

import unittest
from unittest.mock import patch

from test_game_repeated_lifecycles import REAPER, ProtocolHarness
from test_server_journal import pick

from ow174.game import bodytrace, messages, movelog
from ow174.game.bits import BitReader, BitWriter
from ow174.game.commands import FIRE, FRAME_HALF
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.link import ACK, FIN, KIND_FRAME
from ow174.game.server import LEAVE_GAME

LAST_VALID = 0xFF7FFFFF
FIRST_UNSAFE = LAST_VALID + 1


def input_window(first, count, *, buttons=0, forward=0):
    """One complete command followed by consecutive identical compressed commands."""
    body = BitWriter()
    body.bits(0, 24)  # latency and key flags
    body.bits(count, 8)
    body.bits(first, 32)
    body.bit(bool(forward))
    if forward:
        body.bits(0b100, 3)  # arbitrary throttles, forward follows
        body.signed(forward, 8)
    body.bits(0, 32)  # yaw and pitch
    body.bits(buttons, 24)
    body.bits(0, 24)  # action and extra
    for _ in range(count - 1):
        body.bits(0, 2)  # next frame, same command
    payload = BitWriter()
    messages.write_channel(payload, [])
    payload.bit(1)
    payload.bits(body.count, 32)
    payload.append(body)
    return payload.getvalue()


def leave_messages(link):
    """Datagram sequence, reliable ordinal and decoded value for each leave attempt."""
    result = []
    for packet in link.got:
        if packet.kind != KIND_FRAME:
            continue
        reader = BitReader(packet.payload)
        reader.var_a()
        reliable, _ = messages.read_channel(reader)
        result.extend((packet.seq, ordinal, value) for ordinal, kind, value in reliable if kind == LEAVE_GAME)
    return result


class CommandHandoffTests(unittest.TestCase):
    def setUp(self):
        self.run = ProtocolHarness()
        self.addCleanup(self.run.close)
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)

    def soldier(self, anchor=None):
        (handoff,) = self.run.server.create_match(PRACTICE_RANGE, [(1, "Clock", 0, 0, False)])
        link, player = self.run.connect(handoff)
        if anchor is not None:
            player.commands.reset(last_frame=anchor)
        self.run.message(link, 21616, pick(player.entity, SOLDIER))
        script = player.body_script
        self.assertIsNotNone(script)
        self.run.until(lambda: script.stream.base is not None and player.body in link.models)
        self.assertEqual(link.models[player.body].vars[(11, 53)], 30)
        return handoff, link, player

    @staticmethod
    def ammo(player):
        return player.body_script.component.instances[11].vars[53].value()

    @staticmethod
    def motion(player):
        return (
            player.commands.last_frame, player.mover.last_command, tuple(player.mover.position),
            tuple(sample.frame for sample in player.mover.history), player.body_script.frame,
        )

    def test_valid_prefix_fires_then_reliable_leave_and_fresh_handoff(self):
        handoff, link, player = self.soldier(LAST_VALID - 1)
        script, client = player.body_script, player.client
        combat = player.match.combat
        with patch.object(combat, "_shoot", wraps=combat._shoot) as shoot:
            self.run.send(link, payload=input_window(LAST_VALID, 3, buttons=FIRE, forward=127))
        shoot.assert_called_once()
        _, command, state, _, shot_time = shoot.call_args.args
        self.assertEqual((command.frame, shot_time // state.instance.component.frame_ms),
                         (LAST_VALID, LAST_VALID))
        self.assertEqual((player.commands.last_frame, player.mover.last_command.frame, script.frame),
                         (LAST_VALID, LAST_VALID, LAST_VALID))
        self.assertEqual(self.ammo(player), 29)
        self.assertEqual(client.leaving_since, self.run.now)
        self.assertTrue(self.run.server.going_home(player))

        body, before = player.body, self.motion(player)
        self.run.message(link, 21616, pick(player.entity, REAPER), ack=(0, 0))
        self.run.send(link, payload=input_window(FIRST_UNSAFE, 2), ack=(0, 0))
        self.assertEqual((player.body, player.hero.guid, self.motion(player)), (body, SOLDIER, before))
        self.assertIs(player.body_script, script)

        self.run.until(lambda: bool(leave_messages(link)), ack_except=(link,))
        (first,) = leave_messages(link)
        self.run.send(link, ack=(first[0] + 1, 0))
        self.run.until(lambda: len(leave_messages(link)) == 2, ack_except=(link,))
        _, resent = leave_messages(link)
        self.assertGreater(resent[0], first[0])
        self.assertEqual(resent[1:], first[1:])
        self.assertEqual(resent[2], {"+0x78": 0, "+0x79": False})

        self.run.send(link)
        self.run.send(link, FIN)
        self.assertEqual(link.got[-1].flags & (ACK | FIN), ACK | FIN)
        self.assertTrue(player.gone and script.retired and client.closed)
        self.assertEqual(self.run.left.count(player), 1)
        self.assertNotIn(link.conn, self.run.server.clients)

        fresh, new_link, new_player = self.soldier()
        self.assertNotEqual(fresh.conn, handoff.conn)
        self.assertNotEqual((fresh.key_in, fresh.key_out), (handoff.key_in, handoff.key_out))
        self.assertEqual(new_player.client.address, client.address)
        before = self.motion(new_player)
        self.run.send(link, payload=input_window(FIRST_UNSAFE, 1, buttons=FIRE))
        self.run.message(link, 21616, pick(player.entity, REAPER))
        self.run.send(link, FIN)
        self.assertEqual(self.motion(new_player), before)
        self.assertEqual(new_player.hero.guid, SOLDIER)
        self.assertIsNone(new_player.client.leaving_since)
        self.run.command(new_link, buttons=FIRE)
        self.assertEqual(self.ammo(new_player), 29)

    def test_unsafe_first_command_preserves_state_and_blocks_later_valid_input(self):
        _, link, player = self.soldier(LAST_VALID - 3)
        self.run.send(link, payload=input_window(LAST_VALID - 2, 1, forward=127))
        script = player.body_script
        before = self.motion(player), script.snapshot()
        self.run.send(link, payload=input_window(FIRST_UNSAFE, 2, buttons=FIRE, forward=127))
        self.assertIsNotNone(player.client.leaving_since)
        self.assertIs(player.body_script, script)
        self.assertEqual((self.motion(player), script.snapshot()), before)
        self.run.send(link, payload=input_window(LAST_VALID - 1, 1, buttons=FIRE))
        self.assertEqual((self.motion(player), script.snapshot()), before)

    def test_first_stale_high_and_half_range_frames_use_existing_body_anchor(self):
        _, link, player = self.soldier()
        self.assertIsNone(player.commands.last_frame)
        anchor = player.body_script.frame
        before = self.motion(player)
        self.run.send(link, payload=input_window(FIRST_UNSAFE, 2, buttons=FIRE))
        self.run.send(link, payload=input_window(anchor + FRAME_HALF, 1, buttons=FIRE))
        self.assertEqual(self.motion(player), before)
        self.assertEqual(self.ammo(player), 30)
        self.assertIsNone(player.client.leaving_since)
        self.run.command(link, buttons=FIRE)
        self.assertEqual(player.commands.last_frame, anchor + 1)
        self.assertEqual(self.ammo(player), 29)
