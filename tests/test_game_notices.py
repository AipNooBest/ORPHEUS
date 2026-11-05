"""The chat notices of a match (notices.py): joined / left (20308) and the hero switch line (graph 0C90)."""

import dataclasses
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import heroselect, messages, notices, world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.match import Match, ShownController
from ow174.game.notices import PLAYER_CARD, Switch, card_message, joined_message, left_message
from ow174.game.script import expr
from ow174.game.script.decode import read_chunk
from ow174.game.script.graph import graph
from ow174.game.statescript import chunk

REAPER = 0x02E0000000000002
MERCY = 0x02E0000000000004
ASSAULT = 0x0230000000000014  # a mode whose teams' controller is 0C90
SWITCH_MAP = dataclasses.replace(PRACTICE_RANGE, mode_guid=ASSAULT, mode_name="Assault", team_sizes=(6, 6))
FREE_FOR_ALL = dataclasses.replace(
    PRACTICE_RANGE, mode_guid=0x023000000000001E, mode_name="Deathmatch", team_sizes=(8,), free_for_all=True
)
ID_HIGH = 0x0100000000000000
OLD, NEW, WHO = 4513, 4514, 4515  # 0C90's variables of the switch line
TEXTS = {"112B": " joined the game.", "112C": " left the game.", "2C0B": " started spectating."}


def client_text(message: dict) -> str | None:
    """The text 20308 posts, as its handler chooses it (0x7FF7896E8902..0x7FF7896E893F: +0x78 and +0x7B
    on, then +0x79 off -> 112C, else +0x7A on -> 112B, off -> 2C0B)."""
    if not (message.get("+0x78") and message.get("+0x7B")):
        return None
    if not message.get("+0x79"):
        return TEXTS["112C"]
    return TEXTS["112B"] if message.get("+0x7A") else TEXTS["2C0B"]


def through_the_link(msg_id: int, value: dict) -> dict:
    """A message encoded into a payload's message channel and read back with our reader."""
    bits = BitWriter()
    messages.write_channel(bits, [(0, messages.encode(msg_id, value))])
    reliable, _ = messages.read_channel(BitReader(bits.getvalue()))
    ((_, read_id, read_value),) = reliable
    assert read_id == msg_id
    return read_value


def card(number: int, tag: str) -> dict:
    return {
        "+0x0": {"+0x0": number, "+0x8": ID_HIGH},
        "+0x10": {"+0x0": number, "+0x8": ID_HIGH},
        "+0x40": tag,
    }


class FakeClient:
    def __init__(self) -> None:
        self.reliable: list[tuple[int, dict]] = []
        self.entities: list = []

    def queue_reliable(self, msg_id: int, value: dict) -> None:
        self.reliable.append((msg_id, value))

    def queue_entities(self, updates: list) -> None:
        self.entities.extend(updates)


def into_world(match: Match, player, tick: int) -> None:
    """A player whose game loaded and got its controller."""
    player.client = FakeClient()
    player.spawned = True
    player.steps_done = 4
    match.tick = tick
    match.notices.entered_world(player)


def show_everyone(match: Match) -> None:
    """Every client has every other player's entity, as Match._show_players makes it."""
    for viewer in match.players:
        for other in match.players:
            if other is not viewer:
                viewer.shown[other.entity] = ShownController(other.entity, match.tick)


def controller_vars(player) -> list[dict]:
    """The variables of instance 1 (the controller) in every controller frame the player's client got."""
    found = []
    for update in player.client.entities:
        if update.entity == player.entity and update.chunk is not None:
            data = update.chunk.getvalue()
            frame = read_chunk(data, update.chunk.count)
            found.append({var: value for var, (value, _) in frame.instances[1].vars.items()})
    return found


def client_lines(frames: list[dict]) -> list[tuple]:
    """The switch lines a client posts from these controller frames, each applied and its events run before
    the next, as the client does: a full frame nulls the variables the network set and tells their watchers at
    once (0x7FF78991C270 -> 0x7FF78AAB45B0 -> 0x7FF78AA9ED40), then its values are told in the flush
    (0x7FF78991B850); the Watch on v4514 queues a timer whenever v4514 differs from its baseline
    (0x7FF78AAA8D60); each timer runs the conditions and the post with the values of then, and takes the new
    baseline (0x7FF78AAB0660)."""
    values: dict = {}
    baseline = None
    lines = []
    for frame in frames:
        timers = 0
        for var in [var for var in values if values[var] is not None]:
            values[var] = None
            if var == NEW and values[NEW] != baseline:
                timers += 1
        values.update(frame)
        if NEW in frame and values[NEW] != baseline:
            timers += 1
        for _ in range(timers):
            old, new = values.get(OLD), values.get(NEW)
            if old != new and old is not None:
                lines.append((values.get(WHO), new, old))
            baseline = new
    return lines


