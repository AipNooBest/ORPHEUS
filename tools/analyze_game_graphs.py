#!/usr/bin/env python3
"""Report hero graph references, static entry links, layouts and unresolved evidence."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.content import find_hero
from ow174.game.script.analysis import analyze_body, render_text, source_fingerprint


def graph_index(text: str) -> int:
    try:
        index = int(text, 16)
    except ValueError as error:
        raise argparse.ArgumentTypeError("graph indexes are hexadecimal, e.g. 0254") from error
    if not 0 <= index <= 0xFFFF:
        raise argparse.ArgumentTypeError("graph index must fit in 16 bits")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hero", default="soldier", help="hero name or full hero GUID")
    parser.add_argument("--detail-graph", action="append", type=graph_index, help="include node fields (hex)")
    parser.add_argument(
        "--supported-node", action="append", help="declare a handler class; not proof of coverage",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable layouts and node summaries")
    parser.add_argument("--output", type=Path, help="write UTF-8 report to this file instead of stdout")
    args = parser.parse_args(argv)
    hero = find_hero(args.hero)
    if hero is None:
        parser.error(f"Unknown or ambiguous hero: {args.hero}")
    details = args.detail_graph or []
    report = analyze_body(
        hero.body, supported_nodes=None if args.supported_node is None else set(args.supported_node),
        detail_graphs=details,
    )
    outside = sorted(set(details) - set(report["closure"]["found"]))
    if outside:
        parser.error("Detailed graphs are absent from this hero's closure: "
                     + ", ".join(f"{index:04X}" for index in outside))
    report["source"] = source_fingerprint()
    if args.json:
        text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    else:
        text = render_text(report, details)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {len(report['graphs'])} graph summaries to {args.output}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
