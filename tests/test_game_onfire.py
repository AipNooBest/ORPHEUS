import math
import struct
import unittest

from ow174.game import onfire, world


class OnFireTest(unittest.TestCase):
    def test_meter_is_the_clients(self):
        """100 * min(score, 1.28 t) / (1.28 t) in float32 (0x7FF78982EBB0)."""
        self.assertEqual(onfire.meter(0.0, 1000.0), 0.0)
        self.assertEqual(onfire.meter(640.0, 500.0), 100.0)
        self.assertEqual(onfire.meter(900.0, 500.0), 100.0)
        self.assertAlmostEqual(onfire.meter(320.0, 500.0), 50.0, places=4)
        self.assertTrue(math.isnan(onfire.meter(10.0, 0.0)))

    def test_update_record(self):
        """Component 64 with fields 19 and 20 (f32) and 36 (u8): a 6-byte mask for 45 fields."""
        data = world.update(0, onfire.stats(250.0, 500.0, False)).getvalue()
        self.assertEqual(data[:3], bytes([1, onfire.GMP_STATS, 6]))
        mask = int.from_bytes(data[3:9], "little")
        self.assertEqual(mask, 1 << 19 | 1 << 20 | 1 << 36)
        self.assertEqual(data[9:], struct.pack("<ffB", 250.0, 500.0, 0))

    def test_flag_alone(self):
        """Another client's copy of the entity gets only the on-fire flag."""
        values = onfire.stats(on_fire=True)[onfire.GMP_STATS]
        self.assertEqual([k for k, value in enumerate(values) if value is not None], [onfire.ON_FIRE])
        self.assertEqual(len(values), len(onfire.FIELDS))
        self.assertEqual(len(onfire.FIELDS), 45)


if __name__ == "__main__":
    unittest.main()
