"""Matchmaking: parties that search a queue are put into full matches, and their games go to the
game server.

A queue card's rules (data/game_queues_174.json, tools/extract_game_queues.py) give its teams, the
players per team, the role slots of a role queue (two of each role per team) and its maps. A party
always plays on one team. A match starts once the waiting parties fill every team exactly and, in a
role queue, every player gets one of the roles they picked. Parties that waited longest go first.
For tests, a minimum number of players starts a match early with whoever is waiting.

What the client needs:
- 44202 {queue key, reason} drops the client's queue entry and stops the search timer. Every reason
  does that; reason 1 (QueuePopped) only differs in telemetry (0x7FF7896190D0). It goes first, or the
  menu keeps showing the search.
- "Game Found!" is the client's play state 4 (0x7FF789A9D590, text 046B.07C via 0x7FF78985BC70). A
  queue match gets it before the game: 53000 puts the client in the match lobby of its popped queue
  (type 4, it answers 52903), and 53003 with reason 4 takes it out again and sets play state 4 for 5 s
  (0x7FF789693120). Both make the client redraw the search widget's status (event 36); 44202 does
  not. The client waits for nothing: 20600 follows after FOUND_SECONDS, inside those 5 s. It cannot
  cancel then either: without a queue entry it sends no 44102 (0x7FF789618950).
- 20600 then sends the game to the game server; its handler (0x7FF789685CD0) has no queue or menu
  check. +0x78 must be False: True means the struct is XOR-scrambled, and the address the client
  then reads is garbage (why our first try of 20600 never connected). The client dials the host text
  (+0x2E) and port (+0x2C), puts the connection id (+0x28) in every datagram header, and encrypts with
  the key at +0xAE; the server encrypts with the one at +0xCE. host2 (+0x6E) is only shown.
- A game that is still in a match when it gets 20600 (the Practice Range while searching) closes the old
  link and dials the new match through the old world's link; the old link's close then deletes that
  world, the new link with it. So such a game goes to the menu first (20304, at the pop) and
  gets its 20600 after "Game Found!", like a game that searched from the menu.
"""

import ipaddress
import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ow174.game.content import PRACTICE_RANGE, game_map
from ow174.game.match import CARD_RANK, CARD_ROLE
from ow174.jam.groups import CHAT_IN, HANDOFF, MATCH_LOBBY, QUEUE
from ow174.lobby import lineup
from ow174.paths import DATA_DIR
from ow174.services.social import Party

log = logging.getLogger("ow174.lobby")

QUEUES_PATH = DATA_DIR / "game_queues_174.json"
QUEUE_POPPED = 1
FOUND_SECONDS = 4.0  # "Game Found!" before the 20600 of a queue match; the client shows it for 5 s
GAME_QUEUE = 4  # 53000 match lobby type of a popped queue (0x7FF7896954B0)
GAME_FOUND = 4  # 53003 reason that shows "Game Found!" (0x7FF789693589)
# Least time between the 20304 that sends a game home from another match and its 20600: its old world must
# be gone first (_leave_other_games). It closes its link in ~30 ms and deletes the world a frame after.
AWAY_SECONDS = 1.0
ANY_ROLE = (2, 1, 3)  # tank, damage, support: what "All roles" picks
ROLE_NUMBERS = {0x0D80000000000850: 2, 0x0D8000000000084E: 1, 0x0D80000000000851: 3}  # role GUID -> number
ROLE_GUIDS = {number: guid for guid, number in ROLE_NUMBERS.items()}
SEARCH_LIMIT = 20000  # roster search steps per attempt
QUICK_PLAY = 0x06300000000000ED


@dataclass(frozen=True)
class Ruleset:
    """One way a card is played: its game mode and the players each team takes."""

    mode: int = 0
    mode_name: str = ""
    team_sizes: tuple[int, ...] = (6, 6)  # per team; free for all: one entry, the number of players
    free_for_all: bool = False


