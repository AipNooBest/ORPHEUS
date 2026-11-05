"""A hero body's health pools, as the common body graph 0033 makes them.

0033's first Entry begins a HealthPool for each pool the body has: st1 (type 2, shields) with #221 when it
is set, st3 (type 1, armour) with #1736 when it is set, and st0 (type 0, health) with #199. The values are
0033's schema defaults (#199 1000, #1736 0, #221 0) or the body's overrides of them (the initial graph
0033 of its STUStatescriptComponent): Soldier 200, Reinhardt 300 and 200 armour, Zarya 200 and 200
shields, Wrecking Ball 500 and 100 armour, Zenyatta 50 and 150 shields. D.Va's 150 is her pilot's; the mech's
pools come from her own graphs (0109), which only her body statescript would add.
"""

import logging
from functools import cache

from ow174.game.script import graph

log = logging.getLogger("ow174.game")

BODY_GRAPH = 0x0033
HEALTH_VAR, ARMOUR_VAR, SHIELDS_VAR = 199, 1736, 221
FALLBACK = (200.0, 0.0, 0.0)  # a hero without body data: the flat value the server sent before


def _numbers(entries) -> dict[int, float]:
    """Schema entries (STUStatescriptSchemaEntry) as variable -> number."""
    values = {}
    for entry in entries or []:
        var = int(entry["m_0D09D2D9"], 16) & 0xFFFF
        value = (entry.get("m_value") or {}).get("m_value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[var] = float(value)
    return values


@cache
def hero_pools(hero: int) -> tuple[float, float, float] | None:
    """(health, armour, shields) of a hero's body, or None when its data has no 0033."""
    record = next((item for item in graph.bodies().values() if int(item["hero"], 16) == hero), None)
    body = graph.graph(BODY_GRAPH)
    item = next((item for item in (record or {}).get("graphs", []) if item["graph"] == BODY_GRAPH), None)
    if item is None or body is None:
        log.warning("[game] no 0033 in the body data of the hero %016X: no health pools", hero)
        return None
    values = _numbers(body.fields["m_publicSchema"]["m_entries"])
    values.update(_numbers(item.get("m_1EB5A024")))
    return values.get(HEALTH_VAR, 0.0), values.get(ARMOUR_VAR, 0.0), values.get(SHIELDS_VAR, 0.0)


def body_pools(hero: int) -> tuple[float, float, float]:
    """hero_pools, or FALLBACK (logged) for a hero without data."""
    return hero_pools(hero) or FALLBACK
