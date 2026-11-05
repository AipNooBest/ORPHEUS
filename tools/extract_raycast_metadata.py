#!/usr/bin/env python3
"""Export local 104319 raycast metadata from DataTool's 097 and 098 DragonML directories."""

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.extract_game_graphs import EnumTables, StuXml, guid_text, tag  # noqa: E402

SCHEMA = "orpheus-raycast-metadata-v1"
BUILD = "1.74.0.0.104319"
FILTER = "STU_8EFC337B"
RECEIVER = "STURayCastReceiver"


def _typed(value, kind: str, field: str) -> None:
    if not isinstance(value, dict) or value.get("$") != kind:
        raise ValueError(f"{field} must be {kind}")


def _array(value, field: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array or null")
    return value


def _integer(value, field: str, bits: int, *, signed: bool = False) -> None:
    low, high = (-(1 << (bits - 1)), 1 << (bits - 1)) if signed else (0, 1 << bits)
    if type(value) is not int or not low <= value < high:
        raise ValueError(f"invalid {field} integer")


def _filter(fields: dict) -> None:
    for field in ("m_BC69BB40", "m_5CE47069"):
        _integer(fields.get(field), field, 64)
    _integer(fields.get("m_0220680A"), "m_0220680A", 8)
    for field in ("m_CBCB2AB9", "m_category"):
        flags = fields.get(field)
        if flags is not None:
            _typed(flags, "STU_933AD0A9", field)
            for name, value in flags.items():
                if name != "$":
                    _integer(value, f"{field}.{name}", 8)
    for row in _array(fields.get("m_BC131C03"), "m_BC131C03"):
        _typed(row, "STU_1E8E0BC3", "m_BC131C03 row")
        rule = row.get("m_B5051BCE")
        if rule is None:
            continue
        _typed(rule, "STU_ABCB02A5", "m_B5051BCE")
        for field in ("m_DEB2C67E", "m_E7094A1D", "m_1C0BC578", "m_ECAEDBE2"):
            _integer(rule.get(field), field, 64)
        for field in ("m_BE02E348", "m_283D5F20"):
            _integer(rule.get(field), field, 32, signed=True)


def _result(value, field: str) -> None:
    if value is not None:
        _typed(value, "STURayCastReceiverResult", field)
        _integer(value.get("m_flags"), f"{field}.m_flags", 32)


def _receiver(fields: dict, filters: dict) -> None:
    for field in ("m_DC35ABD3", "m_DE3392C3"):
        _result(fields.get(field), field)
    for row in _array(fields.get("m_6228373E"), "m_6228373E"):
        _typed(row, "STURayCastReceiverOverride", "m_6228373E row")
        for guid in _array(row.get("m_354CDA7A"), "m_354CDA7A"):
            if not isinstance(guid, str) or not re.fullmatch(r"0x0690[0-9A-F]{12}", guid):
                raise ValueError("m_354CDA7A must contain .097 profile GUIDs")
            if guid not in filters:
                raise ValueError(f"m_354CDA7A references absent profile {guid}")
        for field in ("m_8EB25F67", "m_C258455A"):
            _result(row.get(field), field)


def _catalog(
    root: Path,
    suffix: str,
    kind: str,
    enums: EnumTables,
    validate: Callable[[dict], None],
) -> dict[str, dict]:
    records = {}
    for path in sorted((root / suffix).glob("*")):
        name = path.name.upper()
        if not path.is_file() or not name.endswith(f".{suffix}.XML"):
            continue
        if not re.fullmatch(rf"[0-9A-F]{{1,12}}\.{suffix}\.XML", name) or not int(name.split(".")[0], 16):
            raise ValueError(f"invalid .{suffix} asset filename {path.name}")
        guid = guid_text(name[:-4])
        if guid in records:
            raise ValueError(f"duplicate .{suffix} asset {guid} in {path.name}")
        try:
            source = path.read_bytes()
            doc = StuXml(io.BytesIO(source), enums)
            if tag(doc.root) != kind:
                raise ValueError(f"expected a {kind} root")
            if kind == RECEIVER:
                for element in doc.root.iter():
                    ref = element.get("GUID")
                    if ref is not None and not re.fullmatch(r"[0-9A-Fa-f]{1,12}\.097", ref):
                        raise ValueError(f"expected a .097 profile reference, got {ref}")
            fields = doc.value(doc.root)
            validate(fields)
        except (ValueError, KeyError, ET.ParseError) as error:
            raise ValueError(f"{path.name}: {error}") from error
        records[guid] = {"sha256": hashlib.sha256(source).hexdigest(), "fields": fields}
    if not records:
        raise ValueError(f"no .{suffix} XML files in {root / suffix}")
    return records


def raycast_metadata(root: Path, enums: EnumTables) -> dict:
    """Read typed assets and validate references while keeping authored arrays and nulls."""
    filters = _catalog(root, "097", FILTER, enums, _filter)
    receivers = _catalog(root, "098", RECEIVER, enums, lambda fields: _receiver(fields, filters))
    return {"schema": SCHEMA, "build": BUILD, "filters": filters, "receivers": receivers}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path, help="DataTool XML dump root containing 097 and 098")
    parser.add_argument(
        "--owlib", type=Path, required=True, help="pinned OWLib checkout for STU enum definitions"
    )
    parser.add_argument("--output", type=Path, required=True, help="local JSON path outside the repository")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or output.suffix.lower() != ".json":
        parser.error("--output must be a JSON path outside the repository")
    types = args.owlib / "TankLib" / "STU" / "Types"
    if not types.is_dir():
        parser.error("--owlib must contain TankLib/STU/Types")
    try:
        metadata = raycast_metadata(args.xml, EnumTables(types))
        contents = json.dumps(metadata, indent=1, allow_nan=False) + "\n"
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            temporary.write_text(contents, encoding="utf-8")
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"{len(metadata['filters'])} filters, {len(metadata['receivers'])} receivers -> {output}")


if __name__ == "__main__":
    main()
