"""Building full teams from solo players and parties, and the queue cards' rules."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.content import PRACTICE_RANGE, game_map
from ow174.game.match import CARD_RANK, CARD_ROLE
from ow174.game.server import GameServer, OpenMatch
from ow174.jam.codec import Schemas
from ow174.jam.groups import HANDOFF, MATCH_LOBBY, MATCH_LOBBY_OUT, QUEUE
from ow174.lobby.handlers import build_router
from ow174.lobby.matchmaker import (
    FOUND_SECONDS,
    QUICK_PLAY,
    Matchmaker,
    Roster,
    Ruleset,
    Ticket,
    build_roster,
    game_address,
    load_rules,
)

TANK, DAMAGE, SUPPORT = 2, 1, 3
ROLE_QUEUE = 0x06300000000001B3  # season 32's competitive role queue card
SIX = Ruleset()
TWO_OF_EACH = {TANK: 2, DAMAGE: 2, SUPPORT: 2}
KINGS_ROW, HYBRID, DEATHMATCH = 0x08000000000000D4, 0x0230000000000016, 0x023000000000001E
ILIOS, CHATEAU_GUILLARD = 0x080000000000066D, 0x08000000000007A4  # control; deathmatch only


def tickets(*parties):
    """One ticket per party; a party is a list of the members' picked roles ([] = any)."""
    made = []
    for number, members in enumerate(parties):
        party = SimpleNamespace(name=f"party {number}")
        accounts = [(SimpleNamespace(name=f"p{number}.{k}"), roles) for k, roles in enumerate(members)]
        made.append(Ticket(party, {}, 1, number, accounts))
    return made


def sizes(roster):
    return [len(team) for team in roster.teams]


class GameAddressTests(unittest.TestCase):
    def test_players_outside_the_hosts_network_get_the_public_address(self):
        def sock(reached, peer):
            return SimpleNamespace(getsockname=lambda: (reached, 3724), getpeername=lambda: (peer, 50000))

        self.assertEqual(game_address("1.2.3.4", sock("192.168.1.5", "85.10.20.30")), "1.2.3.4")
        # The host's own game and its network's keep the address they reached the lobby at.
        self.assertEqual(game_address("1.2.3.4", sock("127.0.0.1", "127.0.0.1")), "127.0.0.1")
        self.assertEqual(game_address("1.2.3.4", sock("192.168.1.5", "192.168.1.7")), "192.168.1.5")
        self.assertEqual(game_address("", sock("192.168.1.5", "85.10.20.30")), "192.168.1.5")


