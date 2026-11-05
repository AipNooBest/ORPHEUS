"""A game that goes from one match straight into the next: one socket for every link of the game, the
old link's FIN and the new link's SYN in either order, a new address, the old link's leftovers. And the
matchmaker sends a game that is still in the Practice Range to the menu when its queue match pops."""

import dataclasses
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import messages
from ow174.game.bits import BitReader
from ow174.game.content import PRACTICE_RANGE
from ow174.game.link import ACK, FIN, KIND_FRAME, SYN, LinkCipher, Packet, peek_connection
from ow174.game.server import LEAVE_GAME, GameServer
from ow174.lobby.matchmaker import AWAY_SECONDS, FOUND_SECONDS, QUICK_PLAY, Matchmaker, Roster, Ticket

TWO_TEAMS = dataclasses.replace(PRACTICE_RANGE, team_sizes=(1, 1))


def reliable_ids(packet: Packet) -> list[int]:
    """The reliable messages of a world frame."""
    if packet.kind != KIND_FRAME or not packet.payload:
        return []
    reader = BitReader(packet.payload)
    reader.var_a()
    reliable, _ = messages.read_channel(reader)
    return [msg_id for _, msg_id, _ in reliable]


class PretendGame:
    """One game's socket: the client opens every link of a game on one socket (0x7FF78930F920) and
    routes what it gets by the connection id in the header (0x7FF7893127F0), as this does."""

    def __init__(self, port: int) -> None:
        self.server = ("127.0.0.1", port)
        self.sock = self._socket()
        self.links: dict[int, Link] = {}

    @staticmethod
    def _socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)
        return sock

    def link(self, handoff) -> "Link":
        self.links[handoff.conn] = Link(self, handoff)
        return self.links[handoff.conn]

    def move(self) -> socket.socket:
        """A new socket (a new port) from now on; returns the old one."""
        old, self.sock = self.sock, self._socket()
        return old

    def pump(self) -> None:
        """Read what arrived and hand each datagram to its link."""
        try:
            data, _ = self.sock.recvfrom(4096)
        except TimeoutError:
            return
        link = self.links.get(peek_connection(data))
        if link is not None:
            packet = link.cipher.open(data)
            if packet is not None:
                link.expected = packet.seq + 1
                link.got.append(packet)

    def close(self) -> None:
        self.sock.close()


