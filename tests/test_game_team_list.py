"""The hero select team list: every client gets the other players' entities, cards and picks."""

import dataclasses
import struct
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_server import REAPER, PretendGame, pick

from ow174.game import heroselect, messages, world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.content import PRACTICE_RANGE, SOLDIER, heroes
from ow174.game.link import ACK, KIND_FRAME, SYN
from ow174.game.match import CARD_AGAIN_TICKS, CARD_RANK, CARD_ROLE, PLAYER_CARD, RANKED, UNRANKED, Match
from ow174.game.server import GameServer
from ow174.game.statescript import chunk
from ow174.game.world import OP_CREATE, OP_DESTROY, EntityUpdate, _write_entity

ID_HIGH = 0x0100000000000000  # the high half of every lobby account id (jam.values.id16)
PVP = dataclasses.replace(
    PRACTICE_RANGE, mode_guid=0x0230000000000002, mode_name="Control", team_sizes=(6, 6)
)
ALPHA, BETA, GAMMA = 0xA0000100, 0xA0000200, 0xA0000300


def bitstring(bits: BitWriter) -> str:
    return "".join(str((bits.value >> k) & 1) for k in range(bits.count))


def _widths(reader: BitReader, small: int, middle: int, large: int) -> int:
    if not reader.bit():
        return reader.bits(small)
    if not reader.bit():
        return reader.bits(middle)
    return reader.bits(large)


def read_entities(reader: BitReader, count: int) -> list[dict]:
    """The entity blocks of a world frame, as world._write_entity writes them: the chunk's payload (its
    value and bit count) and the ch4 op, with the reader at the start of the record."""
    entities = []
    for _ in range(count):
        item = {"entity": reader.entity_id(), "chunk": None, "op": 0, "components": None}
        entities.append(item)
        reader.bit()  # ch0
        if not reader.bit():
            continue
        if reader.bit():
            reader.bit()
            _widths(reader, 16, 24, 32)  # first frame
            _widths(reader, 8, 16, 32)  # span
            reader.bit()  # fragmented
            size = _widths(reader, 8, 16, 32)
            item["chunk"] = (reader.bits(size), size)
        if not reader.bit():  # ch2
            continue
        reader.bits(2)
        reader.bit()  # ch3
        if reader.bit():
            reader.bits(reader.bits(12) - 12)
        if not reader.bit():  # ch4
            continue
        item["op"] = reader.bits(2)
        if item["op"] != OP_DESTROY:
            size = reader.bits(14)
            end = reader.pos + size
            if item["op"] == OP_CREATE:
                item["components"] = read_create(reader)
            reader.pos = end
        reader.bit()  # ch5
    return entities


def _byte_masked(reader: BitReader) -> int:
    mask, number = reader.bits(8), 0
    for k in range(8):
        if mask >> k & 1:
            number |= reader.bits(8) << (8 * k)
    return number


def read_create(reader: BitReader) -> dict | None:
    """A create record of a 003 definition: {component: {field number: value}} (no arrays: none of our
    creates sends one). None for a map placeable."""
    if reader.bit():
        return None
    for small in (12, 20):
        if reader.bit():
            reader.bits(small)
            break
    else:
        reader.bits(32)
    flags = reader.bits(8)
    reader.bits(32 * (7 if flags & world.WITH_TRANSFORM else 0) + 32 * 3 * bool(flags & 0xC))
    components = {}
    for _ in range(reader.bits(8)):
        index = reader.bits(8)
        reader.align()
        mask = int.from_bytes(bytes(reader.bits(8) for _ in range(reader.bits(8))), "little")
        values = {}
        for number, kind in enumerate(world.COMPONENT_FIELDS[index]):
            if not mask >> number & 1:
                continue
            if kind == "id":
                reader.align()
                values[number] = _byte_masked(reader)
            elif kind == "dbid":
                values[number] = (_byte_masked(reader), _byte_masked(reader))
            elif kind == "entity":
                values[number] = reader.bits(32)
            elif kind == "string":
                reader.align()
                size = reader.bits(32)
                values[number] = bytes(reader.bits(8) for _ in range(size)).decode("utf-8")
            elif kind.startswith("blob:"):
                values[number] = reader.bits(8 * int(kind[5:]))
            else:
                values[number] = reader.bits(8 * struct.calcsize(world.SCALAR_FORMATS[kind]))
        components[index] = values
    return components


