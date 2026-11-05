"""Exercise the entity reader with synthetic STUv2 bytes and invented asset GUIDs."""

import struct
import unittest

from ow174.game.props import PropError, entity_model_refs


def entity_fixture() -> bytes:
    field_bags = (
        ((0x8AF8F4F5, 4), (0xF29E4995, 0)),  # Root map offset and inline child array.
        ((0x2D124BE2, 8), (0xDEB1159A, 5)),  # Model GUID and inline breakable config.
        ((0xE1B0C07F, 1),),
        ((0x2D124BE2, 8),),
        ((0x436BEFE6, 8), (0x2D3CDFA9, 8), (0x49F782CE, 8)),
    )
    # Fixed wire words for this invented header (CRC64 0x63A08B583BF5D927). The
    # fixture is independent of the production CRC and GUID implementation.
    children = (
        struct.pack("<i", 2)
        + struct.pack("<i3Q", 4, 0x23C964BE7C9E32C0, 0xE5A854F1DB090B9C, 0)
        + struct.pack("<i3Q", 4, 0x27CD64BE709E32C4, 0, 0xE7FC09969D8256E9)
    )
    instances = (
        (0x73BB2F3A, struct.pack("<3i", 0, 0, len(children)) + children),
        (0x191835F9, struct.pack("<iQiB", 1, 0x1886C0BA6EE79FD7, 2, 1)),
        (0x191835F9, struct.pack("<iQ", 3, 0x0DD7C0BA37E79F42)),
        (0x191835F9, struct.pack("<iQ", 3, 0x1886C0BA6EE79FD7)),  # Unreferenced model.
    )
    # The last bucket gives the map length. Each value has four padding bytes.
    entries = b"".join(struct.pack("<QiI", key, index, 0xDEADBEEF) for key, index in enumerate((2, -1, 1), 1))
    dynamic = struct.pack("<IIqqII", 2, 0, 24, 32, 1, 3) + entries
    infos_at = 36
    infos = b"".join(struct.pack("<I8xi", kind, len(body)) for kind, body in instances)
    bags_at = infos_at + len(infos)
    fields_at = bags_at + 8 * len(field_bags)
    bag_headers, fields = bytearray(), bytearray()
    for bag in field_bags:
        bag_headers.extend(struct.pack("<ii", len(bag), fields_at + len(fields)))
        for field, size in bag:
            fields.extend(struct.pack("<Ii", field, size))
    dynamic_at = fields_at + len(fields)
    body_at = dynamic_at + len(dynamic)
    header = struct.pack(
        "<9i", len(instances), infos_at, 0, 0, len(field_bags), bags_at, len(dynamic), dynamic_at, body_at
    )
    return b"".join((header, infos, bag_headers, fields, dynamic, *(body for _, body in instances)))


class PropEntityModelRefsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = entity_fixture()
        self.dynamic_at, self.body_at = struct.unpack_from("<ii", self.fixture, 28)

    def test_decodes_models_in_component_map_order_and_child_null_refs(self):
        models, children = entity_model_refs(self.fixture)
        self.assertEqual(models, [(0x0D00000087654321, False), (0x0D00000012345678, True)])
        self.assertEqual(
            children,
            [
                (0x0400000001020304, 0x0DC000001234ABCD, 0),
                (0x0400000005060708, 0, 0x0D80000000ABCDEF),
            ],
        )

    def test_rejects_out_of_bounds_component_map(self):
        for offset, value, reason in (
            (self.body_at + 4, -2, "component map offset"),
            (self.dynamic_at, 0x7FFFFFFF, "component map buckets"),
            (self.dynamic_at + 28, 0x7FFFFFFF, "component map entries"),
            (self.dynamic_at + 40, 4, "component index"),
        ):
            with self.subTest(reason=reason):
                data = bytearray(self.fixture)
                struct.pack_into("<i", data, offset, value)
                with self.assertRaisesRegex(PropError, reason):
                    entity_model_refs(bytes(data))

    def test_rejects_invalid_child_count_bag_and_reference_width(self):
        bags_at = struct.unpack_from("<i", self.fixture, 20)[0]
        child_fields_at = struct.unpack_from("<i", self.fixture, bags_at + 4 * 8 + 4)[0]
        for offset, value, reason in (
            (self.body_at + 12, 0x7FFFFFFF, "child entity array count"),
            (self.body_at + 16, 5, "inline instance field bag"),
            (child_fields_at + 4, 4, "truncated entity definition data"),
        ):
            with self.subTest(reason=reason):
                data = bytearray(self.fixture)
                struct.pack_into("<i", data, offset, value)
                with self.assertRaisesRegex(PropError, reason):
                    entity_model_refs(bytes(data))


if __name__ == "__main__":
    unittest.main()
