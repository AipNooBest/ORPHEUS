"""A player's match stats, as his Tab board shows them.

What the board reads (image base 0x7FF788F10000):
- The client opens it itself: its HUD makes a local entity (01F1.003, 0x7FF789A1AEE0) and starts graph 041C
  on it (0x7FF789A0DF86), whose SubScript 01BE holds the board (presenter STU_A4F3808F, screen 0403.05A)
  while the Scoreboard key (logical button 77, or 78 to toggle) is down.
- Its rows are every player entity with a slot (component 74) and a team (component 25 from 26), split by
  the relation to the local player (0x7FF7899B6B90): the name, level and rank come from his card (20308), the
  hero from his component 75 (match.py sends both).
- Its own numbers are the local player's component 64 (0x7FF7899B3F90 -> 0x7FF789A1CB00): fields 5-12 name 8
  stats (062 GUIDs) with their values in 21-28 and a medal each in 37-44, fields 13-18 name 6 more with values
  in 29-34. A GUID of 0 is no entry. The client prints a stat's name and its value the way the stat's display
  type says (0x7FF789ABFD50): a count rounded ("n0"), a fraction as a percentage ("p0"), seconds as a time.
  A medal (the stat item's Rank, 005A.05E) is 1 gold, 2 silver, 3 bronze, 0 none. Fields 5-34 and 37-44 are
  owner-only (field flag 0x10), so only the player's own client gets them.
- Which stats: the game data's lists (data/game_stats_174.json, tools/extract_game_stats.py): the player stats
  of 0051.054 (eliminations, objective kills, objective time, hero damage, healing, deaths; some modes list
  others) in the medal slots, the hero's own stats (his 075) in the other six. Which list goes in which slots
  is unverified: the medals and the slot counts fit (8 >= the 6 player stats, every hero list has 5 or 6
  entries).
- Medals are server rules (unverified, as OW1 showed them): among teammates, the best of each stat gets gold,
  the second silver, the third bronze, for a value above 0 and a stat the data does not flag (Deaths).

The combat code adds to a player's record (player.stats.add(stats.ELIMINATIONS)); Match sends the
component when the numbers change.
"""

import json
from functools import cache

from ow174.game import onfire  # registers component 64's fields in world.COMPONENT_FIELDS
from ow174.paths import DATA_DIR

STATS_PATH = DATA_DIR / "game_stats_174.json"
GMP_STATS = onfire.GMP_STATS
MEDAL_SLOTS, OTHER_SLOTS = 8, 6
MEDAL_STAT, OTHER_STAT = 5, 13  # the first field of each list's stat GUIDs
MEDAL_VALUE, OTHER_VALUE = 21, 29  # and of their values
MEDAL = 37  # the first medal field
GOLD, SILVER, BRONZE = 1, 2, 3

# The player stats of 0051.054 (the per-match ones the board lists in most modes).
ELIMINATIONS = 0x0860000000000025
OBJECTIVE_KILLS = 0x0860000000000326
OBJECTIVE_TIME = 0x0860000000000327  # seconds
HERO_DAMAGE = 0x08600000000004B8
HEALING = 0x08600000000001D1
DEATHS = 0x0860000000000029


@cache
def _data() -> dict:
    return json.loads(STATS_PATH.read_text(encoding="utf-8"))


def _pick(lists: dict, mode: int) -> list[tuple[int, bool]]:
    entries = lists["modes"].get(f"0x{mode:016X}", lists["default"])
    return [(int(stat, 16), bool(no_medal)) for stat, no_medal in entries]


def player_stats(mode: int) -> list[tuple[int, bool]]:
    """(stat, no medal) the board lists as the player stats in a game mode."""
    return _pick(_data()["player"], mode)


def hero_stats(hero: int, mode: int) -> list[tuple[int, bool]]:
    """(stat, no medal) the board lists for a hero in a game mode; [] for an unknown hero."""
    lists = _data()["heroes"].get(f"0x{hero:016X}")
    return _pick(lists, mode) if lists else []


def display_type(stat: int) -> int | None:
    """How the client prints the stat's value (STUStatDisplayType): 0 a count, 2 a fraction, 8 seconds."""
    return _data()["stats"].get(f"0x{stat:016X}")


class PlayerStats:
    """One player's numbers in this match, by stat GUID."""

    def __init__(self) -> None:
        self.values: dict[int, float] = {}

    def add(self, stat: int, amount: float = 1.0) -> None:
        self.values[stat] = self.values.get(stat, 0.0) + amount

    def set(self, stat: int, value: float) -> None:
        self.values[stat] = value

    def value(self, stat: int) -> float:
        return self.values.get(stat, 0.0)


def medals(team: list[PlayerStats], mode: int) -> list[dict[int, int]]:
    """Per record of one team, the medal of each player stat: gold, silver and bronze for the three best
    values above 0 (unverified: OW1's rule; ties keep the team's order)."""
    out: list[dict[int, int]] = [{} for _ in team]
    for stat, no_medal in player_stats(mode):
        if no_medal:
            continue
        scored = [k for k in range(len(team)) if team[k].value(stat) > 0]
        ranked = sorted(scored, key=lambda k: -team[k].value(stat))
        for place, k in enumerate(ranked[:3]):
            out[k][stat] = GOLD + place
    return out


def component(record: PlayerStats, mode: int, hero: int | None, medal: dict[int, int] | None = None) -> list:
    """Component 64's board fields for the player's own client: every slot is written (GUID 0 when the list
    is shorter), so a hero switch clears the old hero's stats. hero None: no hero yet, no hero stats."""
    values: list = [None] * len(onfire.FIELDS)
    lists = (
        (player_stats(mode), MEDAL_SLOTS, MEDAL_STAT, MEDAL_VALUE),
        (hero_stats(hero, mode) if hero else [], OTHER_SLOTS, OTHER_STAT, OTHER_VALUE),
    )
    for entries, slots, stat_field, value_field in lists:
        for k in range(slots):
            stat = entries[k][0] if k < len(entries) else 0
            values[stat_field + k] = stat
            values[value_field + k] = record.value(stat) if stat else 0.0
    for k in range(MEDAL_SLOTS):
        stat = values[MEDAL_STAT + k]
        values[MEDAL + k] = (medal or {}).get(stat, 0) if stat else 0
    return values
