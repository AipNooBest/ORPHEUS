"""Which statescript state classes of a hero the server cannot send yet (ow174/game/script/coverage.py): the
networked states in the hero's graphs whose payload the encoder has no writer for.

    py tools/codec_coverage.py soldier       one hero (a name prefix or the hero GUID, 0x02E...)
    py tools/codec_coverage.py --all         every hero, one line each
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script import coverage, graph


def main(args: list[str]) -> int:
    sys.stdout.reconfigure(errors="replace")  # hero names such as Lúcio on a cp1251 console
    if not args:
        print(__doc__)
        return 1
    if args[0] == "--all":
        for record in sorted(graph.bodies().values(), key=lambda item: item["name"]):
            rows = coverage.missing(int(record["hero"], 16))
            states = sum(len(row.states) for row in rows)
            names = ", ".join(row.cls for row in rows[:5]) + (" ..." if len(rows) > 5 else "")
            print(f"{record['name']:16s} {len(rows):3d} classes, {states:4d} states: {names}")
        return 0
    hero = int(args[0], 16) if args[0].lower().startswith("0x") else args[0]
    print(coverage.report(hero))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
