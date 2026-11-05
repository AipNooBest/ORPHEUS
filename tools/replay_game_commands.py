"""Replay a v1 decoded-command JSON recording without starting servers or the game."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.simulation import replay_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path, help="ow174.command-replay.v1 JSON recording")
    parser.add_argument("--output", type=Path, help="write the result JSON here (default: stdout)")
    parser.add_argument("--expect-state-hash", help="fail if this SHA256 does not match the replay")
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.recording.read_text(encoding="utf-8-sig"))
        result = replay_document(document)
        rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.output is not None:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (OSError, ValueError, TypeError) as error:
        print(f"Replay failed: {error}", file=sys.stderr)
        return 1
    if args.expect_state_hash and result["state_hash"] != args.expect_state_hash.lower():
        print(f"State hash mismatch: actual {result['state_hash']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
