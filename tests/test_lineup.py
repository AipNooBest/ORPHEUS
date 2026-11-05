"""The VERSUS loading screen of a competitive match: the match lobby's settings (53001) and roster (53002)
that go with "Game Found!" (lobby/lineup.py)."""

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_game_team_list import PVP, card

from ow174.game.match import CARD_RANK, CARD_ROLE
from ow174.game.server import GameServer
from ow174.jam.codec import Schemas
from ow174.jam.groups import MATCH_LOBBY
from ow174.jam.values import id16
from ow174.lobby import lineup
from ow174.lobby.matchmaker import QUICK_PLAY, Matchmaker
from ow174.lobby.server import LobbyServer

KEY = {"+0x0": {"+0x0": QUICK_PLAY, "+0x8": 0, "+0x10": 0, "+0x18": 0}, "+0x20": 0, "+0x24": 1, "+0x28": 2}
MATCH_ID = (7, 8)
OTHER_CARD = 0x06300000000001B3
TANK = 2


def rating(card_guid: int, sr: int) -> dict:
    """A rating struct as Ranked.party_ratings makes it (20700 +0x68)."""
    entry = {
        "+0x0": 0x0D80000000000850,
        "+0x8": 1,
        "+0xC": 2,
        "+0x10": sr,
        "+0x12": 0,
        "+0x14": sr,
        "+0x16": 3,
    }
    return {"+0x0": [entry], "+0x18": card_guid, "+0x20": 0}


class FakeServer:
    """What the matchmaker and lineup.roster use of the lobby server, with a game that keeps its players."""

    def __init__(self, ranked: bool = True):
        self.sent, self.later, self.sessions, self.parties, self.made = [], [], {}, {}, []

        def create_match(game_map, players, card=0):
            """As GameServer: the match keeps each card without the rank and the role (match.Player)."""
            handoffs = []
            for account_lo, _name, _hero, team, _tournament, player_card in players:
                kept = dict(player_card)
                kept.pop(CARD_RANK, None)
                self.made.append((account_lo, team, kept.pop(CARD_ROLE, 0), kept))
                keys = {"key_in": bytes(32), "key_out": bytes(32)}
                handoffs.append(SimpleNamespace(conn=account_lo, match_id=MATCH_ID, **keys))
            return handoffs

        def lineup_of(match_id):
            return [made for made in self.made if match_id == MATCH_ID]

        self.game = SimpleNamespace(
            create_match=create_match,
            lineup=lineup_of,
            open_matches=lambda card: [],
            in_match=lambda account_lo: False,
            elsewhere=lambda account_lo, match_id: None,
            port=3730,
            lock=threading.RLock(),
            handoffs={},
        )
        self.session_of = self.sessions.get
        self.social = SimpleNamespace(
            open_match_chat=lambda match_id, accounts: {"match": match_id},
            general={"general": 1},
            party_of=lambda account: self.parties[account.account_lo],
        )
        self.settings = SimpleNamespace(game_host="")
        self.content = SimpleNamespace(
            menu_hero=SimpleNamespace(picked=lambda profile: 0),
            player=SimpleNamespace(record=lambda profile, ident: {"+0x40": f"P{profile}#1"}),
            ranked=SimpleNamespace(
                rank=lambda profile, card, role: (2500, 0, 3) if ranked else None,
                party_ratings=lambda profile: [rating(OTHER_CARD, 1), rating(QUICK_PLAY, 2000 + profile)],
            ),
        )
        self.notify_party = lambda party: None
        self.state_lock = threading.RLock()
        self.match_lobbies = {}

    def party(self):
        number = len(self.sessions) + 1
        account = SimpleNamespace(account_lo=number, name=f"P{number}", virtual=False)
        self.sessions[number] = SimpleNamespace(
            account=account,
            tournament=False,
            profile=number,
            ident=None,
            logged_in=True,
            send=lambda crc, msg_id, value, name=account.name: self.sent.append((name, crc, msg_id, value)),
            log=lambda text: None,
            sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3724)),
        )
        self.parties[number] = SimpleNamespace(
            members=[account],
            leader=account,
            party_id=(0x500 + number, 0x600),
            roles={number: []},
            queue=None,
            queue_state=0,
            accepted=set(),
            ready=set(),
            pass_roles={},
            passes_taken=set(),
        )
        return self.parties[number]

    def ids(self, name):
        return [msg_id for who, _, msg_id, _ in self.sent if who == name]

    def value(self, name, wanted):
        return next(value for who, _, msg_id, value in self.sent if who == name and msg_id == wanted)


def pop(server: FakeServer) -> Matchmaker:
    matchmaker = Matchmaker(server, minimum_players=2)
    matchmaker.schedule = lambda seconds, function: server.later.append((seconds, function))
    for party in (server.party(), server.party()):
        matchmaker.search(party, KEY, QUICK_PLAY)
    return matchmaker


