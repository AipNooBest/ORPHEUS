"""Validate an observed native Soldier body trace (JSON document or header-first JSONL)."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.native_replay import parse_trace, replay_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expect-state-hash")
    args = parser.parse_args(argv)
    try:
        result = replay_document(parse_trace(args.trace.read_text(encoding="utf-8-sig")))
        rendered = json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, ValueError, TypeError) as error:
        print(f"Native body replay failed: {error}", file=sys.stderr)
        return 1
    if result["status"] == "incomplete":
        return 3
    if args.expect_state_hash and result["state_hash"] != args.expect_state_hash.lower():
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
