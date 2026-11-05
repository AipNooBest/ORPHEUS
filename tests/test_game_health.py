import struct
import unittest

from ow174.game import pools, world
from ow174.game.content import SOLDIER

REINHARDT, ZARYA = 0x02E0000000000007, 0x02E0000000000068


def bar(parts):
    """What the client's HUD bar sums from component 51 (0x7FF7894CCEE0): per pool type, the plain
    (current, max) and the over-health current. A pool is plain only with byte 17 set and byte 18 clear."""
    plain = [[0.0, 0.0] for _ in range(3)]
    over = [0.0] * 3
    for field, part in enumerate(parts):
        if part is None:
            continue
        pool_id, most, now, _ = struct.unpack_from("<IffI", part)
        if not pool_id:
            continue
        kind = field // world.POOLS
        if part[17] and not part[18]:
            plain[kind][0] += now
            plain[kind][1] += most
        else:
            over[kind] += now
    return plain, over


class HealthPoolTest(unittest.TestCase):
    def test_pool_layout(self):
        """u32 id, f32 max, f32 current, u32 type, then bytes 16-19."""
        part = world.pool(3, world.SHIELDS_POOL, 200.0, 150.0)
        self.assertEqual(part, struct.pack("<IffI4B", 3, 200.0, 150.0, 2, 0, 1, 0, 1))

    def test_soldier_bar_is_plain_health(self):
        """The old part had byte 17 clear: the bar drew it as over-health (the turquoise bar)."""
        health = pools.body_pools(SOLDIER)[0]
        parts = world.health(health, health)[51]
        self.assertEqual(bar(parts), ([[200.0, 200.0], [0.0, 0.0], [0.0, 0.0]], [0.0, 0.0, 0.0]))
        old = [struct.pack("<IffII", 1, 200.0, 200.0, 1, 1)]
        self.assertEqual(bar(old)[1], [200.0, 0.0, 0.0])

    def test_armour_and_shields_go_in_their_own_arrays(self):
        health, armour, shields = pools.body_pools(REINHARDT)
        parts = world.health(health, health, armour, shields)[51]
        self.assertEqual(bar(parts)[0], [[300.0, 300.0], [200.0, 200.0], [0.0, 0.0]])
        self.assertEqual(struct.unpack_from("<I", parts[16], 12)[0], world.ARMOUR_POOL)
        health, armour, shields = pools.body_pools(ZARYA)
        parts = world.health(health, health, armour, shields)[51]
        self.assertEqual(bar(parts)[0], [[200.0, 200.0], [0.0, 0.0], [200.0, 200.0]])
        ids = [struct.unpack_from("<I", part)[0] for part in parts if part is not None]
        self.assertEqual(sorted(ids), [1, 2])

    def test_damaged_health(self):
        plain, over = bar(world.health(120.0, 200.0)[51])
        self.assertEqual((plain[0], over), ([120.0, 200.0], [0.0, 0.0, 0.0]))

    def test_hero_pools_from_0033(self):
        expected = {
            SOLDIER: (200.0, 0.0, 0.0),
            REINHARDT: (300.0, 200.0, 0.0),
            ZARYA: (200.0, 0.0, 200.0),
            0x02E00000000001CA: (500.0, 100.0, 0.0),  # Wrecking Ball
            0x02E0000000000020: (50.0, 0.0, 150.0),  # Zenyatta
            0x02E0000000000016: (100.0, 0.0, 125.0),  # Symmetra
            0x02E000000000013E: (200.0, 250.0, 0.0),  # Orisa
            0x02E0000000000003: (150.0, 0.0, 0.0),  # Tracer
            0x02E0000000000040: (600.0, 0.0, 0.0),  # Roadhog
        }
        for hero, values in expected.items():
            self.assertEqual(pools.hero_pools(hero), values, f"{hero:016X}")
        self.assertIsNone(pools.hero_pools(0x02E0000000FFFFFF))
        self.assertEqual(pools.body_pools(0x02E0000000FFFFFF), pools.FALLBACK)

    def test_update_mask(self):
        """The record's mask names the fields of the pools that are there: 0, then 16 or 32."""
        record = world.update(0, world.health(300.0, 300.0, 200.0))
        data = record.getvalue()
        self.assertEqual(data[0], 1)  # one component
        self.assertEqual(data[1], 51)
        length = data[2]
        mask = int.from_bytes(data[3 : 3 + length], "little")
        self.assertEqual(mask, 1 | 1 << 16)


if __name__ == "__main__":
    unittest.main()
