"""The in-match screens: the Tab board's own numbers (component 64, stats.py), the other players' heroes and
bodies on it (44 and 75), and the F1 hero info's hero (v940 in the controller frame)."""

import dataclasses
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_notices import FakeClient, into_world, show_everyone
from test_game_team_list import read_create

from ow174.game import heroselect, stats, world
from ow174.game.bits import BitReader
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.match import Match
from ow174.game.script import expr
from ow174.game.script.decode import read_chunk
from ow174.game.statescript import chunk
from ow174.game.world import OP_UPDATE

REAPER = 0x02E0000000000002
TRACER = 0x02E0000000000003
CONTROL = 0x0230000000000002  # a PvP mode without lists of its own
PVP = dataclasses.replace(PRACTICE_RANGE, mode_guid=CONTROL, mode_name="Control", team_sizes=(6, 6))
PLAYER_STATS = [
    stats.ELIMINATIONS,
    stats.OBJECTIVE_KILLS,
    stats.OBJECTIVE_TIME,
    stats.HERO_DAMAGE,
    stats.HEALING,
    stats.DEATHS,
]


def read_components(data: bytes) -> dict:
    """An update record's body as the client's component reader takes it (world._write_components; the
    leaf reader 0x7FF78AAF0300: a u64 without field flag 2, an f32 and a u8 are raw bytes): {component:
    {field: value}}, floats as floats."""
    reader = BitReader(data)
    found = {}
    for _ in range(reader.bits(8)):
        index = reader.bits(8)
        reader.align()
        mask = int.from_bytes(bytes(reader.bits(8) for _ in range(reader.bits(8))), "little")
        values = {}
        for number, kind in enumerate(world.COMPONENT_FIELDS[index]):
            if not mask >> number & 1:
                continue
            if kind == "f32":
                values[number] = struct.unpack("<f", struct.pack("<I", reader.bits(32)))[0]
            elif kind == "entity":
                values[number] = reader.bits(32)
            elif kind == "id":
                reader.align()
                byte_mask, number_read = reader.bits(8), 0
                for k in range(8):
                    if byte_mask >> k & 1:
                        number_read |= reader.bits(8) << (8 * k)
                values[number] = number_read
            else:
                values[number] = reader.bits(8 * struct.calcsize(world.SCALAR_FORMATS[kind]))
        found[index] = values
    return found


def updates_of(client: FakeClient, entity: int) -> list[dict]:
    """The components of every update record queued for an entity."""
    found = []
    for update in client.entities:
        if update.entity == entity and update.op == OP_UPDATE:
            found.append(read_components(update.build(0).getvalue()))
    return found


class StatsDataTests(unittest.TestCase):
    def test_the_player_stats_are_the_data_list(self):
        # 0051.054 m_FC833C02: eliminations, objective kills, objective time, hero damage, healing and deaths,
        # deaths flagged (no medal).
        self.assertEqual(stats.player_stats(CONTROL), [(stat, stat == stats.DEATHS) for stat in PLAYER_STATS])
        self.assertEqual(stats.display_type(stats.OBJECTIVE_TIME), 8)  # seconds, printed as a time
        self.assertEqual(stats.display_type(stats.ELIMINATIONS), 0)  # a count

    def test_a_mode_can_list_others(self):
        lucioball = 0x023000000000001D
        self.assertEqual(len(stats.player_stats(lucioball)), 6)
        self.assertNotEqual(stats.player_stats(lucioball), stats.player_stats(CONTROL))

    def test_each_hero_has_five_or_six_stats(self):
        # The board's second list has 6 slots; every hero list (and every mode's) fits.
        for hero in heroes():
            listed = stats.hero_stats(hero, CONTROL)
            self.assertIn(len(listed), (5, 6), hero)
        tracer = [stat for stat, _ in stats.hero_stats(TRACER, CONTROL)]
        self.assertEqual(tracer[0], 0x086000000000002F)  # weapon accuracy
        self.assertEqual(stats.display_type(tracer[0]), 2)  # a fraction, printed as a percentage