class Game(PretendGame):
    def world(self, wanted, seconds: float = 4.0) -> list:
        """Read frames until wanted(messages, entities) of everything seen so far is true; returns it:
        [(message id, value)] and [entity block]."""
        seen_messages, seen_entities = [], []
        deadline = time.time() + seconds
        while time.time() < deadline:
            packet = self.receive()
            if packet.kind != KIND_FRAME:
                continue
            reader = BitReader(packet.payload)
            reader.var_a()
            reliable, _ = messages.read_channel(reader)
            reader.var_a()
            reader.bits(4 + 1 + 1 + 1)
            seen_messages += [(msg_id, value) for _, msg_id, value in reliable]
            seen_entities += read_entities(reader, reader.bits(7))
            if wanted(seen_messages, seen_entities):
                return seen_messages, seen_entities
        raise AssertionError(f"not seen in time: {[m for m, _ in seen_messages]}")

    def join(self) -> dict:
        """Connect and load the map; returns 20300."""
        self.send(SYN)
        self.receive()
        self.send(ACK)
        found, _ = self.world(lambda found, entities: any(msg_id == 20300 for msg_id, _ in found))
        self.send_message(21601, {})
        return next(value for msg_id, value in found if msg_id == 20300)


def card(number: int, tag: str, frame: int, level: int, rank=None) -> dict:
    """A card as the lobby makes it (content.player.record), with the matchmaker's rank if given."""
    made = {
        "+0x0": {"+0x0": number, "+0x8": ID_HIGH},
        "+0x10": {"+0x0": number, "+0x8": ID_HIGH},
        "+0x20": 0x02500000000002F7,
        "+0x28": frame,
        "+0x30": 1700000000,
        "+0x38": level,
        "+0x3C": 1,
        "+0x40": tag,
    }
    if rank is not None:
        made[CARD_RANK] = rank
    return made


def cards(found: list) -> dict:
    """The 20308 cards seen: their id's low half -> card."""
    found_cards = [value["+0x80"] for msg_id, value in found if msg_id == PLAYER_CARD]
    return {card["+0x0"]["+0x0"]: card for card in found_cards}


def cards_of(number: int, found: list) -> list:
    """The 20308 messages seen for the player whose id's low half is `number`."""
    sent = [value for msg_id, value in found if msg_id == PLAYER_CARD]
    return [value for value in sent if value["+0x80"]["+0x0"]["+0x0"] == number]


def chunks_of(entity: int, entities: list) -> list:
    return [item["chunk"] for item in entities if item["entity"] == entity and item["chunk"]]


def ops_of(entities: list) -> list:
    return [(item["entity"], item["op"]) for item in entities if item["op"]]