class RosterTests(unittest.TestCase):
    def test_solo_players_and_parties_fill_both_teams(self):
        roster = build_roster(tickets([[]] * 4, [[]] * 2, [[]], [[]] * 3, [[]] * 2), SIX, {})
        self.assertEqual(sizes(roster), [6, 6])

    def test_nobody_plays_until_the_teams_are_full(self):
        self.assertIsNone(build_roster(tickets([[]] * 5, [[]] * 6), SIX, {}))

    def test_a_party_is_never_split(self):
        roster = build_roster(tickets([[]] * 4, [[]] * 4, [[]] * 2, [[]] * 2), SIX, {})
        for team in roster.teams:
            for ticket in {id(entry[0]): entry[0] for entry in team}.values():
                self.assertEqual(sum(1 for entry in team if entry[0] is ticket), len(ticket.members))

    def test_a_role_queue_needs_two_of_each_role(self):
        self.assertIsNone(build_roster(tickets(*[[[DAMAGE]]] * 12), SIX, TWO_OF_EACH))
        mixed = tickets(*([[[TANK]]] * 4 + [[[DAMAGE]]] * 4 + [[[SUPPORT]]] * 4))
        for team in build_roster(mixed, SIX, TWO_OF_EACH).teams:
            self.assertEqual(
                sorted(role for _, _, role in team), [DAMAGE, DAMAGE, TANK, TANK, SUPPORT, SUPPORT]
            )

    def test_flex_players_take_the_role_that_is_missing(self):
        parties = [[[TANK]]] * 2 + [[[DAMAGE]]] * 4 + [[[SUPPORT]]] * 4 + [[[TANK, DAMAGE, SUPPORT]]] * 2
        self.assertEqual(sizes(build_roster(tickets(*parties), SIX, TWO_OF_EACH)), [6, 6])

    def test_three_against_three_and_one_against_five(self):
        roster = build_roster(tickets([[]] * 2, [[]], [[]] * 3), Ruleset(team_sizes=(3, 3)), {})
        self.assertEqual(sizes(roster), [3, 3])
        yeti = build_roster(tickets([[]] * 5, [[]]), Ruleset(team_sizes=(1, 5)), {})
        self.assertEqual(sizes(yeti), [1, 5])

    def test_free_for_all(self):
        rules = Ruleset(team_sizes=(8,), free_for_all=True)
        self.assertIsNone(build_roster(tickets(*[[[]]] * 7), rules, {}))
        roster = build_roster(tickets(*[[[]]] * 9), rules, {})
        self.assertEqual(sum(sizes(roster)), 8)
        self.assertTrue(all(size == 1 for size in sizes(roster)))

    def test_a_test_minimum_starts_with_whoever_waits(self):
        self.assertEqual(sum(sizes(build_roster(tickets([[]]), SIX, TWO_OF_EACH, minimum=1))), 1)
        self.assertEqual(sizes(build_roster(tickets([[]], [[]]), SIX, {}, minimum=2)), [1, 1])

    def test_players_to_start_never_wait_for_more_than_the_mode_holds(self):
        deathmatch = Ruleset(team_sizes=(8,), free_for_all=True)
        self.assertEqual(sum(sizes(build_roster(tickets(*[[[]]] * 8), deathmatch, {}, minimum=12))), 8)
        duel = Ruleset(team_sizes=(1, 1))
        self.assertEqual(sizes(build_roster(tickets([[]], [[]]), duel, {}, minimum=4)), [1, 1])

    def test_players_to_start_takes_everyone_who_fits(self):
        roster = build_roster(tickets([[]], [[]], [[]] * 2, [[]]), SIX, {}, minimum=2)
        self.assertEqual(sorted(sizes(roster)), [2, 3])
        self.assertIsNone(build_roster(tickets([[]]), SIX, {}, minimum=2))

    def test_a_test_minimum_keeps_the_roles_picked(self):
        def roles(roster):
            return sorted(role for team in roster.teams for _, _, role in team)

        role_queue = build_roster(tickets([[TANK]], [[DAMAGE, SUPPORT]]), SIX, TWO_OF_EACH, minimum=2)
        self.assertEqual(roles(role_queue), [DAMAGE, TANK])
        self.assertEqual(roles(build_roster(tickets([[TANK]]), SIX, {}, minimum=1)), [0])


