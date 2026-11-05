"""The training bots (ow174/game/bots.py) and their place in the match. The walk's numbers are the ones
measured on the retail OW2 Practice Range."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_world import read_record

from ow174.game import bots, world
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.match import BOT_RECORD_DELAY, Match, _rotation
from ow174.game.world import NO_INPUT, OP_CREATE

TRAINING, FRIENDLY = "Training Bot", "Friendly Bot"
TICK = 0.016
DEGREE = bots.YAW_UNITS / 360
HEROES = {  # hero -> (name, body)
    "0x02E000000000016B": (TRAINING, "0x0400000000001403"),
    "0x02E000000000016E": (TRAINING, "0x04000000000014ED"),  # a far runner
    "0x02E000000000016C": (FRIENDLY, "0x040000000000143E"),
}


def make_bot(points=(), index=2, hero="0x02E000000000016B", yaw=0):
    """A bot at points[0] (or the origin), facing `yaw`, that walks points when there are any."""
    names = [f"p{n}" for n in range(len(points))]
    spawner = {
        "hero": hero,
        "team": 1,
        "identifier": None,
        "position": list(points[0]) if points else [0.0, 0.0, 0.0],
        "rotation": list(_rotation(yaw)),
        "graphs": [f"0x{bots.PATROL:016X}"] if points else [],
        "vars": {bots.ROUTE: names, bots.INDEX: index} if points else {},
    }
    name, body = HEROES[hero]
    return bots.Bot(
        bots.BOT_ENTITY,
        spawner,
        {"name": name, "body": body, "health": 200.0},
        dict(zip(names, points, strict=True)),
    )


class Walker:
    """Steps a bot tick by tick and keeps what each tick left."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.tick = 0
        self.log = {}

    def run(self, seconds: float, collision=None) -> None:
        for _ in range(round(seconds / TICK)):
            self.tick += 1
            self.bot.step(self.tick, TICK, collision)
            bot = self.bot
            self.log[self.tick] = (bot.position, bot.yaw, bot.held, bot.pace)

    def until(self, test, limit=3000) -> int:
        for _ in range(limit):
            self.run(TICK)
            if test(self.bot):
                return self.tick
        raise AssertionError("never happened")

    def keys(self) -> list[int]:
        return [tick for tick, entry in self.log.items() if entry[2] != (0, 0)]

    def walks(self) -> list[tuple[int, int]]:
        """(first, last) tick of each run of ticks with keys held."""
        runs = []
        for tick in self.keys():
            if runs and tick == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], tick)
            else:
                runs.append((tick, tick))
        return runs


def behind(length=20.0):
    """A bot facing +z whose first point is `length` behind it."""
    return Walker(make_bot([(0.0, 0.0, 0.0), (0.0, 0.0, -length)], index=0))


def speed_over(walker, last, ticks=3):
    """The ground speed over the `ticks` up to `last`, as a retail record of that many frames gave it."""
    (x1, _, z1), (x0, _, z0) = walker.log[last][0], walker.log[last - ticks][0]
    return math.hypot(x1 - x0, z1 - z0) / (ticks * TICK)


