#!/usr/bin/env python3
"""Offline body initialization evidence. Exit 2 means unresolved runtime paths."""

# ruff: noqa: E402 -- support direct invocation from the repository's tools folder

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ow174.game.content import find_hero
from ow174.game.script.graph import GRAPHS_PATH, bodies
from ow174.game.script.replication import ReplicationError, from_runtime
from ow174.game.script.research_runtime import BodyRuntime


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hero", default="soldier")
    parser.add_argument("--frame", type=int, default=100)
    parser.add_argument("--quanta-us", type=int, default=16000)
    parser.add_argument("--only-graph", type=lambda text: int(text, 16))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    hero = find_hero(args.hero)
    if hero is None:
        parser.error(f"Unknown hero: {args.hero}")
    catalog = None
    if args.only_graph is not None:
        # Deliberately separate a graph experiment from a complete hero bootstrap.
        catalog = {hero.body: {"graphs": [{"graph": args.only_graph}], "manager": None, "weapons": []}}
    runtime = BodyRuntime(hero.body, quanta_us=args.quanta_us, body_catalog=catalog)
    snapshot = runtime.initialize(args.frame)
    try:
        replica = from_runtime(runtime)
        wire = {"ready": True, "bits": replica.frame(args.frame).count}
    except ReplicationError as error:
        wire = {"ready": False, "reason": str(error)}
    result = {
        "schema": "ow174.body-probe.v1",
        "target_build": "1.74.0.0.104319",
        "target_build_note": "Target label; the extracted bundle does not independently prove its build.",
        "bundle_sha256": hashlib.sha256(GRAPHS_PATH.read_bytes()).hexdigest(),
        "hero": hero.name,
        "scope": "single-graph" if catalog else "body-definition",
        "graph": args.only_graph,
        "definition": (bodies() if catalog is None else catalog)[hero.body],
        "runtime": snapshot,
        "replication": wire,
        "real_client_verified": False,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(
            f"instances={len(snapshot['instances'])} diagnostics={len(snapshot['diagnostics'])} "
            f"complete={snapshot['complete']} wire_ready={wire['ready']}"
        )
    else:
        print(text, end="")
    return 0 if snapshot["complete"] and wire["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