class GameFoundLineupTests(unittest.TestCase):
    def test_a_competitive_match_makes_the_lobby_ranked_and_sends_both_teams(self):
        server = FakeServer()
        pop(server)
        # 53001 between 53000 (the lobby must have type 4) and 53003; the roster after 53003, which empties
        # the lobby (0x7FF789D2E5B0)
        self.assertEqual(
            [server.ids(name) for name in ("P1", "P2")], [[44202, 53000, 53001, 53003, 53002]] * 2
        )
        settings = server.value("P1", 53001)["+0x78"]
        self.assertEqual((settings["+0x2A4"], settings["+0x278"]), (lineup.RANKED, id16(*MATCH_ID)))
        records = server.value("P2", 53002)["+0x78"]
        self.assertEqual(sorted(record["+0xA8"] for record in records), [0, 1])  # one player per team
        first = next(record for record in records if record["+0x0"]["+0x40"] == "P1#1")
        self.assertEqual(first["+0x68"], rating(QUICK_PLAY, 2001))  # the rating on the queue card
        self.assertEqual(first["+0x90"], id16(0x501, 0x600))  # the party
        self.assertEqual(first["+0xAF"], 0)
        self.assertNotIn(CARD_RANK, first["+0x0"])
        # the game still follows after "Game Found!"
        for _, function in server.later:
            function()
        self.assertEqual(server.ids("P1")[5:], [20404, 20402, 20600])

    def test_an_unranked_match_gets_no_lineup(self):
        server = FakeServer(ranked=False)
        pop(server)
        self.assertEqual(server.ids("P1"), [44202, 53000, 53003])

    def test_the_settings_and_the_roster_go_through_the_real_schemas(self):
        server = FakeServer()
        pop(server)
        schemas = Schemas()
        for msg_id in (53001, 53002):
            value = server.value("P1", msg_id)
            again = schemas.decode(MATCH_LOBBY, msg_id, schemas.encode(MATCH_LOBBY, msg_id, value))
            if msg_id == 53001:
                self.assertEqual((again["+0x78"]["+0x2A4"], again["+0x78"]["+0x278"]), (9, id16(*MATCH_ID)))
                continue
            for sent, read in zip(value["+0x78"], again["+0x78"], strict=True):
                self.assertEqual(read["+0x0"]["+0x40"], sent["+0x0"]["+0x40"])
                self.assertEqual(
                    (read["+0xA8"], read["+0xB1"], read["+0xA0"]), (sent["+0xA8"], sent["+0xB1"], 0)
                )
                self.assertEqual(read["+0x68"]["+0x0"][0]["+0x10"], sent["+0x68"]["+0x0"][0]["+0x10"])


class RosterTests(unittest.TestCase):
    def test_a_player_without_a_lobby_session_keeps_his_card_without_a_rating(self):
        server = FakeServer()
        server.made = [(9, 1, TANK, {"+0x40": "Gone#9"})]
        (record,) = lineup.roster(server, MATCH_ID, QUICK_PLAY)["+0x78"]
        self.assertEqual((record["+0x0"], record["+0x68"]), ({"+0x40": "Gone#9"}, lineup.NO_RATING))
        self.assertEqual((record["+0xA8"], record["+0xB1"], record["+0x90"]), (1, TANK, id16(9, 0)))

    def test_slots_count_per_team(self):
        server = FakeServer()
        server.made = [(10 + k, k % 2, 0, {}) for k in range(5)]
        records = lineup.roster(server, MATCH_ID, QUICK_PLAY)["+0x78"]
        self.assertEqual(
            [(r["+0xA8"], r["+0xAF"]) for r in records], [(0, 0), (1, 0), (0, 1), (1, 1), (0, 2)]
        )

    def test_the_game_server_lists_the_players_still_in_the_match(self):
        game = GameServer("127.0.0.1", 0)
        players = [
            (1, "Alpha", 0, 0, False, card(1, "Alpha#1", 0, 25, rank=(2500, 0, 3)) | {CARD_ROLE: TANK}),
            (2, "Beta", 0, 1, False, card(2, "Beta#2", 0, 30)),
        ]
        handoffs = game.create_match(PVP, players)
        game.matches[0].players[1].gone = True
        (only,) = game.lineup(handoffs[0].match_id)
        self.assertEqual(only[:3], (1, 0, TANK))
        self.assertEqual(only[3]["+0x40"], "Alpha#1")
        self.assertNotIn(CARD_RANK, only[3])  # the match keeps the rank apart (match.Player)
        self.assertEqual(game.lineup((0, 0)), [])

    def test_leaving_a_ranked_match_empties_the_roster(self):
        """Also for a game that never connected: it had no match lobby of its own (handlers/leaving.py)."""
        sent = []
        session = SimpleNamespace(
            account="A", send=lambda crc, msg_id, value: sent.append((crc, msg_id, value))
        )
        lobby = SimpleNamespace(
            session_of={1: session}.get,
            state_lock=threading.RLock(),
            social=SimpleNamespace(leave_match_chat=lambda account, match_id: None, party_of=lambda a: None),
            notify_party=lambda party: None,
            match_lobbies={},
        )
        for ranked in (True, False):
            match = SimpleNamespace(id=MATCH_ID, ranked=ranked, card=0)
            LobbyServer.game_left(lobby, SimpleNamespace(account_lo=1, match=match))
        self.assertEqual(sent, [(MATCH_LOBBY, 53002, lineup.NO_ROSTER)])


if __name__ == "__main__":
    unittest.main()
