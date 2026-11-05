"""The game server: one UDP port for every match, one thread.

The lobby sends each player of a match 20600 with this server's address, a connection id of their
own and two keys (lobby/matchmaker.py). The client then sends SYN datagrams with that id in the
header until it gets SYN|ACK, and the session is open with its first ACK. From then on the server
sends one world frame per tick; the first reliable message is 20300 (load the map).

Datagrams go to their game by that connection id, as in the client: it routes every datagram by the id
in its header and opens it with that connection's keys, whatever the address (0x7FF7893127F0). All
links of one game share one socket (0x7FF78930F920), so a game that goes from one match to the next
sends the old connection's FIN and the new one's SYN from the same address; the address of a client
follows its newest datagram.
"""

import ctypes
import logging
import os
import socket
import struct
import threading
import weakref
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass

from ow174.game import correction, messages, movelog, world
from ow174.game.bits import BitReader
from ow174.game.commands import InputError, read_commands
from ow174.game.environment import ExecutionEnvironment, SystemEnvironment
from ow174.game.link import (
    ACK,
    FIN,
    HEADER_SIZE,
    KIND_CONTROL,
    KIND_FRAME,
    MAX_DATAGRAM,
    SYN,
    LinkCipher,
    Packet,
    ReceiveWindow,
    delivery,
    peek_connection,
)
from ow174.game.match import Match, Player, no_skin, pong, sends
from ow174.game.script.driver import warm_up
from ow174.jam.codec import DecodeError
from ow174.jam.values import to_jsonable

log = logging.getLogger("ow174.game")

TICK_SECONDS = 0.016
FIRST_TICK = 1000
SILENCE_SECONDS = 10.0  # the client sends at least every 250 ms; after this long it is gone
# A game that has not connected this long after its 20600 is not coming (for example it could not
# reach the game server's address): the player leaves the match, and the lobby takes them back.
HANDOFF_SECONDS = 60.0
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_SIZE
MAX_PENDING_BOT_NOTICES = 256  # per bot create, retain the newest notices during prolonged loss
PING = 21610
MAP_LOADED = (21601, 21602)
GAME_MESSAGE = 21616
LEAVE_GAME = 20304
# A game sent to the menu (20304) closes its link within ~30 ms; one that does
# not is dropped after this long, so that its match still ends.
LEAVE_GRACE_SECONDS = 3.0
DROPPED_LOG_SECONDS = 5.0  # datagrams we drop: one line per address, connection and reason in this time
SIO_UDP_CONNRESET = 0x9800000C