class DataTests(unittest.TestCase):
    def test_the_practice_range_has_its_fifteen_bots(self):
        made = bots.make(PRACTICE_RANGE.map_guid)
        self.assertEqual(len(made), 15)
        self.assertEqual(sum(1 for bot in made if (bot.name, bot.team) == (TRAINING, 1)), 11)
        self.assertEqual(sum(1 for bot in made if (bot.name, bot.team) == (FRIENDLY, 0)), 4)
        self.assertEqual({bot.health for bot in made if bot.name == TRAINING}, {200.0})
        self.assertEqual({bot.health for bot in made if bot.name == FRIENDLY}, {225.0})
        self.assertEqual([bot.entity for bot in made], [bots.BOT_ENTITY + n for n in range(15)])
        self.assertEqual(sum(1 for bot in made if bot.route), 6)

    def test_a_map_without_spawners_has_no_bots(self):
        self.assertEqual(bots.make(0x080000000000005B), [])

    def test_its_walking_bots_walk_with_their_bodies_values(self):
        """Retail's walking Training Bots: 4.5 m/s, 4.46 m/s^2 up (fit), 67.5 m/s^2 down (body 1403); the
        far runners with their own 1.74 body 14ED: 6 m/s, 66 m/s^2 up, 39 m/s^2 down."""
        walkers = [bot for bot in bots.make(PRACTICE_RANGE.map_guid) if bot.route]
        far = [bot for bot in walkers if bot.hero == bots.FAR_RUNNER]
        self.assertEqual(len(far), 4)
        for bot in far:
            self.assertEqual((bot.speed, bot.accel, bot.brake), (6.0, 66.0, 39.0))
        for bot in walkers:
            if bot.name == TRAINING and bot.hero != bots.FAR_RUNNER:
                self.assertEqual((bot.speed, bot.accel, bot.brake), (4.5, 4.5, 67.5))

    def test_the_friendly_bot_walks_with_its_own_body(self):
        bot = make_bot([(0.0, 0.0, 0.0), (0.0, 0.0, 10.0)], hero="0x02E000000000016C")
        self.assertEqual((bot.speed, bot.accel, bot.brake), (5.5, 5.5, 82.5))


class AngleTests(unittest.TestCase):
    def test_a_yaw_comes_back_from_its_rotation(self):
        for units in (0, 1000, 16384, -16384, 32767, -32768):
            self.assertEqual(bots.yaw_of(_rotation(units)), units)

    def test_the_heading_faces_the_walk(self):
        self.assertEqual(bots.heading(0.0, 1.0), 0)
        self.assertEqual(bots.heading(1.0, 0.0), 16384)
        self.assertEqual(bots.heading(-1.0, 0.0), -16384)
        self.assertEqual(bots.heading(0.0, -1.0), -32768)

    def test_throttles_are_seen_from_the_yaw(self):
        self.assertEqual(bots.throttles(0, (0.0, 1.0)), (0, bots.FORWARD))
        self.assertEqual(bots.throttles(0, (1.0, 0.0)), (-bots.FORWARD, 0))  # right is (-cos, sin)
        self.assertEqual(bots.throttles(16384, (1.0, 0.0)), (0, bots.FORWARD))


