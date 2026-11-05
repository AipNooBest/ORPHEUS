"""Local game/jam model identity for replay headers, without importing a capture.

Hashes source/table bytes and explicitly supplied nonsecret run configuration.
This is not a checkpoint, code loader, dependency lock or collision-content manifest.
"""

import copy
import hashlib
import importlib
import json
import math
import platform
import re
import struct
import sys
from importlib import metadata
from pathlib import Path

SCHEMA = "ow174.local-game-model-manifest.v1"
BUILD = "1.74.0.0.104319"
SCOPE = "game-jam-source-tables-runtime-config-without-collision-content"
SOURCE_PACKAGES = ("ow174/game", "ow174/jam")
SOURCE_FILES = ("ow174/__init__.py", "ow174/paths.py", "requirements.txt")
DATA_FILES = (
    "ai_spawners_174.json", "collision_keys_174.json", "collision_props_174.json",
    "game_graphs_174.json.gz", "game_heroes_174.json", "game_maps_174.json",
    "game_movers_174.json", "game_schemas_174.json", "game_stats_174.json",
    "schemas_174.json", "statescript_codecs_174.json",
)
DEPENDENCIES = ("cryptography", "protobuf", "websockets")
_RUNTIME_FIELDS = {
    "implementation", "version", "implementation_version", "cache_tag",
    "pointer_bits", "machine", "byteorder", "system",
}
_CONFIG_MODULES = (
    "ow174.game.bots", "ow174.game.casc", "ow174.game.collision", "ow174.game.combat", "ow174.game.commands",
    "ow174.game.contacts", "ow174.game.correction", "ow174.game.heroselect",
    "ow174.game.link", "ow174.game.match", "ow174.game.messages", "ow174.game.mover",
    "ow174.game.notices", "ow174.game.observers", "ow174.game.onfire", "ow174.game.pools",
    "ow174.game.projectiles", "ow174.game.props", "ow174.game.replay_values",
    "ow174.game.server", "ow174.game.statescript",
    "ow174.game.stats", "ow174.game.voicelines", "ow174.game.world",
    "ow174.game.script.driver", "ow174.game.script.expr", "ow174.game.script.runtime",
    "ow174.jam.codec",
)
_REQUIRED_CONFIG = {
    "ow174.game.match": {"HEALTH", "BODY_SCRIPT_HEROES", "SEND_EVERY", "RETAIL_QUANTA", "TOURNAMENT_QUANTA"},
    "ow174.game.correction": {"CORRECTIONS", "HEROES"},
    "ow174.game.script.runtime": {
        "EVENTS_PER_TIME", "MAX_INSTANCE_UPDATES", "MAX_COMPONENT_UPDATES",
        "MAX_SCRIPT_WORK", "MAX_FOLLOW_DEPTH",
    },
    "ow174.game.script.expr": {"MAX_EXPRESSION_STEPS"},
    "ow174.game.script.driver": {"MAX_GAP", "MAX_SHOTS", "MAX_REPORTS", "ULT_SECONDS", "SETTLE_FRAMES"},
}
_HEX256 = re.compile(r"[0-9a-f]{64}\Z")


class ManifestError(ValueError):
    """The local model or a supplied manifest cannot be identified strictly."""


class ManifestMismatch(ManifestError):
    def __init__(self, differences: list[dict]) -> None:
        self.differences = differences
        locations = ", ".join(f"{item['kind']}:{item['path']}" for item in differences[:8])
        super().__init__(f"local model manifest differs ({len(differences)}): {locations}")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _keys(value, fields: set[str], where: str) -> None:
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != fields:
        raise ManifestError(f"invalid fields at {where}")


