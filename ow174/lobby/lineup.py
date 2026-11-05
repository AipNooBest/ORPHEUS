"""The VERSUS screen a competitive match shows while it loads.

What the client reads (image base 0x7FF788F10000):
- The loading screen (graph 0147, which the client runs itself) shows VERSUS 0501.05A (065E.05A in free for
  all) for 7 s instead of the plain loading screen when the post-game instance type (subsystem 0x30 +0x20)
  is 9 Ranked or 13 Ranked Offseason, or 11 Ruleset with a flag (STU_F667872C, 0x7FF7898427B0).
- Only 53001 sets that type, from its +0x2A4, and only while the match lobby has type 4 (53000) and its
  settings id (+0x278) changed (0x7FF7896949C0). 53003 leaves it alone; leaving a game clears it (20304,
  0x7FF7896E6830).
- The teams are the match lobby's roster (53002, kept at lobby +0xC0 by 0x7FF789694BA0). The screen finds
  the local player by record +0x0 (his account id), takes his team (+0xA8) as the friendly one and needs
  exactly one other team (0x7FF789AAB880). Each record gives the card (+0x0, as 20308), the rating (+0x68,
  as 20700 +0x68), the role (+0xB1, as component 74's) and the group (0x7FF78999A9D0, 0x7FF789999BD0).
- 53003 empties the match lobby (0x7FF789D2E5B0: state, roster, settings), so the roster goes after the
  53003 of "Game Found!". A 53000 only replaces the lobby state (0x7FF789D2E430), so the roster stays when
  the game's own match lobby comes at connect (handlers/leaving.py). Its lobby id lets the screen show the
  groups (+0x90, the party id; 0x7FF789AAB7C0 needs a lobby id). The social menu's team lists need the
  settings' team sizes (0x7FF789A94BB0), which stay empty.
- A game that never connects gets no match lobby and no 53003, and one reader in the menu finds the local
  player in an old roster (0x7FF78973E420 region: not a spectator, team not 2), so the lobby also sends an
  empty roster when a player leaves a ranked match (LobbyServer.game_left).
"""

from ow174.jam.values import id16

RANKED = 9  # the instance type; match.py sends the same in 20300 +0x78
NO_RATING = {"+0x0": [], "+0x18": 0, "+0x20": 0}
NO_ROSTER = {"+0x78": []}


def settings(match_id: tuple[int, int], instance_type: int = RANKED) -> dict:
    """53001: the match lobby's game settings. Only the settings id (the match id: it must differ from the
    last one) and the instance type matter here; the rest stays empty."""
    return {"+0x78": {"+0x278": id16(*match_id), "+0x2A4": instance_type}}


def record(card: dict, team: int, role: int, rating: dict | None, group: tuple[int, int], slot: int) -> dict:
    """One player of the roster (a 184-byte 53002 record). +0xA0 stays 0: the client then adds the card to
    its card cache."""
    return {
        "+0x0": card,
        "+0x68": rating or NO_RATING,
        "+0x90": id16(*group),
        "+0xA8": team,
        "+0xAF": slot,
        "+0xB1": role,
    }


def roster(server, match_id: tuple[int, int], card: int) -> dict:
    """53002: every player still in the match, with his rating on the queue card and his group."""
    records, slots = [], {}
    for account_lo, team, role, player_card in server.game.lineup(match_id):
        session = server.session_of(account_lo)
        rating, group = None, (account_lo, 0)
        if session is not None:
            ratings = server.content.ranked.party_ratings(session.profile)
            rating = next((entry for entry in ratings if entry["+0x18"] == card), None)
            group = tuple(server.social.party_of(session.account).party_id)
        slots[team] = slots.get(team, -1) + 1
        records.append(record(player_card, team, role, rating, group, slots[team]))
    return {"+0x78": records}
