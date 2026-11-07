"""A match: its players, their entities, and what each player's client is told.

How a player gets into the world:
- When the map is loaded the client sends 21601 (and later 21602). 0.3 s later the server sends
  20302 {the player entity}: "this is your entity" (0x7FF7896E6C10 writes world +0x298). It has to
  come before the player's create, or the client does not add its command components (59, 63).
- At least 0.6 s after loading and after acknowledgement of the ownership message and its predecessors,
  one frame creates the map's server-owned placeables (the loading screen only
  ends once they all exist), the player entity (000E.003, flags 0x3C) and the game mode entity
  (16B2.003).
- Once its create is acknowledged, the player's entity gets its controller's statescript (heroselect.py)
  for the HUD and the hero select screen. That frame puts the entity's statescript in owner mode,
  so small owner frames keep telling the client which of its commands the server has (CmFD), else it
  replays old key presses.
- The hero's body comes with the pick, as in retail (OW2 traffic): the body with its movement state
  and an update of the player entity whose component 44 possesses it. A switch replaces the body
  under a new id in the same frame (the client ignores a destroyed id for 30 s).
- The client then moves the body itself; it never tells the server where it is. The server moves
  a copy from the commands, and that copy is what the other players see.
- In the PvP modes the first spawn starts "assemble your team": 30 s shown on the hero select
  screen, which stays open after a pick and closes when the time is up.
- Every client also gets the other players' entities, as in retail (their team lists, B_team_list
  research): each one's card (20308: name, icon, level), his entity with his id, and a plain frame
  with the team list entry 288D and his pick. 288D leaves enemies out of the list on the client.
  The entity also names his portrait frame (component 74), which makes the client load it: the list
  draws a frame only when it is loaded. On a competitive card the match is a ranked one (20300) and
  the entity carries his rank (component 29), which the list shows in place of level and frame.
"""

import logging
import math
from collections import deque
from copy import deepcopy

from ow174.game import bodytrace, bots, combat, correction, heroselect, movelog, notices, pools, stats, world
from ow174.game.collision import (
    CollisionProvider,
    LoadState,
    World,
    default_collision_provider,
    mover_category,
)
from ow174.game.commands import Command, CommandQueue
from ow174.game.content import PRACTICE_RANGE, SOLDIER, GameMap, Hero, heroes, spawn_point
from ow174.game.environment import ExecutionEnvironment, SystemEnvironment
from ow174.game.mover import CROUCHED, GRAVITY_SCALE, Mover, mover_data, statescript_mods
from ow174.game.projectiles import MAX_LEASE_FRAME
from ow174.game.script import expr, graph, nodes, runtime
from ow174.game.script.bot import BotScript
from ow174.game.script.driver import BodyScript, CommandEpochError
from ow174.game.script.runtime import ScriptBudgetError
from ow174.game.statescript import Stream, owner_ack
from ow174.game.world import (
    GAME_MODE_ENTITY,
    NO_INPUT,
    OP_CREATE,
    OP_DESTROY,
    OP_UPDATE,
    PLAYER_PATH,
    WITH_PARTICIPANT,
    WITH_SCALE,
    WITH_TRANSFORM,
    EntityUpdate,
    Movement,
)
from ow174.jam.values import id16

log = logging.getLogger("ow174.game")

PLAYER_DEFINITION = 0x040000000000000E  # 000E.003, the player (controller) entity
GAME_MODE_DEFINITION = 0x04000000000016B2
PLAYER_FLAGS = WITH_PARTICIPANT | PLAYER_PATH | WITH_TRANSFORM | WITH_SCALE
BODY_FLAGS = WITH_TRANSFORM | WITH_SCALE

# Component 26, the filter bits: 0x800000 << team picks the team's physics proxy (team 0 or 1). The
# client's own session builder gives its player 0x10040001 | that bit (0x7FF78973950E), but
# 0x10000000 is the spectator bit (GetEntityIsSpectator, 0x7FF789B2F000): a player with it
# gets the spectator's HUD panel, and a body with only it has no collision and no enemy
# outline. So the player gets 0x00040001 | the team bit, a body only the team bit.
TEAM_BIT = 0x800000
# The client knows five teams, bits 23-27 of the filter (GetTeam 0x7FF7894E9570); team 4 is the
# free-for-all one, whose members are all enemies (0x7FF789B2C690). Bit 28 is the spectator.
FREE_FOR_ALL_TEAM = 4
PLAYER_FILTER = 0x00040001
# The body statescript's health (its HUD number): None = the hero's own pools added up (pools.py).
HEALTH: float | None = None
# The heroes whose body statescript the server runs (ow174/game/script): their weapons and
# abilities work and the client keeps its predictions. None = every hero.
BODY_SCRIPT_HEROES: set[int] | None = None
MAX_BODY_MESSAGES = 256  # bound the match's deferred cross-body work per tick

# ClientInGame 20308 {4 flags, card}: a player's card for the client's player list, keyed by the id in
# his entity's component 29 (0x7FF7896E88B0). With the first and fourth flag on it also posts "<name>
# joined the game." (or left, or started spectating) in the chat.
PLAYER_CARD = 20308
NEW_PLAYERS_PER_TICK = 3  # a card is about 90 bytes, and a datagram's payload at most 1230
# The team list draws a portrait frame only when the frame's asset is loaded (0x7FF78A64BA30), and the
# client loads it when a player entity's component 74 names it (0x7FF7894C9D90). A team list entry is
# filled again whenever a card arrives (UI-world event 0x47 of the upsert), so each card goes once more a
# little later, when the frames have loaded.
CARD_AGAIN_TICKS = 125  # 2 s of 16 ms ticks
# Beside the 20308 fields, the lobby's card may carry the player's rank on a competitive card:
# (skill rating, Top 500 place, tier). It goes in component 29, and makes the match a ranked one.
CARD_RANK = "rank"
# On a role queue card it also carries the role the matchmaker gave the player. Component 74's
# m_roleQueueRole then makes hero select refuse heroes of the other roles (CanPlayHero 0x7FF789CB0F92,
# while the global 0x7FF78BD4FB90 is on, which it is from the start).
CARD_ROLE = "role"
ROLE_NUMBERS = {"Damage": 1, "Tank": 2, "Support": 3}  # a hero's role -> m_roleQueueRole
# 20300 +0x78, the instance type (world system 0x38 +0x18). With 9 the team list shows each
# player's rank (component 29) in place of his level and frame (0x7FF789A15CC9, 0x7FF789A1D8E0).
UNRANKED, RANKED = 1, 9

RETAIL_QUANTA = 16000  # microseconds per command frame; 20300 must name the client's own value
TOURNAMENT_QUANTA = 7000  # tank_TournamentModeTickRate, which tournament mode turns on
# Ticks between the datagrams to each client: retail sent one every third tick, 48 ms (OW2 traffic). The
# statescript frames that go in them are built on those ticks only; bodies and scripts still run every tick.
SEND_EVERY = 3


