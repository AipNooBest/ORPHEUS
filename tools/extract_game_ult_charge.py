#!/usr/bin/env python3
"""Write data/game_ult_charge_174.json, the ultimate's combat charge rates (ult_charge.py).

These are not extracted: OW1 computed damage, healing and elimination charge in its server code,
and no 1.74 artifact of ours carries the rates (see ult_charge.py). The table is placeholder data
of community measurements — 380 HP of damage fill Soldier: 76's meter, an elimination adds 20 % —
stated per hero as the HP that fill a meter, and kept here so the server reads one kind of number
either way. Replace the lines from the original server's code when it is available, then delete
the placeholder status.

Usage: python tools/extract_game_ult_charge.py
"""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "data" / "game_ult_charge_174.json"

# hero guid (075) -> the line. The community's OW1 figures, by hero, as damage that fills the meter.
TABLE = {
    "status": "placeholder",
    "note": (
        "Community-measured OW1 1.74 rates, not server-code values: the hero's entity definition has no "
        "charge component and the generic settings no charge keys, so no 1.74 artifact of ours carries "
        "them. The mechanics (combat.py through ult_charge.py) are the feature; replace these numbers "
        "from the original server's code or measurements before trusting them."
    ),
    "defaults": {"damage_to_full": 380.0, "heal_to_full": None, "kill_bonus_percent": 20.0},
    "heroes": {
        # Soldier: 76: the Pulse Rifle and Helix Rockets together, per the community's OW1 count.
        "02E000000000006E": {"damage_to_full": 380.0},
    },
}


def main() -> None:
    path = PATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(TABLE, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