class JoinedAndLeftTests(unittest.TestCase):
    def test_the_flags_choose_the_text_as_the_client_does(self):
        tag = card(2, "Beta#1002")
        self.assertIsNone(client_text(through_the_link(PLAYER_CARD, card_message(tag))))
        self.assertEqual(client_text(through_the_link(PLAYER_CARD, joined_message(tag))), " joined the game.")
        self.assertEqual(client_text(through_the_link(PLAYER_CARD, left_message(tag))), " left the game.")
        self.assertEqual(through_the_link(PLAYER_CARD, joined_message(tag))["+0x80"]["+0x40"], "Beta#1002")

    def test_a_player_who_comes_later_joins_the_game(self):
        match = Match(SWITCH_MAP)
        alpha = match.add_player(1, "Alpha", 0, 0, False, card(1, "Alpha#1001"))
        beta = match.add_player(2, "Beta", 0, 1, False, card(2, "Beta#1002"))
        into_world(match, alpha, 100)
        into_world(match, beta, 160)
        self.assertEqual(match.notices.card(alpha, beta), joined_message(beta.card))
        self.assertEqual(match.notices.card(beta, alpha), card_message(alpha.card))  # he was there first
        match.tick = 200
        match.update(10.0, 200)
        self.assertIn((PLAYER_CARD, joined_message(beta.card)), alpha.client.reliable)
        self.assertNotIn((PLAYER_CARD, joined_message(alpha.card)), beta.client.reliable)

    def test_players_who_come_in_the_same_tick_join_quietly(self):
        match = Match(SWITCH_MAP)
        alpha = match.add_player(1, "Alpha", 0, 0, False)
        beta = match.add_player(2, "Beta", 0, 1, False)
        into_world(match, alpha, 100)
        into_world(match, beta, 100)
        self.assertEqual(match.notices.card(alpha, beta), card_message(beta.card))

    def test_a_player_who_leaves_left_the_game_for_those_who_saw_him(self):
        match = Match(SWITCH_MAP)
        alpha = match.add_player(1, "Alpha", 0, 0, False)
        beta = match.add_player(2, "Beta", 0, 1, False)
        gamma = match.add_player(3, "Gamma", 0, 1, False)
        for player in (alpha, beta, gamma):
            into_world(match, player, 100)
        alpha.shown[beta.entity] = ShownController(beta.entity, 100)
        match.leave(beta)
        self.assertEqual(alpha.client.reliable, [(PLAYER_CARD, left_message(beta.card))])
        self.assertEqual(gamma.client.reliable, [])  # his client never had Beta

    def test_a_game_that_never_came_gets_no_line(self):
        match = Match(SWITCH_MAP)
        alpha = match.add_player(1, "Alpha", 0, 0, False)
        beta = match.add_player(2, "Beta", 0, 1, False)
        into_world(match, alpha, 100)
        match.leave(beta, "did not connect")
        self.assertEqual(alpha.client.reliable, [])


class BattleTagTests(unittest.TestCase):
    def test_the_battletag_is_a_byte_aligned_counted_string(self):
        # Component 29 fields 0 (dbid) and 1 (m_battleTag, a teString): mask 03 00, the id's two byte masks,
        # then on a byte the u32 byte count and the bytes (0x7FF78AAF0300: 0x7FF7893008F0, 0x7FF789300B90).
        record = world.update(0, {29: [(0x10ABCDEF, ID_HIGH), "TheDong#1234"]})
        self.assertEqual(
            record.getvalue().hex(" "),
            "01 1d 02 03 00 0f ef cd ab 10 80 01 0c 00 00 00 54 68 65 44 6f 6e 67 23 31 32 33 34",
        )

    def test_a_string_after_odd_bits_goes_on_the_next_byte(self):
        out = world.RecordWriter(3)
        out.bits(1, 2)
        world._write_value(out, "string", "Ab")
        self.assertEqual(out.count, 2 + 3 + 32 + 16)  # 3 bits pad to the payload's byte (3 + 2 + 3 = 8)

    def test_the_player_entity_carries_the_battletag(self):
        match = Match(SWITCH_MAP)
        alpha = match.add_player(1, "Alpha", 0, 0, False, card(1, "Alpha#1001"))
        nameless = match.add_player(2, "Beta", 0, 0, False)
        self.assertEqual(match._player_info(alpha)[1], "Alpha#1001")
        self.assertEqual(match._player_info(nameless)[1], "Beta")


