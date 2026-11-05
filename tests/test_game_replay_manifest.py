"""Local model identity, without game launch, services, user files or collision reads."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import correction, match
from ow174.game import replay_manifest as manifest
from ow174.game.script import driver, runtime
from ow174.paths import DATA_DIR, ROOT

RUNTIME = {
    "implementation": "cpython", "version": (3, 12, 6, "final", 0),
    "implementation_version": (3, 12, 6, "final", 0), "cache_tag": "cpython-312",
    "pointer_bits": 64, "machine": "fixture", "byteorder": "little", "system": "fixture",
}
DEPENDENCIES = {"cryptography": "50.0.0", "protobuf": "6.33.6", "websockets": "17.0.1"}


class ReplayManifestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "checkout"
        self.data = self.base / "tables"
        self.data.mkdir()
        for name in (*manifest.SOURCE_FILES, "ow174/game/__init__.py", "ow174/jam/__init__.py",
                     "ow174/game/model.py", "ow174/jam/model.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"# fixture {name}\n".encode())
        for name in manifest.DATA_FILES:
            (self.data / name).write_bytes(f"fixture {name}\n".encode())
        self.configuration = {"HEALTH": None, "HEROES": {3, 1}, "quanta": 16000, "ratio": -0.0}

    def capture(self, **changes):
        options = {
            "root": self.root, "data_dir": self.data, "configuration": self.configuration,
            "runtime": RUNTIME, "dependencies": DEPENDENCIES,
        }
        options.update(changes)
        return manifest.capture_manifest(**options)

    def test_source_table_bytes_and_complete_file_inventory_are_sealed(self):
        captured = self.capture()
        rows = {row["path"]: row for row in captured["files"]}
        self.assertEqual(list(rows), sorted(rows))
        self.assertEqual(len(rows), 7 + len(manifest.DATA_FILES))
        for name, row in rows.items():
            path = self.data / name[5:] if name.startswith("data/") else self.root / name
            blob = path.read_bytes()
            self.assertEqual((row["size"], row["sha256"]), (len(blob), hashlib.sha256(blob).hexdigest()))
        self.assertEqual(manifest.manifest_digest(captured), captured["sha256"])
        manifest.assert_compatible(captured, self.capture())
        self.assertNotIn(str(self.root), json.dumps(captured))
        self.assertNotIn(str(self.data), json.dumps(captured))

    def test_relocation_mtime_map_and_set_order_do_not_change_identity(self):
        first = self.capture()
        other = self.base / "elsewhere"
        shutil.copytree(self.root, other)
        for path in other.rglob("*"):
            if path.is_file():
                os.utime(path, (1, 1))
        configuration = {"ratio": -0.0, "quanta": 16000, "HEROES": {1, 3}, "HEALTH": None}
        second = self.capture(root=other, configuration=configuration)
        self.assertEqual(first, second)

    def test_changes_to_same_length_source_and_data_bytes_are_diagnosed(self):
        first = self.capture()
        source = self.root / "ow174/game/model.py"
        source.write_bytes(source.read_bytes().replace(b"fixture", b"changed"))
        graph = self.data / "game_graphs_174.json.gz"
        graph.write_bytes(graph.read_bytes().replace(b"fixture", b"changed"))
        expected = [
            {"path": "files/data/game_graphs_174.json.gz", "kind": "changed"},
            {"path": "files/ow174/game/model.py", "kind": "changed"},
        ]
        self.assertEqual(manifest.compare_manifests(first, self.capture()), expected)
        with self.assertRaises(manifest.ManifestMismatch) as caught:
            manifest.assert_compatible(first, self.capture())
        self.assertEqual(caught.exception.differences, expected)
        self.assertIn("ow174/game/model.py", str(caught.exception))

    def test_added_and_deleted_source_files_are_not_hidden_by_a_fixed_filename_list(self):
        first = self.capture()
        (self.root / "ow174/game/model.py").unlink()
        new = self.root / "ow174/jam/new_package/new_model.py"
        new.parent.mkdir()
        new.write_bytes(b"# new source\n")
        self.assertEqual(manifest.compare_manifests(first, self.capture()), [
            {"path": "files/ow174/game/model.py", "kind": "missing"},
            {"path": "files/ow174/jam/new_package/new_model.py", "kind": "added"},
        ])

    def test_required_data_and_source_roots_fail_explicitly(self):
        for name in manifest.DATA_FILES:
            with self.subTest(name=name):
                path = self.data / name
                content = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(manifest.ManifestError, f"data/{name}"):
                    self.capture()
                path.write_bytes(content)
        for name in manifest.SOURCE_FILES:
            with self.subTest(name=name):
                path = self.root / name
                content = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(manifest.ManifestError, name):
                    self.capture()
                path.write_bytes(content)
        (self.root / "ow174/jam/__init__.py").unlink()
        with self.assertRaisesRegex(manifest.ManifestError, "source package missing: ow174/jam"):
            self.capture()

    def test_logs_profiles_secrets_collision_bytes_and_unrelated_data_are_excluded(self):
        first = self.capture()
        for name in ("logs/private.log", "profiles/1.json", "secrets.txt", "cache/collision/0688.col",
                     "ow174/game/__pycache__/model.pyc", "ow174/lobby/unrelated.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"private not part of the local game model")
        (self.data / "user_profile.json").write_bytes(b"private")
        self.assertEqual(self.capture(), first)

    def test_configuration_changes_have_typed_and_specific_diagnostics(self):
        first = self.capture()
        changes = [
            ({**self.configuration, "HEALTH": 200.0}, "configuration/HEALTH"),
            ({**self.configuration, "quanta": True}, "configuration/quanta"),
            ({**self.configuration, "ratio": 0.0}, "configuration/ratio"),
            ({**self.configuration, "HEROES": [1, 3]}, "configuration/HEROES"),
        ]
        for configuration, location in changes:
            with self.subTest(location=location):
                self.assertEqual(
                    manifest.compare_manifests(first, self.capture(configuration=configuration)),
                    [{"path": location, "kind": "changed"}],
                )
        self.assertEqual(manifest.compare_manifests(first, self.capture(configuration={"new": 1})), [
            {"path": "configuration/HEALTH", "kind": "missing"},
            {"path": "configuration/HEROES", "kind": "missing"},
            {"path": "configuration/new", "kind": "added"},
            {"path": "configuration/quanta", "kind": "missing"},
            {"path": "configuration/ratio", "kind": "missing"},
        ])

    def test_all_typed_values_are_detached_and_unsupported_objects_fail(self):
        config = {"none": None, "bool": True, "int": 1 << 20000, "float": -0.0, "bytes": b"\x00\xff",
                  "str": "value", "list": [1], "tuple": (1,), "set": {1}, "frozen": frozenset({1})}
        captured = self.capture(configuration=config)
        config["list"].append(2)
        manifest.manifest_digest(captured)
        self.assertNotEqual(captured["sha256"], self.capture(configuration=config)["sha256"])
        for value in (object(), Path("local"), float("nan"), float("inf"), {1: "non-string key"}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(manifest.ManifestError):
                self.capture(configuration={"value": value})

    def test_runtime_and_dependency_identity_are_explicit_and_diagnostic(self):
        first = self.capture()
        for field, value in (("implementation", "pypy"), ("version", (3, 13, 0, "final", 0)),
                             ("pointer_bits", 32), ("machine", "other")):
            with self.subTest(field=field):
                actual = self.capture(runtime={**RUNTIME, field: value})
                self.assertEqual(manifest.compare_manifests(first, actual), [
                    {"path": f"runtime/{field}", "kind": "changed"},
                ])
        versions = {**DEPENDENCIES, "cryptography": "next"}
        self.assertEqual(manifest.compare_manifests(first, self.capture(dependencies=versions)), [
            {"path": "dependencies/cryptography", "kind": "changed"},
        ])
        for runtime_value in ({}, {**RUNTIME, "pointer_bits": True}, {**RUNTIME, "version": [3, 12]}):
            with self.subTest(runtime=runtime_value), self.assertRaises(manifest.ManifestError):
                self.capture(runtime=runtime_value)
        for versions in ({}, {**DEPENDENCIES, "protobuf": None}):
            with self.subTest(versions=versions), self.assertRaises(manifest.ManifestError):
                self.capture(dependencies=versions)
        with (
            mock.patch.object(manifest, "runtime_identity", side_effect=AssertionError("no fallback")),
            mock.patch.object(manifest, "dependency_versions", side_effect=AssertionError("no fallback")),
        ):
            self.capture()

    def test_missing_installed_dependency_metadata_has_no_unknown_or_system_fallback(self):
        with (
            mock.patch.object(
                manifest.metadata, "version",
                side_effect=manifest.metadata.PackageNotFoundError("cryptography"),
            ) as version,
            self.assertRaisesRegex(manifest.ManifestError, "metadata missing: cryptography"),
        ):
            self.capture(dependencies=None)
        version.assert_called_once_with("cryptography")

    def test_live_configuration_captures_both_quanta_limits_and_mutable_knobs(self):
        with mock.patch("ow174.game.server.GameServer", side_effect=AssertionError("must not construct")):
            config = manifest.runtime_configuration(quanta_us=16000, run_options={"mode": "controlled"})
        self.assertEqual(config["ow174.game.match.RETAIL_QUANTA"], match.RETAIL_QUANTA)
        self.assertEqual(config["ow174.game.match.TOURNAMENT_QUANTA"], match.TOURNAMENT_QUANTA)
        for name in ("EVENTS_PER_TIME", "MAX_SCRIPT_WORK", "MAX_INSTANCE_UPDATES", "MAX_COMPONENT_UPDATES"):
            self.assertEqual(config[f"ow174.game.script.runtime.{name}"], getattr(runtime, name))
        self.assertEqual(config["ow174.game.script.driver.MAX_SHOTS"], driver.MAX_SHOTS)
        with (
            mock.patch.object(match, "HEALTH", 222.0),
            mock.patch.object(correction, "HEROES", {123, 456}),
            mock.patch.object(runtime, "MAX_SCRIPT_WORK", runtime.MAX_SCRIPT_WORK + 1),
        ):
            changed = manifest.runtime_configuration(quanta_us=7000, run_options={"mode": "controlled"})
        differences = manifest.compare_manifests(
            self.capture(configuration=config), self.capture(configuration=changed)
        )
        self.assertEqual({row["path"] for row in differences}, {
            "configuration/ow174.game.match.HEALTH", "configuration/ow174.game.correction.HEROES",
            "configuration/ow174.game.script.runtime.MAX_SCRIPT_WORK", "configuration/run.quanta_us",
        })
        self.assertEqual(config["ow174.game.correction.HEROES"], correction.HEROES)
        for quanta, options in ((True, {}), (0, {}), (16000, None)):
            with self.subTest(quanta=quanta, options=options), self.assertRaises(manifest.ManifestError):
                manifest.runtime_configuration(quanta_us=quanta, run_options=options)

    def test_digest_rejects_unsealed_tampering_and_malformed_typed_values(self):
        captured = self.capture()
        tampered = copy.deepcopy(captured)
        tampered["files"][0]["size"] += 1
        with self.assertRaisesRegex(manifest.ManifestError, "digest does not match"):
            manifest.manifest_digest(tampered)
        for mutate in (
            lambda m: m.update(extra=True), lambda m: m.update(schema="other"),
            lambda m: m["files"][0].update(path="../outside.py"),
            lambda m: m["files"][0].update(path="C:/outside.py"),
            lambda m: m["files"][0].update(size=True),
            lambda m: m["files"][0].update(sha256="A" * 64),
            lambda m: m["files"].reverse(), lambda m: m["files"].append(copy.deepcopy(m["files"][0])),
            lambda m: m.update(configuration={"type": "int", "value": "0X1"}),
            lambda m: m.update(configuration={"type": "dict", "value": [
                ["x", {"type": "bool", "value": 1}],
            ]}),
            lambda m: m.update(configuration={"type": "dict", "value": [
                ["x", {"type": "float", "value": "7ff0000000000000"}],
            ]}),
        ):
            with self.subTest(mutate=mutate):
                invalid = copy.deepcopy(captured)
                mutate(invalid)
                with self.assertRaises(manifest.ManifestError):
                    manifest.manifest_digest(invalid)

    def test_current_repository_coverage_includes_actual_helpers_without_reading_collision_contents(self):
        captured = manifest.capture_manifest(
            root=ROOT, data_dir=DATA_DIR, configuration=self.configuration,
            runtime=RUNTIME, dependencies=DEPENDENCIES,
        )
        files = {row["path"] for row in captured["files"]}
        for name in ("ow174/game/match.py", "ow174/game/projectiles.py", "ow174/game/replay_manifest.py",
                     "ow174/jam/codec.py", "data/game_graphs_174.json.gz", "data/game_heroes_174.json",
                     "data/collision_keys_174.json", "data/collision_props_174.json"):
            self.assertIn(name, files)
        self.assertEqual({name[5:] for name in files if name.startswith("data/")}, set(manifest.DATA_FILES))
        self.assertFalse(any(name.endswith(".col") for name in files))

    def test_fresh_process_hash_seed_does_not_change_canonical_set_identity(self):
        code = (
            "import json,sys; from pathlib import Path; "
            "from ow174.game.replay_manifest import capture_manifest; "
            f"runtime={RUNTIME!r}; deps={DEPENDENCIES!r}; "
            "m=capture_manifest(root=Path(sys.argv[1]),data_dir=Path(sys.argv[2]),"
            "configuration={'set':{'alpha','beta','gamma'}},runtime=runtime,dependencies=deps); "
            "print(m['sha256'])"
        )
        digests = []
        for seed in ("1", "23"):
            digests.append(subprocess.check_output(
                [sys.executable, "-B", "-c", code, str(self.root), str(self.data)], cwd=ROOT,
                env={**os.environ, "PYTHONHASHSEED": seed}, text=True,
            ).strip())
        self.assertEqual(digests[0], digests[1])


if __name__ == "__main__":
    unittest.main()
