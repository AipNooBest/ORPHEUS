"""Copy the runtime files from a prepared local checkout into an external directory."""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.paths import LOBBY_RUNTIME_DATA, MATCH_RUNTIME_DATA


def prepare(source: Path, destination: Path, maps: list[str]) -> None:
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ValueError("The prepared checkout does not exist")
    if destination.is_relative_to(source) or destination.exists():
        raise ValueError("Choose a new destination outside the source checkout")

    inputs = {f"data/{name}": source / "data" / name for name in LOBBY_RUNTIME_DATA + MATCH_RUNTIME_DATA}
    inputs["bnet/descriptors.pb"] = source / "ow174" / "bnet" / "descriptors.pb"
    cache = source / "cache" / "collision"
    if maps:
        if any(not re.fullmatch("[0-9a-fA-F]{4,8}", value) for value in maps):
            raise ValueError("Map indexes must contain four to eight hexadecimal digits")
        caches = [cache / f"{value.upper()}.col" for value in maps]
    else:
        caches = sorted(path for path in cache.glob("*.col") if re.fullmatch("[0-9a-fA-F]{4,8}", path.stem))
    if not caches:
        raise ValueError("No prepared collision caches were found")
    inputs.update({f"collision/{path.name}": path for path in caches})
    missing = [name for name, path in inputs.items() if not path.is_file()]
    if missing:
        raise ValueError("Missing runtime inputs: " + ", ".join(missing))

    destination.mkdir(parents=True)
    manifest = {}
    for name, path in sorted(inputs.items()):
        output = destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
        with output.open("rb") as handle:
            manifest[name] = hashlib.file_digest(handle, "sha256").hexdigest()
    (destination / "manifest.json").write_text(
        json.dumps({"schema": "orpheus-runtime-copy-v1", "sha256": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="prepared local research checkout")
    parser.add_argument("--destination", type=Path, required=True, help="new directory outside the checkout")
    parser.add_argument("--map", dest="maps", action="append", default=[], help="map index such as 0688")
    args = parser.parse_args()
    try:
        prepare(args.source, args.destination, args.maps)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Prepared runtime data at {args.destination.resolve()}")


if __name__ == "__main__":
    main()
