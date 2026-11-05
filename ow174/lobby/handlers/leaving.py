"""Leaving a match: "Leave" and "Leave as group" of the in-match leave dialog (05A/0715, graph 0C1E), and the
match lobby the client needs for them.

What the client does (PC 1.74):
- With a match lobby id (system 0x6B +0x88) both buttons send 52902 {+0x78 lobby id, +0x88 as group,
  +0x89 0} (0x7FF789695010) and wait for the server: the client does not leave the game by itself.
  "Leave as group" is offered only to the leader of a party with others, outside competitive
  (CanLeaveAsGroup, 0C1E #24749).
- Without one, "Leave" sends 21801 {} and leaves 3 s later by itself (0x7FF7896831A0), and "Leave as group"
  only cancels a queue entry (44102, 0x7FF789A9DEB0): nothing reaches the server.
- No client path leaves the party: a player who leaves alone is taken out of it by the server.

So every game that connects gets a match lobby (53000, lobby id = match id) and loses it again (53003) when
it leaves the match. On 52902 the server takes the players out of the match (20304 on the game link,
GameServer.take_out): the leader's whole party that is in the same match when he leaves as a group (the
party stays), else only the player, who also leaves the party. 21801 leaves the party the same way.
"""

from ow174.jam.groups import MATCH_LOBBY, MATCH_LOBBY_OUT, OUT_CONNECT
from ow174.lobby.router import Router
from ow174.lobby.session import Session

routes = Router()

LEAVE_MATCH_LOBBY = 52902  # {+0x78 lobby id, +0x88 as group, +0x89 bool} (0x7FF789695010)
LEAVING_GAME = 21801  # {}: the client leaves its game by itself 3 s later (0x7FF7896831A0)
LOBBY_STATE = 53000
LOBBY_LEFT = 53003
# 53000 type of a game's match lobby: 0 (UNKNOWN). The dialog sends 52902 with any type. 4 (GAME_QUEUE)
# would also turn the client's play state to 5 for the whole match (0x7FF789A9D590), which the menu's search
# widget shows as "Game Found!"; with 0 the play state stays 0.
LOBBY_TYPE = 0


def lobby_state(match_id: tuple) -> dict:
    """53000: the match lobby of the player's match. No queue key, so the client always resets the lobby
    itself after its own 52902 (0x7FF789A9DBC0 keeps it only for a queue card asset)."""
    return {"+0x78": {"+0x30": {"+0x0": list(match_id)}, "+0x60": LOBBY_TYPE}, "+0xE0": False, "+0xE8": 0}


def lobby_left(match_id: tuple) -> dict:
    """53003: out of the match lobby (0x7FF789693120 resets it). +0x88 0 and reason 0: no status shown."""
    return {"+0x78": {"+0x0": list(match_id)}, "+0x88": 0, "+0x90": {"+0x0": [0, 0]}, "+0xA0": 0}


def game_joined(server, player) -> None:
    """A player's game connected to its match: its client gets the match lobby. Under the state lock."""
    session = server.session_of(player.account_lo)
    if session is None:
        return
    if session.send(MATCH_LOBBY, LOBBY_STATE, lobby_state(player.match.id)):
        server.match_lobbies[player.account_lo] = player.match.id
        session.log(f"[>>>] Match lobby of {player.match.label()} (53000)")


def game_left(server, session, player) -> None:
    """A player left its match: its client leaves that match lobby, unless it reset it itself (52902).
    Under the state lock."""
    if server.match_lobbies.get(player.account_lo) != player.match.id:
        return
    del server.match_lobbies[player.account_lo]
    if session is not None:
        session.send(MATCH_LOBBY, LOBBY_LEFT, lobby_left(player.match.id))


def leave_party(session: Session, why: str) -> None:
    """Out of the party, as 22107 does; nothing for a party of one."""
    server = session.server
    party = server.social.party_of(session.account)
    if len(party.members) < 2:
        return
    server.social.leave(session.account)
    server.notify_party(party)
    session.send_all(session.party_messages())
    session.log(f"[<<<] Left the party ({why})")


@routes.on(MATCH_LOBBY_OUT, LEAVE_MATCH_LOBBY)
def leave_match_lobby(session: Session, value: dict) -> None:
    """52902: "Leave" or "Leave as group" in the leave dialog, with the match lobby the client has."""
    server = session.server
    account = session.account
    as_group = bool(value.get("+0x88"))
    lobby_id = tuple((value.get("+0x78") or {}).get("+0x0") or (0, 0))
    if server.match_lobbies.get(account.account_lo) == lobby_id:
        del server.match_lobbies[account.account_lo]  # the client resets its lobby itself
    if value.get("+0x89"):
        # Only STU_3618B2A9 sets it, while a match lobby request (52900) waits (0x7FF78999E3A0): a cancel in
        # the menu, not a leave.
        session.log("[<<<] Leave match lobby (52902) for a waiting request: nothing to leave")
        return
    game = server.game
    player = game.player_of(account.account_lo) if game is not None else None
    if player is None:
        session.log(f"[<<<] Leave match lobby (52902, as group {as_group}): not in a match")
        return
    if tuple(player.match.id) != lobby_id:
        session.log(f"[<<<] Leave match lobby (52902): lobby {lobby_id} is not {player.match.label()}")
    party = server.social.party_of(account)
    if as_group and party.leader is account and len(party.members) > 1:
        members = [game.player_of(member.account_lo) for member in party.members]
        members = [other for other in members if other is not None and other.match is player.match]
        done = [f"{other.name} {game.take_out(other, 'left with the group')}" for other in members]
        session.log(f"[<<<] Leave as group (52902), {player.match.label()}: {', '.join(done)}")
        return
    session.log(f"[<<<] Leave (52902), {player.match.label()}: {game.take_out(player, 'left')}")
    leave_party(session, "left the match alone")


@routes.on(OUT_CONNECT, LEAVING_GAME)
def leaving_game(session: Session, value: dict) -> None:
    """21801: the client leaves its game by itself 3 s later (a leave without a match lobby, the loot box
    dialog, the Esc menu's direct leave). Leaving alone takes the player out of the party, but not after
    the server sent the game home (take_out, send_home), as the client may send 21801 on its way out."""
    game = session.server.game
    player = game.player_of(session.account.account_lo) if game is not None else None
    if player is None or game.going_home(player):
        session.log("[<<<] Leaving the game (21801)")
        return
    session.log(f"[<<<] Leaving the game (21801), {player.match.label()}")
    leave_party(session, "left the match alone")
