"""On Fire: what the client reads from component 64 (MECGMPStats) of a player's entity.

The player card's fire meter (01CF st37's FireScore, 0x7FF78982EBB0) reads the local player's component 64:
100 * min(score, 1.28 * threshold) / (1.28 * threshold), score = field 19 and threshold = field 20, both
owner-only fields. With no threshold the client divides 0 by 0. The portrait burns (OnFire, 0x7FF789BE2370;
01CF st17's effects) while field 36 is set, read through a body's possessor, on any client.

The bar itself shows only in the Tutorial mode (0019.0C5): its FireBarVisible asks GetPvPEntity, which this
client always answers with no entity (0x7FF789BD8000), so in every other mode the card hides it.

The rule that fills the score is server code the client does not have. The data has the score events and
their weights (003F.054), not the threshold, the decay or when a player catches fire, and the events need
damage, healing and kills. So this only builds what the client is sent.
"""

from ow174.game import world
from ow174.game.script.expr import f32

GMP_STATS = 64
SCORE, THRESHOLD, ON_FIRE = 19, 20, 36
FULL = 1.28  # the bar is full at 1.28 times the threshold (0x7FF78B53BD08)
# The component's fields (type 0x7FF78BFE9DA0): 5 nested {u64, f32} (not written here), 14 u64, 16 f32 (19
# and 20 among them), u16, u8 (36), 8 u8. Fields 5-34 and 37-44 are owner-only (flag 0x10).
FIELDS = ["struct"] * 5 + ["u64"] * 14 + ["f32"] * 16 + ["u16", "u8"] + ["u8"] * 8
world.COMPONENT_FIELDS.setdefault(GMP_STATS, FIELDS)


def meter(score: float, threshold: float) -> float:
    """What the client's meter shows, 0 to 100 (NaN with no threshold, as on the client)."""
    top = f32(f32(threshold) * f32(FULL))
    if top == 0:
        return float("nan")
    return f32(f32(min(f32(score), top) / top) * 100.0)


def stats(score: float | None = None, threshold: float | None = None, on_fire: bool | None = None) -> dict:
    """Component 64 with the given fields set: score and threshold for the player's own client only, the
    on-fire flag for every client that has the player's entity."""
    values: list = [None] * len(FIELDS)
    values[SCORE] = score
    values[THRESHOLD] = threshold
    values[ON_FIRE] = None if on_fire is None else int(on_fire)
    return {GMP_STATS: values}
