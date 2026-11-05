#!/usr/bin/env python3
"""
Write data/game_movers_174.json: each hero body's character mover values (STUCharacterMoverComponent,
type 0x6EFDBD73), which the server's mover (ow174/game/mover.py) runs the hero with.

Reads DataTool's STU dumps of the bodies (type 003, `extract-stu-type <out> 003 --xml`) for the bodies in
data/game_heroes_174.json and the training bots' bodies in data/ai_spawners_174.json.

    py tools/extract_game_movers.py <XML dump folder>\003

The dump names the fields by hash. Their meaning comes from where the client's mover reads them: it keeps
the component's values at mover +0x418 + the field's offset (TankLib's STU layout), for example the jump
speed at +0x49C (jump 0x7FF789B48880), the gravity scale at +0x4A0 (0x7FF789AD03A0), the speeds at
+0x460..+0x474 (wish 0x7FF789B44340), the capsule at +0x430..+0x444 (stance 0x7FF789B4C3D0).
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
COMPONENT = re.compile(r"<STUCharacterMoverComponent dragon:id=\"\d+\" dragon:name=\"dragon:value\"([^>]*)/>")
ATTRIBUTE = re.compile(r'm_([0-9A-F]{8})="([^"]*)"')

# Field hash -> name, in the component's order (STU offset 0x18 = mover +0x430 ... 0xB1 = +0x4C9).
FIELDS = {
    "1972C687": "stand_height",
    "7A70E3E3": "stand_radius",
    "4A576BC2": "stand_pogo",
    "03E04168": "crouch_height",
    "915FF368": "crouch_radius",
    "D97B3735": "crouch_pogo",
    "D4AE9D05": "ground_circle_radius",
    "5C6B4602": "ground_circle_rays",
    "6B7FDB90": "pogo_frequency",
    "E54438F7": "pogo_damping",
    "AEBAD1B0": "max_slope",
    "08FFAA0A": "step_down",
    "CFD3E635": "run_forward",
    "61D6B4E9": "run_backward",
    "19452F15": "run_strafe",
    "B470B8DA": "crouch_forward",
    "AA6F4CE1": "crouch_backward",
    "0DDD18C3": "crouch_strafe",
    "DAA0618B": "accel_ground",
    "E631BA84": "accel_air",
    "D24BF4E6": "accel_air_touching",
    "E0A0FE25": "decel_ground_base",
    "6F25AA23": "decel_ground_base_speed",
    "4E03E892": "decel_ground_per_speed",
    "142DA8D4": "decel_air_base",
    "031FE7EE": "decel_air_base_speed",
    "8895A9C8": "decel_air_per_speed",
    "EC289987": "jump_speed",
    "C2C40969": "gravity_scale",
    "B74245E8": "terminal_velocity",
    "975F21A5": "terminal_velocity_up",
    "B99EC983": "ground_gravity_accel",
    "CEF4898C": "ground_gravity_decel",
    "D4A3C694": "character_collision",
}
# TankLib's defaults for fields a dump may leave out.
DEFAULTS = {
    "ground_circle_radius": 0.25,
    "ground_circle_rays": 4.0,
    "accel_air_touching": 0.5,
    "character_collision": 1.0,
}


def mover_of(text: str) -> dict | None:
    match = COMPONENT.search(text)
    if match is None:
        return None
    values = dict(DEFAULTS)
    for field, value in ATTRIBUTE.findall(match.group(1)):
        if field in FIELDS:
            values[FIELDS[field]] = float(value.replace(",", "."))
    values["ground_circle_rays"] = int(values["ground_circle_rays"])
    values["character_collision"] = int(values["character_collision"])
    missing = [name for name in FIELDS.values() if name not in values]
    if missing:
        raise ValueError(f"missing {missing}")
    return values


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    folder = Path(argv[1])
    heroes = list(json.loads((DATA_DIR / "game_heroes_174.json").read_text(encoding="utf-8")).values())
    spawners = DATA_DIR / "ai_spawners_174.json"
    if spawners.is_file():
        heroes += json.loads(spawners.read_text(encoding="utf-8"))["heroes"].values()
    table = {}
    for hero in heroes:
        body = int(hero["body"], 16)
        dump = folder / f"{body & 0xFFFFFFFFFFFF:012X}.003.xml"
        values = mover_of(dump.read_text(encoding="utf-8")) if dump.is_file() else None
        if values is None:
            print(f"skipped {hero['name']}: no character mover in {dump}")
            continue
        table[hero["body"]] = {"name": hero["name"], **values}
    out = DATA_DIR / "game_movers_174.json"
    out.write_text(
        json.dumps(dict(sorted(table.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"{len(table)} bodies -> {out}")


if __name__ == "__main__":
    main(sys.argv)