class WalkTests(unittest.TestCase):
    """A Training Bot (body 1403) on a 20 m leg behind it, as retail's far bots walked theirs."""

    def test_it_waits_three_seconds_after_it_spawns(self):
        walker = behind()
        walker.run(2.95)
        self.assertFalse(walker.bot.walking())
        self.assertEqual(walker.bot.position, (0.0, 0.0, 0.0))
        walker.run(0.1)
        self.assertTrue(walker.bot.walking())

    def test_it_walks_at_once_backward_while_it_turns(self):
        walker = behind()
        start = walker.until(lambda bot: bot.held != (0, 0))
        self.assertEqual(walker.bot.held, (2, -bots.FORWARD))  # backward, already a tick into its turn
        self.assertLess(walker.bot.position[2], 0.0)
        walker.run(1.0)
        self.assertAlmostEqual(walker.bot.yaw, -(63 * bots.TURN_RATE * TICK), places=6)
        walker.run(2.0)  # 180 degrees take 3 s: it faces the way it walks
        self.assertEqual(walker.bot.yaw, -32768)
        self.assertEqual(walker.bot.held, (0, bots.FORWARD))
        self.assertTrue(all(walker.log[tick][2] != (0, 0) for tick in range(start, walker.tick + 1)))

    def test_it_turns_sixty_degrees_a_second(self):
        walker = behind()
        start = walker.until(lambda bot: bot.held != (0, 0))
        walker.run(1.0)
        turned = -(walker.log[start + 62][1] - walker.log[start - 1][1]) / DEGREE
        self.assertAlmostEqual(turned, 63 * 60 * TICK, places=6)  # 174.76 units a tick

    def test_it_speeds_up_in_a_second_with_full_throttle(self):
        walker = behind()
        start = walker.until(lambda bot: bot.held != (0, 0))
        walker.run(1.2)
        paces = [walker.log[tick][3] for tick in range(start, start + 70)]
        self.assertAlmostEqual(paces[0], 4.5 * TICK)
        self.assertAlmostEqual(paces[30], 4.5 * 31 * TICK)
        self.assertEqual(paces[62:], [4.5] * 8)
        for tick in range(start, start + 70):  # retail sent 126-127 from the first tick on
            self.assertGreaterEqual(math.hypot(*walker.log[tick][2]), 126)

    def test_it_lets_go_and_slides_onto_its_point(self):
        """Retail's records over the last tick with keys and after: 4.13 and 1.26 m/s, then 0."""
        walker = behind()
        walker.run(12.0)
        last = walker.walks()[0][1]
        # 4.03 here: its last stride with keys is a little short, to slide onto the point
        self.assertAlmostEqual(speed_over(walker, last + 1), 4.13, delta=0.1)
        self.assertAlmostEqual(speed_over(walker, last + 4), 1.26, places=2)
        self.assertEqual(speed_over(walker, last + 7), 0.0)
        paces = [speed_over(walker, last + k, 1) for k in range(1, 6)]  # friction 67.5 m/s^2
        self.assertEqual([round(pace, 2) for pace in paces], [3.42, 2.34, 1.26, 0.18, 0.0])
        self.assertEqual(walker.log[last + 5][0], (0.0, 0.0, -20.0))
        self.assertTrue(all(entry[0][2] >= -20.0 for entry in walker.log.values()))

    def test_the_next_walk_starts_75_ticks_after_it_let_go(self):
        walker = behind()
        walker.run(12.0)
        (_, last), (first, _) = walker.walks()
        self.assertEqual(first - last, 75)

    def test_a_20_metre_leg_takes_as_long_as_retail(self):
        walker = behind()
        walker.run(12.0)
        first, last = walker.walks()[0]
        self.assertAlmostEqual((last - first + 1) * TICK, 4.9, delta=0.1)  # retail: 4.78-4.93 s for 19.4 m

    def test_on_a_short_leg_it_turns_on_half_a_second_after_it_let_go(self):
        """Retail's 6 m legs: about 110 degrees of the turn while walking, about 25 more after it let
        go, the rest on its next walk."""
        walker = behind(6.0)
        walker.run(7.0)
        last = walker.walks()[0][1]
        at_release = -walker.log[last][1] / DEGREE
        self.assertAlmostEqual(at_release, 108.5, delta=1.0)
        yaws = [walker.log[last + k][1] for k in range(0, 60)]
        turning = [k for k in range(1, 60) if yaws[k] != yaws[k - 1]]
        self.assertEqual(turning[-1], 32)  # 0.5 s
        self.assertAlmostEqual(-yaws[-1] / DEGREE - at_release, 32 * 60 * TICK, places=6)

    def test_a_point_it_stands_on_needs_no_walk(self):
        walker = Walker(make_bot([(0.0, 0.0, 0.0), (0.0, 0.0, 10.0)], index=1))  # first point: its own
        walker.run(3.1)
        self.assertFalse(walker.bot.walking())
        self.assertEqual(walker.keys(), [])
        walker.until(lambda bot: bot.held != (0, 0))
        self.assertAlmostEqual(walker.tick * TICK, 3.008 + bots.WALK_GAP, delta=TICK)