class RulesTests(unittest.TestCase):
    def test_quick_play_is_six_against_six_with_two_of_each_role(self):
        rules = load_rules()[QUICK_PLAY]
        self.assertEqual({ruleset.team_sizes for ruleset in rules.rulesets}, {(6, 6)})
        self.assertEqual(rules.roles, TWO_OF_EACH)
        self.assertTrue(rules.maps)

    def test_every_quick_play_map_can_be_hosted(self):
        rules = load_rules()[QUICK_PLAY]
        choices = [
            game_map(guid, ruleset.mode)
            for guid, modes in rules.maps
            for ruleset in rules.rulesets
            if ruleset.mode in modes
        ]
        self.assertTrue(all(choices), "a Quick Play map without placeables or spawns")

    def test_a_map_in_a_mode_has_that_modes_placeables_and_both_teams_spawns(self):
        hybrid = game_map(KINGS_ROW, HYBRID)
        self.assertTrue(hybrid.placeables)
        self.assertEqual({spawn.team for spawn in hybrid.spawns}, {0, 1})
        self.assertIsNone(game_map(KINGS_ROW, 0x0230000000000008))  # its loading needs a mode script

    def test_a_search_picks_a_map_of_the_card(self):
        server = SimpleNamespace(game=None)
        matchmaker = Matchmaker(server)
        chosen, ruleset = matchmaker._pick_map(matchmaker.rules_of(QUICK_PLAY))
        self.assertIn(chosen.mode_guid, {r.mode for r in matchmaker.rules_of(QUICK_PLAY).rulesets})
        self.assertEqual(chosen.mode_guid, ruleset.mode)

    def test_the_dashboards_map_wins_where_the_queues_modes_allow_it(self):
        matchmaker = Matchmaker(SimpleNamespace(game=None))
        rules = matchmaker.rules_of(QUICK_PLAY)
        matchmaker.forced_map = ILIOS
        chosen, ruleset = matchmaker._pick_map(rules)
        self.assertEqual((chosen.map_guid, chosen.mode_name), (ILIOS, "Control"))
        self.assertEqual(ruleset.mode, chosen.mode_guid)
        matchmaker.forced_map = CHATEAU_GUILLARD  # not in any Quick Play mode: the queue keeps its pick
        chosen, _ = matchmaker._pick_map(rules)
        self.assertNotEqual(chosen.map_guid, CHATEAU_GUILLARD)
        matchmaker.forced_map = PRACTICE_RANGE.map_guid  # loads in every queue
        chosen, _ = matchmaker._pick_map(rules)
        self.assertIs(chosen, PRACTICE_RANGE)


class PracticeTests(unittest.TestCase):
    def test_a_group_goes_to_one_practice_range_together(self):
        def account(number, virtual=False):
            return SimpleNamespace(account_lo=number, name=f"P{number}", virtual=virtual)

        leader, friend, busy, bot = account(1), account(2), account(3), account(9, virtual=True)
        party = SimpleNamespace(members=[leader, friend, busy, bot])
        sent = []

        def session(owner):
            return SimpleNamespace(
                account=owner,
                tournament=False,
                profile=None,
                ident=None,
                send=lambda *message: sent.append((owner.name, message[1])),
                log=lambda text: None,
                sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3724)),
            )

        sessions = {member.account_lo: session(member) for member in (leader, friend, busy)}
        created = []

        def create_match(game_map, players, card=0):
            created.append((game_map, players))
            handoff = {"match_id": (1, 2), "key_in": bytes(32), "key_out": bytes(32)}
            return [SimpleNamespace(conn=n, **handoff) for n in range(len(players))]

        server = SimpleNamespace(
            game=SimpleNamespace(create_match=create_match, in_match=lambda number: number == 3, port=3730),
            social=SimpleNamespace(
                party_of=lambda owner: party,
                open_match_chat=lambda *channel: {"match": 1},
                general={"general": 1},
            ),
            session_of=sessions.get,
            settings=SimpleNamespace(game_host=""),
            content=SimpleNamespace(
                menu_hero=SimpleNamespace(picked=lambda profile: 0),
                player=SimpleNamespace(record=lambda profile, ident: {}),
            ),
        )
        Matchmaker(server).practice(sessions[1])
        ((game_map, players),) = created
        self.assertEqual(game_map.map_guid, PRACTICE_RANGE.map_guid)
        self.assertEqual(game_map.team_sizes, (2, 0))  # the leader and the friend, not the busy or the bot
        self.assertEqual([(player[1], player[3]) for player in players], [("P1", 0), ("P2", 0)])
        self.assertEqual({name for name, message in sent if message == 20600}, {"P1", "P2"})
        # out of General (a match has the team, match and group chats), into the match chat, then the game
        self.assertEqual([message for name, message in sent if name == "P1"][-3:], [20404, 20402, 20600])
        self.assertEqual([CARD_RANK in player[5] for player in players], [False, False])