@dataclass(frozen=True)
class QueueRules:
    name: str = ""
    rulesets: tuple[Ruleset, ...] = (Ruleset(),)
    roles: dict = field(default_factory=dict)  # role number -> slots per team; empty = any roles
    maps: tuple = ()  # (map GUID, the modes it is played in)
    competitive: bool = False


@dataclass
class ModeSettings:
    """A queue card's own matchmaking settings, set in the dashboard."""

    players_to_start: int = 0  # 0: none of its own, the server-wide number counts
    fill_running: bool = False  # unranked: players who search join its matches that are on and have room


def load_rules(path=QUEUES_PATH) -> dict[int, QueueRules]:
    """The queue cards' rules (data/game_queues_174.json, tools/extract_game_queues.py)."""
    if not path.is_file():
        return {}
    cards = json.loads(path.read_text(encoding="utf-8")).get("cards", {})
    return {int(key, 16): _card_rules(entry) for key, entry in cards.items() if entry.get("rulesets")}


def _card_rules(entry: dict) -> QueueRules:
    roles = {}
    for guid, count in (entry.get("roles") or {}).items():
        number = ROLE_NUMBERS.get(int(guid, 16))
        if number and int(count) > 0:
            roles[number] = int(count)
    maps = tuple(
        (int(item["map"], 16), tuple(int(mode, 16) for mode in item.get("modes") or []))
        for item in entry.get("maps") or []
    )
    rulesets = tuple(_ruleset(item) for item in entry["rulesets"])
    return QueueRules(entry.get("name") or "", rulesets, roles, maps, bool(entry.get("competitive")))


def _ruleset(item: dict) -> Ruleset:
    free_for_all = bool(item.get("free_for_all"))
    if free_for_all:
        sizes = (int(item.get("players") or 8),)
    else:
        by_team = item.get("team_sizes") or {}
        sizes = tuple(int(by_team[team]) for team in ("blue", "red") if team in by_team) or (6, 6)
    return Ruleset(int(item["mode"], 16), item.get("mode_name") or "", sizes, free_for_all)


@dataclass
class Ticket:
    party: Party
    key: dict  # the queue key of the party's 44100
    card: int  # the queue card GUID
    since: float
    members: list = field(default_factory=list)  # (account, the roles they picked, best first)


@dataclass
class Roster:
    teams: list  # per team: [(ticket, account, role)]


@dataclass
class Found:
    """A player of a queue match while the client shows "Game Found!", until the 20600."""

    session: object
    ticket: Ticket
    handoff: object  # what the game server made for the player (game.server.Handoff)
    map_name: str


def _match_roles(players: list, slots: dict) -> dict | None:
    """Give each player one of their roles within the slots: {index: role}, or None."""
    taken: dict[int, list[int]] = {role: [] for role in slots}

    def place(index: int, seen: set) -> bool:
        for role in players[index]:
            if role not in slots or role in seen:
                continue
            seen.add(role)
            if len(taken[role]) < slots[role]:
                taken[role].append(index)
                return True
            for other in list(taken[role]):
                taken[role].remove(other)
                if place(other, seen):
                    taken[role].append(index)
                    return True
                taken[role].append(other)
        return False

    for index in range(len(players)):
        if not place(index, set()):
            return None
    return {index: role for role, indexes in taken.items() for index in indexes}


def build_roster(tickets: list[Ticket], ruleset: Ruleset, roles: dict, minimum: int = 0) -> Roster | None:
    """Pick waiting parties that fill the teams (oldest first), or None when they can't yet.
    `minimum` > 0 starts a match with fewer players once that many wait (never more than the mode
    holds): as many of the waiting players as fit go in, and a role queue's slots don't count."""
    capacity = [1] * ruleset.team_sizes[0] if ruleset.free_for_all else list(ruleset.team_sizes)
    full = sum(capacity)
    if not minimum or minimum >= full:
        return _fill(tickets, ruleset, capacity, roles, full, bool(roles))
    waiting = sum(len(ticket.members) for ticket in tickets)
    for goal in range(min(waiting, full), minimum - 1, -1):
        roster = _fill(tickets, ruleset, capacity, {}, goal, bool(roles))
        if roster is not None:
            return roster
    return None