class TeamListFrameTests(unittest.TestCase):
    def test_the_team_entry_frame_is_the_reference_frame(self):
        # frames10_out.txt F3: a teammate's controller 0xA0000200 with 288D as its root instance and
        # v940 = v13161 = Soldier: 76, v2114 = true; 167 payload bits, the first chunk.
        frame = heroselect.team_entry_frame(SOLDIER)
        self.assertEqual(frame.count, 167)
        block = BitWriter()
        _write_entity(block, EntityUpdate(BETA, chunk=chunk(frame, 0, 1)))
        self.assertEqual(
            block.getvalue().hex(" "),
            "05 20 70 00 00 04 70 8a 68 44 01 30 d6 81 0c 2e c0 4d 42 08 50 69 33 19 5c 80 1b 00 00",
        )

    def test_a_player_still_choosing_has_only_v2114_off(self):
        # Without a hero the two assets (49 bits each) go and v2114 is false.
        picked = bitstring(heroselect.team_entry_frame(SOLDIER))
        choosing = bitstring(heroselect.team_entry_frame(None))
        self.assertEqual(len(choosing), 167 - 2 * 49)
        self.assertEqual(choosing, picked[:33] + picked[82:104] + "0" + picked[105] + picked[155:])

    def test_a_player_id_is_two_unpadded_byte_masks(self):
        # Component 29 field 0 (dbid): the low and the high u64, each a byte mask and its bytes.
        record = world.update(0, {29: [(0x10ABCDEF, ID_HIGH)]})
        self.assertEqual(record.getvalue().hex(" "), "01 1d 02 01 00 0f ef cd ab 10 80 01")

    def test_the_level_and_the_rank_are_raw_bytes_after_the_id(self):
        # Component 29 fields 0 (dbid), 2 (u32 m_playerLevel), 7 and 8 (s16 m_rankedLevel, m_heroicRank)
        # and 9 (s8 m_rankedLevelTier): mask bits 0, 2, 7 | 8, 9, then each value's bytes, low first,
        # with no padding (all flags 0 in the type's field records).
        values = [(0x10ABCDEF, ID_HIGH), None, 25, None, None, None, None, 2345, 17, 4]
        record = world.update(0, {29: values})
        self.assertEqual(
            record.getvalue().hex(" "), "01 1d 02 85 03 0f ef cd ab 10 80 01 19 00 00 00 29 09 11 00 04"
        )

    def test_the_portrait_frame_is_the_first_field_of_component_74(self):
        # m_portraitFrame is a flag-2 GUID: on a byte, a mask of its non-zero bytes (0xC3: bytes 0, 1, 6,
        # 7) and those bytes; then the slot and the two hero select flags.
        record = world.update(0, {74: [0x0250000000000921, None, 3, None, None, None, 1, 1, None]})
        self.assertEqual(record.getvalue().hex(" "), "01 4a 02 c5 00 c3 21 09 50 02 03 01 01")

    def test_the_game_struct_gives_each_team_its_size(self):
        for game_map, sizes in ((PVP, [6, 6]), (PRACTICE_RANGE, [1, 0])):
            match = Match(game_map)
            viewer = match.add_player(1, "Alpha", 0, 0, False)
            teams = match.game_struct(viewer)["+0x0"]["+0x0"]["+0xC0"]
            self.assertEqual([team["+0x60"] for team in teams], [0, 1])
            self.assertEqual([team["+0x61"] for team in teams], sizes)


class Recorder:
    """A client that keeps what the match queues for it."""

    def __init__(self) -> None:
        self.reliable, self.entities = [], []

    def queue_reliable(self, msg_id: int, value: dict) -> None:
        self.reliable.append((msg_id, value))

    def queue_entities(self, updates: list) -> None:
        self.entities += updates


class ShowPlayersTests(unittest.TestCase):
    def test_new_players_come_a_few_per_tick_and_their_frames_a_tick_later(self):
        match = Match(PVP)
        players = [match.add_player(number, f"P{number}", 0, number % 2, False) for number in range(1, 6)]
        for player in players:
            player.client, player.spawned = Recorder(), True
        viewer = players[0].client
        match.tick = 10
        match._show_players(players[0])
        self.assertEqual([msg_id for msg_id, _ in viewer.reliable], [PLAYER_CARD] * 4)  # its own and 3
        self.assertEqual([update.op for update in viewer.entities], [OP_CREATE] * 3)
        viewer.entities.clear()
        match.tick = 11
        match._show_players(players[0])
        # the fourth player's create, and the frames of the three created a tick before
        self.assertEqual([update.op for update in viewer.entities].count(OP_CREATE), 1)
        self.assertEqual(len([update for update in viewer.entities if update.chunk is not None]), 3)
        viewer.entities.clear()
        match.tick = 12
        match._show_players(players[0])
        self.assertEqual(len(viewer.entities), 1)  # only the fourth one's frame: nothing else changed

    def test_each_card_goes_once_more_later_within_the_cards_per_tick(self):
        # A second card makes the client fill the team list again (event 0x47), once the portrait
        # frames the creates named have loaded. They share the new players' limit per tick.
        match = Match(PVP)
        players = [match.add_player(number, f"P{number}", 0, number % 2, False) for number in range(1, 6)]
        for player in players:
            player.client, player.spawned = Recorder(), True
        viewer = players[0].client
        for tick in (10, 11):
            match.tick = tick
            match._show_players(players[0])

        def cards_sent():
            sent = [value["+0x80"]["+0x0"]["+0x0"] for _, value in viewer.reliable]
            viewer.reliable.clear()
            return sent

        cards_sent()
        match.tick = 10 + CARD_AGAIN_TICKS
        match._show_players(players[0])
        self.assertEqual(cards_sent(), [1, 2, 3, 4])  # its own and the three shown at tick 10
        match.tick += 1
        match._show_players(players[0])
        self.assertEqual(cards_sent(), [5])  # shown a tick later
        match.tick += 1
        match._show_players(players[0])
        self.assertEqual(cards_sent(), [])