def _typed(value, where: str):
    if value is None:
        return {"type": "none"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": hex(value)}
    if type(value) is float:
        if not math.isfinite(value):
            raise ManifestError(f"nonfinite float at {where}")
        return {"type": "float", "value": struct.pack(">d", value).hex()}
    if type(value) is str:
        return {"type": "str", "value": value}
    if type(value) is bytes:
        return {"type": "bytes", "value": value.hex()}
    if type(value) in (list, tuple, set, frozenset):
        items = [_typed(item, f"{where}[]") for item in value]
        if type(value) in (set, frozenset):
            items.sort(key=_json)
        return {"type": type(value).__name__, "value": items}
    if type(value) is dict and all(type(key) is str for key in value):
        return {"type": "dict", "value": [
            [key, _typed(value[key], f"{where}/{key}")] for key in sorted(value)
        ]}
    raise ManifestError(f"unsupported configuration type at {where}")


def _validate_typed(value, where: str) -> None:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise ManifestError(f"invalid typed value at {where}")
    tag = value["type"]
    _keys(value, {"type"} if tag == "none" else {"type", "value"}, where)
    if tag == "none":
        return
    item = value["value"]
    if (tag == "bool" and type(item) is bool) or (tag == "str" and type(item) is str):
        return
    if tag in ("int", "float", "bytes") and type(item) is str:
        try:
            if tag == "int":
                valid = hex(int(item, 16)) == item
            elif tag == "bytes":
                valid = bytes.fromhex(item).hex() == item
            else:
                valid = len(item) == 16 and math.isfinite(struct.unpack(">d", bytes.fromhex(item))[0])
                valid = valid and bytes.fromhex(item).hex() == item
            if valid:
                return
        except (ValueError, struct.error):
            pass
    elif tag in ("list", "tuple", "set", "frozenset") and type(item) is list:
        for number, child in enumerate(item):
            _validate_typed(child, f"{where}/{number}")
        if tag not in ("set", "frozenset") or [_json(child) for child in item] == sorted(
            {_json(child) for child in item}
        ):
            return
    elif tag == "dict" and type(item) is list:
        keys = []
        for pair in item:
            if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str:
                raise ManifestError(f"invalid typed mapping at {where}")
            keys.append(pair[0])
            _validate_typed(pair[1], f"{where}/{pair[0]}")
        if keys == sorted(set(keys)):
            return
    raise ManifestError(f"invalid or noncanonical typed value at {where}")


def runtime_identity() -> dict:
    """Read interpreter identity only; no hostname, executable path, time or environment variables."""
    return {
        "implementation": sys.implementation.name, "version": tuple(sys.version_info),
        "implementation_version": tuple(sys.implementation.version),
        "cache_tag": sys.implementation.cache_tag, "pointer_bits": struct.calcsize("P") * 8,
        "machine": platform.machine(), "byteorder": sys.byteorder, "system": platform.system(),
    }


def dependency_versions() -> dict[str, str]:
    """Require installed metadata for every direct requirements.txt dependency."""
    values = {}
    for name in DEPENDENCIES:
        try:
            values[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise ManifestError(f"required dependency metadata missing: {name}") from None
    return values


def _validate_runtime(value: dict) -> None:
    _keys(value, _RUNTIME_FIELDS, "runtime")
    for field in ("implementation", "machine", "system"):
        if type(value[field]) is not str or not value[field]:
            raise ManifestError(f"invalid runtime {field}")
    for field in ("version", "implementation_version"):
        version = value[field]
        if (
            type(version) is not tuple or len(version) != 5
            or any(type(version[index]) is not int or version[index] < 0 for index in (0, 1, 2, 4))
            or type(version[3]) is not str or version[3] not in {"alpha", "beta", "candidate", "final"}
        ):
            raise ManifestError(f"invalid runtime {field}")
    if value["cache_tag"] is not None and (type(value["cache_tag"]) is not str or not value["cache_tag"]):
        raise ManifestError("invalid runtime cache_tag")
    if type(value["pointer_bits"]) is not int or value["pointer_bits"] <= 0 or value["pointer_bits"] % 8:
        raise ManifestError("invalid runtime pointer_bits")
    if type(value["byteorder"]) is not str or value["byteorder"] not in {"little", "big"}:
        raise ManifestError("invalid runtime byteorder")


def runtime_configuration(*, quanta_us: int, run_options: dict) -> dict:
    """Snapshot declared live model knobs; run_options must contain only nonsecret scenario options.

    Both Match quanta constants are captured independently of the scenario's selected
    quanta_us. No server, match, collision worker or system environment is constructed.
    """
    if type(quanta_us) is not int or quanta_us <= 0 or type(run_options) is not dict:
        raise ManifestError("explicit positive quanta_us and run_options mapping are required")
    values = {"run.quanta_us": quanta_us, "run.options": run_options}
    for name in _CONFIG_MODULES:
        module = importlib.import_module(name)
        required = _REQUIRED_CONFIG.get(name, set())
        if any(not hasattr(module, field) for field in required):
            raise ManifestError(f"required runtime configuration missing in {name}")
        for field, value in vars(module).items():
            if not field.isupper() or field.startswith("_"):
                continue
            numeric = value is None or type(value) in (bool, int, float)
            limit = field.startswith("MAX_") or field.endswith("_MAX") or "BUDGET" in field
            if numeric or limit or field in required:
                values[f"{name}.{field}"] = value
    # Return detached raw values without changing their Python types.
    _typed(values, "configuration")
    return copy.deepcopy(values)


def _file(base: Path, relative: str, logical: str) -> dict:
    path = base / relative
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
            raise ManifestError(f"model file escapes its declared root: {logical}")
        data = path.read_bytes()
    except OSError:
        raise ManifestError(f"required model file unreadable: {logical}") from None
    return {"path": logical, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def capture_manifest(
    *, root: Path, data_dir: Path, configuration: dict,
    runtime: dict | None = None, dependencies: dict | None = None,
) -> dict:
    """Read explicitly rooted source/table bytes and seal a detached JSON-compatible manifest."""
    root, data_dir = Path(root), Path(data_dir)
    if type(configuration) is not dict:
        raise ManifestError("configuration must be an explicitly supplied mapping")
    files = [_file(root, name, name) for name in SOURCE_FILES]
    for package in SOURCE_PACKAGES:
        folder = root / package
        if folder.is_symlink() or not folder.is_dir() or not (folder / "__init__.py").is_file():
            raise ManifestError(f"required source package missing: {package}")
        for path in sorted(folder.rglob("*.py")):
            name = path.relative_to(root).as_posix()
            files.append(_file(root, name, name))
    files.extend(_file(data_dir, name, f"data/{name}") for name in DATA_FILES)
    actual_runtime = runtime_identity() if runtime is None else runtime
    _validate_runtime(actual_runtime)
    actual_dependencies = dependency_versions() if dependencies is None else dependencies
    _keys(actual_dependencies, set(DEPENDENCIES), "dependencies")
    if any(type(value) is not str or not value for value in actual_dependencies.values()):
        raise ManifestError("dependency versions must be nonempty strings")
    document = {
        "schema": SCHEMA, "build": BUILD, "scope": SCOPE,
        "files": sorted(files, key=lambda row: row["path"]),
        "runtime": _typed(actual_runtime, "runtime"),
        "dependencies": _typed(actual_dependencies, "dependencies"),
        "configuration": _typed(configuration, "configuration"),
    }
    document["sha256"] = _hash(document)
    manifest_digest(document)
    return document


def manifest_digest(document) -> str:
    """Validate a sealed manifest and return its verified SHA-256; never silently reseal edits."""
    _keys(
        document, {"schema", "build", "scope", "files", "runtime", "dependencies", "configuration", "sha256"},
        "manifest",
    )
    if type(document["schema"]) is not str or document["schema"] != SCHEMA:
        raise ManifestError("unsupported manifest schema")
    if any(type(document[key]) is not str or not document[key] for key in ("build", "scope", "sha256")):
        raise ManifestError("invalid manifest identity")
    if type(document["files"]) is not list:
        raise ManifestError("manifest files must be a list")
    paths = []
    for row in document["files"]:
        _keys(row, {"path", "size", "sha256"}, "file")
        name = row["path"]
        if type(name) is not str or not name or "\\" in name or ":" in name or name.startswith("/"):
            raise ManifestError("file identity must be a relative POSIX path")
        if any(part in ("", ".", "..") for part in name.split("/")):
            raise ManifestError("invalid relative file identity")
        if type(row["size"]) is not int or row["size"] < 0:
            raise ManifestError(f"invalid file size: {name}")
        if type(row["sha256"]) is not str or not _HEX256.fullmatch(row["sha256"]):
            raise ManifestError(f"invalid file hash: {name}")
        paths.append(name)
    if paths != sorted(set(paths)):
        raise ManifestError("manifest file identities must be unique and sorted")
    for name in ("runtime", "dependencies", "configuration"):
        _validate_typed(document[name], name)
        if document[name]["type"] != "dict":
            raise ManifestError(f"manifest {name} must be a typed mapping")
    if not _HEX256.fullmatch(document["sha256"]):
        raise ManifestError("invalid manifest digest")
    digest = _hash({key: value for key, value in document.items() if key != "sha256"})
    if digest != document["sha256"]:
        raise ManifestError("manifest digest does not match its content")
    return digest


def _differences(expected: dict, actual: dict, path: str, result: list[dict]) -> None:
    if expected == actual:
        return
    if expected["type"] == actual["type"] == "dict":
        left, right = dict(expected["value"]), dict(actual["value"])
        for key in sorted(left.keys() | right.keys()):
            location = f"{path}/{key}"
            if key not in right:
                result.append({"path": location, "kind": "missing"})
            elif key not in left:
                result.append({"path": location, "kind": "added"})
            else:
                _differences(left[key], right[key], location, result)
    else:
        result.append({"path": path, "kind": "changed"})


def compare_manifests(expected: dict, actual: dict) -> list[dict]:
    """Compare two valid manifests; diagnostics contain logical locations, not private values."""
    manifest_digest(expected)
    manifest_digest(actual)
    differences = []
    for field in ("build", "scope"):
        if expected[field] != actual[field]:
            differences.append({"path": field, "kind": "changed"})
    left, right = ({row["path"]: row for row in manifest["files"]} for manifest in (expected, actual))
    for name in sorted(left.keys() | right.keys()):
        kind = "missing" if name not in right else "added" if name not in left else "changed"
        if left.get(name) != right.get(name):
            differences.append({"path": f"files/{name}", "kind": kind})
    for name in ("runtime", "dependencies", "configuration"):
        _differences(expected[name], actual[name], name, differences)
    return differences


def assert_compatible(expected: dict, actual: dict) -> None:
    differences = compare_manifests(expected, actual)
    if differences:
        raise ManifestMismatch(differences)
