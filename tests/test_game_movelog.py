"""The movement log (ow174/game/movelog.py): off and free by default; on, the commands, snapshots and the
owner's records of a player, line by line."""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import correction, movelog
from ow174.game.commands import Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.match import Match
from ow174.game.server import Client, GameServer
from ow174.game.world import OP_CREATE, EntityUpdate


class LogTests(unittest.TestCase):
    """A Soldier: 76 in the Practice Range whose client has his body."""

    def setUp(self):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        flat = mock.patch("ow174.game.match.world_when_ready", return_value=None)  # a flat floor
        flat.start()
        self.addCleanup(flat.stop)
        self.folder = Path(tempfile.mkdtemp())
        self.match = Match(PRACTICE_RANGE)
        self.match.tick = 6999
        self.player = self.match.add_player(1, "Alpha One", SOLDIER, 0, False)
        self.player.has_body = True
        self.match.start_body_script(self.player, EntityUpdate(self.player.body, OP_CREATE))
        self.player.body_script.create_arrived = True

    def enable(self):
        for name, value in (("ENABLED", True), ("FOLDER", self.folder)):
            patch = mock.patch.object(movelog, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def lines(self) -> list[dict]:
        (path,) = self.folder.glob("*.jsonl")
        self.assertIn("Alpha_One", path.name)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_off_writes_nothing(self):
        with mock.patch.object(movelog, "FOLDER", self.folder / "movelog"):
            self.player.take_commands([Command(7000 + n, forward=127) for n in range(5)])
            self.match.tick = 7004
            self.match.remote_movements(self.player)
            self.assertFalse((self.folder / "movelog").exists())

    def test_commands_snapshots_and_records(self):
        self.enable()
        commands = [Command(7000 + n, forward=127, yaw=100 * n) for n in range(5)]
        commands += [Command(7007 + n, right=-127, buttons=0x40 if n == 1 else 0) for n in range(3)]
        self.player.take_commands(commands[:5])
        self.player.take_commands(commands[3:])  # overlapping windows: each frame once
        self.match.tick = 7005
        self.match.remote_movements(self.player)
        lines = self.lines()
        kinds = [line["k"] for line in lines]
        self.assertEqual(kinds[:2], ["begin", "body"])
        self.assertEqual(lines[0]["hero"], f"{SOLDIER:016X}")
        self.assertEqual(lines[0]["quanta"], 16000)
        cmds = [line for line in lines if line["k"] == "cmd"]
        self.assertEqual([line["f"] for line in cmds], [command.frame for command in commands])
        self.assertEqual((cmds[5]["right"], cmds[6]["btn"], cmds[4]["yaw"]), (-127, 0x40, 400))
        ins = [(line["n"], line["first"], line["newest"]) for line in lines if line["k"] == "in"]
        self.assertEqual(ins, [(5, 7000, 7004), (3, 7007, 7009)])
        snaps = [line for line in lines if line["k"] == "snap"]
        self.assertEqual([line["frame"] for line in snaps], list(range(7000, 7010)))  # 7005, 7006 filled
        history = {snapshot.frame: snapshot for snapshot in self.player.mover.history}
        for line in snaps:
            snapshot = history[line["frame"]]
            self.assertEqual(line["position"], list(snapshot.position))
            self.assertEqual(line["flags"], snapshot.flags)
            self.assertEqual(line["spring"], list(snapshot.spring))
        (sent,) = [line for line in lines if line["k"] == "sent"]
        self.assertEqual((sent["f"], sent["tick"], sent["count"]), (7005, 7005, 0))
        self.assertEqual(sent["ack"], 7007)  # two past the record, not the newest 7009

    @unittest.skipUnless(os.name == "nt", "the size in a folder's entry is NTFS's")
    def test_a_folder_listing_shows_the_open_log_with_its_lines(self):
        # Flushed lines alone leave the folder's entry at 0 bytes while the server keeps the log open.
        self.enable()
        with mock.patch.object(movelog, "SHOW_EVERY", 0.0):
            self.player.take_commands([Command(7000 + n, forward=127) for n in range(5)])
        with os.scandir(self.folder) as entries:
            (entry,) = list(entries)
        listed = entry.stat().st_size  # the folder's entry (FindFirstFileW), the file is not opened
        self.assertGreater(listed, 0)
        self.assertEqual(listed, os.path.getsize(entry.path))

    def test_a_dropped_game_closes_its_log(self):
        self.enable()
        self.player.take_commands([Command(7000 + n, forward=127) for n in range(3)])
        log = movelog.log_of(self.player)
        server = GameServer()
        client = Client(server, ("127.0.0.1", 50000), 1, None, self.player)
        server.clients[client.conn] = client  # by connection id, as GameServer._datagram keeps them
        self.player.client = client
        server.drop(client)
        self.assertEqual(server.clients, {})
        self.assertTrue(log.file.closed)
        self.assertIsNot(movelog.log_of(self.player), log)  # a game that comes back writes a new one

    def test_a_new_body_starts_a_new_run_of_frames(self):
        self.enable()
        self.player.take_commands([Command(7000 + n, forward=127) for n in range(3)])
        self.player.new_body()
        self.player.take_commands([Command(7003 + n, forward=127) for n in range(2)])
        lines = self.lines()
        bodies = [line for line in lines if line["k"] == "body"]
        self.assertEqual([line["body"] for line in bodies], [self.player.body - 1, self.player.body])
        snaps = [line["frame"] for line in lines if line["k"] == "snap"]
        self.assertEqual(snaps, [7000, 7001, 7002, 7003, 7004])


class AckTests(unittest.TestCase):
    """The command ack stays within reach of the newest record of the owner's body."""

    def setUp(self):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        flat = mock.patch("ow174.game.match.world_when_ready", return_value=None)
        flat.start()
        self.addCleanup(flat.stop)
        self.match = Match(PRACTICE_RANGE)
        self.match.tick = 6999
        self.player = self.match.add_player(1, "Alpha", SOLDIER, 0, False)
        self.player.has_body = True
        self.match.start_body_script(self.player, EntityUpdate(self.player.body, OP_CREATE))
        self.player.body_script.create_arrived = True

    def test_no_commands_yet_is_the_tick(self):
        self.assertEqual(correction.command_ack(self.player, 7000), 7000)

    def test_before_the_first_record_the_newest_command(self):
        self.player.take_commands([Command(7000 + n) for n in range(10)])
        self.assertEqual(correction.command_ack(self.player, 7003), 7009)

    def test_at_most_two_past_the_newest_record(self):
        self.player.take_commands([Command(7000 + n) for n in range(10)])
        self.match.tick = 7003
        self.match.remote_movements(self.player)
        self.assertEqual(correction.command_ack(self.player, 7003), 7005)
        self.match.tick = 7020
        self.match.remote_movements(self.player)  # the newest it has: 7009
        self.assertEqual(correction.command_ack(self.player, 7020), 7009)

    def test_no_limit_without_records(self):
        self.player.take_commands([Command(7000 + n) for n in range(10)])
        self.match.tick = 7003
        self.match.remote_movements(self.player)
        with mock.patch.object(correction, "CORRECTIONS", False):
            self.assertEqual(correction.command_ack(self.player, 7003), 7009)
        self.player.body_script.create_arrived = False
        self.assertEqual(correction.command_ack(self.player, 7003), 7009)


if __name__ == "__main__":
    unittest.main()