def sends(tick: int) -> bool:
    """Whether this tick sends the clients a datagram."""
    return tick % SEND_EVERY == 0


# Seconds after the client reports its map loaded.
SEND_OWN_ENTITY = 0.3
SEND_GAME = 0.4
SEND_WORLD = 0.6
SEND_CONTROLLER = 0.7  # a frame after the entity exists
OWNER_ACK_EVERY = 0.1  # seconds between owner frames that only move CmFD on

# "Assemble your team" in the PvP modes: their mode graphs (0F4A, 0EC2) set the game mode entity's
# v1303 to 30 and count it down; 288B shows it on the hero select screen as long as it is open.
ASSEMBLE_SECONDS = 30
COUNTDOWN = 1303
COUNTDOWN_EVERY = 1.0  # the screen shows whole seconds
STATS_EVERY = 0.5  # seconds between updates of a player's own board numbers (component 64, stats.py)

# The bots' creates span several datagrams. Give them this many ticks before adding movement records,
# and wait for each create's ACK separately.
BOT_RECORD_DELAY = 8


def _yaw_units(degrees: float) -> int:
    return round(degrees * 65536 / 360)


def _rotation(yaw_units: int) -> tuple[float, float, float, float]:
    half = yaw_units * math.pi / 65536
    return (0.0, math.sin(half), 0.0, math.cos(half))


class ShownController:
    """Another player's controller entity as one client has it."""

    def __init__(self, entity: int, created: int) -> None:
        self.script = Stream(entity)  # its statescript on that client: plain (H=0) full frames
        self.create_acked = False
        self.pick: int | None = None  # the hero its last frame carried
        self.card_again: int | None = created + CARD_AGAIN_TICKS  # when its card goes once more
        self.possession: tuple | None = None  # the (body, hero, skin theme) its last 44 and 75 named

    def arrived(self, last: int) -> None:
        self.create_acked = True


class Player:
    def __init__(
        self,
        match: "Match",
        slot: int,
        account_lo: int,
        name: str,
        hero: Hero,
        team: int,
        quanta: int,
        card: dict | None = None,
    ):
        self.match = match
        self.slot = slot
        self.account_lo = account_lo
        self.name = name
        # The player's card (20308) as the lobby shows it: account id, icon, portrait frame, level and
        # BattleTag. Without one from the lobby, only the ids and the name.
        card = dict(card or {"+0x0": id16(account_lo), "+0x10": id16(account_lo), "+0x40": name})
        # (skill rating, Top 500 place, tier) on a competitive card, else None
        self.rank: tuple[int, int, int] | None = card.pop(CARD_RANK, None)
        self.role: int = card.pop(CARD_ROLE, 0)  # ROLE_NUMBERS; 0 = any hero
        self.card = card
        self.card_sent = False  # the player's client has its own card
        self.card_again: int | None = None  # when its own card goes to it once more
        self.hero = hero
        self.team = team
        self.quanta = quanta
        self.entity = 0xA0000000 | (slot + 1) << 8
        self.body = self.entity | 1
        self.has_body = False  # not before the first pick
        self.skin = (0, False)  # the body's (skin theme, golden weapon)
        self.client = None  # the connected client (server.Client), None before it joins or after it left
        self.gone = False  # left the match, or its game never connected
        self.loaded_at: float | None = None
        self.steps_done = 0
        self.spawned = False
        # The n-th player of the team takes the team's n-th spawn point; in a free-for-all everyone is
        # on a team of his own, so the n-th player of the match.
        if match.game_map.free_for_all:
            number = len(match.players)
        else:
            number = sum(1 for other in match.players if other.team == team)
        self.spawn = spawn_point(match.game_map, team, number)  # (position, yaw in degrees)
        self.mover = self.new_mover()
        self.commands = CommandQueue()
        self.body_script: BodyScript | None = None  # the body's statescript, run from the commands
        self.body_script_failed = False  # a hero reselect can replace this body's failed script
        # Other players' bodies this player's client has: body -> False in the frame of its create.
        self.seen: dict[int, bool] = {}
        self.shown: dict[int, ShownController] = {}  # other players' controller entities on this client
        self.script = Stream(self.entity)  # the player entity's statescript: the controller
        self.mode_script = Stream(GAME_MODE_ENTITY)  # the game mode entity's variables: the countdown
        self.next_countdown = 0.0
        self.select_open = True
        self.controller_hero: int | None = None  # the hero (v940) the last controller frame named
        # The numbers his Tab board shows (stats.py); the combat code adds to them. His client gets them
        # in component 64 when they change.
        self.stats = stats.PlayerStats()
        self.stats_sent: list | None = None
        self.next_stats = 0.0

    @property
    def team_bit(self) -> int:
        """The filter bit of the player's team; in a free-for-all the free-for-all team's, since each
        player's own team number would run past the five teams into the spectator bit."""
        if self.match.game_map.free_for_all:
            return TEAM_BIT << FREE_FOR_ALL_TEAM
        return TEAM_BIT << self.team

    @property
    def player_id(self) -> tuple[int, int]:
        """The 16-byte id of the player's entity (component 29) as (low, high): his card's key. The
        lobby's account id, which a client also holds for itself (20500): its own entity is then the
        local player in the team list."""
        account = self.card["+0x0"]
        return account["+0x0"], account["+0x8"]

    def movement(self) -> Movement:
        """The body's movement state for other players' clients, as the client's own mover writes it: their
        animation plays idle, run, crouch or a jump from it. A movement key counts as held while the last
        command with throttles is less than 0.1 s old (0x7FF789B20A70), so a standing body carries the
        tick of that command. Ticks are the server's, like the records' frames."""
        mover = self.mover
        tick = mover.input_tick
        # The record's frame is the match's tick: that frame must go as the default, or the client would
        # not read +34/+35.
        if mover.throttles != (0, 0) or (tick is not None and tick >= self.match.tick):
            input_frame = None  # moving now: the state's own frame
        else:
            input_frame = NO_INPUT if tick is None else tick
        return Movement(
            mover.position,
            mover.yaw,
            mover.pitch,
            mover.velocity,
            mover.flags | (combat.DEAD if self.match.combat.is_dead(self) else 0),
            throttles=mover.throttles,
            input_frame=input_frame,
            input_throttles=mover.input_throttles,
            air_ticks=mover.air_ticks,
            gravity=GRAVITY_SCALE,
        )

    def pose(self) -> Movement:
        """The own body's state in its create and first record: only the pose and the crouch, as
        always. The client moves its own body itself."""
        mover = self.mover
        flags = CROUCHED if mover.crouched else 0
        return Movement(mover.position, mover.yaw, mover.pitch, mover.velocity, flags)

    def take_commands(self, commands: list[Command]) -> int | None:
        """Simulate the encodable command prefix and report the first frame needing a new link."""
        collision = self.match.collision_world
        if collision is None:
            return None
        seconds = self.quanta / 1e6
        script = self.body_script
        fresh, boundary = self.commands.before_limit(
            commands, MAX_LEASE_FRAME, anchor=script.frame if script is not None else None
        )
        if fresh:
            self.mover.use(mover_data(self.hero.body), collision)
        if movelog.ENABLED:
            movelog.commands(self, fresh, received=commands)
        for command in fresh:
            if self.has_body:
                dead = self.match.combat.is_dead(self)
                first_pose = command.frame if dead else self.mover.first_step_frame(command.frame)
                self._advance_body_gap(first_pose)
                if not dead and self.body_script is not None:
                    self.mover.step(
                        command, seconds, self.match.tick,
                        simulate_pose=self._step_body_pose,
                    )
                else:
                    if not dead:  # a dead body lies where it fell
                        self.mover.step(command, seconds, self.match.tick)
                    self._advance_body_frame(command)
        if movelog.ENABLED:
            movelog.snapshots(self)
        return boundary

    def _advance_body_gap(self, first_pose: int) -> None:
        """Release old input and advance timers across frames without a movement pose."""
        script = self.body_script
        if script is None or script.frame + 1 >= first_pose:
            return
        first_missing = script.frame + 1
        self._advance_body_frame(Command(first_missing), posed=False)
        if first_missing < first_pose - 1:
            self._advance_body_frame(Command(first_pose - 1), posed=False)

    def _step_body_pose(self, command: Command, seconds: float, tick: int) -> None:
        script = self.body_script
        requests = []
        if script is not None:
            requests = [
                (state, movement)
                for instance in script.component.instances.values()
                for state in instance.states.values()
                if isinstance(state, nodes.MoveTo) and (movement := state.move_request()) is not None
            ]
        # Arbitration between simultaneous world-target states is unresolved.
        movement_end = None
        if len(requests) == 1:
            state, (target, speed) = requests[0]
            outcome = self.mover.step_pose(
                command, seconds, tick, move_to=((target.x, target.y, target.z), speed),
            )
            if outcome is not None:
                movement_end = state, outcome
        else:
            self.mover.step_pose(command, seconds, tick)
        self._advance_body_frame(command, movement_end=movement_end)

    def _advance_body_frame(
        self, command: Command, *, posed: bool = True, movement_end: tuple[nodes.MoveTo, bool] | None = None,
    ) -> None:
        """Resolve body input and consume combat reports from this simulation step."""
        if self.body_script is not None:
            self.match.body_command(self, command, movement_end=movement_end)
            self.match.combat.command(self, command if posed else None)
        # Modifier changes take effect on the next movement frame.
        script = self.body_script
        self.mover.track_mods(statescript_mods(script.component) if script else {}, command.frame)

    def new_body(self, entity: int) -> None:
        """Set up the allocated body at the spawn point."""
        self.body = entity
        self.mover = self.new_mover()

    def new_mover(self) -> Mover:
        """The body's mover at the spawn point, with the hero's values and the match's pinned collision.
        It cannot run before collision is ready. It uses what stops its own team's physics
        proxy (some props stop one team only, as on Blizzard World), both in a free-for-all."""
        game_map = self.match.game_map
        world = self.match.collision_world
        category = mover_category(self.team, game_map.free_for_all)
        return Mover(
            self.spawn[0], _yaw_units(self.spawn[1]), world, mover_data(self.hero.body), category=category
        )

    def describe(self) -> str:
        return f"{self.name} ({self.hero.name}, team {self.team + 1})"