class CompetitiveCardTests(unittest.TestCase):
    def test_each_card_carries_the_rank_of_the_role_played(self):
        # The game server shows each player's rank in the team list (component 29): the lobby's rating
        # for the role the roster gave the player, asked of Ranked.rank with the role's GUID.
        tank, support = 0x0D80000000000850, 0x0D80000000000851
        asked = []

        def rank(profile, card, role):
            asked.append((profile, card, role))
            return {tank: (4100, 0, 6), support: (2345, 17, 4)}[role]

        accounts = [SimpleNamespace(account_lo=number, name=f"P{number}") for number in (1, 2)]
        sessions = {
            account.account_lo: SimpleNamespace(
                account=account,
                tournament=False,
                profile=f"profile {account.name}",
                ident=None,
                send=lambda *message: None,
                log=lambda text: None,
                sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3724)),
            )
            for account in accounts
        }
        created = []

        def create_match(game_map, players, card=0):
            created.append(players)
            handoff = {"match_id": (1, 2), "key_in": bytes(32), "key_out": bytes(32)}
            return [SimpleNamespace(conn=n, **handoff) for n in range(len(players))]

        server = SimpleNamespace(
            game=SimpleNamespace(create_match=create_match, port=3730),
            social=SimpleNamespace(open_match_chat=lambda *channel: {}, general={}),
            session_of=sessions.get,
            settings=SimpleNamespace(game_host=""),
            content=SimpleNamespace(
                menu_hero=SimpleNamespace(picked=lambda profile: 0),
                player=SimpleNamespace(record=lambda profile, ident: {"+0x40": profile}),
                ranked=SimpleNamespace(rank=rank),
            ),
        )
        ticket = Ticket(SimpleNamespace(), {}, ROLE_QUEUE, 0.0)
        roster = Roster([[(ticket, accounts[0], 2), (ticket, accounts[1], 3)]])  # tank, support
        Matchmaker(server)._start(PRACTICE_RANGE, roster, popped=False, card=ROLE_QUEUE)
        ((first, second),) = created
        self.assertEqual(first[5], {"+0x40": "profile P1", CARD_RANK: (4100, 0, 6), CARD_ROLE: 2})
        self.assertEqual(second[5], {"+0x40": "profile P2", CARD_RANK: (2345, 17, 4), CARD_ROLE: 3})
        self.assertEqual(asked, [("profile P1", ROLE_QUEUE, tank), ("profile P2", ROLE_QUEUE, support)])


