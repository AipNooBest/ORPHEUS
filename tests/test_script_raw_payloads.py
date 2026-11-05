"""Independent bit-offset vectors for the isolated, strict 1.74 raw payload tools."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import statescript
from ow174.game.bits import BitReader, BitWriter
from ow174.game.script import codecs
from ow174.game.script import raw_payloads as raw

TARGET = "STU_9935FBC8"
OPAQUE = "STU_861D9015"
SENTINEL = 0x12AB
LINKS = {0x1234: raw.LinkMetadata(5, frozenset({0, 19, 31}), 3, frozenset({0, 5, 7}))}


def unset_values():
    return [{"set": 0} for _ in range(7)]


def opaque_fields(**changes):
    fields = {
        "a": 1,
        "v0": 0xFFFF,
        "v1": 0,
        "v2": 0xFFFFFFFF,
        "v3": 0x7FC00001,
        "v4": 0x80000000,
        "v5": 0x1234,
        "v6": 0x8000,
        "has_entity": 0,
        "g": 1,
    }
    fields.update(changes)
    return fields


# These offsets follow the table by hand: a, 16/16/32/32/32/16/16 raw bits.
OPAQUE_VALUES = [
    (0, 1, 1),
    (1, 16, 0xFFFF),
    (17, 16, 0),
    (33, 32, 0xFFFFFFFF),
    (65, 32, 0x7FC00001),
    (97, 32, 0x80000000),
    (129, 16, 0x1234),
    (145, 16, 0x8000),
]


class RawPayloadVectorTests(unittest.TestCase):
    def assert_vector(self, cls, fields, spans, count, *, owner, full, links=None):
        """Compare encode and decode separately to manually placed wire fields plus a sentinel."""
        expected = 0
        occupied = 0
        for offset, width, value in spans:
            mask = ((1 << width) - 1) << offset
            self.assertEqual(mask & occupied, 0, "manual vector overlaps")
            self.assertLessEqual(offset + width, count)
            self.assertLess(value, 1 << width)
            occupied |= mask
            expected |= value << offset
        self.assertEqual(occupied, (1 << count) - 1, "manual vector must account for every payload bit")
        out = raw.encode_payload(cls, fields, owner=owner, full=full, links=links)
        self.assertEqual(out.count, count)
        self.assertEqual(out.value, expected)
        self.assertEqual(out.getvalue(), expected.to_bytes((count + 7) // 8, "little"))
        for offset, width, value in spans:
            self.assertEqual((out.value >> offset) & ((1 << width) - 1), value, (offset, width))

        # Decode a manually assembled buffer, not the encoder's returned buffer.
        number = expected | SENTINEL << count
        reader = BitReader(number.to_bytes((count + 13 + 7) // 8, "little"))
        reader.size = count + 13
        got = raw.decode_payload(reader, cls, owner=owner, full=full, links=links)
        self.assertEqual(got, fields)
        self.assertEqual(reader.pos, count)
        self.assertEqual(reader.bits(13), SENTINEL)
        self.assertEqual(reader.left(), 0)
        return got

    def test_linked_target_owner_full_manual_offsets(self):
        fields = {
            "link": None,
            "has_entity": 1,
            "entity": 0xE0001234,
            "values": [
                {"set": 1, "value": 1.5},
                {"set": 0},
                {"set": 1, "value": -2.0},
                {"set": 0},
                {"set": 0},
                {"set": 1, "value": -0.0},
                {"set": 0},
            ],
        }
        # link@0, presence@1, entity kind@2, var_b 10 selector@4, low 16 bits@6.
        # The optional floats start at 23, 57, 92; the seventh set flag ends at 125.
        spans = [
            (0, 1, 0),
            (1, 1, 1),
            (2, 2, 3),
            (4, 2, 1),
            (6, 16, 0x1234),
            (22, 1, 1),
            (23, 32, 0x3FC00000),
            (55, 1, 0),
            (56, 1, 1),
            (57, 32, 0xC0000000),
            (89, 1, 0),
            (90, 1, 0),
            (91, 1, 1),
            (92, 32, 0x80000000),
            (124, 1, 0),
        ]
        got = self.assert_vector(TARGET, fields, spans, 125, owner=True, full=True)
        self.assertEqual(math.copysign(1.0, got["values"][5]["value"]), -1.0)

    def test_linked_target_only_owner_full_has_entity_and_values(self):
        for owner, full in ((True, False), (False, True), (False, False)):
            with self.subTest(owner=owner, full=full):
                self.assert_vector(TARGET, {"link": None}, [(0, 1, 0)], 1, owner=owner, full=full)
                with self.assertRaisesRegex(raw.PayloadError, "unexpected fields"):
                    raw.encode_payload(TARGET, {"link": None, "has_entity": 0}, owner=owner, full=full)
        fields = {"link": None, "has_entity": 0, "values": unset_values()}
        self.assert_vector(TARGET, fields, [(0, 1, 0), (1, 1, 0), (2, 7, 0)], 9, owner=True, full=True)

    def test_linked_target_all_seven_float_flags_and_f32_boundaries(self):
        fields = {
            "link": None,
            "has_entity": 0,
            "values": [
                {"set": 1, "value": value}
                for value in (0.0, -0.0, 1.0, -1.0, 2**-149, (2 - 2**-23) * 2**127, -(2 - 2**-23) * 2**127)
            ],
        }
        words = (0, 0x80000000, 0x3F800000, 0xBF800000, 1, 0x7F7FFFFF, 0xFF7FFFFF)
        spans = [(0, 1, 0), (1, 1, 0)]
        # First set@2, first value@3; each subsequent pair advances exactly 33 bits.
        spans.extend(
            item for i, word in enumerate(words) for item in ((2 + 33 * i, 1, 1), (3 + 33 * i, 32, word))
        )
        self.assert_vector(TARGET, fields, spans, 233, owner=True, full=True)

    def test_link_indexes_use_explicit_owner_or_remote_width_without_translation(self):
        for owner, full, index, width in ((True, False, 19, 5), (False, False, 5, 3), (False, True, 7, 3)):
            with self.subTest(owner=owner, full=full):
                # link@0, w_u16's 11 selector@1, instance@3, raw graph index@19.
                spans = [(0, 1, 1), (1, 2, 3), (3, 16, 0x1234), (19, width, index)]
                self.assert_vector(
                    TARGET, {"link": (0x1234, index)}, spans, 19 + width, owner=owner, full=full, links=LINKS
                )
        fields = {"link": (0x1234, 31), "has_entity": 0, "values": unset_values()}
        self.assert_vector(
            TARGET,
            fields,
            [(0, 1, 1), (1, 2, 3), (3, 16, 0x1234), (19, 5, 31), (24, 1, 0), (25, 7, 0)],
            32,
            owner=True,
            full=True,
            links=LINKS,
        )

    def test_link_instance_selector_boundaries_and_zero_width(self):
        meta = raw.LinkMetadata(0, frozenset({0}), 1, frozenset({0, 1}))
        # Explicit selectors are their on-wire LSB-first integer values.
        for instance, selector, selector_bits, width in (
            (0, 0, 1, 4),
            (15, 0, 1, 4),
            (16, 1, 2, 8),
            (255, 1, 2, 8),
            (256, 3, 2, 16),
            (65535, 3, 2, 16),
        ):
            with self.subTest(instance=instance):
                spans = [(0, 1, 1), (1, selector_bits, selector), (1 + selector_bits, width, instance)]
                self.assert_vector(
                    TARGET,
                    {"link": (instance, 0)},
                    spans,
                    1 + selector_bits + width,
                    owner=True,
                    full=False,
                    links={instance: meta},
                )

        wide = raw.LinkMetadata(32, frozenset({0xFFFFFFFF}), 0, frozenset({0}))
        self.assert_vector(
            TARGET,
            {"link": (1, 0xFFFFFFFF)},
            [(0, 1, 1), (1, 1, 0), (2, 4, 1), (6, 32, 0xFFFFFFFF)],
            38,
            owner=True,
            full=False,
            links={1: wide},
        )

    def test_861_disabled_is_one_bit_for_all_contexts(self):
        for owner in (False, True):
            for full in (False, True):
                self.assert_vector(OPAQUE, {"a": 0}, [(0, 1, 0)], 1, owner=owner, full=full)
                with self.assertRaisesRegex(raw.PayloadError, "unexpected fields"):
                    raw.encode_payload(OPAQUE, {"a": 0, "g": 0}, owner=owner, full=full)

    def test_861_enabled_raw_words_and_terminal_g_have_fixed_offsets(self):
        for owner in (False, True):
            for full in (False, True):
                self.assert_vector(
                    OPAQUE,
                    opaque_fields(),
                    [*OPAQUE_VALUES, (161, 1, 0), (162, 1, 1)],
                    163,
                    owner=owner,
                    full=full,
                )

    def test_861_optional_entity_and_extra_manual_offsets(self):
        fields = opaque_fields(has_entity=1, entity=0xA0000100, has_extra=1, extra=0xFFFFFFFF, g=0)
        spans = [
            *OPAQUE_VALUES,
            (161, 1, 1),
            (162, 2, 1),
            (164, 2, 1),
            (166, 16, 0x100),
            (182, 1, 1),
            (183, 2, 3),
            (185, 32, 0xFFFFFFFF),
            (217, 1, 0),
        ]
        self.assert_vector(OPAQUE, fields, spans, 218, owner=False, full=True)
        fields = opaque_fields(has_entity=1, entity=0, has_extra=0)
        spans = [*OPAQUE_VALUES, (161, 1, 1), (162, 2, 0), (164, 1, 0), (165, 8, 0), (173, 1, 0), (174, 1, 1)]
        self.assert_vector(OPAQUE, fields, spans, 175, owner=True, full=False)

    def test_861_extra_var_b_width_boundaries(self):
        for value, selector, selector_bits, width in (
            (0, 0, 1, 8),
            (255, 0, 1, 8),
            (256, 1, 2, 16),
            (65535, 1, 2, 16),
            (65536, 3, 2, 32),
            (0xFFFFFFFF, 3, 2, 32),
        ):
            with self.subTest(extra=value):
                fields = opaque_fields(has_entity=1, entity=0xFFFFFFFF, has_extra=1, extra=value)
                spans = [
                    *OPAQUE_VALUES,
                    (161, 1, 1),
                    (162, 2, 3),
                    (164, 2, 3),
                    (166, 32, 0x1FFFFFFF),
                    (198, 1, 1),
                    (199, selector_bits, selector),
                    (199 + selector_bits, width, value),
                    (199 + selector_bits + width, 1, 1),
                ]
                self.assert_vector(OPAQUE, fields, spans, 200 + selector_bits + width, owner=True, full=True)


class RawPayloadValidationTests(unittest.TestCase):
    def assert_rejected(self, cls, fields, *, owner=True, full=False, links=None):
        # This is the intended atomic append usage: a validation failure leaves the
        # destination and the caller's field dictionary untouched.
        destination = BitWriter()
        destination.bits(0xA, 4)
        before = repr(fields)
        with self.assertRaises(raw.PayloadError):
            destination.append(raw.encode_payload(cls, fields, owner=owner, full=full, links=links))
        self.assertEqual((destination.value, destination.count), (0xA, 4))
        self.assertEqual(repr(fields), before)

    def test_missing_unknown_and_inactive_fields_are_rejected(self):
        self.assert_rejected(TARGET, {})
        self.assert_rejected(TARGET, {"link": None, "typo": 0})
        self.assert_rejected(TARGET, {"link": None}, full=True)
        self.assert_rejected(TARGET, {"link": None, "has_entity": 0}, full=True)
        self.assert_rejected(TARGET, {"link": None, "has_entity": 1, "values": unset_values()}, full=True)
        self.assert_rejected(
            TARGET, {"link": None, "has_entity": 0, "entity": 0, "values": unset_values()}, full=True
        )
        self.assert_rejected(OPAQUE, {})
        for missing in opaque_fields():
            fields = opaque_fields()
            del fields[missing]
            self.assert_rejected(OPAQUE, fields)
        self.assert_rejected(OPAQUE, opaque_fields(entity=0))
        self.assert_rejected(OPAQUE, opaque_fields(has_extra=0))
        self.assert_rejected(OPAQUE, opaque_fields(has_entity=1, entity=0))
        self.assert_rejected(OPAQUE, opaque_fields(has_entity=1, entity=0, has_extra=1))
        self.assert_rejected(OPAQUE, opaque_fields(has_entity=1, entity=0, has_extra=0, extra=0))

    def test_repeat_length_and_each_item_are_exact(self):
        for values in ([], unset_values()[:6], [*unset_values(), {"set": 0}], {}, None):
            self.assert_rejected(TARGET, {"link": None, "has_entity": 0, "values": values}, full=True)
        for item in (
            {},
            {"set": 1},
            {"set": 2},
            {"set": -1},
            {"set": 0, "value": 0},
            {"set": 0, "extra": 0},
            None,
        ):
            self.assert_rejected(
                TARGET, {"link": None, "has_entity": 0, "values": [item, *unset_values()[:6]]}, full=True
            )

    def test_integer_widths_types_and_flags_are_strict(self):
        for name, width in (
            ("v0", 16),
            ("v1", 16),
            ("v2", 32),
            ("v3", 32),
            ("v4", 32),
            ("v5", 16),
            ("v6", 16),
            ("a", 1),
            ("has_entity", 1),
            ("g", 1),
        ):
            for invalid in (-1, 1 << width, 0.5, "0", None):
                with self.subTest(field=name, value=invalid):
                    self.assert_rejected(OPAQUE, opaque_fields(**{name: invalid}))
        self.assert_rejected(OPAQUE, opaque_fields(v0=True))
        self.assert_rejected(OPAQUE, opaque_fields(has_entity=1, entity=0, has_extra=1, extra=-1))
        self.assert_rejected(OPAQUE, opaque_fields(has_entity=1, entity=0, has_extra=1, extra=1 << 32))

    def test_float_values_are_finite_and_fit_f32(self):
        for invalid in (math.inf, -math.inf, math.nan, 1e39, -(1 << 1024), True, "1.0", None):
            with self.subTest(value=invalid):
                fields = {
                    "link": None,
                    "has_entity": 0,
                    "values": [{"set": 1, "value": invalid}, *unset_values()[:6]],
                }
                self.assert_rejected(TARGET, fields, full=True)

    def test_entity_ids_cannot_be_silently_masked_or_collapse_to_null(self):
        for invalid in (-1, 1 << 32, 1, 0x7FFFFFFF, 0x80000000, 0xA0000000, 0xE0000000, None, True):
            self.assert_rejected(OPAQUE, opaque_fields(has_entity=1, entity=invalid, has_extra=0))

    def test_link_values_and_allowed_indexes_are_strict(self):
        for value in (
            0,
            False,
            [0x1234, 19],
            (0x1234,),
            (0x1234, 19, 0),
            (0x1234, 19.0),
            (-1, 0),
            (65536, 0),
            (True, 0),
            (1, 0),
            (0x1234, 32),
            (0x1234, 18),
        ):
            self.assert_rejected(TARGET, {"link": value}, links=LINKS)
        self.assert_rejected(TARGET, {"link": (0x1234, 19)})
        self.assert_rejected(TARGET, {"link": (0x1234, 19)}, owner=False, links=LINKS)
        self.assert_rejected(TARGET, {"link": (0x1234, 5)}, links=LINKS)

    def test_all_supplied_link_metadata_is_validated_even_when_unused(self):
        good = frozenset({0})
        bad_metadata = (
            [],
            {1: {}},
            {-1: raw.LinkMetadata(1, good, 1, good)},
            {65536: raw.LinkMetadata(1, good, 1, good)},
            {True: raw.LinkMetadata(1, good, 1, good)},
        )
        for links in bad_metadata:
            self.assert_rejected(TARGET, {"link": None}, links=links)
        for width in (-1, 33, True, 1.0):
            self.assert_rejected(TARGET, {"link": None}, links={1: raw.LinkMetadata(width, good, 1, good)})
            self.assert_rejected(TARGET, {"link": None}, links={1: raw.LinkMetadata(1, good, width, good)})
        for indices in ([], {2}, {-1}, {True}, {0.5}):
            self.assert_rejected(TARGET, {"link": None}, links={1: raw.LinkMetadata(1, indices, 1, good)})
            self.assert_rejected(TARGET, {"link": None}, links={1: raw.LinkMetadata(1, good, 1, indices)})
        self.assert_rejected(TARGET, {"link": (1, 0)}, links={1: raw.LinkMetadata(0, frozenset(), 1, good)})

    def test_decode_failures_leave_the_original_reader_untouched(self):
        fields = {
            "link": None,
            "has_entity": 1,
            "entity": 0xE0001234,
            "values": [{"set": 1, "value": 1.5}, *unset_values()[:6]],
        }
        out = raw.encode_payload(TARGET, fields, owner=True, full=True)
        # Prefix places the payload at a non-byte boundary. Try every truncated bit limit.
        number = 5 | out.value << 3
        data = number.to_bytes((out.count + 3 + 7) // 8, "little")
        for limit in range(3, out.count + 3):
            reader = BitReader(data, 3)
            reader.size = limit
            with self.assertRaisesRegex(raw.PayloadError, "truncated"):
                raw.decode_payload(reader, TARGET, owner=True, full=True)
            self.assertEqual(reader.pos, 3)
        reader = BitReader(data, 3)
        reader.size = out.count + 3
        self.assertEqual(raw.decode_payload(reader, TARGET, owner=True, full=True), fields)
        self.assertEqual(reader.pos, out.count + 3)

        # Link to instance 1, then an index bit. Its w_u16 is the small 0+4 form.
        reader = BitReader(bytes([0b01000101]))
        with self.assertRaisesRegex(raw.PayloadError, "no explicit link metadata"):
            raw.decode_payload(reader, TARGET, owner=True, full=False)
        self.assertEqual(reader.pos, 0)
        reader = BitReader(bytes([0b01000101]))
        with self.assertRaisesRegex(raw.PayloadError, "not in the supplied"):
            raw.decode_payload(
                reader,
                TARGET,
                owner=True,
                full=False,
                links={1: raw.LinkMetadata(1, frozenset({0}), 1, frozenset({0}))},
            )
        self.assertEqual(reader.pos, 0)

    def test_decode_rejects_nonfinite_float_and_oversized_entity_low_bits(self):
        # link=0, has_entity=0, first set=1; the first f32 starts at bit 3.
        for word in (0x7F800000, 0xFF800000, 0x7FC00001):
            number = 1 << 2 | word << 3
            reader = BitReader(number.to_bytes(6, "little"))
            with self.assertRaisesRegex(raw.PayloadError, "finite"):
                raw.decode_payload(reader, TARGET, owner=True, full=True)
            self.assertEqual(reader.pos, 0)
        # link=0, has_entity=1, kind=0, var_b selector=11, value has an illegal bit 29.
        number = 1 << 1 | 3 << 4 | (1 << 29) << 6
        reader = BitReader(number.to_bytes(6, "little"))
        with self.assertRaisesRegex(raw.PayloadError, "29 bits"):
            raw.decode_payload(reader, TARGET, owner=True, full=True)
        self.assertEqual(reader.pos, 0)

    def test_decode_rejects_invalid_reader_bounds_before_reading(self):
        for position, size in ((-1, 8), (9, 8), (0, 9), (True, 8), (0, 1.0)):
            reader = BitReader(b"\x00")
            reader.pos, reader.size = position, size
            with self.assertRaisesRegex(raw.PayloadError, "invalid position or bit limit"):
                raw.decode_payload(reader, OPAQUE, owner=True, full=False)
            self.assertEqual((reader.pos, reader.size), (position, size))
        with self.assertRaisesRegex(raw.PayloadError, "expected BitReader"):
            raw.decode_payload(b"\x00", OPAQUE, owner=True, full=False)

    def test_raw_support_does_not_register_a_runtime_writer(self):
        for cls in (TARGET, OPAQUE):
            self.assertTrue(codecs.codec(cls).verified)
            self.assertEqual(statescript.family(cls), "unknown")
            self.assertFalse(statescript.sendable(cls, True))
        for cls in ("STUStatescriptStateLogicalButton", "BAD", None, []):
            self.assert_rejected(cls, {})
        for owner, full in ((1, False), (True, 0), (None, False)):
            self.assert_rejected(TARGET, {"link": None}, owner=owner, full=full)
        for cls in ("9935FBC8", "STU_9935FBC8"):
            self.assertEqual(raw.encode_payload(cls, {"link": None}, owner=True, full=False).count, 1)


if __name__ == "__main__":
    unittest.main()