def no_skin(account_lo: int, hero: int) -> tuple[int, bool]:
    return 0, False


def card_message(player: Player) -> dict:
    """20308 with the player's card. The client keeps the card unless it has one with a newer +0x30
    for the same id (0x7FF789529C90), such as a friend's from the lobby. No chat text."""
    return {"+0x78": False, "+0x80": player.card}


class Match:
    def __init__(
        self,
        game_map: GameMap,
        on_leave=None,
        skin_of=no_skin,
        card: int = 0,
        *,
        environment: ExecutionEnvironment | None = None,
        collision_provider: CollisionProvider | None = None,
    ) -> None:
        self.environment = SystemEnvironment() if environment is None else environment
        self.id = (
            self.environment.randbits(63, callsite="match.id.low"),
            self.environment.randbits(63, callsite="match.id.high"),
        )
        self.game_map = game_map
        self.card = card  # the queue card it was made for; 0 = none (the Practice Range)
        # (account, hero GUID) -> (skin theme, golden weapon) the player has equipped, from the lobby.
        # The client's hero select equips a skin through the lobby (24500), even in a match.
        self.skin_of = skin_of
        self.controller = heroselect.controller_of(game_map.mode_guid)
        self.tick = 0  # the server's frame number, set every tick
        self.assemble_ends: float | None = None  # PvP: when "assemble your team" ends
        self.assembled = False
        self.players: list[Player] = []
        self._body_messages: list[tuple[BodyScript, Player, BodyScript, int, dict]] = []
        self.started = self.environment.wall_time(callsite="match.started")
        self.on_leave = on_leave  # called with each player who leaves
        self.ended = False
        self.bots = bots.make(game_map.map_guid, free_for_all=game_map.free_for_all)
        self._pending_bot_scripts = deque(bot for bot in self.bots if graph.entity_body(bot.body) is not None)
        self.bots_shown: dict[int, int] = {}  # player slot -> the tick the bots' creates went to its queue
        self.notices = notices.Notices(self)  # joined / left / hero switch lines in the clients' chat
        self.combat = combat.Combat(self)  # hits, health, deaths, respawns (combat.py)
        self.collision_provider = (
            default_collision_provider() if collision_provider is None else collision_provider
        )
        self.collision_state = LoadState("pending")
        self.collision_world: World | None = None
        self._collision_failed = False
        self._refresh_collision()

    def _refresh_collision(self) -> bool:
        """Observe asynchronous completion at a match boundary, then keep the World for this match."""
        if self.collision_state.status == "pending":
            game_map = self.game_map
            self.collision_state = self.collision_provider.poll(
                game_map.map_guid, game_map.name, game_map.mode_guid
            )
            if self.collision_state.status == "ready":
                self.collision_world = self.collision_state.world
                for player in self.players:
                    player.mover.use(world=self.collision_world)
        return self.collision_world is not None

    def _collision_failure(self, now: float) -> None:
        if self._collision_failed:
            return
        self._collision_failed = True
        reason = f"collision unavailable: {self.collision_state.error}"
        log.error("[game] %s: %s", self.label(), reason)
        for player in self.players:
            if not player.gone:
                if player.client is not None:
                    player.client.go_home(now)
                self.leave(player, reason)
        self.ended = True

    def add_player(
        self,
        account_lo: int,
        name: str,
        hero_guid: int,
        team: int,
        tournament: bool,
        card: dict | None = None,
    ) -> Player:
        hero = heroes().get(hero_guid) or heroes()[SOLDIER]
        quanta = TOURNAMENT_QUANTA if tournament else RETAIL_QUANTA
        player = Player(self, len(self.players), account_lo, name, hero, team, quanta, card)
        self.players.append(player)
        return player

    def label(self) -> str:
        return f"match {self.id[0] & 0xFFFF:04X}"

    # --- messages ------------------------------------------------------------------------------

    def game_struct(self, viewer: Player) -> dict:
        """The 1064-byte game struct of 20300 and 20301: the mode, the players and their teams. A team's
        +0x60 is its number and +0x61 its size, from which the hero select screen's team list takes its
        empty slots (0x7FF789CADB40; size minus the players it lists). Players who left are not in it:
        a player who joins later takes their place."""
        playing = [player for player in self.players if not player.gone]
        records = [{"+0x50": player.name} for player in playing]
        sizes = self.game_map.team_sizes
        teams = []
        if self.game_map.free_for_all:  # the mode's one team, "FFA", holds everyone
            slots = [{"+0x0": player.entity} for player in playing]
            teams.append({"+0x18": slots, "+0x60": FREE_FOR_ALL_TEAM, "+0x61": sizes[0]})
        for team in () if self.game_map.free_for_all else (0, 1):
            slots = [{"+0x0": player.entity} for player in playing if player.team == team]
            size = sizes[team] if team < len(sizes) else 0
            teams.append({"+0x18": slots, "+0x60": team, "+0x61": size})
        return {
            "+0x0": {
                "+0x0": {
                    "+0x0": {"+0x0": records},
                    "+0xB0": self.game_map.mode_guid,
                    "+0xC0": teams,
                    "+0xD8": [{"+0x0": viewer.entity}],
                },
                "+0x274": 16,
            }
        }

    @property
    def ranked(self) -> bool:
        """Whether the match is played on a competitive card: the lobby gave its players a rank."""
        return any(player.rank is not None for player in self.players)

    def instance_message(self, viewer: Player) -> dict:
        """20300: load this map with this mode. +0x500 must equal the client's command frame length
        (0x7FF7896E94B0), else error 262445; +0x519 must match a byte that is 0 (error 262455). +0x78 is
        the instance type: ranked on a competitive card, so the team list shows the players' ranks."""
        return {
            "+0x78": RANKED if self.ranked else UNRANKED,
            "+0x80": self.game_struct(viewer),
            "+0x4A8": self.game_map.map_guid,
            "+0x500": viewer.quanta,
            "+0x508": {"+0x0": 1, "+0x8": 1},
        }

    # --- entities ------------------------------------------------------------------------------

    def placeable_ids(self) -> list[int]:
        return [0x80000000 | index for index in self.game_map.placeables]

    def spawn_updates(self, viewer: Player) -> list[EntityUpdate]:
        """The frame that ends the loading screen: the placeables, the viewer's player (and body, if it
        has one), the game mode entity, the training bots, and the bodies of the players already in the
        world."""
        updates = [
            EntityUpdate(entity, OP_CREATE, lambda origin, index=index: world.create_placeable(origin, index))
            for entity, index in zip(self.placeable_ids(), self.game_map.placeables, strict=True)
        ]
        if viewer.has_body:
            updates += self.own_body_updates(viewer)
        updates.append(
            EntityUpdate(viewer.entity, OP_CREATE, lambda origin: self._player_create(origin, viewer))
        )
        updates.append(EntityUpdate(GAME_MODE_ENTITY, OP_CREATE, self._game_mode_create))
        updates.extend(self._bot_create_update(bot) for bot in self.bots)
        if self.bots:
            self.bots_shown[viewer.slot] = self.tick
        for other in self.players:
            if other is not viewer and self._shown(other):
                updates.append(self.remote_body_create(viewer, other))
        return updates

    def _prepare_bot_script(self, bot: bots.Bot) -> None:
        if graph.entity_body(bot.body) is None:
            return
        try:
            firing = (bot.firing and self.game_map.map_guid == PRACTICE_RANGE.map_guid
                      and self.game_map.mode_guid == PRACTICE_RANGE.mode_guid)
            bot.body_script = BotScript(
                bot.body, bot.hero, bot.entity, self.tick, self.game_map.mode_guid, bot.health,
                firing_position=bot.position if firing else None,
            )
        except Exception:
            log.exception("[game] %s: could not initialize %s's statescript", self.label(), bot.name)

    def _bot_create_update(self, bot: bots.Bot) -> EntityUpdate:
        # A later viewer cannot depend on the globally cleared health dirty bits.
        # Freeze its initial pools here so retransmitting the create keeps the same baseline.
        vitals = self.combat.vitals.get(bot.entity)
        components = world.health(bot.health, bot.health) if vitals is None else vitals.component(vitals.ids)
        components[26] = [TEAM_BIT << bot.team]
        return EntityUpdate(bot.entity, OP_CREATE, lambda origin: self._bot_create(origin, bot, components))

    @staticmethod
    def _bot_create(origin: int, bot: bots.Bot, components: dict) -> world.RecordWriter:
        """A training bot's body, as a player's body on another client: placed by its transform, moved
        by movement records from the next frame on. Without a player body's 123 (the hero): the bots'
        bodies have no such component, and the client fails the whole frame on one it cannot place
        (assert 0xD62712AE, "Failed to deserialize component update ... 143E ... /123"; 143E has 1, 3, 4,
        22, 24, 25, 26, 28, 31, 35, 39, 43, 47, 49, 51, 69, 76, 78, 83, 86, 90, 97, 115, 119, 122, 127)."""
        return world.create(
            origin, bot.body, components, flags=BODY_FLAGS, position=bot.position, rotation=_rotation(bot.yaw)
        )

    @staticmethod
    def _shown(player: Player) -> bool:
        """Whether other players' clients get this player's body."""
        return player.spawned and player.has_body and player.client is not None

    def own_body_updates(self, viewer: Player) -> list[EntityUpdate]:
        """The viewer's own body: its movement state rides in the create (at frame 0xFFFFFFFE) and in a
        movement record at the packet frame."""
        state = deepcopy(viewer.pose())
        create = EntityUpdate(viewer.body, OP_CREATE, movement=state)
        self.start_body_script(viewer, create)
        # The new script must exist before its projectile lease is captured. The create's
        # stream remains the native driver's arrival gate; only the payload is frozen here.
        create.build = self._body_builder(viewer, state, own=True)
        return [create]

    def _body_create(
        self, origin: int, owner: Player, state: Movement | None, own: bool = False
    ) -> world.RecordWriter:
        return self._body_builder(owner, state, own)(origin)

    def _body_builder(self, owner: Player, state: Movement | None, own: bool = False):
        """Freeze a player's current health pools, team and hero, with the projectile lease for its owner."""
        vitals = self.combat.vitals.get(owner.body)
        if vitals is None:
            health, armour, shields = pools.body_pools(owner.hero.guid)
            components = world.health(health, health, armour, shields)
        else:
            components = vitals.component(vitals.ids)
        components[26] = [owner.team_bit]
        components[123] = [owner.hero.guid]
        if own:
            lease = self.combat.projectiles.lease(owner)
            if lease:
                components[90] = [lease]
        definition, skin = owner.hero.body, tuple(owner.skin)
        position, rotation = tuple(owner.mover.position), _rotation(owner.mover.yaw)
        components, state = deepcopy(components), deepcopy(state)

        def build(origin: int) -> world.RecordWriter:
            return world.create(
                origin,
                definition,
                components,
                flags=BODY_FLAGS,
                position=position,
                rotation=rotation,
                movement=state,
                skin=skin,
            )

        return build

    def _player_create(self, origin: int, player: Player, own: bool = True) -> world.RecordWriter:
        """A player's entity, on the player's own client or another's. The same flags for both: the
        client adds the command components (59, 63) only to the entity 20302 named (0x7FF78955EE90). It
        carries the team in the filter bits (26, which the client copies into the component GetTeam and
        IsEnemy read), the player's info (29) and the hero select participant (74) with the portrait
        frame, which makes every client load the frame the team list draws (0x7FF7894C9D90), and the
        role queue role; on the player's own client also the body the player possesses (44, 75) and his
        Tab board numbers (64, owner-only). Other clients get his 44 and 75 once his body is on them
        (_show_players)."""
        frame = player.card.get("+0x28") or None
        components = {
            26: [PLAYER_FILTER | player.team_bit],
            29: self._player_info(player),
            74: [frame, None, player.slot, None, None, player.role or None, 1, 1, None],
        }
        if own and player.has_body:
            components.update(self._possession(player))
        if own:
            # Packet budget probes may serialize a candidate without sending it.
            components[stats.GMP_STATS] = self._board(player)
        return world.create(origin, PLAYER_DEFINITION, components, flags=PLAYER_FLAGS)

    @staticmethod
    def _player_info(player: Player) -> list:
        """Component 29 (MPlayerComponentData): the player's id, his BattleTag (m_battleTag: the client
        names the entity by it, as in the hero switch line of the chat), his level (m_playerLevel, the HUD
        panel's number) and on a competitive card his rank (m_rankedLevel, m_heroicRank,
        m_rankedLevelTier), which the team list shows (0x7FF789A16620)."""
        rating, place, tier = player.rank or (None, None, None)
        tag = player.card.get("+0x40") or player.name or None
        return [player.player_id, tag, player.card.get("+0x38"), None, None, None, None, rating, place, tier]

    @staticmethod
    def _possession(player: Player) -> dict:
        """Components 44 (the body the player possesses, views and controls) and 75 (its hero, skin
        theme and the last valid hero)."""
        theme = player.skin[0] or None
        return {44: [player.body] * 3, 75: [player.hero.guid, theme, None, player.hero.guid]}

    def _board(self, player: Player) -> list:
        """Component 64's board fields for the player's own client (stats.py): the mode's player stats
        with their medals among his teammates (all players in a free-for-all) and his hero's stats."""
        team = [
            other
            for other in self.players
            if not other.gone and (self.game_map.free_for_all or other.team == player.team)
        ]
        if player not in team:
            team.append(player)
        mode = self.game_map.mode_guid
        medals = stats.medals([other.stats for other in team], mode)[team.index(player)]
        hero = player.hero.guid if player.has_body else None
        return stats.component(player.stats, mode, hero, medals)

    def _send_board(self, player: Player, now: float) -> None:
        """The board numbers again when they changed, at most every STATS_EVERY seconds."""
        if not player.client.controller_ready or now < player.next_stats:
            return
        values = self._board(player)
        if values == player.stats_sent:
            return
        player.stats_sent = values
        player.next_stats = now + STATS_EVERY
        update = {stats.GMP_STATS: values}
        player.client.queue_entities(
            [EntityUpdate(player.entity, OP_UPDATE, lambda origin: world.update(origin, update))]
        )

    def _game_mode_create(self, origin: int) -> world.RecordWriter:
        return world.create(origin, GAME_MODE_DEFINITION, {114: [None, self.game_map.mode_guid]})

    def remote_body_create(self, viewer: Player, other: Player) -> EntityUpdate:
        """Another player's body: placed by its transform, then moved by movement records from the next
        frame on (a movement state in the create would sit at frame 0xFFFFFFFE, and the client refuses
        every later record that is not newer)."""
        viewer.seen[other.body] = False
        update = EntityUpdate(other.body, OP_CREATE, self._body_builder(other, None))
        self.combat.observers.created(viewer, other, update)  # its statescript once it arrived (observers.py)
        return update

    def ensure_remote_body(self, viewer: Player, other: Player) -> None:
        """Queue the current visible body's create once, including before the next match update."""
        if (viewer is not other and viewer.spawned and viewer.client is not None
                and self._shown(other) and other.body not in viewer.seen):
            viewer.client.queue_entities([self.remote_body_create(viewer, other)])

    def remote_movements(self, viewer: Player) -> list[EntityUpdate]:
        """A movement record for every other player's body in every frame, standing or not, as the
        retail server sent them (OW2's live traffic); the first in the frame after the body's create. And
        the viewer's own body's state, which its client checks its prediction against (correction.py)."""
        updates = []
        for other in self.players:
            if other is viewer or other.body not in viewer.seen:
                continue
            if not viewer.seen[other.body]:
                continue
            updates.append(EntityUpdate(other.body, movement=other.movement()))
        shown = self.bots_shown.get(viewer.slot)
        client = viewer.client
        if client is not None and shown is not None and self.tick - shown >= BOT_RECORD_DELAY:
            updates += [
                EntityUpdate(bot.entity, movement=bot.movement(self.tick))
                for bot in self.bots
                if client.bot_create_acked(bot.entity) and bot.record_due(self.tick, SEND_EVERY)
            ]
        own = correction.own_record(viewer, self.tick)
        if own is not None:
            updates.append(own)
        return updates

    @staticmethod
    def sent_updates(viewer: Player, updates) -> None:
        """Commit only records selected for a successfully sent datagram, never budget candidates."""
        script = viewer.body_script
        for update in updates:
            if update.op == OP_CREATE and update.entity in viewer.seen:
                viewer.seen[update.entity] = True
            correction.sent(viewer, update)
            if (
                script is not None and not script.retired and update.entity == viewer.body
                and update.stream is script.stream and update.chunk is not None and not update.resend
            ):
                script.report_sent(update.chunk_last)

    def destroy_for_others(self, player: Player, body: int) -> None:
        for other in self.players:
            if other is not player and body in other.seen:
                del other.seen[body]
                if other.client is not None:
                    shown = other.shown.get(player.entity)
                    if shown is not None and shown.possession is not None and shown.possession[0] == body:
                        shown.possession = None
                        other.client.cancel_possession(player.entity)
                    other.client.queue_entities([EntityUpdate(body, OP_DESTROY)])

    # --- the other players, for the hero select team list ---------------------------------------

    @staticmethod
    def _in_world(player: Player) -> bool:
        """Whether the other clients have this player's entity: from his spawn until he leaves."""
        return player.spawned and player.client is not None

    def _show_players(self, viewer: Player) -> None:
        """Keep the viewer's client up to date on the players in the world: their cards (its own one
        too), each other player's entity, created with his id, and after its acknowledgement a frame with
        the team list entry and his pick, sent again when the pick changes. A player who left goes.
        Every player, teammate or enemy, as retail does: 288D keeps enemies out of the list. A few new
        players per tick, so that their cards fit in one datagram with the rest. Each card goes once
        more CARD_AGAIN_TICKS later, which redraws the team list after the portrait frames loaded."""
        client = viewer.client
        if not viewer.card_sent:
            viewer.card_sent = True
            viewer.card_again = self.tick + CARD_AGAIN_TICKS
            client.queue_reliable(PLAYER_CARD, card_message(viewer))
        elif viewer.card_again is not None and self.tick >= viewer.card_again:
            viewer.card_again = None
            client.queue_reliable(PLAYER_CARD, card_message(viewer))
        cards = 0  # other players' cards this tick, new ones and ones sent again
        for other in self.players:
            if other is viewer:
                continue
            shown = viewer.shown.get(other.entity)
            if not self._in_world(other):
                if shown is not None:
                    del viewer.shown[other.entity]
                    client.queue_entities([EntityUpdate(other.entity, OP_DESTROY)])
                continue
            if shown is None:
                if cards == NEW_PLAYERS_PER_TICK:
                    continue
                cards += 1
                shown = viewer.shown[other.entity] = ShownController(other.entity, self.tick)
                client.queue_reliable(PLAYER_CARD, self.notices.card(viewer, other))
                create = EntityUpdate(
                    other.entity, OP_CREATE,
                    lambda origin, other=other: self._player_create(origin, other, own=False),
                )
                create.stream = shown
                client.queue_entities([create])
                continue
            again = shown.card_again
            if again is not None and self.tick >= again and cards < NEW_PLAYERS_PER_TICK:
                cards += 1
                shown.card_again = None
                client.queue_reliable(PLAYER_CARD, card_message(other))
            pick = other.hero.guid if other.has_body else None
            if shown.create_acked and (not shown.script.last or pick != shown.pick):
                shown.pick = pick
                client.queue_entities([shown.script.full_frame(heroselect.team_entry_frame(pick))])
            self._show_possession(viewer, other, shown)

    def _show_possession(self, viewer: Player, other: Player, shown: ShownController) -> None:
        """Another player's 44 (his body) and 75 (his hero) on the viewer's client, once that client has
        both his entity and his body, and again after a switch. The Tab board shows the hero of every
        row from 75 (0x7FF789A16620, read when 74 is there), and 44 makes the client set the body's
        possessor (43, the comp 44 hook 0x7FF789719850), by which it names the body (0x7FF789CB3F80) and
        finds his On Fire flag (0x7FF789BE2370). The hook's re-possess (0x7FF789AB2CF0) resets the input
        and the mover only for a possessor with component 59, which only the local player has; a ch3
        record of the body likewise feeds a possessor's 59 only (0x7FF7896DAA95)."""
        if (
            not shown.create_acked or not other.has_body
            or other.body not in viewer.seen or not viewer.client.bot_create_acked(other.body)
        ):
            return
        possession = (other.body, other.hero.guid, other.skin[0])
        if possession == shown.possession:
            return
        shown.possession = possession
        components = self._possession(other)
        update = EntityUpdate(other.entity, OP_UPDATE, lambda origin: world.update(origin, components))
        viewer.client.queue_possession(update, other.body)

    # --- lifetime ------------------------------------------------------------------------------

    def update(self, now: float, tick: int, *, frames: bool | None = None) -> None:
        """Run the spawn steps of players who loaded, keep their statescript acked, walk the training
        bots, and show new players and bodies to the others."""
        if frames is None:
            frames = sends(tick)
        self.tick = tick
        ready = self._refresh_collision()
        if self.collision_state.status == "unavailable":
            self._collision_failure(now)
            return
        if ready and self.bots:
            if self._pending_bot_scripts:
                # Keep graph setup within the loading window and off the world-create burst.
                self._prepare_bot_script(self._pending_bot_scripts.popleft())
            for bot in self.bots:
                bot.step(tick, RETAIL_QUANTA / 1e6, self.collision_world)
        if ready:
            self.combat.update(now, tick, frames=frames)
        if self.assemble_ends is not None and not self.assembled and now >= self.assemble_ends:
            self._end_assemble(now)
        for player in self.players:
            client = player.client
            if client is None:
                continue
            if player.loaded_at is not None:
                self._spawn_steps(player, client, now)
            if player.script.data_last and now >= player.script.next_ack:
                self._owner_ack(player, player.script, now)
            if player.body_script is not None and player.spawned and frames:
                self._body_frames(player, client)
            if self.assembling() and player.steps_done >= 4 and now >= player.next_countdown:
                self._send_countdown(player, now)
            if player.steps_done >= 4:
                self._send_board(player, now)
                if (player.hero.guid if player.has_body else None) != player.controller_hero:
                    self.send_controller(player)  # v940 follows the hero (a switch that kept the screen)
        self.notices.update(now)
        for viewer in self.players:
            if not viewer.spawned or viewer.client is None:
                continue
            self._show_players(viewer)
            for other in self.players:
                self.ensure_remote_body(viewer, other)

    def _spawn_steps(self, player: Player, client, now: float) -> None:
        waited = now - player.loaded_at
        if player.steps_done == 0 and waited >= SEND_OWN_ENTITY:
            client.queue_ownership()
            player.steps_done = 1
        if player.steps_done == 1 and waited >= SEND_GAME:
            client.queue_reliable(20301, {"+0x78": self.game_struct(player)})
            player.steps_done = 2
        if (
            player.steps_done == 2
            and waited >= SEND_WORLD
            and client.ownership_delivered()
            and self.collision_world is not None
            and not self._pending_bot_scripts
        ):
            client.queue_entities(self.spawn_updates(player))
            player.steps_done = 3
            player.spawned = True
            self.notices.entered_world(player)
            log.info("[game] %s: %s spawned", self.label(), player.describe())
            if self.controller is heroselect.PVP and self.assemble_ends is None:
                self.assemble_ends = now + ASSEMBLE_SECONDS
                log.info("[game] %s: assemble your team, %d s", self.label(), ASSEMBLE_SECONDS)
        if player.steps_done == 3 and waited >= SEND_CONTROLLER:
            player.steps_done = 4
            self.send_controller(player)
            self.combat.spawned(player)
        elif player.steps_done == 4 and not player.script.data_last:
            self.send_controller(player)  # the initial frame may be waiting for the create ACK

    # --- assembling heroes ---------------------------------------------------------------------

    def assembling(self) -> bool:
        return self.assemble_ends is not None and not self.assembled

    def _send_countdown(self, player: Player, now: float) -> None:
        """The seconds left, as the game mode entity's v1303. The client shows them rounded up."""
        player.next_countdown = now + COUNTDOWN_EVERY
        seconds = max(0.0, self.assemble_ends - now)
        frame = combat.mode_frame({COUNTDOWN: float(seconds)})  # with the mode's UI graph (combat.py)
        player.client.queue_mode_frame(frame)

    def _end_assemble(self, now: float) -> None:
        """The countdown is over: the screens of the players who have a hero close, and the skin
        selector goes."""
        self.assembled = True
        log.info("[game] %s: heroes assembled", self.label())
        for player in self.players:
            if player.client is None or player.steps_done < 4:
                continue
            self._send_countdown(player, now)
            if player.has_body:
                player.select_open = False
            self.send_controller(player)

    # --- the player's statescript --------------------------------------------------------------

    def _newest_frame(self, player: Player) -> int:
        """The newest command frame we have from the client, or our tick before the first."""
        frame = player.commands.last_frame
        return self.tick if frame is None else frame

    def send_controller(self, player: Player) -> None:
        """A full frame of the controller's statescript, with hero select open or closed. The skin
        selector shows in the PvP modes while heroes are assembled, in the Practice Range whenever
        the screen is open."""
        if player.client is None or not player.client.controller_ready:
            return
        cmfd = player.script.next_cmfd(self._newest_frame(player))
        practice = self.controller is heroselect.PRACTICE
        skins = player.select_open and (practice or self.assembling())
        switch = self.notices.presence(player)  # the last hero switch for the chat line (notices.py)
        hero = player.controller_hero = player.hero.guid if player.has_body else None
        frame = heroselect.controller_frame(
            self.controller, cmfd, player.select_open, skins, practice, presence=switch, hero=hero
        )
        update = player.script.full_frame(frame)
        # A switch notice is a separate event, even when the menu is already closed.
        update.controller_close = player.has_body and not player.select_open and not switch
        player.client.queue_entities([update])
        state = "open" if player.select_open else "closed"
        log.info("[game] %s: %s's controller sent (hero select %s)", self.label(), player.name, state)

    def _owner_ack(self, player: Player, stream: Stream, now: float) -> None:
        """A delta owner frame that only moves CmFD on, so the client replays fewer of its own commands.
        Only once the frames with data have arrived: a newer chunk whose range covers a queued one makes
        the client drop that one."""
        stream.next_ack = now + OWNER_ACK_EVERY
        if stream.data_in_flight or (player.commands.last_frame or 0) <= stream.cmfd:
            return
        cmfd = stream.next_cmfd(self._newest_frame(player))
        player.client.queue_entities([stream.ack_delta(owner_ack(cmfd))])

    # --- the body's statescript (ow174/game/script) ------------------------------------------------

    def script_body_position(self, entity: int, owner: int) -> expr.Vec3 | None:
        """The current match pose of a body, including the owner during its own create."""
        for player in self.players:
            if (player.body == entity and player.has_body and player.client is not None
                    and (player.spawned or entity == owner)):
                return expr.Vec3(*player.mover.position)
        for bot in self.bots:
            if bot.entity == entity:
                return expr.Vec3(*bot.position)
        return None

    def _message_player(self, entity: int) -> Player | None:
        shown = (player for player in self.players if player.body == entity and self._shown(player))
        return next(shown, None)

    def queue_body_message(self, target: int, message: int, params: dict, sender: int) -> bool:
        """Capture the source and recipient scripts before either entity ID can be reused."""
        source, recipient = self._message_player(sender), self._message_player(target)
        if source is None or recipient is None:
            return False
        source_script, script = source.body_script, recipient.body_script
        if source_script is None or source_script.retired or script is None or script.retired:
            return False
        if len(self._body_messages) >= MAX_BODY_MESSAGES:
            expr.warn_once("body-message-limit", "match body message queue reached %d", MAX_BODY_MESSAGES)
            return False
        self._body_messages.append((source_script, recipient, script, message, dict(params)))
        return True

    def deliver_body_messages(self) -> None:
        """Drain one match batch without advancing recipients' command frames."""
        if not self._body_messages:
            return
        pending, self._body_messages = self._body_messages, []
        # Messages emitted by a receiver enter the next batch, outside its VM call stack.
        for source, player, script, message, params in pending:
            if source.retired or source.component.fault is not None:
                continue
            if player.body_script is not script or script.retired or not self._shown(player):
                continue
            try:
                component = script.component
                # An earlier message can start or stop a later message's entry state.
                if runtime.send_message(component, message, params, source.entity):
                    component.run_now()
            except Exception:
                log.exception("[game] %s: %s's body message handling failed", self.label(), player.name)
                self.stop_body_script(player, failed=True)

    def start_body_script(self, player: Player, create: EntityUpdate) -> None:
        """The statescript of the player's new body; the old body's stops. It runs from
        the newest command frame on, and its full frame goes out once the client acknowledged the create."""
        self.stop_body_script(player)
        if BODY_SCRIPT_HEROES is not None and player.hero.guid not in BODY_SCRIPT_HEROES:
            return
        try:
            script = BodyScript(
                player.hero.guid,
                player.body,
                self._newest_frame(player),
                quanta=player.quanta,
                mode=self.game_map.mode_guid,
                health=HEALTH,
                match=self,
            )
        except Exception as error:
            player.body_script_failed = isinstance(error, ScriptBudgetError)
            log.exception("[game] %s: no body statescript for %s", self.label(), player.describe())
            return
        script.created(create)
        player.body_script = script
        bodytrace.started(player, script)

    @staticmethod
    def stop_body_script(player: Player, *, failed: bool = False) -> None:
        script = player.body_script
        if script is not None:
            bodytrace.stopped(player, script)
            initial = script.stream.initial_owner
            if initial is not None and player.client is not None:
                player.client.cancel_entity_record(initial)
            script.retire()
            player.body_script = None
        player.body_script_failed = failed

    def body_command(
        self, player: Player, command: Command, *, movement_end: tuple[nodes.MoveTo, bool] | None = None,
    ) -> None:
        """One new command frame for the body's statescript. A failure turns it off for this body (logged)
        instead of breaking the tick."""
        script = player.body_script
        before_hash = bodytrace.before(script)
        try:
            if movement_end is not None:
                state, arrived = movement_end
                delay = command.frame * script.frame_ms - script.component.now
                state.request(runtime.FINISH if arrived else runtime.ABORT, max(0, delay))
            script.command(command.frame, command.buttons, command.action, command.forward, command.right)
        except Exception as error:
            bodytrace.failed(script, error)
            log.exception("[game] %s: %s's body statescript failed", self.label(), player.name)
            # Command epochs also involve the controller stream and movement history.
            self.stop_body_script(player, failed=not isinstance(error, CommandEpochError))
        else:
            bodytrace.command(player, script, command, before_hash)

    def _body_frames(self, player: Player, client) -> None:
        """This tick's frames of the body's statescript: the full frame, then owner frames."""
        try:
            script = player.body_script
            # A newer unsent projection stays non-correcting until the first
            # owner frame goes out. Its command frame starts the settle interval.
            replace_initial = (
                script.first_owner_frame is not None and not script.report_count
                and bool(script.stream.pending)
            )
            kill_feed = None
            if self.combat.is_dead(player):
                kill_feed = self.combat.kill_feed_allowed(player, player.body)
            client.queue_entities(script.frames(
                kill_feed=kill_feed, replace_initial=replace_initial, queued=client.entities,
            ))
        except Exception:
            log.exception("[game] %s: %s's body statescript failed", self.label(), player.name)
            self.stop_body_script(player, failed=True)

    def game_message(self, player: Player, value: dict, now: float) -> None:
        """21616: a game message a statescript action sends to the server, such as a hero pick."""
        message = value.get("+0xA8", 0)
        parameters = bytes(value.get("+0x80") or b"")
        log.info(
            "[game] %s: %s sent game message %X (instance %s) %s",
            self.label(),
            player.name,
            message & 0xFFFFFFFF,
            value.get("+0xB0"),
            parameters.hex(" "),
        )
        if message == heroselect.PICK_HERO:
            hero = heroes().get(heroselect.picked_hero(parameters) or 0)
            if hero is None:
                log.warning("[game] %s: no hero found in the pick", self.label())
                return
            if player.role and ROLE_NUMBERS.get(hero.role) != player.role:
                label, name = self.label(), player.name
                log.warning("[game] %s: %s picked %s, not of the queued role", label, name, hero.name)
                return
            skin = self.skin_of(player.account_lo, hero.guid)
            needs_body = not player.has_body or player.body_script_failed
            if ((needs_body or hero is not player.hero or skin != player.skin)
                    and not self.switch_hero(player, hero)):
                return
            if self.assembling():
                return  # the screen stays open until the heroes are assembled, as in retail
            player.select_open = False
            self.send_controller(player)
        elif message == heroselect.OPEN_OR_CLOSE:
            wanted = heroselect.wanted_open(parameters)
            player.select_open = not player.select_open if wanted is None else wanted
            if not player.has_body:
                player.select_open = True  # no hero yet: nothing to go back to
            self.send_controller(player)

    def skin_changed(self, player: Player, hero_guid: int) -> bool:
        """A skin equipped in the lobby for the hero the player plays: a new body with it, as a pick of
        the same hero with another skin gets (switch_hero). Other heroes' skins wait for their pick."""
        if not player.has_body or player.hero.guid != hero_guid:
            return False
        if self.skin_of(player.account_lo, hero_guid) == player.skin:
            return False
        return self.switch_hero(player, player.hero)

    def map_loaded(self, player: Player, now: float) -> None:
        if player.loaded_at is None:
            player.loaded_at = now
            log.info("[game] %s: %s loaded the map", self.label(), player.name)

    def _next_body_id(self, player: Player) -> int | None:
        """Walk the player's ID ring, excluding bodies any connected peer still retains."""
        clients = [viewer.client for viewer in self.players if viewer.client is not None]
        suffix = player.body & 0xFF
        for offset in range(254):
            entity = player.entity | ((suffix + offset) % 255 + 1)
            if all(client.body_id_available(entity) for client in clients):
                return entity
        return None

    def switch_hero(self, player: Player, hero: Hero) -> bool:
        """The body of a picked hero. The first one just comes. A switch creates the new body under a
        new id at the spawn point, moves the player's components 44 and 75 to it and destroys the old
        body. These records can share a datagram in that order. When the packet budget splits them,
        the later records wait for acknowledgement of their prerequisites."""
        old = player.body if player.has_body else None
        old_hero = player.hero.guid if old is not None else None
        if old is not None:
            entity = self._next_body_id(player)
            if entity is None:
                return False
            self.combat.retire_player_body(player)
            player.new_body(entity)
            self.combat.record_replacement(entity)
        player.hero = hero
        player.skin = self.skin_of(player.account_lo, hero.guid)
        player.has_body = True
        if player.client is not None and player.spawned:
            components = self._possession(player)
            player.client.queue_entities(self.own_body_updates(player))
            player.client.queue_possession(
                EntityUpdate(player.entity, OP_UPDATE, lambda origin: world.update(origin, components)),
                player.body,
            )
            if old is not None:
                player.client.queue_entities([EntityUpdate(old, OP_DESTROY)])
        if old is not None:
            self.destroy_for_others(player, old)
        self.notices.hero_switched(player, old_hero)
        theme, golden = player.skin
        look = f", skin theme {theme:X}{' + golden weapon' if golden else ''}" if theme else ""
        log.info("[game] %s: %s plays %s%s", self.label(), player.name, hero.name, look)
        return True

    def leave(self, player: Player, reason: str = "left") -> None:
        """The player's game left the match, or never came: the lobby takes the player back (the match
        chat goes), and the match ends with its last player."""
        if player.gone:
            return
        self.notices.left(player)
        player.gone = True
        player.client = None
        player.spawned = False
        self.stop_body_script(player)
        if player.has_body:
            self.combat.retire_player_body(player)
            self.destroy_for_others(player, player.body)
        log.info("[game] %s: %s %s", self.label(), player.name, reason)
        if self.on_leave is not None:
            self.on_leave(player)
        if all(other.gone for other in self.players):
            self.ended = True

    @property
    def waiting_for_players(self) -> bool:
        """Before hero select counts down: the players are still joining."""
        return self.assemble_ends is None

    def free_places(self) -> list[int]:
        """Places left per team: the mode's team sizes less the players still in the match (one entry
        for free for all)."""
        playing = [player for player in self.players if not player.gone]
        sizes = self.game_map.team_sizes
        if self.game_map.free_for_all:
            return [sizes[0] - len(playing)]
        return [size - sum(1 for player in playing if player.team == team) for team, size in enumerate(sizes)]

    def roles_taken(self) -> list[dict[int, int]]:
        """Per team, how many players still in the match play each role queue role."""
        taken: list[dict[int, int]] = [{} for _ in self.game_map.team_sizes]
        for player in self.players:
            if not player.gone and player.role and player.team < len(taken):
                taken[player.team][player.role] = taken[player.team].get(player.role, 0) + 1
        return taken

    def snapshot(self) -> dict:
        return {
            "id": self.label(),
            "map": self.game_map.name,
            "started": self.started,
            "collision": {"state": self.collision_state.status, "error": self.collision_state.error},
            "players": [
                {
                    "name": player.name,
                    "hero": player.hero.name,
                    "team": player.team + 1,
                    "state": "playing" if player.spawned else ("loading" if player.client else "waiting"),
                }
                for player in self.players
            ],
        }


def pong(ping: dict, received_ns: int, *, environment: ExecutionEnvironment | None = None) -> dict:
    """20306 for a 21610 ping: its index and stamps, plus when we got it and when we answer."""
    environment = SystemEnvironment() if environment is None else environment
    stamps = [*ping.get("+0x80", []), received_ns, environment.wall_time_ns(callsite="match.pong.sent_ns")]
    return {"+0x78": ping.get("+0x78", 0), "+0x80": stamps}
