#!/usr/bin/env python3
"""
Build the collision caches the server's mover walks on (cache/collision/<map>.col, git-ignored), from your
own copy of the game: each map's collision chunk and the physics shapes of its props (fences, crates, ...)
are read straight from the game's storage (no DataTool needed) and only the part the mover collides with
is kept. Caches of an older format are built again.

    py tools/build_collision.py                  every map the server can host
    py tools/build_collision.py 0x688 0xD4       only these maps (GUID or index)
    py tools/build_collision.py --game <path to Overwatch.exe> --force

The game is found from game_path.txt unless --game names it. Maps are done one at a time. The server
also builds a missing cache by itself the first time a match is played on that map.
"""

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ow174.game import collision  # noqa: E402
from ow174.game.casc import CascError, Storage, game_root  # noqa: E402
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
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # map names such as Estadio das Ras in any console
    try:
        root = game_root(args.game) if args.game else collision.installed_game()
    except CascError as error:
        print(error)
        return 1
    if root is None:
        print("No game found: start the server once and pick Overwatch.exe, or pass --game.")
        return 1
    storage = Storage(root)
    total_size = total_time = 0.0
    built = 0
    for guid in map_guids(args.maps):
        name = map_name(guid) or f"0x{guid:X}"
        path = collision.cache_path(guid)
        if collision.cache_version(path) == collision.VERSION and not args.force:
            print(f"{name}: already built ({path.stat().st_size / 1e6:.2f} MB)")
            continue
        started = time.monotonic()
        try:
            path = collision.build(guid, storage, name)
        except (CascError, collision.CollisionError, OSError) as error:
            print(f"{name}: failed: {error}")
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