def _fill(tickets: list[Ticket], ruleset: Ruleset, capacity: list, roles: dict, goal: int, role_queue: bool):
    """At least `goal` players from the waiting parties, oldest first: a party always on one team, the
    emptiest team first, and each player in a role they picked while `roles` gives the slots."""
    teams = len(capacity)
    chosen: list[list[tuple]] = [[] for _ in range(teams)]
    steps = 0

    def fits(team: int, extra: list) -> bool:
        if len(chosen[team]) + len(extra) > capacity[team]:
            return False
        if not roles:
            return True
        picks = [member_roles or list(ANY_ROLE) for _, _, member_roles in chosen[team] + extra]
        return _match_roles(picks, roles) is not None

    def search(start: int, count: int) -> bool:
        nonlocal steps
        if count >= goal:
            return True
        for index in range(start, len(tickets)):
            steps += 1
            if steps > SEARCH_LIMIT:
                return False
            ticket = tickets[index]
            extra = [(ticket, account, picked) for account, picked in ticket.members]
            if ruleset.free_for_all:  # every player on a team of their own
                free = [team for team in range(teams) if not chosen[team]]
                if len(free) < len(extra):
                    continue
                for team, entry in zip(free, extra, strict=False):
                    chosen[team].append(entry)
                if search(index + 1, count + len(extra)):
                    return True
                for team in free[: len(extra)]:
                    chosen[team].clear()
                continue
            for team in sorted(range(teams), key=lambda t: len(chosen[t]) - capacity[t]):
                if fits(team, extra):
                    chosen[team].extend(extra)
                    if search(index + 1, count + len(extra)):
                        return True
                    del chosen[team][-len(extra) :]
        return False

    if not search(0, 0):
        return None
    result = []
    for team in chosen:
        picks = [member_roles or list(ANY_ROLE) for _, _, member_roles in team]
        if roles:
            given = _match_roles(picks, roles)
        else:
            # Starting with fewer players drops a role queue's slots: each player plays the first role
            # they picked (hero select offers its heroes; a competitive card's rank is the role's).
            given = {index: pick[0] for index, pick in enumerate(picks)} if role_queue else {}
        result.append(
            [(ticket, account, given.get(index, 0)) for index, (ticket, account, _) in enumerate(team)]
        )
    return Roster(result)


def game_address(public: str, sock) -> str:
    """Where a player's game finds the game server: the address it reached the lobby at, or the public
    address (--game-host) for a player from outside the host's network. A player on the host's own PC or
    network keeps the address it reached, since many routers do not let it back in through their public
    address."""
    reached = sock.getsockname()[0]
    if not public:
        return reached
    peer = ipaddress.ip_address(sock.getpeername()[0])
    return reached if not peer.is_global else public


def handoff_message(handoff, host: str, port: int) -> dict:
    """20600 for one player; `handoff` is what the game server made for them (game.server.Handoff)."""
    address = list(host.encode("ascii")[:63].ljust(64, b"\0"))
    return {
        "+0x78": False,
        "+0x80": {
            "+0x0": {"+0x0": handoff.match_id[0], "+0x8": handoff.match_id[1]},
            "+0x28": handoff.conn,
            "+0x2C": port,
            "+0x2E": address,
            "+0x6E": address,
            "+0xAE": list(handoff.key_in),
            "+0xCE": list(handoff.key_out),
            "+0xEE": False,
        },
    }


def match_lobby(key: dict, match_id: tuple) -> dict:
    """53000: the match lobby of a popped queue (0x7FF789692FE0): the queue key, the lobby id (the match
    id) and the type. +0xE8 is the ticket of the queue entry 44202 took out (44201 +0x48, which we send
    as 0); the client answers 52903 with whether the two are the same."""
    return {
        "+0x78": {"+0x0": key, "+0x30": {"+0x0": list(match_id)}, "+0x60": GAME_QUEUE},
        "+0xE0": False,
        "+0xE8": 0,
    }