class StatsComponentTests(unittest.TestCase):
    def test_the_fields_of_both_lists(self):
        record = stats.PlayerStats()
        record.add(stats.ELIMINATIONS, 3)
        record.add(stats.HERO_DAMAGE, 1234.5)
        record.set(stats.OBJECTIVE_TIME, 61.0)
        values = stats.component(record, CONTROL, SOLDIER, {stats.ELIMINATIONS: stats.GOLD})
        # 5-12: the player stats and two empty slots; 21-28 their values; 37-44 their medals
        self.assertEqual(values[5:13], [*PLAYER_STATS, 0, 0])
        self.assertEqual(values[21:29], [3.0, 0.0, 61.0, 1234.5, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual(values[37:45], [stats.GOLD, 0, 0, 0, 0, 0, 0, 0])
        # 13-18: Soldier's stats, padded with empty slots; 29-34 their values
        soldier = [stat for stat, _ in stats.hero_stats(SOLDIER, CONTROL)]
        self.assertEqual(values[13:19], soldier + [0] * (6 - len(soldier)))
        self.assertEqual(values[29:35], [0.0] * 6)
        # the rest (heroes played, On Fire) is left alone
        self.assertEqual(values[0:5] + values[19:21] + values[35:37], [None] * 9)

    def test_no_hero_yet_empties_the_hero_list(self):
        values = stats.component(stats.PlayerStats(), CONTROL, None)
        self.assertEqual(values[13:19], [0] * 6)

    def test_medals_go_to_the_three_best_teammates(self):
        team = [stats.PlayerStats() for _ in range(5)]
        for record, kills in zip(team, (4, 9, 0, 9, 2), strict=True):
            record.set(stats.ELIMINATIONS, kills)
            record.set(stats.DEATHS, kills)
        medals = stats.medals(team, CONTROL)
        self.assertEqual([medal.get(stats.ELIMINATIONS, 0) for medal in medals], [3, 1, 0, 2, 0])
        self.assertTrue(all(stats.DEATHS not in medal for medal in medals))  # flagged: no medal
        self.assertTrue(all(stats.HEALING not in medal for medal in medals))  # nobody healed

    def test_the_component_bytes_read_back(self):
        record = stats.PlayerStats()
        record.add(stats.ELIMINATIONS, 7)
        values = stats.component(record, CONTROL, TRACER, {stats.ELIMINATIONS: stats.SILVER})
        made = world.update(0, {stats.GMP_STATS: values})
        read = read_components(made.getvalue())[stats.GMP_STATS]
        self.assertEqual(sorted(read), list(range(5, 19)) + list(range(21, 35)) + list(range(37, 45)))
        self.assertEqual(read[5], stats.ELIMINATIONS)
        self.assertEqual(read[21], 7.0)
        self.assertEqual(read[37], stats.SILVER)
        self.assertEqual(read[13], 0x086000000000002F)
        # 14 u64 + 14 f32 + 8 u8, after the index, the padding, the mask's size and its 6 bytes
        self.assertEqual(len(made.getvalue()), 1 + 1 + 1 + 6 + 14 * 8 + 14 * 4 + 8)


class BoardInMatchTests(unittest.TestCase):
    def setUp(self):
        self.match = Match(PVP)
        self.alpha = self.match.add_player(1, "Alpha", 0, 0, False)
        self.beta = self.match.add_player(2, "Beta", 0, 0, False)
        self.gamma = self.match.add_player(3, "Gamma", 0, 1, False)
        for player in (self.alpha, self.beta, self.gamma):
            into_world(self.match, player, 100)

    def test_the_own_create_carries_the_board(self):
        record = self.match._player_create(0, self.alpha)
        components = read_create(BitReader(record.getvalue()))
        self.assertEqual(components[stats.GMP_STATS][5], stats.ELIMINATIONS)
        self.assertEqual(components[stats.GMP_STATS][13], 0)  # no hero yet
        other = read_create(BitReader(self.match._player_create(0, self.alpha, own=False).getvalue()))
        self.assertNotIn(stats.GMP_STATS, other)  # owner-only fields

    def test_new_numbers_go_to_the_owner_only(self):
        self.match._player_create(0, self.alpha)  # what its client was sent
        self.alpha.stats.add(stats.ELIMINATIONS, 2)
        self.match.update(10.0, 101)
        (update,) = updates_of(self.alpha.client, self.alpha.entity)
        self.assertEqual(update[stats.GMP_STATS][21], 2.0)
        self.assertEqual(update[stats.GMP_STATS][37], stats.GOLD)  # the best of his team
        self.assertEqual(updates_of(self.beta.client, self.alpha.entity), [])
        self.alpha.client.entities.clear()
        self.alpha.stats.add(stats.ELIMINATIONS, 1)
        self.match.update(10.2, 102)  # not yet: at most every STATS_EVERY
        self.assertEqual(updates_of(self.alpha.client, self.alpha.entity), [])
        self.match.update(10.6, 103)
        (update,) = updates_of(self.alpha.client, self.alpha.entity)
        self.assertEqual(update[stats.GMP_STATS][21], 3.0)
        self.alpha.client.entities.clear()
        self.match.update(11.2, 104)  # nothing changed
        self.assertEqual(updates_of(self.alpha.client, self.alpha.entity), [])

    def test_a_teammate_with_more_takes_the_medal(self):
        self.match._player_create(0, self.alpha)
        self.alpha.stats.add(stats.HEALING, 100)
        self.beta.stats.add(stats.HEALING, 300)
        self.gamma.stats.add(stats.HEALING, 900)  # the other team
        self.match.update(10.0, 101)
        (update,) = updates_of(self.alpha.client, self.alpha.entity)
        self.assertEqual(update[stats.GMP_STATS][37 + 4], stats.SILVER)  # healing is the fifth stat


class PossessionTests(unittest.TestCase):
    def setUp(self):
        self.match = Match(PVP)
        self.alpha = self.match.add_player(1, "Alpha", 0, 0, False)
        self.gamma = self.match.add_player(3, "Gamma", 0, 1, False)
        for player in (self.alpha, self.gamma):
            into_world(self.match, player, 100)
        show_everyone(self.match)

    def test_another_players_hero_and_body_follow_his_body(self):
        self.match.switch_hero(self.gamma, heroes()[REAPER])
        self.match.tick = 101
        self.match._show_players(self.alpha)
        self.assertEqual(updates_of(self.alpha.client, self.gamma.entity), [])  # his body is not there yet
        self.alpha.client.queue_entities([self.match.remote_body_create(self.alpha, self.gamma)])
        self.match.tick = 102
        self.match._show_players(self.alpha)
        (update,) = updates_of(self.alpha.client, self.gamma.entity)
        body = self.gamma.body
        self.assertEqual(update[44], {0: body, 1: body, 2: body})
        self.assertEqual(update[75], {0: REAPER, 3: REAPER})
        self.alpha.client.entities.clear()
        self.match.tick = 103
        self.match._show_players(self.alpha)
        self.assertEqual(updates_of(self.alpha.client, self.gamma.entity), [])  # once

    def test_a_switch_names_the_new_body(self):
        self.match.switch_hero(self.gamma, heroes()[REAPER])
        self.alpha.client.queue_entities([self.match.remote_body_create(self.alpha, self.gamma)])
        self.match.tick = 101
        self.match._show_players(self.alpha)
        self.match.switch_hero(self.gamma, heroes()[TRACER])
        self.alpha.client.entities.clear()
        self.alpha.client.queue_entities([self.match.remote_body_create(self.alpha, self.gamma)])
        self.match.tick = 102
        self.match._show_players(self.alpha)
        (update,) = updates_of(self.alpha.client, self.gamma.entity)
        self.assertEqual(update[44][0], self.gamma.body)
        self.assertEqual(update[75][0], TRACER)

    def test_the_create_of_another_player_has_no_possession(self):
        self.match.switch_hero(self.gamma, heroes()[REAPER])
        components = read_create(BitReader(self.match._player_create(0, self.gamma, own=False).getvalue()))
        self.assertEqual(sorted(components), [26, 29, 74])


class HeroInfoTests(unittest.TestCase):
    def test_the_controller_frame_names_the_hero(self):
        # Read back by the graphs' own variable lists (decode.py): v940 among the entity variables.
        frame = heroselect.controller_frame(heroselect.PVP, 1234, False, hero=SOLDIER)
        made = chunk(frame, 0, 1)
        decoded = read_chunk(made.getvalue(), made.count)
        entity_vars = {var: value for var, (value, _) in decoded.entity_vars.items()}
        self.assertEqual(entity_vars[heroselect.CURRENT_HERO], expr.Asset(SOLDIER))
        plain = heroselect.controller_frame(heroselect.PVP, 1234, False)
        made = chunk(plain, 0, 1)
        self.assertNotIn(heroselect.CURRENT_HERO, read_chunk(made.getvalue(), made.count).entity_vars)

    def test_the_match_sends_the_hero_after_a_pick_that_kept_the_screen(self):
        match = Match(PVP)
        alpha = match.add_player(1, "Alpha", 0, 0, False)
        into_world(match, alpha, 100)
        match.assemble_ends = 1000.0  # assembling: the pick leaves the screen open, no controller frame
        match.switch_hero(alpha, heroes()[REAPER])
        self.assertIsNone(alpha.controller_hero)
        match.update(10.0, 101)
        self.assertEqual(alpha.controller_hero, REAPER)
        frames = [update for update in alpha.client.entities if update.chunk is not None]
        decoded = read_chunk(frames[-1].chunk.getvalue(), frames[-1].chunk.count)
        self.assertEqual(decoded.entity_vars[heroselect.CURRENT_HERO][0], expr.Asset(REAPER))
        alpha.client.entities.clear()
        match.update(10.1, 102)
        self.assertEqual([update for update in alpha.client.entities if update.chunk is not None], [])


if __name__ == "__main__":
    unittest.main()
