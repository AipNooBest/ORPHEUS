"""How the server turns damage, healing and eliminations into ultimate charge.

OW1 computed these rates inside its own server code: no client artifact carries them. On 1.74 data
the hero's entity definition has no charge component (its components are health, weapon, mover,
model, first-person, statescript, target tags and voice) and the generic settings have no charge
keys, so the numbers below are the table's, and the table says how far they can be trusted.

The table is data: game_ult_charge_174.json, generated like the other game files. A rate is stated
as the amount of damage (or healing) that fills the meter, never as points: cost #338e differs per
hero, so points are computed against the cost the hero's own Entries carry (see driver.py), and a
meter fills identically whatever scale its graphs use.
"""

import json
import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from ow174.paths import DATA_DIR

log = logging.getLogger("ow174")

TABLE_PATH = DATA_DIR / "game_ult_charge_174.json"

# The fallback for a hero the table has no entry for, so one missing line cannot make a hero
# unplayable; it is the community's Soldier rate (see the file's status). Without the file at all
# nothing but the passive rule (driver.ULT_SECONDS) charges, and that is announced, not silent.
FALLBACK = {"damage_to_full": 380.0, "heal_to_full": None, "kill_bonus_percent": 20.0}

DAMAGE, HEALING, KILL = "damage", "healing", "kill"


@dataclass(frozen=True)
class Rule:
    damage_to_full: float | None  # HP dealt that fills the meter; None: damage does not charge
    heal_to_full: float | None  # HP healed that fills it; None: healing does not charge
    kill_bonus_percent: float | None  # percent of a full meter per elimination; None: no bonus
    known: bool  # False: the hero has no line and FALLBACK speaks


@cache
def _table() -> dict:
    if not Path(TABLE_PATH).is_file():
        log.warning("[game] %s is missing: only the passive rule charges ultimates", TABLE_PATH.name)
        return {}
    table = json.loads(Path(TABLE_PATH).read_text(encoding="utf-8"))
    if table.get("status") == "placeholder":
        log.info("[game] ultimate charge rates are placeholder data: %s", table.get("note", "unverified"))
    return table


def rule(hero: int) -> Rule:
    """The hero's line with the table's defaults under it, or the fallback with no file."""
    table = _table()
    base = {**table.get("defaults", FALLBACK), **(table.get("heroes") or {}).get(f"{hero:016X}", {})}
    for field in ("damage_to_full", "heal_to_full", "kill_bonus_percent"):
        base.setdefault(field, FALLBACK[field])
    known = f"{hero:016X}" in (table.get("heroes") or {})
    return Rule(**base, known=known)


def points(kind: str, hero: int, amount: float, cost: float) -> float:
    """The charge points for one combat event of the hero, in the meter's own scale."""
    if kind not in (DAMAGE, HEALING, KILL):
        raise ValueError(f"unknown charge event {kind!r}")
    if cost <= 0.0:
        return 0.0  # a meterless ultimate (no graph set its cost): nothing to fill
    hero_rule = rule(hero)
    if kind == KILL:
        percent = hero_rule.kill_bonus_percent
        return 0.0 if percent is None else cost * percent / 100.0
    limit = hero_rule.damage_to_full if kind == DAMAGE else hero_rule.heal_to_full
    if limit is None or limit <= 0.0:
        return 0.0
    return cost * amount / limit