class TeamListServerTests(unittest.TestCase):
    def setUp(self):
        self.server = GameServer("127.0.0.1", 0)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_teammates_see_each_other_in_the_team_list(self):
        alpha_card = card(1, "Alpha#1001", 0x0250000000000921, 25)
        players = [
            (1, "Alpha", 0, 0, False, alpha_card),
            (2, "Beta", 0, 0, False),
            (3, "Gamma", 0, 1, False),  # the enemy
        ]
        games = [Game(self.server.port, handoff) for handoff in self.server.create_match(PVP, players)]
        for game in games:
            self.addCleanup(game.sock.close)
            instance = game.join()
            self.assertEqual(instance["+0x78"], UNRANKED)
            game_struct = instance["+0x80"]["+0x0"]["+0x0"]
            self.assertEqual([team["+0x61"] for team in game_struct["+0xC0"]], [6, 6])

        # Alpha's game: its own card and entity with its account id, then Beta's and Gamma's entities
        # with theirs, each followed by a frame with 288D and no pick yet.
        found, entities = games[0].world(
            lambda found, entities: chunks_of(BETA, entities) and chunks_of(GAMMA, entities)
        )
        known = cards(found)
        self.assertEqual(known[1]["+0x40"], "Alpha#1001")
        self.assertEqual(known[1]["+0x38"], 25)
        self.assertEqual(known[2]["+0x40"], "Beta")
        self.assertEqual(known[2]["+0x0"], {"+0x0": 2, "+0x8": ID_HIGH})
        self.assertIn(3, known)
        creates = {item["entity"]: item["components"] for item in entities if item["op"] == OP_CREATE}
        self.assertEqual(creates[BETA][29], {0: (2, ID_HIGH), 1: "Beta"})  # his id and BattleTag
        self.assertEqual(creates[BETA][26], {0: 0x00840001})  # team 0
        self.assertEqual(creates[GAMMA][26], {0: 0x01040001})  # team 1: 288D leaves him out
        self.assertEqual(sorted(creates[BETA]), [26, 29, 74])
        choosing = heroselect.team_entry_frame(None)
        self.assertEqual(chunks_of(BETA, entities)[0], (choosing.value, choosing.count))
        (own,) = [item["components"] for item in entities if item["entity"] == ALPHA and item["components"]]
        self.assertEqual(own[29], {0: (1, ID_HIGH), 1: "Alpha#1001", 2: 25})  # 20500's account id, the level
        self.assertEqual(own[74][0], 0x0250000000000921)  # the frame the client loads for the list
        self.assertNotIn(0, creates[BETA][74])  # no card from the lobby: no frame

        # Beta picks Reaper: Alpha's game gets Beta's frame again, with the pick, and no new create.
        games[1].send_message(21616, pick(BETA, REAPER))
        expected = heroselect.team_entry_frame(REAPER)
        _, entities = games[0].world(
            lambda found, entities: (expected.value, expected.count) in chunks_of(BETA, entities)
        )
        self.assertNotIn(BETA, [item["entity"] for item in entities if item["op"] == OP_CREATE])

    def test_a_competitive_match_sends_frames_levels_and_ranks(self):
        # Every player entity names his portrait frame (74 field 0: the client loads it, so the team list
        # can draw it) and carries his level and, from the lobby on a competitive card, his rank (29:
        # rating, Top 500 place, tier); 20300 says the match is ranked. Each card comes once more.
        players = [
            (1, "Alpha", 0, 0, False, card(1, "Alpha#1001", 0x0250000000000F97, 3000, (4100, 0, 6))),
            (2, "Beta", 0, 0, False, card(2, "Beta#1002", 0x0250000000000921, 100, (2345, 17, 4))),
        ]
        with mock.patch("ow174.game.match.CARD_AGAIN_TICKS", 10):
            games = [Game(self.server.port, handoff) for handoff in self.server.create_match(PVP, players)]
            for game in games:
                self.addCleanup(game.sock.close)
                self.assertEqual(game.join()["+0x78"], RANKED)
            found, entities = games[0].world(lambda found, entities: len(cards_of(2, found)) == 2)
        self.assertNotIn(CARD_RANK, cards_of(2, found)[0]["+0x80"])  # the rank goes in 29, not in 20308
        creates = {item["entity"]: item["components"] for item in entities if item["op"] == OP_CREATE}
        self.assertEqual(creates[BETA][29], {0: (2, ID_HIGH), 1: "Beta#1002", 2: 100, 7: 2345, 8: 17, 9: 4})
        self.assertEqual(creates[BETA][74], {0: 0x0250000000000921, 2: 1, 6: 1, 7: 1})
        self.assertEqual(creates[ALPHA][29], {0: (1, ID_HIGH), 1: "Alpha#1001", 2: 3000, 7: 4100, 8: 0, 9: 6})
        self.assertEqual(creates[ALPHA][74][0], 0x0250000000000F97)

    def test_a_role_queue_player_can_pick_only_that_role(self):
        # The matchmaker's role goes in 74 field 5 (m_roleQueueRole: hero select offers only that role's
        # heroes), and the server refuses a pick of another role.
        tank = next(hero.guid for hero in heroes().values() if hero.role == "Tank")
        beta_card = card(2, "Beta#1002", 0x0250000000000921, 100)
        beta_card[CARD_ROLE] = 2
        players = [(1, "Alpha", 0, 0, False), (2, "Beta", 0, 0, False, beta_card)]
        games = [Game(self.server.port, handoff) for handoff in self.server.create_match(PVP, players)]
        for game in games:
            self.addCleanup(game.sock.close)
            game.join()
        found, entities = games[0].world(
            lambda found, entities: chunks_of(BETA, entities) and 2 in cards(found)
        )
        creates = {item["entity"]: item["components"] for item in entities if item["op"] == OP_CREATE}
        self.assertEqual(creates[BETA][74][5], 2)
        self.assertNotIn(CARD_ROLE, cards(found)[2])  # the role goes in 74, not in 20308

        games[1].send_message(21616, pick(BETA, REAPER))
        games[1].send_message(21616, pick(BETA, tank))
        picked = heroselect.team_entry_frame(tank)
        _, entities = games[0].world(
            lambda found, entities: (picked.value, picked.count) in chunks_of(BETA, entities)
        )
        reaper = heroselect.team_entry_frame(REAPER)
        self.assertNotIn((reaper.value, reaper.count), chunks_of(BETA, entities))

    def test_a_player_who_leaves_is_destroyed_for_the_others(self):
        players = [(1, "Alpha", 0, 0, False), (2, "Beta", 0, 0, False)]
        games = [Game(self.server.port, handoff) for handoff in self.server.create_match(PVP, players)]
        for game in games:
            self.addCleanup(game.sock.close)
            game.join()
        games[0].world(lambda found, entities: chunks_of(BETA, entities))
        match = self.server.matches[0]
        with self.server.lock:
            match.leave(match.players[1])
        games[0].world(lambda found, entities: (BETA, OP_DESTROY) in ops_of(entities))


if __name__ == "__main__":
    unittest.main()