class Link:
    """The client's side of one link, with the keys of its 20600."""

    def __init__(self, game: PretendGame, handoff) -> None:
        self.game = game
        self.conn = handoff.conn
        self.cipher = LinkCipher(key_in=handoff.key_out, key_out=handoff.key_in)
        self.seq = 0
        self.expected = 0
        self.got: list[Packet] = []

    def send(self, flags: int, sock: socket.socket | None = None, seq: int | None = None) -> None:
        packet = Packet(self.conn, 0, self.seq if seq is None else seq, self.expected, 1, flags)
        if seq is None:
            self.seq += 1
        (sock or self.game.sock).sendto(self.cipher.seal(packet), self.game.server)

    def wait(self, wanted, seconds: float = 3.0) -> Packet:
        """The first packet of this link that wanted(packet) takes, reading until it comes."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            while self.got:
                packet = self.got.pop(0)
                if wanted(packet):
                    return packet
            self.game.pump()
        raise AssertionError(f"connection {self.conn}: nothing wanted in {seconds} s")

    def flags(self, flags: int, seconds: float = 3.0) -> Packet:
        return self.wait(lambda packet: packet.flags & (SYN | ACK | FIN) == flags, seconds)

    def message(self, msg_id: int, seconds: float = 3.0) -> Packet:
        return self.wait(lambda packet: msg_id in reliable_ids(packet), seconds)

    def connect(self) -> None:
        self.send(SYN)
        self.flags(SYN | ACK)
        self.send(ACK)
        self.message(20300)


def wait_until(condition, seconds: float = 3.0) -> None:
    deadline = time.time() + seconds
    while not condition():
        if time.time() > deadline:
            raise AssertionError("not in time")
        time.sleep(0.01)


class GameToGameTests(unittest.TestCase):
    def setUp(self):
        self.left = []
        self.server = GameServer("127.0.0.1", 0, on_leave=self.left.append)
        self.server.start()
        self.addCleanup(self.server.stop)
        (self.old,) = self.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        (self.new, _) = self.server.create_match(
            TWO_TEAMS, [(1, "Alpha", 0, 0, False), (2, "Beta", 0, 1, False)]
        )
        self.game = PretendGame(self.server.port)
        self.addCleanup(self.game.close)
        self.practice = self.game.link(self.old)
        self.practice.connect()

    def client(self, handoff):
        with self.server.lock:
            return self.server.clients.get(handoff.conn)

    def test_the_old_links_fin_then_the_new_links_syn_from_the_same_socket(self):
        self.practice.send(FIN)
        self.practice.flags(FIN | ACK)
        wait_until(lambda: [player.name for player in self.left] == ["Alpha"])  # out of the Practice Range
        match = self.game.link(self.new)
        match.send(SYN)
        self.assertEqual(match.flags(SYN | ACK).conn, self.new.conn)
        match.send(ACK)
        match.message(20300)
        self.assertEqual(self.client(self.new).address, self.game.sock.getsockname())
        self.assertIsNone(self.client(self.old))

    def test_the_new_links_syn_before_the_old_links_fin(self):
        match = self.game.link(self.new)
        match.send(SYN)
        match.flags(SYN | ACK)
        match.send(ACK)
        match.message(20300)
        # both links on one address: each keeps its own connection, keys and frames
        self.assertEqual(self.client(self.old).address, self.client(self.new).address)
        self.practice.send(FIN)
        self.practice.flags(FIN | ACK)
        wait_until(lambda: self.client(self.old) is None)
        self.assertTrue(self.client(self.new).open)
        match.wait(lambda packet: packet.kind == KIND_FRAME)  # the new link still gets its frames
        self.assertEqual([player.name for player in self.left], ["Alpha"])  # only the Practice Range

    def test_a_syn_from_a_new_address_moves_the_link_there(self):
        match = self.game.link(self.new)
        match.send(SYN)
        match.flags(SYN | ACK)
        old_socket = self.game.move()
        self.addCleanup(old_socket.close)
        match.send(SYN)  # the client tries again from another port
        match.flags(SYN | ACK)
        match.send(ACK)
        match.message(20300)
        self.assertEqual(self.client(self.new).address, self.game.sock.getsockname())
        with self.assertLogs("ow174.game", level="INFO") as logs:
            match.send(ACK, sock=old_socket, seq=0)  # a late datagram from the old port moves nothing
            wait_until(lambda: any("an old datagram from another address" in line for line in logs.output))
        self.assertEqual(self.client(self.new).address, self.game.sock.getsockname())

    def test_the_old_links_leftovers_are_dropped_and_logged(self):
        match = self.game.link(self.new)
        self.practice.send(FIN)
        self.practice.flags(FIN | ACK)
        match.connect()
        with self.assertLogs("ow174.game", level="INFO") as logs:
            self.practice.send(ACK)  # the old link's last ACK (state 9, 0x7FF789311350)
            wait_until(lambda: any("unknown connection" in line for line in logs.output))
        (line,) = [line for line in logs.output if "unknown connection" in line]
        self.assertIn(f"connection {self.old.conn}: unknown connection", line)
        self.assertIn(f"this address also has connection {self.new.conn}", line)
        self.assertTrue(self.client(self.new).open)

    def test_a_datagram_that_does_not_open_is_logged_with_its_connection(self):
        stranger = Link(self.game, dataclasses.replace(self.old, key_in=bytes(32)))
        with self.assertLogs("ow174.game", level="INFO") as logs:
            stranger.send(ACK)
            wait_until(lambda: any("does not open" in line for line in logs.output))
        (line,) = [line for line in logs.output if "does not open" in line]
        self.assertIn(f"connection {self.old.conn}: does not open with that connection's keys", line)
        self.assertTrue(self.client(self.old).open)


class PracticeRangeToQueueMatchTests(unittest.TestCase):
    """The queue pops while a player is in the Practice Range: that game goes to the menu (20304) and
    gets its 20600 after "Game Found!", as from the menu."""

    def setUp(self):
        self.sent, self.later, self.sessions = [], [], {}
        self.game = GameServer("127.0.0.1", 0, on_leave=lambda player: None)
        self.game.start()
        self.addCleanup(self.game.stop)
        self.lobby = SimpleNamespace(
            game=self.game,
            session_of=self.sessions.get,
            social=SimpleNamespace(open_match_chat=lambda match_id, accounts: {"match": 1}, general={}),
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
        self.matchmaker = Matchmaker(self.lobby)
        self.matchmaker.schedule = lambda seconds, function: self.later.append((seconds, function))
        self.accounts = [self.account(number) for number in (1, 2)]

    def account(self, number: int):
        account = SimpleNamespace(account_lo=number, name=f"P{number}", virtual=False)
        self.sessions[number] = SimpleNamespace(
            account=account,
            tournament=False,
            profile=None,
            ident=None,
            logged_in=True,
            send=lambda crc, msg_id, value: self.sent.append((number, msg_id)),
            log=lambda text: None,
            sock=SimpleNamespace(getsockname=lambda: ("127.0.0.1", 3724)),
        )
        return account

    def pop(self):
        """P1 and P2 searched Quick Play; the matchmaker starts their match."""
        party = SimpleNamespace(
            queue=None, queue_state=0, accepted=set(), ready=set(), pass_roles={}, passes_taken=set()
        )
        ticket = Ticket(party, {}, QUICK_PLAY, time.time())
        roster = Roster([[(ticket, self.accounts[0], 0)], [(ticket, self.accounts[1], 0)]])
        with self.lobby.state_lock:
            self.matchmaker._start(TWO_TEAMS, roster, card=QUICK_PLAY)

    def ids(self, number: int) -> list[int]:
        return [msg_id for who, msg_id in self.sent if who == number]

    def in_practice_range(self):
        (handoff,) = self.game.create_match(PRACTICE_RANGE, [(1, "P1", 0, 0, False)])
        practice_range = PretendGame(self.game.port)
        self.addCleanup(practice_range.close)
        link = practice_range.link(handoff)
        link.connect()
        self.lobby.match_lobbies[1] = handoff.match_id  # its 53000 at the connect (LobbyServer.game_joined)
        return link

    def test_the_practice_range_game_goes_to_the_menu_and_the_20600_follows(self):
        link = self.in_practice_range()
        self.pop()
        link.message(LEAVE_GAME)  # 20304: to the menu, now
        self.assertEqual(self.ids(1), [44202, 53000, 53003])
        self.assertEqual(self.ids(2), [44202, 53000, 53003])
        self.assertEqual([seconds for seconds, _ in self.later], [FOUND_SECONDS])
        # its match lobby went with the 53003 above, so the Practice Range's leave sends no 53003
        self.assertEqual(self.lobby.match_lobbies, {})
        link.send(FIN)  # the game leaves the Practice Range
        link.flags(FIN | ACK)
        wait_until(lambda: [match.game_map.name for match in self.game.matches] == [TWO_TEAMS.name])
        for _, function in self.later:
            function()
        self.assertEqual(self.ids(1)[-1], 20600)
        self.assertEqual(self.ids(2)[-1], 20600)

    def test_without_a_found_step_a_game_sent_home_still_waits_for_its_20600(self):
        self.matchmaker.found_seconds = 0
        link = self.in_practice_range()
        self.pop()
        link.message(LEAVE_GAME)
        self.assertNotIn(20600, self.ids(1))
        self.assertEqual([seconds for seconds, _ in self.later], [AWAY_SECONDS])

    def test_games_from_the_menu_go_as_before(self):
        self.matchmaker.found_seconds = 0
        self.pop()
        self.assertEqual(self.ids(1), [44202, 20404, 20402, 20600])
        self.assertEqual(self.later, [])


if __name__ == "__main__":
    unittest.main()
