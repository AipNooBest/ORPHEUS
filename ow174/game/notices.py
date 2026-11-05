"""The chat notices of a match: a player joined or left the game, and a hero switch. The clients have the
texts (in their own language); the server only triggers them.

Read in the client (image base 0x7FF788F10000):
- 20308 {4 flags, card} (0x7FF7896E88B0) keeps the card and, with +0x78 and +0x7B on, posts the card's name
  and a text: " left the game." (112C.07C) with +0x79 off, else " joined the game." (112B) with +0x7A on or
  " started spectating." (2C0B) with +0x7A off. These are the only "<name> + text" lines of a match; the
  client's other ones (the table 0x7FF78B5209B0) come from the lobby: friends, groups, custom games.
- "%1$s switched to %2$s (was %3$s)." (0AA4.07C) is graph data: the PvP controller graph 0C90 stores three
  instance variables from game message 037F.025, the old hero (v4513), the new one (v4514) and the player
  (v4515), and a client-only Watch on v4514 posts the line (action STU_7106C356, 0x7FF7899E15D0) when v4514
  differs from the value it saw last and the old hero is a hero that differs. The name is that entity's
  component 29 m_battleTag (0x7FF789CB3F80), and the line shows only if the controller running the graph is
  the local player or a teammate (0x7FF789B2C0E0).
  Retail most likely sent 037F to the switching player's controller (288B takes its new hero as his pick),
  and every client saw the three on its copy of that 0C90. Our clients run 0C90 only on their own
  controller, so the server sets the variables there: on the switching player's and on each teammate's. The
  line is the same; it is attached to the reader's own card rather than the switcher's.
- We send controller frames whole, and a full frame first clears the variables the network set, telling the
  Watch at once (0x7FF78AAB45B0 -> 0x7FF78AA9ED40), then sets the new ones. Sent again in a later frame, the
  three would make the Watch fire on the clearing and post the line again; after other values, it would
  fire twice. So they go in one frame only, and only to a client whose last frame had none of them.
"""

from dataclasses import dataclass

from ow174.game import heroselect
from ow174.game.statescript import Asset, Entity, Value

PLAYER_CARD = 20308
# Retail modes whose teams' controller graph is 0C90 (0C5 m_teams[].m_controllerScript): only these have
# the switch notice. Free for all (0F74), capture the flag (0CA2), elimination (0D2A) and the rest have
# other controllers without it.
SWITCH_MODES = {
    0x0230000000000010,
    0x0230000000000014,
    0x0230000000000015,
    0x0230000000000016,
    0x0230000000000017,
    0x023000000000005A,
    0x0230000000000067,
}
# 0C90's presence bits (its m_syncVars entries with an identifier and the instance scope, in order) of
# v4513 (the old hero), v4514 (the new hero) and v4515 (the player).
OLD_HERO_BIT, NEW_HERO_BIT, PLAYER_BIT = 4, 6, 7
# A frame of ours waits until the client has every controller frame sent before it, and a little more: the
# Watch's events of one frame have to run before the next frame changes the variables again.
NOTICE_GAP = 0.1


def card_message(card: dict) -> dict:
    """20308 with a card and no text."""
    return {"+0x78": False, "+0x80": card}


def joined_message(card: dict) -> dict:
    """20308: "<name> joined the game." (+0x79 and +0x7A on)."""
    return {"+0x78": True, "+0x79": True, "+0x7A": True, "+0x7B": True, "+0x80": card}


def left_message(card: dict) -> dict:
    """20308: "<name> left the game." (+0x79 off)."""
    return {"+0x78": True, "+0x79": False, "+0x7A": False, "+0x7B": True, "+0x80": card}


@dataclass(frozen=True)
class Switch:
    old: int  # the hero GUIDs
    new: int
    player: int  # the switching player's controller entity, whose component 29 has his name

    def presence(self) -> dict[int, Value]:
        return {OLD_HERO_BIT: Asset(self.old), NEW_HERO_BIT: Asset(self.new), PLAYER_BIT: Entity(self.player)}


class Notices:
    def __init__(self, match) -> None:
        self.match = match
        self.entered: dict[int, int] = {}  # player slot -> the tick he came into the world
        self.waiting: dict[int, list[Switch]] = {}  # player slot -> switches still to show him
        self.notice_chunk: dict[int, int] = {}  # player slot -> the chunk of his last notice frame
        self.sent_at: dict[int, float] = {}  # player slot -> when we last sent him a frame
        self.sending: tuple[int, Switch] | None = None  # (slot, switch) while its frame is made

    @property
    def switch_notices(self) -> bool:
        return self.match.controller is heroselect.PVP and self.match.game_map.mode_guid in SWITCH_MODES

    # --- joined and left (20308) ---------------------------------------------------------------

    def entered_world(self, player) -> None:
        self.entered.setdefault(player.slot, self.match.tick)

    def card(self, viewer, other) -> dict:
        """The 20308 that first gives the viewer's client another player's card: "joined the game" when he
        came into the world after the viewer."""
        tick = self.match.tick
        if self.entered.get(other.slot, tick) > self.entered.get(viewer.slot, tick):
            return joined_message(other.card)
        return card_message(other.card)

    def left(self, player) -> None:
        """The "left the game" line to the clients that have the player, before they drop him. A game that
        never came into the world was never shown, so it gets no line."""
        for viewer in self.match.players:
            if viewer is not player and viewer.client is not None and player.entity in viewer.shown:
                viewer.client.queue_reliable(PLAYER_CARD, left_message(player.card))
        for table in (self.waiting, self.notice_chunk, self.sent_at):
            table.pop(player.slot, None)

    # --- hero switches (0C90) ------------------------------------------------------------------

    def presence(self, player) -> dict[int, Value]:
        """The 0C90 presence bits of a switch, in the one controller frame that shows it; none otherwise."""
        if self.sending is None or self.sending[0] != player.slot:
            return {}
        return self.sending[1].presence()

    def hero_switched(self, player, old_hero: int | None) -> None:
        """A switch from one hero to another: the player and the teammates whose clients have his entity
        (its component 29 gives the name) get the line; the next update sends it. The first pick is not a
        switch."""
        new_hero = player.hero.guid
        if old_hero is None or old_hero == new_hero or not self.switch_notices:
            return
        switch = Switch(old_hero, new_hero, player.entity)
        free_for_all = self.match.game_map.free_for_all  # everyone is an enemy (team 4, 0x7FF789B2CF60)
        for reader in self.match.players:
            teammate = reader.team == player.team and not free_for_all and player.entity in reader.shown
            if reader is not player and not teammate:
                continue
            if reader.spawned and reader.client is not None and reader.steps_done >= 4:
                self.waiting.setdefault(reader.slot, []).append(switch)

    def update(self, now: float) -> None:
        """Send the waiting switches, one controller frame each, once the client has every controller frame
        sent before. When his last one was a notice frame, a plain controller frame goes first: it clears the
        three (the old hero goes too, so that posts nothing)."""
        for player in self.match.players:
            waiting = self.waiting.get(player.slot)
            if not waiting:
                continue
            if player.client is None:
                del self.waiting[player.slot]
                continue
            script = player.script
            if script.delivered < script.data_last or now < self.sent_at.get(player.slot, 0.0) + NOTICE_GAP:
                continue
            if script.data_last == self.notice_chunk.get(player.slot):
                self.match.send_controller(player)
            else:
                self.sending = (player.slot, waiting.pop(0))
                try:
                    self.match.send_controller(player)
                finally:
                    self.sending = None
                self.notice_chunk[player.slot] = script.data_last
            self.sent_at[player.slot] = now