def next_tick_after(due: float, now: float) -> tuple[float, int]:
    """When the tick after the one due at `due` is due, and how many ticks fell behind `now` (they are
    skipped, not run late). So the packet frame stays on real time, like retail's (62.5 per second, OW2
    traffic): the client's command clock runs on real time and only ever jumps forward to the packet
    frame (0x7FF7896DB296, 0x7FF789AC7140), so a frame number that fell behind would leave the client's
    commands further ahead of it with every stall, past its 64 predicted states and 32 kept commands."""
    following = due + TICK_SECONDS
    if following >= now:
        return following, 0
    missed = int((now - following) // TICK_SECONDS) + 1
    return following + missed * TICK_SECONDS, missed


def _ignore_connection_resets(sock: socket.socket) -> None:
    """On Windows a closed game's ICMP "port unreachable" comes back as an error from the next
    recvfrom; SIO_UDP_CONNRESET turns that off. Python's sock.ioctl does not take it, so WSAIoctl."""
    if os.name != "nt":
        return
    ws2 = ctypes.WinDLL("ws2_32")
    ws2.WSAIoctl.argtypes = [
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    off = wintypes.BOOL(False)
    returned = wintypes.DWORD()
    ws2.WSAIoctl(
        sock.fileno(),
        SIO_UDP_CONNRESET,
        ctypes.byref(off),
        ctypes.sizeof(off),
        None,
        0,
        ctypes.byref(returned),
        None,
        None,
    )


@dataclass
class Handoff:
    """What the lobby puts in a player's 20600."""

    conn: int
    key_in: bytes  # the client encrypts with it (20600 +0xAE)
    key_out: bytes  # we encrypt with it (20600 +0xCE)
    match_id: tuple[int, int]


@dataclass
class OpenMatch:
    """A match that still has free places, as the matchmaker sees it."""

    match: Match
    waiting: bool  # still waiting for its players: hero select has not started counting down
    free: list[int]  # free places per team (one entry for free for all)
    roles: list[dict[int, int]]  # per team: role queue role -> players playing it
    started: float  # time.time() of its creation


class Client:
    """One connected game: the transport state and the queues of what to send."""

    def __init__(self, server: "GameServer", address, conn: int, cipher: LinkCipher, player: Player) -> None:
        self.server = server
        self.address = address
        self.conn = conn
        self.cipher = cipher
        self.player = player
        self.window = ReceiveWindow()
        self.seq = 0  # of our next datagram
        self.open = False
        self.closed = False
        self.heard = server.environment.wall_time(callsite="client.heard")
        self.next_reliable = 0
        self.reliable = []  # [(seq, encoded)] waiting to go out, oldest first
        self.unreliable = []  # [encoded]
        self.entities = []  # [EntityUpdate] waiting to go out
        self.in_flight = {}  # datagram seq -> (reliable messages, entity updates) it carried
        # The current bot creates only. None means the peer acknowledged this create.
        self._bot_notices: dict[int, deque[tuple[int, bytes]] | None] = {}
        self.expected_in = 0  # the client's next reliable message
        self.parked_in = {}
        self.leaving_since: float | None = None  # when go_home sent 20304

    def log(self, message: str, level: int = logging.INFO) -> None:
        log.log(level, "[game %s:%d %s] %s", self.address[0], self.address[1], self.player.name, message)

    def go_home(self, now: float) -> None:
        """20304 with 0 = leave for good (0x7FF7896E6830): the game goes back to the menu and closes its
        link. GameServer._tick drops it after LEAVE_GRACE_SECONDS if it does not."""
        if self.leaving_since is None:
            self.queue_reliable(LEAVE_GAME, {"+0x78": 0, "+0x79": False})
            self.leaving_since = now

    # --- queues --------------------------------------------------------------------------------

    def queue_reliable(self, msg_id: int, value: dict) -> None:
        self._queue_encoded_reliable(msg_id, messages.encode(msg_id, value))

    def _queue_encoded_reliable(self, msg_id: int, payload: bytes) -> None:
        self.reliable.append((self.next_reliable, payload))
        self.next_reliable += 1
        if msg_id not in (20306,):
            self.log(f"[>>>] {msg_id} (reliable #{self.next_reliable - 1})")

    def bot_create_acked(self, entity: int) -> bool:
        """Whether this connection acknowledged the current create for a bot."""
        return entity in self._bot_notices and self._bot_notices[entity] is None

    def queue_bot_notice(self, entity: int, msg_id: int, value: dict) -> None:
        """Keep references to a bot out of the reliable stream until its create is acknowledged."""
        if entity not in self._bot_notices:
            return
        pending = self._bot_notices[entity]
        if pending is None:
            self.queue_reliable(msg_id, value)
        else:
            pending.append((msg_id, messages.encode(msg_id, value)))

    def _ack_bot_create(self, entity: int) -> None:
        pending = self._bot_notices.get(entity)
        if pending is not None:
            self._bot_notices[entity] = None
            for msg_id, payload in pending:
                self._queue_encoded_reliable(msg_id, payload)

    def queue_unreliable(self, msg_id: int, value: dict) -> None:
        self.unreliable.append(messages.encode(msg_id, value))

    def queue_entities(self, updates: list) -> None:
        # A destroy retires all older records of this entity for this client, including
        # reliable creates/chunks awaiting ACK. A later reuse of the id is a new generation.
        lifecycles = getattr(self, "_entity_lifecycles", None)
        if lifecycles is None:
            lifecycles = self._entity_lifecycles = {}
        generations = getattr(self, "_entity_record_generations", None)
        if generations is None:
            generations = self._entity_record_generations = weakref.WeakKeyDictionary()
        for item in updates:
            generation, alive = lifecycles.get(item.entity, (0, True))
            if item not in generations:
                if item.op in (world.OP_CREATE, world.OP_DESTROY):
                    # Preserve the generation of already-sent records, even for an older
                    # producer that did not put them through queue_entities first.
                    pending = [*self.entities]
                    for _reliable, records in self.in_flight.values():
                        pending.extend(records)
                    for old in pending:
                        if old.entity == item.entity and old not in generations:
                            generations[old] = generation
                    generation += 1
                    alive = item.op == world.OP_CREATE
                    lifecycles[item.entity] = (generation, alive)
                    self._bot_notices.pop(item.entity, None)
                    if alive and any(bot.entity == item.entity for bot in self.player.match.bots):
                        self._bot_notices[item.entity] = deque(maxlen=MAX_PENDING_BOT_NOTICES)
                generations[item] = generation
            if generations[item] == generation and (alive or item.op == world.OP_DESTROY):
                self.entities.append(item)
        self.entities = [
            item
            for item in self.entities
            if generations.get(item, 0) == lifecycles.get(item.entity, (0, True))[0]
            and (lifecycles.get(item.entity, (0, True))[1] or item.op == world.OP_DESTROY)
        ]

    # --- sending -------------------------------------------------------------------------------

    def send_control(self, flags: int) -> None:
        self._send(Packet(self.conn, KIND_CONTROL, flags=flags))

    def _send(self, packet: Packet) -> bool:
        packet.seq = self.seq
        packet.ack = self.window.expected
        packet.ack_bits = self.window.bits
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        try:
            self.server.sock.sendto(self.cipher.seal(packet), self.address)
        except OSError as error:
            self.log(f"[!] send failed: {error}", logging.WARNING)
            return False
        return True

    def send_frame(self, tick: int, now: float) -> None:
        reliable = self.reliable[: messages.MAX_PER_KIND]
        unreliable = self.unreliable[: messages.MAX_PER_KIND]
        entities = (self.entities + self.player.match.remote_movements(self.player))[:127]
        try:
            payload = self._payload(tick, reliable, unreliable, entities)
            while len(payload) > MAX_PAYLOAD:
                if entities:
                    entities = entities[: len(entities) // 2]
                elif unreliable:
                    unreliable = unreliable[: len(unreliable) // 2]
                elif len(reliable) > 1:
                    reliable = reliable[: len(reliable) // 2]
                else:
                    self._frame_rejected("oldest reliable message exceeds datagram budget")
                    return
                payload = self._payload(tick, reliable, unreliable, entities)
        except (ValueError, OverflowError, struct.error) as error:
            self._frame_rejected(f"frame encoding failed: {type(error).__name__}")
            return
        if not reliable and not unreliable and not entities and (
            self.reliable or self.unreliable or self.entities
        ):
            self._frame_rejected("oldest queued entity exceeds datagram budget; fragmentation required")
            return
        sent_seq = self.seq
        if not self._send(Packet(self.conn, KIND_FRAME, flags=ACK, payload=payload)):
            return
        self._frame_budget_warning = None
        del self.reliable[: len(reliable)]
        del self.unreliable[: len(unreliable)]
        sent_records = [item for item in entities
                        if item.op or item.chunk is not None or item.ch2 is not None]
        self.entities = [item for item in self.entities if item not in entities]
        self.in_flight[sent_seq] = (reliable, sent_records)
        self.player.match.sent_updates(self.player, entities)

    def _frame_rejected(self, reason: str) -> None:
        # Do not repeatedly flood logs, consume queues or send an invalid oversized packet.
        if getattr(self, "_frame_budget_warning", None) != reason:
            self.log(f"[!] frame rejected: {reason}", logging.ERROR)
            self._frame_budget_warning = reason

    def _payload(self, tick: int, reliable, unreliable, entities) -> bytes:
        # The command ack: the newest command frame we have. The client stops
        # sending the frames before it again; our tick could make it drop frames we never got. While
        # the owner gets records of its body, it stays near them (correction.command_ack).
        ack = correction.command_ack(self.player, tick, entities)
        return world.frame(tick, ack, reliable, unreliable, entities)

    def acked(self, ack: int, ack_bits: int) -> None:
        """Take back what the client did not get: its reliable messages and entity records go out again."""
        lifecycles = getattr(self, "_entity_lifecycles", {})
        generations = getattr(self, "_entity_record_generations", {})

        def current(item) -> bool:
            generation, alive = lifecycles.get(item.entity, (0, True))
            return generations.get(item, 0) == generation and (alive or item.op == world.OP_DESTROY)

        lost_reliable, lost_records = [], []
        for seq in list(self.in_flight):
            state = delivery(seq, ack, ack_bits)
            if state is None:
                continue
            reliable, records = self.in_flight.pop(seq)
            if state:
                for item in records:
                    if not current(item):
                        continue
                    if item.op == world.OP_CREATE:
                        self._ack_bot_create(item.entity)
                    if item.stream is not None:
                        item.stream.arrived(item.chunk_last)
            else:
                lost_reliable += reliable
                lost_records += [item for item in records if item.resend and current(item)]
        if lost_reliable:
            self.reliable = sorted(lost_reliable + self.reliable)
            self.log(f"[!] resending {len(lost_reliable)} reliable message(s)")
        lost_records = [item for item in lost_records if current(item)]
        if lost_records:
            self.entities = lost_records + self.entities
            self.log(f"[!] resending {len(lost_records)} entity record(s)")

    # --- receiving -----------------------------------------------------------------------------

    def received(self, packet: Packet, now: float) -> None:
        if not self.window.accept(packet.seq):
            return
        self.heard = now
        self.acked(packet.ack, packet.ack_bits)
        if packet.flags & FIN:
            self.log("[<<<] FIN: the game left")
            self.send_control(FIN | ACK)
            self.server.drop(self)
            return
        if packet.flags & SYN:
            self.send_control(SYN | ACK)
        if not self.open and packet.flags & ACK:
            self.open = True
            self.log("[+] connected")
            if self.player.gone or self.server.going_home(self.player):
                self.go_home(now)  # taken out of the match before its game connected (GameServer.take_out)
            else:
                self.queue_reliable(20300, self.player.match.instance_message(self.player))
                self.server.joined(self.player)
        if packet.kind == KIND_FRAME and packet.payload and not self.player.gone:
            self._payload_in(packet.payload, now)

    def _payload_in(self, payload: bytes, now: float) -> None:
        reader = BitReader(payload)
        try:
            reliable, unreliable = messages.read_channel(reader)
        except (DecodeError, KeyError, EOFError) as error:
            self.log(f"[<<<] undecodable messages: {error} {payload.hex()}", logging.WARNING)
            return
        for seq, msg_id, value in reliable:
            self._reliable_in(seq, msg_id, value, now)
        for msg_id, value in unreliable:
            self._message(msg_id, value, now)
        try:
            commands = read_commands(reader)
        except InputError as error:
            self.log(f"[<<<] undecodable input: {error}", logging.DEBUG)
            return
        if commands and self.player.spawned:
            self.player.take_commands(commands)

    def _reliable_in(self, seq: int, msg_id: int, value: dict, now: float) -> None:
        if seq < self.expected_in:
            return  # a repeat
        self.parked_in[seq] = (msg_id, value)
        while self.expected_in in self.parked_in:
            msg_id, value = self.parked_in.pop(self.expected_in)
            self.expected_in += 1
            self._message(msg_id, value, now)

    def _message(self, msg_id: int, value: dict, now: float) -> None:
        if msg_id == PING:
            environment = self.server.environment
            self.queue_reliable(
                20306,
                pong(
                    value,
                    environment.wall_time_ns(callsite="client.ping.received_ns"),
                    environment=environment,
                ),
            )
            return
        if msg_id in MAP_LOADED:
            self.player.match.map_loaded(self.player, now)
        if msg_id == GAME_MESSAGE:
            self.player.match.game_message(self.player, value, now)
            return
        shown = str(to_jsonable(value))[:300]
        self.log(f"[<<<] {msg_id} {shown}")


class GameServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3730,
        on_leave=None,
        skin_of=no_skin,
        on_join=None,
        *,
        environment: ExecutionEnvironment | None = None,
        collision_provider_factory=None,
    ) -> None:
        self.environment = SystemEnvironment() if environment is None else environment
        # None preserves the default per-match asynchronous provider. Explicit factories support
        # controlled server replay and isolated scenes without patching global collision state.
        self.collision_provider_factory = collision_provider_factory
        self.host = host
        self.port = port
        self.on_leave = on_leave  # called with each player who leaves a match
        self.on_join = on_join  # called with each player whose game connected to its match
        self.skin_of = skin_of  # (account, hero) -> the equipped (skin theme, golden weapon); no locks
        self.sock: socket.socket | None = None
        self.lock = threading.RLock()
        # connection id -> (the 20600 data, the waiting player, when to give up on them)
        self.handoffs: dict[int, tuple[Handoff, Player, float]] = {}
        self.clients: dict[int, Client] = {}  # connection id -> client
        self._dropped: dict[tuple, list] = {}  # (address, conn, why) -> [count not logged yet, last line]
        self.matches: list[Match] = []
        self.tick = FIRST_TICK
        self._next_conn = self.environment.randbelow(0x100000, callsite="server.next_conn") + 1
        self._left_players: list[Player] = []  # told to on_leave once the lock is released
        self._joined_players: list[Player] = []  # told to on_join once the lock is released
        self._sent_home: weakref.WeakSet[Player] = weakref.WeakSet()  # taken out by the server (take_out)
        self.running = False

    def start(self) -> None:
        """Bind the port and run the server thread. Raises OSError when the port is taken."""
        warm_up()  # the statescript data (0.35 s), so that the first hero body does not stall a tick
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _ignore_connection_resets(self.sock)
        self.sock.bind((self.host, self.port))
        self.port = self.sock.getsockname()[1]
        self.running = True
        threading.Thread(target=self._run, name="game-server", daemon=True).start()
        log.info("[game] Game server on UDP %s:%d", self.host, self.port)

    def stop(self) -> None:
        self.running = False

    # --- matches -------------------------------------------------------------------------------

    def create_match(self, game_map, players, card: int = 0) -> list[Handoff]:
        """A match for these players: [(account_lo, name, hero GUID, team, tournament[, card])], card
        being the player's card as the lobby shows it (content.player.record). `card` is the queue card
        the match is for (0: none). Returns the 20600 data of each, in the same order."""
        with self.lock:
            match = Match(
                game_map,
                on_leave=self._left,
                skin_of=self.skin_of,
                card=card,
                environment=self.environment,
                collision_provider=(
                    None if self.collision_provider_factory is None else self.collision_provider_factory()
                ),
            )
            handoffs = [self._add_player(match, entry) for entry in players]
            self.matches.append(match)
        names = ", ".join(player.describe() for player in match.players)
        log.info("[game] %s on %s (%s): %s", match.label(), game_map.name, game_map.mode_name, names)
        return handoffs

    def open_matches(self, card: int) -> list[OpenMatch]:
        """The queue card's matches that still have free places, oldest first."""
        with self.lock:
            rooms = []
            for match in self.matches:
                if not card or match.card != card or match.ended:
                    continue
                free = match.free_places()
                if any(places > 0 for places in free):
                    rooms.append(
                        OpenMatch(match, match.waiting_for_players, free, match.roles_taken(), match.started)
                    )
            return rooms

    def join_match(self, match: Match, players) -> list[Handoff] | None:
        """More players for a match that is on, as create_match takes them; None once it has ended."""
        with self.lock:
            if match.ended or match not in self.matches:
                return None
            handoffs = [self._add_player(match, entry) for entry in players]
            names = ", ".join(player.describe() for player in match.players[-len(players) :])
        log.info("[game] %s: %s join", match.label(), names)
        return handoffs

    def lineup(self, match_id: tuple[int, int]) -> list[tuple[int, int, int, dict]]:
        """(account_lo, team, role, card) of each player still in the match: the lobby's roster for its
        VERSUS loading screen (lobby/lineup.py)."""
        with self.lock:
            for match in self.matches:
                if match.id == match_id:
                    return [(p.account_lo, p.team, p.role, dict(p.card)) for p in match.players if not p.gone]
        return []

    def _add_player(self, match: Match, entry) -> Handoff:
        account_lo, name, hero, team, tournament, *card = entry
        player = match.add_player(account_lo, name, hero, team, tournament, card[0] if card else None)
        conn = self._next_conn
        self._next_conn = (self._next_conn % 0x0FFFFFFF) + 1
        handoff = Handoff(
            conn,
            self.environment.token_bytes(32, callsite="server.handoff.key_in"),
            self.environment.token_bytes(32, callsite="server.handoff.key_out"),
            match.id,
        )
        deadline = self.environment.wall_time(callsite="server.handoff.deadline") + HANDOFF_SECONDS
        self.handoffs[conn] = (handoff, player, deadline)
        return handoff

    def expire_handoff(self, conn: int) -> bool:
        """Expire a pending handoff on the next tick. Return whether the seat still exists."""
        with self.lock:
            waiting = self.handoffs.get(conn)
            if waiting is None:
                return False
            handoff, player, _ = waiting
            self.handoffs[conn] = (handoff, player, 0.0)
            return True

    def switch_hero(self, account_lo: int, hero) -> bool:
        with self.lock:
            for match in self.matches:
                for player in match.players:
                    if player.account_lo == account_lo and player.client is not None:
                        match.switch_hero(player, hero)
                        return True
        return False

    def skin_changed(self, account_lo: int, hero_guid: int) -> bool:
        """The lobby equipped an item of a hero (24500): a player in a match who plays that hero gets the
        new skin (Match.skin_changed). Hero select equips skins through the lobby, even in a match, and
        picks nothing again."""
        with self.lock:
            for match in self.matches:
                for player in match.players:
                    if player.account_lo == account_lo and player.client is not None and not player.gone:
                        return match.skin_changed(player, hero_guid)
        return False

    def send_home(self, account_lo: int | None = None) -> int:
        """Send a player's game (every game for None) back to the menu: 20304 with 0 = leave for good
        (0x7FF7896E6830). Returns how many games were told."""
        count = 0
        with self.lock:
            for client in list(self.clients.values()):
                if account_lo is None or client.player.account_lo == account_lo:
                    self._sent_home.add(client.player)
                    client.go_home(self.environment.wall_time(callsite="server.send_home"))
                    count += 1
        return count

    def player_of(self, account_lo: int) -> Player | None:
        """The account's player in a match it has not left, whether its game connected or not."""
        with self.lock:
            for match in self.matches:
                for player in match.players:
                    if player.account_lo == account_lo and not player.gone:
                        return player
        return None

    def elsewhere(self, account_lo: int, match_id: tuple[int, int]) -> Player | None:
        """The account's player in a match other than this one that it has not left: for example the
        Practice Range it plays while its queue match pops (lobby/matchmaker.py)."""
        with self.lock:
            for match in self.matches:
                for player in match.players:
                    if match.id != match_id and player.account_lo == account_lo and not player.gone:
                        return player
        return None

    def take_out(self, player: Player, reason: str) -> str:
        """Take a player out of its match from the server side: a connected game goes back to the menu
        (20304) and leaves the match when its link closes; a game that has not connected leaves the
        match now, and gets 20304 if it still connects. Returns what happened, for the log."""
        with self.lock:
            if player.gone:
                return "had left already"
            self._sent_home.add(player)
            client = player.client
            if client is not None and not client.closed:
                client.go_home(self.environment.wall_time(callsite="server.take_out"))
                return "sent to the menu"
            player.match.leave(player, reason)
            return "left before its game connected"

    def going_home(self, player: Player) -> bool:
        """Whether the server took the player out of its match (take_out, send_home)."""
        with self.lock:
            return player in self._sent_home

    def joined(self, player: Player) -> None:
        self._joined_players.append(player)

    def in_match(self, account_lo: int) -> bool:
        with self.lock:
            return any(
                player.account_lo == account_lo and not player.gone
                for match in self.matches
                for player in match.players
            )

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [match.snapshot() for match in self.matches]

    def _left(self, player: Player) -> None:
        self._left_players.append(player)

    def _report_leaves(self) -> None:
        """Tell the lobby about games that connected and players who left, outside our lock: the lobby
        calls us under its own."""
        with self.lock:
            joined, self._joined_players = self._joined_players, []
            left, self._left_players = self._left_players, []
        for callback, players in ((self.on_join, joined), (self.on_leave, left)):
            for player in players:
                if callback is not None:
                    try:
                        callback(player)
                    except Exception:
                        log.exception("[game] the lobby's join or leave handler failed")

    # --- the loop ------------------------------------------------------------------------------

    def _run(self) -> None:
        next_tick = self.environment.monotonic(callsite="server.run.start")
        while self.running:
            timeout = max(0.0, next_tick - self.environment.monotonic(callsite="server.run.timeout"))
            self.sock.settimeout(timeout or 0.0001)
            try:
                data, address = self.sock.recvfrom(4096)
            except TimeoutError:
                data = None
            except OSError as error:
                log.warning("[game] receive failed: %s", error)
                data = None
            try:
                with self.lock:
                    if data is not None:
                        self._datagram(
                            data, address, self.environment.wall_time(callsite="server.run.datagram")
                        )
                    if self.environment.monotonic(callsite="server.run.due") >= next_tick:
                        next_tick, missed = next_tick_after(
                            next_tick, self.environment.monotonic(callsite="server.run.next_tick")
                        )
                        self.tick += missed
                        self._tick(self.environment.wall_time(callsite="server.run.tick"))
            except Exception:
                log.exception("[game] tick failed")
            self._report_leaves()
        self.sock.close()

    def _datagram(self, data: bytes, address, now: float) -> None:
        conn = peek_connection(data)
        if conn is None:
            self._drop_datagram(address, None, "not a game link datagram", now)
            return
        client = self.clients.get(conn)
        if client is not None:
            packet = client.cipher.open(data)
            if packet is None:
                self._drop_datagram(address, conn, "does not open with that connection's keys", now)
                return
            if address != client.address:
                if not client.window.is_new(packet.seq):
                    self._drop_datagram(address, conn, "an old datagram from another address", now)
                    return
                client.log(f"[+] the game moved to {address[0]}:{address[1]}")
                client.address = address
            client.received(packet, now)
            return
        waiting = self.handoffs.get(conn)
        if waiting is None:
            self._drop_datagram(address, conn, "unknown connection", now)
            return
        handoff, player, _ = waiting
        cipher = LinkCipher(handoff.key_in, handoff.key_out)
        packet = cipher.open(data)
        if packet is None or not packet.flags & SYN:
            self._drop_datagram(address, conn, "not a SYN with the keys of its 20600", now)
            return
        del self.handoffs[conn]
        client = Client(self, address, conn, cipher, player)
        if not player.gone:  # one taken out before it connected only gets its 20304
            player.client = client
        self.clients[conn] = client
        client.log(f"[+] SYN for {player.match.label()}, connection {conn}{self._others_at(address, client)}")
        client.received(packet, now)

    def _others_at(self, address, client=None) -> str:
        """The other connections on this address, for the log: a game's links all share one socket."""
        at = [
            f"{other.conn} ({other.player.match.label()})"
            for other in self.clients.values()
            if other.address == address and other is not client
        ]
        return f"; this address also has connection {', '.join(at)}" if at else ""

    def _drop_datagram(self, address, conn, why: str, now: float) -> None:
        """Log a datagram we drop: the first at once, then one line with the count per address, connection
        and reason every DROPPED_LOG_SECONDS (_tick writes the last count)."""
        key = (address, conn, why)
        seen = self._dropped.get(key)
        if seen is not None:
            seen[0] += 1
            if now - seen[1] < DROPPED_LOG_SECONDS:
                return
        self._log_dropped(key, 1 if seen is None else seen[0], now)

    def _log_dropped(self, key, count: int, now: float) -> None:
        address, conn, why = key
        log.info(
            "[game %s:%d] [!] dropped %d datagram(s) of connection %s: %s%s",
            address[0],
            address[1],
            count,
            conn,
            why,
            self._others_at(address),
        )
        self._dropped[key] = [0, now]

    def _tick(self, now: float) -> None:
        self.tick += 1
        for conn, (_, player, deadline) in list(self.handoffs.items()):
            if now >= deadline:
                del self.handoffs[conn]
                player.match.leave(player, "did not connect")
        for match in list(self.matches):
            match.update(now, self.tick)
        for client in list(self.clients.values()):
            if now - client.heard > SILENCE_SECONDS:
                client.log("[!] no datagrams for 10 s, dropping the game")
                self.drop(client)
            elif client.leaving_since is not None and now - client.leaving_since >= LEAVE_GRACE_SECONDS:
                client.log("[!] still here after 20304, dropping the game")
                self.drop(client)
            elif client.open and sends(self.tick):
                client.send_frame(self.tick, now)
        self.matches = [match for match in self.matches if not match.ended]
        for key, (count, last) in list(self._dropped.items()):
            if now - last >= DROPPED_LOG_SECONDS:
                if count:
                    self._log_dropped(key, count, now)
                else:
                    del self._dropped[key]

    def drop(self, client: Client) -> None:
        if client.closed:
            return
        client.closed = True
        client._bot_notices.clear()
        if self.clients.get(client.conn) is client:
            del self.clients[client.conn]
        movelog.close(client.player)
        client.player.match.leave(client.player)