class OpenMatchTests(unittest.TestCase):
    """A party that searches joins a match of the card that has room."""

    def setUp(self):
        self.joined, self.created, self.rooms, self.sent, self.sessions = [], [], [], [], {}

        def handoffs(numbers, match_id):
            keys = {"key_in": bytes(32), "key_out": bytes(32)}
            return [SimpleNamespace(conn=n, match_id=match_id, **keys) for n in numbers]

        def join_match(match, players):
            self.joined.append(players)
            return handoffs(range(len(players)), (1, 2))

        def create_match(game_map, players, card=0):
            self.created.append(players)
            return handoffs(range(len(players)), (3, 4))

        self.server = SimpleNamespace(
            game=SimpleNamespace(
                open_matches=lambda card: list(self.rooms),
                elsewhere=lambda account_lo, match_id: None,
                join_match=join_match,
                create_match=create_match,
                port=3730,
            ),
            session_of=self.sessions.get,
            social=SimpleNamespace(open_match_chat=lambda *channel: {}, general={}),
            settings=SimpleNamespace(game_host=""),
            content=SimpleNamespace(
                menu_hero=SimpleNamespace(picked=lambda profile: 0),
                player=SimpleNamespace(record=lambda profile, ident: {}),
                ranked=SimpleNamespace(rank=lambda profile, card, role: None),
            ),
            notify_party=lambda party: None,
            state_lock=threading.RLock(),
            match_lobbies={},
        )
        self.matchmaker = Matchmaker(self.server)
        self.later = []
        self.matchmaker.schedule = lambda seconds, function: self.later.append(function)

    def party(self, *picks):
        """A party whose members picked these roles each ([] = any)."""
        members = []
        for roles in picks:
            number = len(self.sessions) + 1
            account = SimpleNamespace(account_lo=number, name=f"P{number}", virtual=False)
            self.sessions[number] = SimpleNamespace(
                account=account,
                tournament=False,
                profile=None,
                ident=None,
                send=lambda *message, name=account.name: self.sent.append((name, message[1])),
                log=lambda text: None,
                sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3724)),
                logged_in=True,
            )
            members.append((account, list(roles)))
        return SimpleNamespace(
            members=[account for account, _ in members],
            leader=members[0][0],
            roles={account.account_lo: roles for account, roles in members},
            queue=None,
            queue_state=0,
            accepted=set(),
            ready=set(),
            pass_roles={},
            passes_taken=set(),
        )

    def room(self, free, roles=None, waiting=True):
        match = SimpleNamespace(game_map=SimpleNamespace(name="Ilios"))
        self.rooms.append(OpenMatch(match, waiting, list(free), roles or [{} for _ in free], 0.0))

    def teams_joined(self):
        return [[player[3] for player in players] for players in self.joined]

    def test_a_party_joins_a_waiting_match_on_the_team_with_the_most_room(self):
        self.room([1, 3])
        self.matchmaker.search(self.party([], []), {}, QUICK_PLAY)
        self.assertEqual(self.teams_joined(), [[1, 1]])
        self.assertEqual((self.created, self.matchmaker.tickets), ([], []))
        self.assertIn(("P2", 53003), self.sent)  # "Game Found!" first, the game after it
        self.assertNotIn(("P2", 20600), self.sent)
        (hand_over,) = self.later
        hand_over()
        self.assertIn(("P2", 20600), self.sent)

    def test_a_party_that_fits_no_team_whole_waits(self):
        self.room([2, 2])
        self.matchmaker.search(self.party([], [], []), {}, QUICK_PLAY)
        self.assertEqual((self.joined, len(self.matchmaker.tickets)), ([], 1))

    def test_a_match_that_is_on_takes_players_only_when_the_dashboard_says_so(self):
        self.room([1, 0], waiting=False)
        self.matchmaker.search(self.party([]), {}, QUICK_PLAY)
        self.assertEqual(self.joined, [])
        self.matchmaker.set_mode(QUICK_PLAY, 0, True)
        self.assertEqual(self.teams_joined(), [[0]])

    def test_a_competitive_match_that_is_on_never_takes_players(self):
        self.room([1, 0], waiting=False)
        self.matchmaker.set_mode(ROLE_QUEUE, 0, True)
        self.matchmaker.search(self.party([DAMAGE]), {}, ROLE_QUEUE)
        self.assertEqual(self.joined, [])
        self.room([1, 0])  # one still waiting for its players takes them
        self.matchmaker.retry()
        self.assertEqual(self.teams_joined(), [[0]])

    def test_a_role_queue_player_joins_the_team_missing_that_role(self):
        self.room([1, 1], roles=[{TANK: 2, DAMAGE: 2, SUPPORT: 1}, {TANK: 2, DAMAGE: 1, SUPPORT: 2}])
        self.matchmaker.search(self.party([DAMAGE]), {}, QUICK_PLAY)
        ((player,),) = self.joined
        self.assertEqual((player[3], player[5][CARD_ROLE]), (1, DAMAGE))

    def test_the_cards_own_settings_are_saved_and_read_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "matchmaking.json"
            Matchmaker(self.server, 0, path).set_mode(QUICK_PLAY, 4, True)
            again = Matchmaker(self.server, 2, path)
            self.assertEqual((again.players_to_start(QUICK_PLAY), again.fills_running(QUICK_PLAY)), (4, True))
            self.assertEqual(again.players_to_start(ROLE_QUEUE), 2)  # the number for every card


KEY = {  # a Quick Play queue key as the client sends it in 44100
    "+0x0": {"+0x0": QUICK_PLAY, "+0x8": 0, "+0x10": 0, "+0x18": 0},
    "+0x20": 0,
    "+0x24": 15959616,
    "+0x28": 16025156,
}


