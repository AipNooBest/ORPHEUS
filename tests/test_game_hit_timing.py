"""Soldier hitscan timing through the native body driver and player command queue."""

import math
import unittest
from collections import deque
from unittest.mock import patch

from test_game_combat import Run, aim

from ow174.game import bodytrace, combat, movelog
from ow174.game.commands import FIRE, SUBFRAME, Command


class HitTimingTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            setting = patch.object(module, "ENABLED", False)
            setting.start()
            self.addCleanup(setting.stop)
        scenario = Run()
        self.player = scenario.players[0]
        self.fight = scenario.match.combat
        origin = self.player.mover.position
        self.bot = min(scenario.match.bots, key=lambda bot: math.dist(bot.position, origin))
        x, y, z = self.bot.position
        self.aim_point = (x, y + 1.0, z)

    def fire(self, frame):
        yaw, pitch = aim(self.fight._eye(self.player), self.aim_point)
        return Command(frame, yaw=yaw, pitch=pitch, buttons=FIRE)

    def hits(self):
        return [
            (hit["+0x78"]["+0x0"]["+0x0"], hit["+0x84"])
            for hit in self.player.client.messages(combat.HIT)
        ]

    def test_first_shot_uses_click_aim_and_subframe_view(self):
        target = next(target for target in self.fight._targets() if target.entity == self.bot.entity)
        x, y, z = target.feet
        ox, _, oz = self.fight._eye(self.player)
        dx, dz = x - ox, z - oz
        scale = 20.0 / math.hypot(dx, dz)
        # At frame 1000 the bot is on the click ray; by 1001 it is 20 m across that ray.
        self.fight.history[target.entity] = deque([
            (1000, target.feet, target.height, target.eye),
            (1001, (x + dz * scale, y, z - dx * scale), target.height, target.eye),
        ])
        click_yaw, click_pitch = aim(self.fight._eye(self.player), self.aim_point)
        away_yaw = click_yaw - 32768 if click_yaw >= 0 else click_yaw + 32768
        # Eight ms of view delay plus the midpoint click's eight ms rewind picks frame 1000.
        command = Command(
            1001, yaw=away_yaw, buttons=FIRE | SUBFRAME, extra=8,
            subframe=128, click_yaw=click_yaw, click_pitch=click_pitch,
        )
        self.player.take_commands([command])
        self.assertEqual(self.hits(), [(target.entity, 1001)])

    def test_catch_up_hits_keep_scheduled_frames_and_late_input_cannot_repeat_them(self):
        self.player.take_commands([self.fire(frame) for frame in (1001, 1002, 1040)])
        # F1035 is the first neutral fallback frame. The round due later in
        # that frame is canceled and the rifle goes through its restart delay.
        expected = [(self.bot.entity, frame) for frame in (1001, 1007, 1014, 1021, 1028)]
        self.assertEqual(self.hits(), expected)
        health = self.fight.vitals[self.bot.entity].total

        self.player.take_commands([Command(1014), self.fire(1040), self.fire(1001)])
        self.assertEqual(self.hits(), expected)
        self.assertEqual(self.fight.vitals[self.bot.entity].total, health)

        self.player.take_commands([self.fire(1042)])
        self.assertEqual(self.hits(), expected)
        self.player.take_commands([self.fire(1045)])
        self.assertEqual(self.hits(), [*expected, (self.bot.entity, 1045)])
