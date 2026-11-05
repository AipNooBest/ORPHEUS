"""Leaving a match: "Leave" and "Leave as group" (52902), 21801, the match lobby a game gets while it plays
(53000 / 53003), and the game server taking players out of their match (20304)."""

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.accounts.profile import Profile, save_profile
from ow174.accounts.registry import Accounts
from ow174.catalog.items import ItemDB
from ow174.catalog.templates import RetailTemplates
from ow174.content import Content
from ow174.game import messages
from ow174.game import server as server_module
from ow174.game.bits import BitReader
from ow174.game.content import PRACTICE_RANGE
from ow174.game.link import ACK, FIN, KIND_FRAME, SYN, LinkCipher, Packet
from ow174.game.server import GameServer
from ow174.jam.codec import Schemas
from ow174.jam.groups import MATCH_LOBBY, MATCH_LOBBY_OUT, OUT_CONNECT, PARTY
from ow174.lobby.handlers import build_router, leaving
from ow174.services.social import Social


class PretendGame:
    """The client's side of the game link, as far as these tests need it."""

    def __init__(self, port: int, handoff) -> None:
        self.address = ("127.0.0.1", port)
        self.conn = handoff.conn
        self.cipher = LinkCipher(key_in=handoff.key_out, key_out=handoff.key_in)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2.0)
        self.seq = 0
        self.expected = 0

    def send(self, flags: int) -> None:
        self.sock.sendto(
            self.cipher.seal(Packet(self.conn, 0, self.seq, self.expected, 1, flags)), self.address
        )
        self.seq += 1

    def connect(self) -> None:
        self.send(SYN)
        self.receive()
        self.send(ACK)

    def receive(self) -> Packet:
        data, _ = self.sock.recvfrom(4096)
        packet = self.cipher.open(data)
        self.expected = packet.seq + 1
        return packet

    def first_reliable(self, seconds: float = 3.0) -> int:
        """The id of the first reliable message the server sends."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            packet = self.receive()
            if packet.kind != KIND_FRAME:
                continue
            reader = BitReader(packet.payload)
            reader.var_a()
            reliable, _ = messages.read_channel(reader)
            if reliable:
                return reliable[0][1]
        raise AssertionError("no reliable message in time")

    def reliable_until(self, wanted: int, seconds: float = 3.0) -> list[int]:
        seen = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            packet = self.receive()
            if packet.kind != KIND_FRAME:
                continue
            reader = BitReader(packet.payload)
            reader.var_a()
            reliable, _ = messages.read_channel(reader)
            seen += [msg_id for _, msg_id, _ in reliable]
            if wanted in seen:
                return seen
        raise AssertionError(f"{wanted} not seen in time: {seen}")


def wait_for(condition, seconds: float = 3.0) -> None:
    deadline = time.time() + seconds
    while not condition():
        if time.time() > deadline:
            raise AssertionError("not in time")
        time.sleep(0.02)


class TakeOutTests(unittest.TestCase):
    """GameServer.take_out and the join report, with a pretend game on UDP."""

    def setUp(self):
        self.left, self.joined = [], []
        self.server = GameServer("127.0.0.1", 0, on_leave=self.left.append, on_join=self.joined.append)
        self.server.start()
        self.addCleanup(self.server.stop)

    def game(self, handoff) -> PretendGame:
        game = PretendGame(self.server.port, handoff)
        self.addCleanup(game.sock.close)
        return game

    def test_a_connected_game_is_reported_and_starts_with_20300(self):
        (handoff,) = self.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        game = self.game(handoff)
        game.connect()
        self.assertEqual(game.first_reliable(), 20300)
        wait_for(lambda: [player.name for player in self.joined] == ["Alpha"])

    def test_a_game_taken_out_gets_20304_and_leaves_with_its_fin(self):
        (handoff,) = self.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        game = self.game(handoff)
        game.connect()
        game.first_reliable()
        player = self.server.player_of(1)
        self.assertEqual(self.server.take_out(player, "left"), "sent to the menu")
        self.assertTrue(self.server.going_home(player))
        self.assertIn(server_module.LEAVE_GAME, game.reliable_until(server_module.LEAVE_GAME))
        self.assertIs(self.server.player_of(1), player)  # in the match until its link closes
        game.send(FIN)
        wait_for(lambda: self.left == [player])
        self.assertIsNone(self.server.player_of(1))

    def test_a_game_that_stays_after_20304_is_dropped(self):
        with mock.patch.object(server_module, "LEAVE_GRACE_SECONDS", 0.3):
            (handoff,) = self.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
            game = self.game(handoff)
            game.connect()
            game.first_reliable()
            player = self.server.player_of(1)
            self.server.take_out(player, "left")
            wait_for(lambda: self.left == [player])
        wait_for(lambda: self.server.matches == [])  # the match ends with its last player

    def test_a_player_taken_out_before_his_game_connects_leaves_at_once_and_only_gets_20304(self):
        (handoff,) = self.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        player = self.server.player_of(1)
        self.assertEqual(self.server.take_out(player, "left"), "left before its game connected")
        wait_for(lambda: self.left == [player])
        game = self.game(handoff)
        game.connect()
        self.assertEqual(game.first_reliable(), server_module.LEAVE_GAME)
        time.sleep(0.1)
        self.assertEqual(self.joined, [])
        self.assertIsNone(player.client)

    def test_send_home_also_drops_a_game_that_stays(self):
        with mock.patch.object(server_module, "LEAVE_GRACE_SECONDS", 0.3):
            (handoff,) = self.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
            game = self.game(handoff)
            game.connect()
            game.first_reliable()
            self.assertEqual(self.server.send_home(1), 1)
            player = self.server.player_of(1)
            self.assertTrue(self.server.going_home(player))
            wait_for(lambda: self.left == [player])


class FakeGame:
    """The game server as the leave handlers use it."""

    def __init__(self):
        self.players = {}  # account_lo -> SimpleNamespace player
        self.taken_out = []  # (name, reason)
        self.home = set()

    def add(self, account, match) -> SimpleNamespace:
        player = SimpleNamespace(account_lo=account.account_lo, name=account.name, match=match)
        self.players[account.account_lo] = player
        return player

    def player_of(self, account_lo):
        return self.players.get(account_lo)

    def take_out(self, player, reason):
        self.taken_out.append((player.name, reason))
        self.home.add(player.account_lo)
        return "sent to the menu"

    def going_home(self, player):
        return player.account_lo in self.home


def fake_match(number: int) -> SimpleNamespace:
    return SimpleNamespace(id=(number, number + 100), label=lambda: f"match {number}")


class LeaveHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = Schemas()
        cls.content = Content(cls.schemas, RetailTemplates(), ItemDB())

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        save_profile(Profile(), root / "template.json")
        self.accounts = Accounts(root / "profiles", root / "template.json")
        self.social = Social(self.accounts, self.content)
        self.game = FakeGame()
        self.notified = []
        self.server = SimpleNamespace(
            social=self.social,
            game=self.game,
            match_lobbies={},
            notify_party=self.notified.append,
            session_of=lambda account_lo: self.social.sessions.get(account_lo),
        )
        self.sent = {}
        self.alpha, self.beta, self.gamma = (self.session(name) for name in ("Alpha", "Beta", "Gamma"))
        self.match = fake_match(1)

    def session(self, name):
        account = self.accounts.get(name)
        sent = self.sent.setdefault(name, [])

        def party_messages():
            return [(PARTY, 20700, self.social.party_state(self.social.party_of(account)))]

        def send(crc, msg_id, value):
            self.schemas.encode(crc, msg_id, value)
            sent.append((crc, msg_id, value))
            return True

        session = SimpleNamespace(
            account=account,
            server=self.server,
            log=lambda *args: None,
            send=send,
            send_all=lambda items: [send(*item) for item in items],
            party_messages=party_messages,
        )
        self.social.sessions[account.account_lo] = session
        return session

    def party(self, *sessions):
        party = self.social.party_of(sessions[0].account)
        for session in sessions[1:]:
            self.social.join(session.account, party)
        return party

    def leave(self, session, as_group, match=None, request=False):
        match = match or self.match
        value = {"+0x78": {"+0x0": list(match.id)}, "+0x88": as_group, "+0x89": request}
        body = self.schemas.encode(MATCH_LOBBY_OUT, leaving.LEAVE_MATCH_LOBBY, value)
        leaving.leave_match_lobby(
            session, self.schemas.decode(MATCH_LOBBY_OUT, leaving.LEAVE_MATCH_LOBBY, body)
        )

    def test_the_handlers_are_registered(self):
        router = build_router()
        self.assertIs(router.get(MATCH_LOBBY_OUT, 52902), leaving.leave_match_lobby)
        self.assertIs(router.get(OUT_CONNECT, 21801), leaving.leaving_game)

    def test_the_leader_takes_the_party_that_is_in_his_match_and_it_stays_a_party(self):
        party = self.party(self.alpha, self.beta, self.gamma)
        for session in (self.alpha, self.beta):
            self.game.add(session.account, self.match)
        self.game.add(self.gamma.account, fake_match(2))  # another match: stays
        self.leave(self.alpha, True)
        self.assertEqual(
            self.game.taken_out, [("Alpha", "left with the group"), ("Beta", "left with the group")]
        )
        self.assertEqual(party.members, [self.alpha.account, self.beta.account, self.gamma.account])
        self.assertIs(party.leader, self.alpha.account)
        self.assertEqual(self.notified, [])

    def test_a_member_not_in_a_match_stays_in_the_menu_and_in_the_party(self):
        party = self.party(self.alpha, self.beta)
        self.game.add(self.alpha.account, self.match)
        self.leave(self.alpha, True)
        self.assertEqual(self.game.taken_out, [("Alpha", "left with the group")])
        self.assertEqual(len(party.members), 2)

    def test_as_group_from_a_member_who_does_not_lead_leaves_alone(self):
        party = self.party(self.alpha, self.beta)
        for session in (self.alpha, self.beta):
            self.game.add(session.account, self.match)
        self.leave(self.beta, True)
        self.assertEqual(self.game.taken_out, [("Beta", "left")])
        self.assertEqual(party.members, [self.alpha.account])
        self.assertEqual(self.notified, [party])
        self.assertIsNot(self.social.party_of(self.beta.account), party)

    def test_a_member_who_leaves_alone_leaves_the_match_and_the_party(self):
        party = self.party(self.alpha, self.beta, self.gamma)
        for session in (self.alpha, self.beta, self.gamma):
            self.game.add(session.account, self.match)
        self.leave(self.beta, False)
        self.assertEqual(self.game.taken_out, [("Beta", "left")])
        self.assertEqual(party.members, [self.alpha.account, self.gamma.account])
        self.assertEqual(self.notified, [party])
        (crc, msg_id, value) = self.sent["Beta"][-1]
        self.assertEqual((crc, msg_id), (PARTY, 20700))
        self.assertEqual(len(value["+0x78"]["+0x0"]), 1)  # a party of one again

    def test_the_leader_who_leaves_alone_hands_the_party_on(self):
        party = self.party(self.alpha, self.beta)
        for session in (self.alpha, self.beta):
            self.game.add(session.account, self.match)
        self.leave(self.alpha, False)
        self.assertEqual(self.game.taken_out, [("Alpha", "left")])
        self.assertEqual(party.members, [self.beta.account])
        self.assertIs(party.leader, self.beta.account)

    def test_a_player_alone_just_leaves_the_match(self):
        self.game.add(self.alpha.account, self.match)
        self.leave(self.alpha, False)
        self.leave(self.alpha, True)  # what the client sends next on "Leave game and queue" when alone
        self.assertEqual(self.game.taken_out, [("Alpha", "left"), ("Alpha", "left")])
        self.assertEqual(self.notified, [])

    def test_52902_without_a_match_does_nothing(self):
        party = self.party(self.alpha, self.beta)
        self.leave(self.alpha, False)
        self.assertEqual(self.game.taken_out, [])
        self.assertEqual(len(party.members), 2)

    def test_52902_that_cancels_a_waiting_request_leaves_nothing(self):
        party = self.party(self.alpha, self.beta)
        for session in (self.alpha, self.beta):
            self.game.add(session.account, self.match)
        self.leave(self.alpha, True, request=True)
        self.assertEqual(self.game.taken_out, [])
        self.assertEqual(len(party.members), 2)

    def test_21801_in_a_match_leaves_the_party(self):
        party = self.party(self.alpha, self.beta)
        self.game.add(self.beta.account, self.match)
        leaving.leaving_game(self.beta, {})
        self.assertEqual(party.members, [self.alpha.account])
        self.assertEqual(self.game.taken_out, [])  # the game leaves by itself

    def test_21801_after_the_server_sent_the_game_home_keeps_the_party(self):
        party = self.party(self.alpha, self.beta)
        for session in (self.alpha, self.beta):
            self.game.add(session.account, self.match)
        self.leave(self.alpha, True)
        leaving.leaving_game(self.beta, {})
        leaving.leaving_game(self.alpha, {})
        self.assertEqual(len(party.members), 2)

    def test_21801_outside_a_match_keeps_the_party(self):
        party = self.party(self.alpha, self.beta)
        leaving.leaving_game(self.beta, {})
        self.assertEqual(len(party.members), 2)

    def test_a_game_gets_the_match_lobby_and_loses_it_when_it_leaves(self):
        player = self.game.add(self.alpha.account, self.match)
        leaving.game_joined(self.server, player)
        (crc, msg_id, value) = self.sent["Alpha"][-1]
        self.assertEqual((crc, msg_id), (MATCH_LOBBY, 53000))
        decoded = self.schemas.decode(MATCH_LOBBY, 53000, self.schemas.encode(MATCH_LOBBY, 53000, value))
        self.assertEqual(decoded["+0x78"]["+0x30"]["+0x0"], [1, 101])  # the lobby id is the match id
        self.assertEqual(decoded["+0x78"]["+0x60"], leaving.LOBBY_TYPE)
        self.assertEqual(self.server.match_lobbies, {self.alpha.account.account_lo: (1, 101)})
        leaving.game_left(self.server, self.alpha, player)
        (crc, msg_id, value) = self.sent["Alpha"][-1]
        self.assertEqual((crc, msg_id), (MATCH_LOBBY, 53003))
        self.assertEqual(value["+0x78"]["+0x0"], [1, 101])
        self.assertEqual((value["+0x88"], value["+0xA0"]), (0, 0))
        self.assertEqual(self.server.match_lobbies, {})

    def test_a_client_that_sent_52902_reset_its_lobby_itself(self):
        party = self.party(self.alpha, self.beta)
        players = [self.game.add(session.account, self.match) for session in (self.alpha, self.beta)]
        for player in players:
            leaving.game_joined(self.server, player)
        self.leave(self.alpha, True)
        for session, player in zip((self.alpha, self.beta), players, strict=True):
            leaving.game_left(self.server, session, player)
        self.assertNotIn((MATCH_LOBBY, 53003), [(crc, msg_id) for crc, msg_id, _ in self.sent["Alpha"]])
        self.assertIn((MATCH_LOBBY, 53003), [(crc, msg_id) for crc, msg_id, _ in self.sent["Beta"]])
        self.assertEqual(len(party.members), 2)

    def test_a_player_who_is_offline_when_he_leaves_only_loses_the_record(self):
        player = self.game.add(self.alpha.account, self.match)
        leaving.game_joined(self.server, player)
        leaving.game_left(self.server, None, player)
        self.assertEqual(self.server.match_lobbies, {})


class GroupLeaveTests(unittest.TestCase):
    """The handler with the real game server: the leader leaves as a group, both games go home."""

    @classmethod
    def setUpClass(cls):
        cls.schemas = Schemas()
        cls.content = Content(cls.schemas, RetailTemplates(), ItemDB())

    def test_both_games_of_the_party_go_home_and_leave_the_match(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        save_profile(Profile(), root / "template.json")
        accounts = Accounts(root / "profiles", root / "template.json")
        social = Social(accounts, self.content)
        left = []
        game_server = GameServer("127.0.0.1", 0, on_leave=left.append)
        game_server.start()
        self.addCleanup(game_server.stop)
        server = SimpleNamespace(
            social=social, game=game_server, match_lobbies={}, notify_party=lambda p: None
        )
        alpha, beta = accounts.get("Alpha"), accounts.get("Beta")
        party = social.party_of(alpha)
        social.join(beta, party)
        sessions = [
            SimpleNamespace(account=account, server=server, log=lambda *a: None) for account in (alpha, beta)
        ]
        handoffs = game_server.create_match(
            PRACTICE_RANGE,
            [(alpha.account_lo, "Alpha", 0, 0, False), (beta.account_lo, "Beta", 0, 0, False)],
        )
        games = [PretendGame(game_server.port, handoff) for handoff in handoffs]
        for game in games:
            self.addCleanup(game.sock.close)
            game.connect()
            game.first_reliable()
        match_id = game_server.matches[0].id
        leaving.leave_match_lobby(
            sessions[0], {"+0x78": {"+0x0": list(match_id)}, "+0x88": True, "+0x89": False}
        )
        for game in games:
            game.reliable_until(server_module.LEAVE_GAME)
            game.send(FIN)
        wait_for(lambda: sorted(player.name for player in left) == ["Alpha", "Beta"])
        wait_for(lambda: game_server.matches == [])  # the match ends with its last player
        self.assertEqual(party.members, [alpha, beta])


if __name__ == "__main__":
    unittest.main()