def lobby_left(match_id: tuple, reason: int) -> dict:
    """53003: the client leaves its match lobby (0x7FF789693120). The handler reads only +0x88 (not 0:
    "Leaving game" for 5 s instead) and the reason; GAME_FOUND shows "Game Found!" for 5 s."""
    return {"+0x78": {"+0x0": list(match_id)}, "+0x88": 0, "+0x90": {"+0x0": [0, 0]}, "+0xA0": reason}


def _later(seconds: float, function) -> None:
    def run() -> None:
        try:
            function()
        except Exception:
            log.exception("[MM] A delayed matchmaking step failed")

    timer = threading.Timer(seconds, run)
    timer.daemon = True
    timer.start()


def _place(ticket: Ticket, room, slots: dict, role_queue: bool) -> tuple[int, dict] | None:
    """Where a whole party fits into an open match (game.server.OpenMatch): (team, {member index: role}),
    the team with the most room first; with role slots, only a team that has free slots in roles the
    members picked."""
    picks = [picked or list(ANY_ROLE) for _, picked in ticket.members]
    for team in sorted(range(len(room.free)), key=lambda t: -room.free[t]):
        if room.free[team] < len(picks):
            continue
        if not slots:
            return team, ({index: pick[0] for index, pick in enumerate(picks)} if role_queue else {})
        left = {role: count - room.roles[team].get(role, 0) for role, count in slots.items()}
        given = _match_roles(picks, {role: count for role, count in left.items() if count > 0})
        if given is not None:
            return team, given
    return None


def load_modes(path: Path | None) -> dict[int, ModeSettings]:
    """The cards' own settings the dashboard saved: {"0x...": {"players_to_start", "fill_running"}}."""
    if path is None or not path.is_file():
        return {}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("[MM] Could not read %s; no card has settings of its own", path)
        return {}
    modes = {}
    for key, value in saved.items() if isinstance(saved, dict) else ():
        try:
            modes[int(key, 16)] = ModeSettings(int(value["players_to_start"]), bool(value["fill_running"]))
        except (KeyError, TypeError, ValueError):
            continue
    return modes