class StateTests(unittest.TestCase):
    def test_its_state_holds_the_keys_then_names_the_last_tick_with_them(self):
        walker = behind()
        walker.until(lambda bot: bot.held != (0, 0))
        walker.run(0.5)
        state = walker.bot.movement(walker.tick)
        self.assertIsNone(state.input_frame)
        self.assertGreaterEqual(math.hypot(*state.throttles), 126)
        walker.until(lambda bot: not bot.walking())
        bot = walker.bot
        self.assertEqual(bot.input_tick, walker.walks()[0][1])
        state = bot.movement(walker.tick)
        self.assertEqual(state.throttles, (0, 0))
        self.assertEqual(state.input_frame, bot.input_tick)
        self.assertEqual(state.input_throttles, bot.last_held)
        self.assertEqual(state.input_throttles, (0, bots.FORWARD))
        self.assertEqual(state.velocity, (0.0, 0.0, 0.0))

    def test_no_state_names_its_own_frame_as_the_last_input(self):
        """+36 equal to the record's frame must go as None (one bit), or +34/+35 would be written where
        the client does not read them."""
        walker = Walker(make_bot([(0.0, 0.0, 0.0), (0.0, 0.0, -10.0), (0.0, 0.0, 10.0), (3.0, 0.0, 9.0)]))
        for _ in range(2500):  # walks, slides, turns on, waits
            walker.run(TICK)
            state = walker.bot.movement(walker.tick)
            self.assertNotEqual(state.input_frame, walker.tick)

    def test_its_state_carries_its_yaw_in_whole_units(self):
        walker = behind()
        walker.run(4.0)
        state = walker.bot.movement(walker.tick)
        self.assertEqual(state.yaw, bots.s16(round(walker.bot.yaw)))
        self.assertIsInstance(state.yaw, int)

    def test_a_bot_that_never_walked_stands(self):
        bot = make_bot()
        Walker(bot).run(10.0)
        self.assertEqual(bot.position, (0.0, 0.0, 0.0))
        self.assertEqual(bot.movement(500).input_frame, NO_INPUT)

    def test_a_late_tick_walks_the_time_it_missed(self):
        bot = make_bot([(0.0, 0.0, 0.0), (0.0, 0.0, 10.0)], index=0)
        bot.step(1, TICK)
        bot.step(201, TICK)  # 3.2 s on: it walked from 3.008 s
        first = bot.position[2]
        self.assertGreater(first, 0.0)
        bot.step(251, TICK)  # 0.8 s more
        self.assertGreater(bot.position[2], first)
        self.assertLessEqual(bot.position[2], first + 4.5 * 0.8)  # never faster than its run speed

    def test_a_standing_bot_sends_its_state_once_a_second(self):
        bot = make_bot()
        due = [tick for tick in range(1000, 1000 + 3 * bots.SETTLE_TICKS) if bot.record_due(tick)]
        self.assertEqual(len(due), 3)

    def test_every_standing_bot_gets_its_state_on_a_tick_that_sends(self):
        # Frames go out every third tick (match.SEND_EVERY); a resting bot's turn must fall on one of them,
        # or the clients never get its state and hide it.
        for index in range(15):
            bot = make_bot()
            bot.entity = bots.BOT_ENTITY + index
            sending = range(999, 999 + 3 * bots.SETTLE_TICKS, 3)
            due = [tick for tick in sending if bot.record_due(tick, 3)]
            self.assertEqual(len(due), 3, index)