class GameFoundTests(unittest.TestCase):
    """A queue match shows "Game Found!" before its 20600; the Practice Range goes at once."""

    def setUp(self):
        self.sent, self.later, self.sessions, self.parties = [], [], {}, {}
        self.seats = {}  # connection -> (handoff, player, deadline), as GameServer.handoffs keeps them

        def create_match(game_map, players, card=0):
            made = []
            for account_lo, *_ in players:
                keys = {"key_in": bytes(32), "key_out": bytes(32)}
                handoff = SimpleNamespace(conn=account_lo, match_id=(7, 8), **keys)
                self.seats[handoff.conn] = (handoff, account_lo, time.time() + 60)
                made.append(handoff)
            return made

        self.server = SimpleNamespace(
            game=SimpleNamespace(
                create_match=create_match,
                open_matches=lambda card: [],
                in_match=lambda account_lo: False,
                elsewhere=lambda account_lo, match_id: None,
                port=3730,
                lock=threading.RLock(),
                handoffs=self.seats,
            ),
            session_of=self.sessions.get,
            social=SimpleNamespace(
                open_match_chat=lambda match_id, accounts: {"match": match_id},
                general={"general": 1},
                party_of=lambda account: self.parties[account.account_lo],
            ),
            settings=SimpleNamespace(game_host=""),
            content=SimpleNamespace(
                menu_hero=SimpleNamespace(picked=lambda profile: 0),
                player=SimpleNamespace(record=lambda profile, ident: {}),
                ranked=SimpleNamespace(rank=lambda profile, card, role: None),
            ),
            notify_party=lambda party: None,
            state_lock=threading.RLock(),
            match_lobbies={},
        )
        self.matchmaker = Matchmaker(self.server, minimum_players=2)
        self.matchmaker.schedule = lambda seconds, function: self.later.append((seconds, function))

    def party(self):
        """A party of one."""
        number = len(self.sessions) + 1
        account = SimpleNamespace(account_lo=number, name=f"P{number}", virtual=False)
        self.sessions[number] = SimpleNamespace(
            account=account,
            tournament=False,
            profile=None,
            ident=None,
            logged_in=True,
            send=lambda crc, msg_id, value, name=account.name: self.sent.append((name, crc, msg_id, value)),
            log=lambda text: None,
            sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3724)),
        )
        self.parties[number] = SimpleNamespace(
            members=[account],
            leader=account,
            roles={number: []},
            queue=None,
            queue_state=0,
            accepted=set(),
            ready=set(),
            pass_roles={},
            passes_taken=set(),
        )
        return self.parties[number]

    def pop(self):
        """Two players search Quick Play, and two start a match (players to start)."""
        first, second = self.party(), self.party()
        self.matchmaker.search(first, KEY, QUICK_PLAY)
        self.matchmaker.search(second, KEY, QUICK_PLAY)
        return first, second

    def ids(self, name):
        return [msg_id for who, _, msg_id, _ in self.sent if who == name]

    def value(self, name, wanted):
        return next(value for who, _, msg_id, value in self.sent if who == name and msg_id == wanted)

    def hand_over(self):
        for _, function in self.later:
            function()
        self.later.clear()

    def test_a_queue_match_shows_game_found_then_goes_to_the_game(self):
        self.pop()
        self.assertEqual([self.ids(name) for name in ("P1", "P2")], [[44202, 53000, 53003]] * 2)
        crcs = [crc for who, crc, _, _ in self.sent if who == "P1"]
        self.assertEqual(crcs, [QUEUE, MATCH_LOBBY, MATCH_LOBBY])
        # the client shows "Game Found!" for 5 s (0x7FF789693562); the game comes inside that time
        self.assertEqual([seconds for seconds, _ in self.later], [FOUND_SECONDS])
        self.assertLess(FOUND_SECONDS, 5)
        lobby, left = self.value("P1", 53000), self.value("P1", 53003)
        # the match lobby of a popped queue (type 4) with the party's queue key, the match id as the
        # lobby id and the ticket that 44201 gave the queue entry (0): the client answers 52903 true
        self.assertEqual(lobby["+0x78"]["+0x0"], KEY)
        self.assertEqual((lobby["+0x78"]["+0x30"], lobby["+0x78"]["+0x60"]), ({"+0x0": [7, 8]}, 4))
        self.assertEqual(lobby["+0xE8"], 0)
        # leaving it with reason 4 is "Game Found!"; +0x88 other than 0 would be "Leaving game"
        self.assertEqual((left["+0x88"], left["+0xA0"]), (0, 4))
        schemas = Schemas()
        again = schemas.decode(MATCH_LOBBY, 53000, schemas.encode(MATCH_LOBBY, 53000, lobby))
        self.assertEqual((again["+0x78"]["+0x0"], again["+0x78"]["+0x60"]), (KEY, 4))
        self.assertEqual(schemas.decode(MATCH_LOBBY, 53003, schemas.encode(MATCH_LOBBY, 53003, left)), left)
        self.hand_over()
        self.assertEqual([self.ids(name)[3:] for name in ("P1", "P2")], [[20404, 20402, 20600]] * 2)
        self.assertEqual(self.value("P2", 20600)["+0x80"]["+0x28"], 2)  # its own connection
        self.assertEqual(self.matchmaker.found, {})

    def test_without_a_found_step_the_game_follows_at_once(self):
        self.matchmaker.found_seconds = 0
        self.pop()
        self.assertEqual(self.ids("P1"), [44202, 20404, 20402, 20600])
        self.assertEqual(self.later, [])

    def test_the_practice_range_goes_at_once(self):
        self.party()
        self.matchmaker.practice(self.sessions[1])
        self.assertEqual(self.ids("P1"), [20404, 20402, 20600])
        self.assertEqual(self.later, [])

    def test_a_player_who_quits_during_game_found_gives_the_seat_back(self):
        _, second = self.pop()
        self.sessions[2].logged_in = False  # what Session._cleanup does first, before it cancels
        self.matchmaker.cancel(second)
        self.assertEqual(self.seats[2][2], 0.0)  # the game server drops it on its next tick
        self.assertGreater(self.seats[1][2], time.time())
        self.hand_over()
        self.assertIn(20600, self.ids("P1"))
        self.assertNotIn(20600, self.ids("P2"))

    def test_a_connection_lost_without_a_cancel_is_seen_at_the_handoff(self):
        self.pop()
        del self.sessions[2]  # say another login took the account over
        self.hand_over()
        self.assertIn((HANDOFF, 20600), [(crc, msg_id) for who, crc, msg_id, _ in self.sent if who == "P1"])
        self.assertNotIn(20600, self.ids("P2"))
        self.assertEqual(self.seats[2][2], 0.0)

    def test_a_send_that_fails_at_the_handoff_frees_that_seat_and_the_others_still_go(self):
        self.pop()

        def closed(crc, msg_id, value):
            raise ConnectionResetError("the connection is closed")

        self.sessions[1].send, self.sessions[1].log = closed, lambda *text: None
        self.hand_over()
        self.assertEqual(self.seats[1][2], 0.0)
        self.assertIn(20600, self.ids("P2"))

    def test_a_freed_seat_leaves_the_game_servers_match_at_its_next_tick(self):
        left = []
        game = GameServer("127.0.0.1", 0, on_leave=left.append)
        (handoff,) = game.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        Matchmaker(SimpleNamespace(game=game))._free_seat(handoff)
        game._tick(time.time())
        game._report_leaves()  # in the server this is LobbyServer.game_left, which places waiting parties
        self.assertEqual(([player.account_lo for player in left], game.handoffs), ([1], {}))

    def test_the_lobby_logs_the_clients_answer_to_the_match_lobby(self):
        logged = []
        build_router().get(MATCH_LOBBY_OUT, 52903)(SimpleNamespace(log=logged.append), {"+0x78": True})
        self.assertEqual(logged, ['[MM] "Game Found!" match lobby answer (52903): True'])


if __name__ == "__main__":
    unittest.main()