class Matchmaker:
    def __init__(self, server, minimum_players: int = 0, modes_path: Path | None = None) -> None:
        self.server = server
        self.minimum_players = minimum_players  # cards without a number of their own: 0 = full teams
        self.forced_map: int | None = None  # the dashboard's map for every queue; None = the card's pick
        self.rules = load_rules()
        self.modes_path = modes_path
        self.modes = load_modes(modes_path)  # card -> its own settings
        self._hostable: dict[int, bool] = {}  # card -> can_host
        self.tickets: list[Ticket] = []
        self.found_seconds = FOUND_SECONDS  # 0: queue matches go to the game at once
        self.found: dict[int, Found] = {}  # account -> its player in "Game Found!"
        self.schedule = _later  # (seconds, function): runs the function later on its own thread
        self.lock = threading.RLock()

    # --- the cards' settings -------------------------------------------------------------------

    def players_to_start(self, card: int) -> int:
        """Searching players that start a match of the card: its own number, else the server-wide
        one; 0 = full teams."""
        own = self.modes.get(card)
        return own.players_to_start if own and own.players_to_start else self.minimum_players

    def fills_running(self, card: int) -> bool:
        """Whether players who search join the card's matches that are on: unranked cards, when the
        dashboard says so. A match still waiting for its players takes them on every card."""
        own = self.modes.get(card)
        return bool(own and own.fill_running) and not self.rules_of(card).competitive

    def set_mode(self, card: int, players_to_start: int, fill_running: bool) -> None:
        """The dashboard's settings for a card. Waiting players are placed again at once."""
        with self.lock:
            if players_to_start or fill_running:
                self.modes[card] = ModeSettings(players_to_start, fill_running)
            else:
                self.modes.pop(card, None)
            if self.modes_path is not None:
                saved = {f"0x{key:X}": asdict(value) for key, value in sorted(self.modes.items())}
                self.modes_path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        self.retry()

    def retry(self, card: int | None = None) -> None:
        """Place the waiting parties again (of one card, or of all): a setting changed, or a player
        left a match and freed a place."""
        with self.lock:
            for waiting in sorted({ticket.card for ticket in self.tickets if card in (None, ticket.card)}):
                self._try_match(waiting)

    # --- the queue -----------------------------------------------------------------------------

    def search(self, party: Party, key: dict, card: int) -> None:
        """The party searches this queue now (every member ready)."""
        members = [(member, party.roles.get(member.account_lo, [])) for member in self._players(party)]
        with self.lock:
            self.tickets = [ticket for ticket in self.tickets if ticket.party is not party]
            self.tickets.append(Ticket(party, key, card, time.time(), members))
            log.info("[MM] %s's party (%d) searches card 0x%X", party.leader.name, len(members), card)
            self._try_match(card)

    def cancel(self, party: Party) -> None:
        """The party leaves the queue. Members in "Game Found!" whose lobby connection closed give their
        seat in the match back at once; the others still go (the client itself cannot cancel then)."""
        with self.lock:
            self.tickets = [ticket for ticket in self.tickets if ticket.party is not party]
            members = {member.account_lo for member in party.members}
            for account_lo, found in list(self.found.items()):
                if account_lo in members and not self._online(found):
                    del self.found[account_lo]
                    self._free_seat(found.handoff)
                    found.session.log('[MM] Left during "Game Found!": the seat in the match is free')

    def waiting(self) -> int:
        with self.lock:
            return sum(len(ticket.members) for ticket in self.tickets)

    def can_host(self, card: int) -> bool:
        """Whether the server can host a map of the card in one of its modes."""
        if card not in self._hostable:
            rules = self.rules.get(card)
            self._hostable[card] = bool(rules and self._map_choices(rules.maps, rules.rulesets))
        return self._hostable[card]

    def rules_of(self, card: int) -> QueueRules:
        """The card's rules; a card the data does not know plays like Quick Play."""
        return self.rules.get(card) or self.rules.get(QUICK_PLAY) or QueueRules()

    def _try_match(self, card: int) -> None:
        """The card's waiting parties join its matches that have room, and the rest start a new one
        once enough of them wait."""
        rules = self.rules_of(card)
        self._join_open_matches(card, rules)
        waiting = [ticket for ticket in self.tickets if ticket.card == card]
        if not waiting:
            return
        choice = self._pick_map(rules)
        if choice is None:
            log.info("[MM] Card 0x%X (%s): no map the server can host yet", card, rules.name)
            return
        chosen_map, ruleset = choice
        minimum = self.players_to_start(card)
        roster = build_roster(waiting, ruleset, rules.roles, minimum)
        if roster is None:
            count = sum(len(ticket.members) for ticket in waiting)
            full = sum(ruleset.team_sizes)
            needed = min(minimum, full) if minimum else full
            log.info("[MM] Card 0x%X (%s): %d of %d players waiting", card, rules.name, count, needed)
            return
        used = {id(ticket) for team in roster.teams for ticket, _, _ in team}
        self.tickets = [ticket for ticket in self.tickets if id(ticket) not in used]
        self._start(chosen_map, roster, card=card)

    def _join_open_matches(self, card: int, rules: QueueRules) -> None:
        """Waiting parties, oldest first, join the card's matches that have room: a match that still
        waits for its players on any card, one that is on only where fills_running says so. A party
        goes whole into one team. With full teams a role queue keeps its role slots."""
        game = self.server.game
        if game is None or not any(ticket.card == card for ticket in self.tickets):
            return
        slots = {} if self.players_to_start(card) else rules.roles
        for room in game.open_matches(card):
            if not room.waiting and not self.fills_running(card):
                continue
            for ticket in [ticket for ticket in self.tickets if ticket.card == card]:
                placed = _place(ticket, room, slots, bool(rules.roles))
                if placed is None or not self._join(room, ticket, *placed, card):
                    continue
                self.tickets.remove(ticket)
                team, given = placed
                room.free[team] -= len(ticket.members)
                for role in given.values():
                    room.roles[team][role] = room.roles[team].get(role, 0) + 1

    def _join(self, room, ticket: Ticket, team: int, given: dict, card: int) -> bool:
        """The party joins an open match (game.server.OpenMatch) on that team."""
        sessions = [self.server.session_of(account.account_lo) for account, _ in ticket.members]
        entries = [
            (session, ticket, team, given.get(index, 0))
            for index, session in enumerate(sessions)
            if session is not None
        ]
        if not entries:
            return False
        handoffs = self.server.game.join_match(room.match, self._game_players(entries, card))
        if handoffs is None:
            return False
        self._send_to_game(entries, handoffs, room.match.game_map.name, popped=True)
        return True

    def _pick_map(self, rules: QueueRules):
        """A map of the card that the server can host, with the ruleset it is played in. If the dashboard
        forced a map, that one is used with any of the card's rulesets it supports; when the forced map
        can't be hosted in this card's modes, the card's own random pick is used so a match still starts."""
        if self.forced_map is not None:
            forced = self._map_choices([(self.forced_map, ())], rules.rulesets)
            if forced:
                return random.choice(forced)
            log.info(
                "[MM] Forced map 0x%X is not playable in %s; using the card's pick",
                self.forced_map,
                rules.name or "this card",
            )
        choices = self._map_choices(rules.maps, rules.rulesets)
        return random.choice(choices) if choices else None

    @staticmethod
    def _map_choices(maps, rulesets) -> list:
        """The (GameMap, ruleset) pairs the server can host from these maps. `modes` of () allows any
        ruleset mode (so a forced map is tried against every mode the card plays)."""
        choices = []
        for map_guid, modes in maps:
            for ruleset in rulesets:
                if modes and ruleset.mode not in modes:
                    continue
                found = game_map(
                    map_guid, ruleset.mode, ruleset.mode_name, ruleset.free_for_all, ruleset.team_sizes
                )
                if found is not None:
                    choices.append((found, ruleset))
        return choices

    def _players(self, party: Party) -> list:
        """The members who can play: online, not the bot."""
        return [
            member
            for member in party.members
            if not member.virtual and self.server.session_of(member.account_lo)
        ]

    # --- starting a match ----------------------------------------------------------------------

    def practice(self, session) -> None:
        """Training > Practice Range: the player's group goes in together, up to six on one team as in
        retail; alone, the player has it to himself. Members already in a match stay where they are."""
        party = self.server.social.party_of(session.account)
        game = self.server.game
        members = [session.account] + [
            member
            for member in self._players(party)
            if member.account_lo != session.account.account_lo
            and not (game is not None and game.in_match(member.account_lo))
        ]
        ticket = Ticket(party, {}, 0, time.time(), [(member, []) for member in members])
        practice_range = replace(PRACTICE_RANGE, team_sizes=(len(members), 0))
        self._start(practice_range, Roster([[(ticket, member, 0) for member in members]]), popped=False)

    def _start(self, game_map, roster: Roster, popped: bool = True, card: int = 0) -> None:
        server = self.server
        game = server.game
        if game is None:
            log.warning("[MM] The game server is off (--game-port 0)")
            return
        entries = []  # (session, ticket, team, role)
        for team, members in enumerate(roster.teams):
            for ticket, account, role in members:
                session = server.session_of(account.account_lo)
                if session is not None:
                    entries.append((session, ticket, team, role))
        if not entries:
            return
        handoffs = game.create_match(game_map, self._game_players(entries, card), card)
        self._send_to_game(entries, handoffs, game_map.name, popped)

    def _game_players(self, entries: list, card: int) -> list:
        """What the game server takes for each (session, ticket, team, role). Each player's card goes to
        every game of the match (20308): the name, icon, level and portrait frame in the team list; on
        a competitive card also the rank, in a role queue the role."""
        players = []
        for session, _, team, role in entries:
            player = (session.account.account_lo, session.account.name, self._hero(session), team)
            players.append((*player, session.tournament, self._card(session, card, role)))
        return players

    def _send_to_game(self, entries: list, handoffs: list, map_name: str, popped: bool) -> None:
        """The players' searches end and their games go to the match. A queue match (popped) shows
        "Game Found!" first; the Practice Range goes at once."""
        players = [
            (session, ticket, handoff)
            for (session, ticket, _, _), handoff in zip(entries, handoffs, strict=True)
        ]
        if popped:
            for ticket in {id(ticket): ticket for _, ticket, _ in players}.values():
                self._end_search(ticket)
            players = [
                player
                for player in players
                if self._deliver(player, [(QUEUE, 44202, {"+0x78": player[1].key, "+0xA8": QUEUE_POPPED})])
            ]
            if not players:
                return
            away = self._leave_other_games(players)
            if self.found_seconds > 0 or away:
                self._game_found(players, map_name, max(self.found_seconds, AWAY_SECONDS if away else 0.0))
                return
        self._enter_game(players, map_name)

    def _leave_other_games(self, players: list) -> bool:
        """Games still in another match (the Practice Range played while searching) go to the menu first
        (20304, GameServer.take_out). A game that gets its 20600 in a match dials the new one through the
        old world's link and socket; when the old link's close then comes in, the client deletes that
        world with the new link in it, and the screen stays black. From the menu it makes a new
        world for the new match. Whether any game was sent."""
        game = self.server.game
        away = False
        for session, _ticket, handoff in players:
            old = game.elsewhere(session.account.account_lo, handoff.match_id)
            if old is not None:
                done = game.take_out(old, "its queue match was found")
                session.log(f"[MM] {old.match.label()} before the queue match: {done}")
                away = True
        return away

    def _deliver(self, player: tuple, messages: list) -> bool:
        """Send (crc, id, value) messages to a (session, ticket, handoff) player; when its lobby connection
        fails, the seat in the match is freed instead."""
        session, _, handoff = player
        try:
            for crc, msg_id, value in messages:
                session.send(crc, msg_id, value)
        except OSError as error:
            session.log(f"[!] No game, the lobby connection failed: {error}", logging.WARNING)
            self._free_seat(handoff)
            return False
        return True

    def _game_found(self, players: list, map_name: str, seconds: float) -> None:
        """Each player's client shows "Game Found!" (53000, then 53003 with reason GAME_FOUND, see the
        module notes); the 20600 follows after `seconds`. On a competitive card 53001 makes the match
        ranked and 53002 gives the teams, for the VERSUS loading screen (lobby/lineup.py)."""
        batch = []
        versus = self._versus(players)
        for player in players:
            session, ticket, handoff = player
            lobby = match_lobby(ticket.key, handoff.match_id)
            left = lobby_left(handoff.match_id, GAME_FOUND)
            messages = [(MATCH_LOBBY, 53000, lobby), (MATCH_LOBBY, 53003, left)]
            if versus:
                messages.insert(1, (MATCH_LOBBY, 53001, lineup.settings(handoff.match_id)))
                messages.append((MATCH_LOBBY, 53002, versus))
            if not self._deliver(player, messages):
                continue
            # 53003 resets the client's match lobby whatever its +0x78 says (0x7FF789693120), so the old
            # match's leave sends none (LobbyServer.game_left): with both ids 0 again it would pass the
            # client's test lobby +0x88 == post-game +0x30 and run "Leaving match lobby" (0x7FF7896931C4).
            self.server.match_lobbies.pop(session.account.account_lo, None)
            found = Found(session, ticket, handoff, map_name)
            self.found[session.account.account_lo] = found
            batch.append(found)
            session.log(f'[MM] "Game Found!": {map_name}, the game in {seconds:g} s')
        if batch:
            self.schedule(seconds, lambda: self._found_over(batch))

    def _versus(self, players: list) -> dict | None:
        """The match's roster (53002) when it is on a competitive card, else None. Competitive as for the
        game's 20300: the lobby gives the players a rank there (_card)."""
        session, ticket, handoff = players[0]
        if self.server.content.ranked.rank(session.profile, ticket.card, 0) is None:
            return None
        return lineup.roster(self.server, handoff.match_id, ticket.card)

    def _found_over(self, batch: list) -> None:
        """After "Game Found!" the players still there go to the game. One whose lobby connection closed
        in between gets no 20600, and its seat in the match is freed."""
        with self.server.state_lock, self.lock:
            going = []
            for found in batch:
                account_lo = found.session.account.account_lo
                if self.found.get(account_lo) is not found:
                    continue  # cancel() freed its seat
                del self.found[account_lo]
                if self._online(found):
                    going.append((found.session, found.ticket, found.handoff))
                else:
                    self._free_seat(found.handoff)
            if going:
                self._enter_game(going, batch[0].map_name)

    def _online(self, found: Found) -> bool:
        """Whether the player's lobby connection is still the one that got "Game Found!"."""
        session = found.session
        return self.server.session_of(session.account.account_lo) is session and session.logged_in

    def _free_seat(self, handoff) -> None:
        """Free the place the game server keeps for a 20600 that will not be used. The server waits 60 s
        for each (GameServer.handoffs); with the wait over, its next tick drops the player as one who did
        not connect (Match.leave), and LobbyServer.game_left places the parties that wait."""
        game = self.server.game
        if game is None:
            return
        with game.lock:
            waiting = game.handoffs.get(handoff.conn)
            if waiting is not None:
                game.handoffs[handoff.conn] = (waiting[0], waiting[1], 0.0)

    def _enter_game(self, players: list, map_name: str) -> None:
        """(session, ticket, handoff) each: out of General (a match has the team, match and group chats
        only), into the match's chat channel, then 20600."""
        server = self.server
        game = server.game
        channel = server.social.open_match_chat(players[0][2].match_id, [s.account for s, _, _ in players])
        for session, _ticket, handoff in players:
            try:
                host = game_address(server.settings.game_host, session.sock)
                session.send(CHAT_IN, 20404, {"+0x78": server.social.general})
                session.send(CHAT_IN, 20402, {"+0x78": channel})
                session.send(HANDOFF, 20600, handoff_message(handoff, host, game.port))
            except OSError as error:
                session.log(f"[!] No 20600, the lobby connection failed: {error}", logging.WARNING)
                self._free_seat(handoff)
                continue
            where = f"game server {host}:{game.port}, connection {handoff.conn}"
            session.log(f"[MM] Match found: {map_name}, {where}")

    def _end_search(self, ticket: Ticket) -> None:
        """The party is out of the queue; the passes it spent stay spent (a match was found)."""
        party = ticket.party
        party.queue = None
        party.queue_state = 0
        party.accepted.clear()
        party.ready.clear()
        party.pass_roles.clear()
        party.passes_taken.clear()
        self.server.notify_party(party)

    def _hero(self, session) -> int:
        """The hero picked for the menu in the dashboard; the game server takes Soldier: 76 for 0."""
        return self.server.content.menu_hero.picked(session.profile)

    def _card(self, session, card: int = 0, role: int = 0) -> dict:
        """The player's card as the lobby shows it (account id, icon, portrait frame, level, BattleTag),
        and on a competitive queue card the rank there under CARD_RANK: (skill rating, Top 500 place,
        tier) of the role played in a role queue, else of the card's one queue. In a role queue the
        role goes under CARD_ROLE: hero select then offers only that role's heroes."""
        record = self.server.content.player.record(session.profile, session.ident)
        if card:
            rank = self.server.content.ranked.rank(session.profile, card, ROLE_GUIDS.get(role, 0))
            if rank is not None:
                record[CARD_RANK] = rank
        if role:
            record[CARD_ROLE] = role
        return record
