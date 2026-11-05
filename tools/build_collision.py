#!/usr/bin/env python3
"""
Build the collision caches the server's mover walks on (cache/collision/<map>.col, git-ignored), from your
own copy of the game: each map's collision chunk and the physics shapes of its props (fences, crates, ...)
are read straight from the game's storage (no DataTool needed) and only the part the mover collides with
is kept. Caches of an older format are built again.

--ray-geometry writes separate *.rays.col caches for static queries. These retain all masks from
the original map chunk and indexed static props. The ordinary movement caches are still required.

    py tools/build_collision.py                  every map the server can host
    py tools/build_collision.py 0x688 0xD4       only these maps (GUID or index)
    py tools/build_collision.py 0688 0CD3 --ray-geometry
    py tools/build_collision.py --game <path to Overwatch.exe> --force

The game is found from game_path.txt unless --game names it. Maps are done one at a time. The server
also builds a missing cache by itself the first time a match is played on that map.
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ow174.game import collision, props  # noqa: E402
from ow174.game.casc import CascError, RecordCatalog, Storage, game_root  # noqa: E402
from ow174.game.content import _map_entries, map_name  # noqa: E402


def map_guids(names: list[str]) -> list[int]:
    keys = collision.collision_keys()
    if not names:
        hosted = set(_map_entries()) | {0x0800000000000688}
        return sorted(guid for guid in keys if guid in hosted)
    guids = []
    for name in names:
        value = int(name, 16)
        guids.append(value if value >> 32 else 0x0800000000000000 | value)
    return guids


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("maps", nargs="*", help="map GUIDs or indexes (hex); default: every hosted map")
    parser.add_argument("--game", help="Overwatch.exe or the game folder (default: game_path.txt)")
    parser.add_argument("--force", action="store_true", help="rebuild caches that exist")
    parser.add_argument(
        "--ray-geometry", action="store_true", help="build optional expanded static ray caches",
    )
    parser.add_argument(
        "--list-unindexed-props", action="store_true", help="print unindexed root definition GUIDs as JSON",
    )
    parser.add_argument(
        "--prop-records", type=Path, help="local definition and model catalog for --ray-geometry",
    )
    args = parser.parse_args(argv)
    if args.prop_records and (not args.ray_geometry or args.list_unindexed_props):
        parser.error("--prop-records is only used when building --ray-geometry")
    sys.stdout.reconfigure(errors="replace")  # map names such as Estadio das Ras in any console
    try:
        root = game_root(args.game) if args.game else collision.installed_game()
        if root is None:
            raise CascError("no game found in game_path.txt or --game")
        storage = Storage(root)
        selected = map_guids(args.maps)
        catalog = RecordCatalog.load(args.prop_records) if args.prop_records else None
        if args.list_unindexed_props:
            missing = set()
            for guid in selected:
                missing.update(props.unindexed_definitions(guid, storage.read_record))
            print(json.dumps([f"0x{definition:016X}" for definition in sorted(missing)]))
            return 0
    except (CascError, OSError, props.PropError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    total_size = total_time = 0.0
    built = failed = 0
    for guid in selected:
        name = map_name(guid) or f"0x{guid:X}"
        if args.ray_geometry:
            path = collision.ray_cache_path(guid)
            ready = collision.ray_cache_ready(guid, prop_records=catalog)
        else:
            path = collision.cache_path(guid)
            ready = collision.cache_version(path) == collision.VERSION
        if ready and not args.force:
            print(f"{name}: already built ({path.stat().st_size / 1e6:.2f} MB)")
            continue
        started = time.monotonic()
        try:
            path = collision.build(guid, storage, name, ray_geometry=args.ray_geometry, prop_records=catalog)
        except (CascError, collision.CollisionError, OSError) as error:
            print(f"{name}: failed: {error}")
            failed += 1
            continue
        seconds = time.monotonic() - started
        size = path.stat().st_size
        header = collision.unpack(path.read_bytes())[0]
        print(f"{name}: {header['triangles']} mesh triangles, {header['hulls']} polytopes, "
              f"{header['props']} props, {size / 1e6:.2f} MB, {seconds:.1f} s")  # fmt: skip
        total_size += size
        total_time += seconds
        built += 1
    print(f"{built} map(s) built, {total_size / 1e6:.1f} MB in {total_time:.0f} s -> {collision.CACHE_DIR}")
    return int(failed > 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
