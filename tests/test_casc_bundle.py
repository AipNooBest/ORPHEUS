"""CASC key records preserve both the bundle checksum and the selected asset checksum."""

import copy
import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ow174.game.casc import CascError, Storage, bucket


class TestStorageRecords(unittest.TestCase):
    def setUp(self):
        # read_record does not need an install: read is the existing checked CASC boundary.
        self.storage = object.__new__(Storage)
        self.prefix = b"bundle header\x00"
        self.content = b"selected collision chunk\x00\xff"
        self.bundle = self.prefix + self.content + b"another asset"
        self.ekey = bytes(range(16))
        self.ckey = hashlib.md5(self.bundle).digest()

    def bundle_record(self):
        return {
            "ckey": hashlib.md5(self.content).hexdigest(),
            "size": len(self.content),
            "bundle": {
                "guid": "0x0123456789ABCDEF",
                "ekey": self.ekey.hex(),
                "ckey": self.ckey.hex(),
                "size": len(self.bundle),
                "offset": len(self.prefix),
            },
        }

    def test_bundle_uses_nested_keys_and_returns_only_the_checked_asset(self):
        record = self.bundle_record()
        with patch.object(self.storage, "read", return_value=self.bundle) as read:
            self.assertEqual(self.storage.read_record(record), self.content)
        read.assert_called_once_with(self.ekey, self.ckey)
        self.assertNotIn("ekey", record)

    def test_real_casc_archive_reads_the_slice_and_checks_both_content_keys(self):
        # One native uncompressed BLTE block, reached through the actual bucket index and archive.
        blte = b"BLTE" + struct.pack(">I", 0) + b"N" + self.bundle
        archive_record = self.ekey[::-1] + struct.pack("<I", 30 + len(blte)) + bytes(10) + blte
        number = bucket(self.ekey)
        layout = struct.pack("<HBBBBBBQ", 7, number, 0, 4, 5, 9, 30, 0)
        header = struct.pack("<II", 16, 0) + layout + bytes(8)
        entry = self.ekey[:9] + (64).to_bytes(5, "big") + struct.pack("<I", len(archive_record))
        index = header + struct.pack("<II", len(entry), 0) + entry
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data" / "casc" / "data"
            data.mkdir(parents=True)
            (data / "data.000").write_bytes(bytes(64) + archive_record)
            (data / f"{number:02x}00000001.idx").write_bytes(index)
            storage = Storage(folder)
            record = self.bundle_record()
            self.assertEqual(storage.read_record(record), self.content)

            wrong_bundle = copy.deepcopy(record)
            wrong_bundle["bundle"]["ckey"] = bytes(16).hex()
            with self.assertRaisesRegex(CascError, self.ekey.hex() + " does not match its content key"):
                storage.read_record(wrong_bundle)

            wrong_slice = copy.deepcopy(record)
            wrong_slice["ckey"] = bytes(16).hex()
            with self.assertRaisesRegex(CascError, "bundle slice does not match its content key"):
                storage.read_record(wrong_slice)

    def test_bundle_rejects_an_outer_encoding_key_before_reading_the_package(self):
        record = self.bundle_record()
        record["ekey"] = bytes(reversed(self.ekey)).hex()
        with patch.object(self.storage, "read") as read, self.assertRaises(CascError):
            self.storage.read_record(record)
        read.assert_not_called()

    def test_records_require_a_mapping_before_reading_storage(self):
        for record in (None, 123, True, False, [], ["ekey", "ckey"], "record"):
            with self.subTest(record=record):
                with patch.object(self.storage, "read") as read, self.assertRaises(CascError):
                    self.storage.read_record(record)
                read.assert_not_called()

    def test_bundle_rejects_a_slice_with_the_wrong_content_hash(self):
        record = self.bundle_record()
        record["ckey"] = bytes(16).hex()
        with patch.object(self.storage, "read", return_value=self.bundle), self.assertRaises(CascError):
            self.storage.read_record(record)

    def test_bundle_preserves_the_storage_read_checksum_failure(self):
        error = CascError("bundle does not match its content key")
        with (
            patch.object(self.storage, "read", side_effect=error) as read,
            self.assertRaises(CascError) as caught,
        ):
            self.storage.read_record(self.bundle_record())
        self.assertIs(caught.exception, error)
        read.assert_called_once_with(self.ekey, self.ckey)

    def test_bundle_rejects_a_declared_package_size_that_does_not_match_the_read(self):
        for difference in (-1, 1):
            with self.subTest(difference=difference):
                record = self.bundle_record()
                record["bundle"]["size"] += difference
                with (
                    patch.object(self.storage, "read", return_value=self.bundle),
                    self.assertRaises(CascError),
                ):
                    self.storage.read_record(record)

    def test_bundle_requires_nonnegative_integer_offset_and_sizes(self):
        for field in ("offset", "asset_size", "bundle_size"):
            for value in (-1, True, False, 1.5, "4", None):
                with self.subTest(field=field, value=value):
                    record = self.bundle_record()
                    if field == "asset_size":
                        record["size"] = value
                    else:
                        record["bundle"]["size" if field == "bundle_size" else field] = value
                    with (
                        patch.object(self.storage, "read", return_value=self.bundle),
                        self.assertRaises(CascError),
                    ):
                        self.storage.read_record(record)

    def test_bundle_requires_all_slice_and_package_lengths(self):
        for field in ("offset", "asset_size", "bundle_size"):
            with self.subTest(field=field):
                record = self.bundle_record()
                if field == "asset_size":
                    del record["size"]
                else:
                    del record["bundle"]["size" if field == "bundle_size" else field]
                with (
                    patch.object(self.storage, "read", return_value=self.bundle),
                    self.assertRaises(CascError),
                ):
                    self.storage.read_record(record)

    def test_records_reject_malformed_encoding_and_content_keys(self):
        for bundled, field in (
            (False, "ekey"),
            (False, "ckey"),
            (True, "ekey"),
            (True, "ckey"),
            (True, "slice_ckey"),
        ):
            for value in ("not hex", "00" * 15, "00" * 17, None, 123):
                with self.subTest(bundled=bundled, field=field, value=value):
                    record = self.bundle_record() if bundled else {
                        "ekey": self.ekey.hex(), "ckey": self.ckey.hex()
                    }
                    target = record["bundle"] if bundled and field != "slice_ckey" else record
                    target["ckey" if field == "slice_ckey" else field] = value
                    with patch.object(self.storage, "read") as read, self.assertRaises(CascError):
                        self.storage.read_record(record)
                    read.assert_not_called()

    def test_bundle_requires_a_mapping(self):
        for value in (None, False, [], "bundle", 123):
            with self.subTest(value=value):
                record = self.bundle_record()
                record["bundle"] = value
                with patch.object(self.storage, "read") as read, self.assertRaises(CascError):
                    self.storage.read_record(record)
                read.assert_not_called()

    def test_bundle_rejects_ranges_outside_the_package(self):
        for offset, size in ((len(self.bundle) + 1, 0), (len(self.bundle), 1), (0, len(self.bundle) + 1)):
            with self.subTest(offset=offset, size=size):
                record = self.bundle_record()
                record["bundle"]["offset"] = offset
                record["size"] = size
                with (
                    patch.object(self.storage, "read", return_value=self.bundle),
                    self.assertRaises(CascError),
                ):
                    self.storage.read_record(record)

    def test_bundle_accepts_exact_boundaries_including_an_empty_slice(self):
        for offset, content in ((0, self.bundle), (len(self.bundle), b"")):
            with self.subTest(offset=offset, size=len(content)):
                record = self.bundle_record()
                record["bundle"]["offset"] = offset
                record["size"] = len(content)
                record["ckey"] = hashlib.md5(content).hexdigest()
                with patch.object(self.storage, "read", return_value=self.bundle):
                    self.assertEqual(self.storage.read_record(record), content)

    def test_direct_records_keep_the_existing_key_based_reader(self):
        record = {"ekey": self.ekey.hex(), "ckey": hashlib.md5(self.content).hexdigest()}
        for sized in (False, True):
            with self.subTest(sized=sized):
                selected = dict(record, size=len(self.content)) if sized else record
                with patch.object(self.storage, "read", return_value=self.content) as read:
                    self.assertEqual(self.storage.read_record(selected), self.content)
                read.assert_called_once_with(self.ekey, hashlib.md5(self.content).digest())

    def test_direct_records_check_a_supplied_length(self):
        record = {"ekey": self.ekey.hex(), "ckey": hashlib.md5(self.content).hexdigest()}
        for difference in (-1, 1):
            with self.subTest(difference=difference):
                selected = dict(record, size=len(self.content) + difference)
                with (
                    patch.object(self.storage, "read", return_value=self.content),
                    self.assertRaises(CascError),
                ):
                    self.storage.read_record(selected)

    def test_record_reading_does_not_change_the_key_table_record(self):
        record = self.bundle_record()
        original = copy.deepcopy(record)
        with patch.object(self.storage, "read", return_value=self.bundle):
            self.storage.read_record(record)
        self.assertEqual(record, original)


if __name__ == "__main__":
    unittest.main()