class FloorTests(unittest.TestCase):
    def test_without_collision_it_climbs_in_step_with_the_way(self):
        walker = Walker(make_bot([(0.0, 0.0, 0.0), (0.0, 2.0, 10.0)], index=0))
        walker.run(3.0 + 2.0)  # the wait, then 2 s of the walk
        _, y, z = walker.bot.position
        self.assertAlmostEqual(y, 2.0 * z / 10.0, places=6)

    def test_on_collision_it_stands_on_the_floor_under_it(self):
        class Ramp:  # a floor that rises 0.1 m per metre of z
            def ground(self, x, z, top, bottom):
                height = 0.1 * z
                return type("Ground", (), {"y": height})() if bottom <= height <= top else None

        bot = make_bot([(0.0, 0.0, 0.0), (0.0, 1.0, 10.0)], index=0)
        for tick in range(1, 600):
            bot.step(tick, TICK, Ramp())
            if bot.walking():
                self.assertAlmostEqual(bot.position[1], 0.1 * bot.position[2], places=6)

    def test_on_a_ramp_its_speed_is_along_the_floor(self):
        """Retail on the 26.5 degree ramp of its far range: 4.04 m/s across, 2.02 up, 4.51 along."""

        class Ramp:  # 0.5 m up per metre of z
            def ground(self, x, z, top, bottom):
                height = 0.5 * z
                return type("Ground", (), {"y": height})() if bottom <= height <= top else None

        walker = Walker(make_bot([(0.0, 0.0, 0.0), (0.0, 10.0, 20.0)], index=0))
        walker.run(5.5, Ramp())  # at full speed
        vx, vy, vz = walker.bot.velocity
        self.assertAlmostEqual(math.hypot(vx, vz), 4.5 / math.hypot(1.0, 0.5), places=2)  # 4.02
        self.assertAlmostEqual(math.hypot(vx, vy, vz), 4.5, places=2)

    def test_a_step_does_not_slow_it(self):
        class Step:  # 0.3 m up at z = 5
            def ground(self, x, z, top, bottom):
                height = 0.3 if z >= 5.0 else 0.0
                return type("Ground", (), {"y": height})() if bottom <= height <= top else None

        walker = Walker(make_bot([(0.0, 0.0, 0.0), (0.0, 0.3, 20.0)], index=0))
        walker.run(5.0, Step())  # full speed from about 4 s, the step at about 4.6 s
        self.assertGreater(walker.bot.position[2], 5.0)
        paces = [round(speed_over(walker, tick, 1), 3) for tick in range(walker.tick - 60, walker.tick)]
        self.assertEqual(set(paces), {4.5})


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.match = Match(PRACTICE_RANGE)
        self.player = self.match.add_player(1, "Alpha", SOLDIER, 0, False)
        self.ids = {bot.entity for bot in self.match.bots}

    def bot_records(self, tick: int) -> list:
        self.match.tick = tick
        return [update for update in self.match.remote_movements(self.player) if update.entity in self.ids]

    def test_the_spawn_frame_creates_every_bot(self):
        creates = [update for update in self.match.spawn_updates(self.player) if update.entity in self.ids]
        self.assertEqual(len(creates), 15)
        self.assertTrue(all(update.op == OP_CREATE for update in creates))
        self.assertGreater(len(world.frame(1000, 0, [], [], creates)), 15 * 50)

    def test_records_start_after_the_creates(self):
        self.match.tick = 1000
        self.match.spawn_updates(self.player)
        self.assertEqual(self.bot_records(1000 + BOT_RECORD_DELAY - 1), [])
        seen = set()
        for tick in range(1000 + BOT_RECORD_DELAY, 1000 + BOT_RECORD_DELAY + bots.SETTLE_TICKS):
            seen |= {update.entity for update in self.bot_records(tick)}
        self.assertEqual(seen, self.ids)  # every bot within a second

    def test_no_records_before_the_player_has_the_bots(self):
        self.assertEqual(self.bot_records(5000), [])

    def test_the_match_walks_its_bots(self):
        walker = next(bot for bot in self.match.bots if bot.route)
        start = walker.position
        farthest = 0.0
        for tick in range(1, 700):
            self.match.update(0.0, tick)
            farthest = max(farthest, math.dist(walker.position, start))
        self.assertGreater(farthest, 5.0)

    def test_its_bots_records_read_back(self):
        """A minute of every bot's records, read back as the client reads them (its length check fails
        when +34/+35 sit where the client does not read them)."""
        for tick in range(1, 3800):
            self.match.update(0.0, tick)
            if tick % 25:
                continue
            for bot in self.match.bots:
                fields = read_record(bot.movement(tick), tick)
                self.assertEqual(fields["throttles"], bot.held)
                if bot.held != (0, 0):
                    self.assertEqual(fields["input_frame"], tick)
                else:
                    self.assertEqual(
                        fields["input_frame"], NO_INPUT if bot.input_tick is None else bot.input_tick
                    )
                self.assertEqual(fields["yaw"], bots.s16(round(bot.yaw)))


if __name__ == "__main__":
    unittest.main()