class SwitchGraphTests(unittest.TestCase):
    def test_the_presence_bits_are_0c90s(self):
        controller = graph(0x0C90)
        self.assertEqual(controller.presence_bit(OLD), notices.OLD_HERO_BIT)
        self.assertEqual(controller.presence_bit(NEW), notices.NEW_HERO_BIT)
        self.assertEqual(controller.presence_bit(WHO), notices.PLAYER_BIT)
        self.assertEqual(controller.presence_bit(8419), heroselect.PVP.allow_bit)  # the bit that works live
        self.assertEqual(len(controller.presence_vars()), heroselect.PVP.graph.sync_vars)

    def test_the_line_names_the_player_and_both_heroes(self):
        # 0C90's chat action: 0AA4 with the name of v4515, the hero v4514 and the hero v4513, under a
        # client-only Watch on v4514 (state 118).
        controller = graph(0x0C90)
        (action,) = [node for node in controller.nodes if node.cls == "STU_7106C356"]
        self.assertTrue(action.client_only)
        self.assertEqual(action.field("m_text.m_6C54C35C.m_displayText"), "0x0DE0000000000AA4")
        args = action.field("m_text.m_367CCBF5")
        self.assertEqual([arg["$"] for arg in args], ["STU_CD7F3617", "STU_49BC3C3C", "STU_49BC3C3C"])
        self.assertEqual(
            [
                args[0]["m_entity"]["m_identifier"],
                args[1]["m_hero"]["m_identifier"],
                args[2]["m_hero"]["m_identifier"],
            ],
            [WHO, NEW, OLD],
        )
        (watch,) = [node for node in controller.nodes if node.cls == "STU_2F4E2E3F" and node.state == 118]
        self.assertTrue(watch.client_only)
        self.assertEqual(watch.field("m_B7A3CF78.m_identifier"), NEW)

    def test_the_controller_frame_carries_the_switch(self):
        # Read back by graph 0C90's own variable list (decode.py), not by the encoder's bit numbers.
        switch = Switch(REAPER, SOLDIER, 0xA0000200)
        frame = heroselect.controller_frame(heroselect.PVP, 123456, False, presence=switch.presence())
        made = chunk(frame, 0, 1)
        decoded = read_chunk(made.getvalue(), made.count)
        values = {var: value for var, (value, _) in decoded.instances[1].vars.items()}
        self.assertEqual(values[OLD], expr.Asset(REAPER))
        self.assertEqual(values[NEW], expr.Asset(SOLDIER))
        self.assertEqual(values[WHO], expr.Entity(0xA0000200))
        self.assertIs(values[8419], True)
        plain = heroselect.controller_frame(heroselect.PVP, 123456, False)
        self.assertEqual(frame.count - plain.count, 2 * 31 + 38)  # two assets and an entity, with bindings


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.match = Match(SWITCH_MAP)
        self.alpha = self.match.add_player(1, "Alpha", 0, 0, False)
        self.beta = self.match.add_player(2, "Beta", 0, 0, False)
        self.gamma = self.match.add_player(3, "Gamma", 0, 1, False)
        for player in (self.alpha, self.beta, self.gamma):
            into_world(self.match, player, 100)
        show_everyone(self.match)

    def switch(self, player, hero: int, now: float) -> None:
        self.match.switch_hero(player, heroes()[hero])
        self.match.notices.update(now)

    def test_the_first_pick_is_no_switch(self):
        self.switch(self.beta, REAPER, 10.0)
        self.assertEqual(controller_vars(self.alpha), [])
        self.assertEqual(controller_vars(self.beta), [])

    def test_the_player_and_his_teammates_get_the_switch(self):
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.beta, SOLDIER, 11.0)
        expected = {OLD: expr.Asset(REAPER), NEW: expr.Asset(SOLDIER), WHO: expr.Entity(self.beta.entity)}
        for player in (self.alpha, self.beta):
            (values,) = controller_vars(player)
            self.assertEqual({var: values[var] for var in (OLD, NEW, WHO)}, expected)
        self.assertEqual(controller_vars(self.gamma), [])  # an enemy

    def test_later_controller_frames_leave_it_out(self):
        # Sent again, the client's clearing of a full frame would make the Watch post the line again.
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.beta, SOLDIER, 11.0)
        self.match.send_controller(self.alpha)  # hero select opens or closes, for example
        shown, later = controller_vars(self.alpha)
        self.assertEqual(shown[NEW], expr.Asset(SOLDIER))
        self.assertFalse({OLD, NEW, WHO} & set(later))

    def test_a_frame_from_elsewhere_clears_it_as_well(self):
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.beta, SOLDIER, 11.0)
        self.match.send_controller(self.alpha)
        self.alpha.script.arrived(self.alpha.script.last)
        self.switch(self.beta, MERCY, 11.5)
        first, plain, second = controller_vars(self.alpha)  # no frame of ours between the two
        self.assertEqual(first[NEW], expr.Asset(SOLDIER))
        self.assertNotIn(NEW, plain)
        self.assertEqual((second[OLD], second[NEW]), (expr.Asset(SOLDIER), expr.Asset(MERCY)))

    def test_each_switch_posts_one_line_on_each_client(self):
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.alpha, MERCY, 10.0)
        self.switch(self.beta, SOLDIER, 11.0)
        self.match.send_controller(self.alpha)  # Alpha opens hero select
        self.match.switch_hero(self.alpha, heroes()[REAPER])
        self.match.switch_hero(self.beta, heroes()[MERCY])
        now = 11.0
        for _ in range(12):  # every frame arrives, a tick or more apart
            now += 0.2
            for player in (self.alpha, self.beta, self.gamma):
                player.script.arrived(player.script.last)
            self.match.notices.update(now)
        alpha, beta = self.alpha.entity, self.beta.entity
        switches = [(beta, SOLDIER, REAPER), (alpha, REAPER, MERCY), (beta, MERCY, SOLDIER)]
        expected = [(expr.Entity(who), expr.Asset(new), expr.Asset(old)) for who, new, old in switches]
        self.assertEqual(client_lines(controller_vars(self.alpha)), expected)
        self.assertEqual(client_lines(controller_vars(self.beta)), expected)
        self.assertEqual(client_lines(controller_vars(self.gamma)), [])

    def test_the_client_model_posts_a_repeated_frame_again(self):
        # Why a notice goes in one frame only: the same values in two frames make two lines.
        frame = {OLD: expr.Asset(REAPER), NEW: expr.Asset(SOLDIER), WHO: expr.Entity(0xA0000200)}
        self.assertEqual(len(client_lines([frame])), 1)
        self.assertEqual(len(client_lines([frame, frame])), 2)
        self.assertEqual(len(client_lines([frame, {}, frame])), 2)
        other = {**frame, NEW: expr.Asset(MERCY)}
        self.assertEqual(len(client_lines([frame, other])), 3)  # cleared and set in one frame: two timers

    def test_the_match_tick_sends_it(self):
        self.match.switch_hero(self.beta, heroes()[REAPER])
        self.match.switch_hero(self.beta, heroes()[MERCY])
        self.match.update(30.0, 300)
        (values,) = controller_vars(self.alpha)
        self.assertEqual((values[OLD], values[NEW]), (expr.Asset(REAPER), expr.Asset(MERCY)))

    def test_a_teammate_whose_client_lacks_the_player_gets_no_line(self):
        # Without his entity the client has no name for him (component 29).
        del self.alpha.shown[self.beta.entity]
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.beta, SOLDIER, 11.0)
        self.assertEqual(controller_vars(self.alpha), [])
        self.assertEqual(len(controller_vars(self.beta)), 1)

    def test_a_skin_change_is_no_switch(self):
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.beta, REAPER, 11.0)
        self.assertEqual(controller_vars(self.alpha), [])

    def test_a_second_switch_waits_for_a_frame_without_the_first(self):
        # Beta goes Reaper -> Soldier, then Alpha Mercy -> Soldier: Alpha's client gets the first, a frame
        # without the three (the Watch fires on their clearing, with no old hero: no line), then the second.
        self.switch(self.beta, REAPER, 10.0)
        self.switch(self.alpha, MERCY, 10.0)
        self.switch(self.beta, SOLDIER, 11.0)
        self.match.switch_hero(self.alpha, heroes()[SOLDIER])
        self.match.notices.update(11.05)  # the first notice frame has not arrived yet
        self.assertEqual(len(controller_vars(self.alpha)), 1)
        self.alpha.script.arrived(self.alpha.script.last)
        self.match.notices.update(11.2)
        self.alpha.script.arrived(self.alpha.script.last)
        self.match.notices.update(11.4)
        first, cleared, second = controller_vars(self.alpha)
        self.assertEqual(first[WHO], expr.Entity(self.beta.entity))
        self.assertNotIn(NEW, cleared)
        self.assertEqual((second[OLD], second[NEW]), (expr.Asset(MERCY), expr.Asset(SOLDIER)))
        self.assertEqual(second[WHO], expr.Entity(self.alpha.entity))

    def test_no_switch_line_without_0c90s_notice(self):
        for game_map in (PRACTICE_RANGE, FREE_FOR_ALL):
            match = Match(game_map)
            alpha = match.add_player(1, "Alpha", 0, 0, False)
            beta = match.add_player(2, "Beta", 0, 0, False)
            for player in (alpha, beta):
                into_world(match, player, 100)
            show_everyone(match)
            match.switch_hero(beta, heroes()[REAPER])
            match.switch_hero(beta, heroes()[SOLDIER])
            match.notices.update(20.0)
            self.assertEqual(match.notices.presence(alpha), {})
            self.assertEqual(match.notices.waiting, {})


if __name__ == "__main__":
    unittest.main()
