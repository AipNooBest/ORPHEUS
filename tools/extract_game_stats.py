#!/usr/bin/env python3
"""
Write data/game_stats_174.json: the stats the Tab board lists for a player, from the game data.

The board shows the local player's component 64: the player stats (eliminations, objective kills and time,
hero damage, healing, deaths) and the hero stats (accuracy, hero abilities). Which stats those are is in
the data: the generic settings 0051.054 (STU_13047F85) m_FC833C02 holds the player stats and
m_A38A50B4 the game modes that list others; each hero (075) m_FC833C02 holds its hero stats and
m_A341183E the game modes that list others. An entry's m_60BFB2D1 is 1 on Deaths (the server's medals
leave it out). Each stat (062) says how the client prints its value (m_displayType, STUStatDisplayType:
0 a count, 2 a fraction shown as a percentage, 8 seconds shown as a time; enum table 0x7FF78C049280).

The file: "player" and "heroes" (by hero GUID) hold {"default": [[stat, no medal]], "modes": {mode:
[...]}}, "stats" each stat's display type.

Reads DataTool's STU dumps (`extract-stu-type <out> 054|075|062 --xml`) and data/game_heroes_174.json.

    py tools/extract_game_stats.py <XML dump folder>

Export the separate 003F.054 score event catalog locally using the pinned OWLib types:

    py tools/extract_game_stats.py <XML dump folder> --owlib <checkout> --score-events <local JSON>
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.extract_game_graphs import EnumTables, StuXml  # noqa: E402

DATA_DIR = ROOT / "data"
PLAYER_STATS_ASSET = "000000000051.054.xml"
SCORE_EVENTS_ASSET = "00000000003F.054.xml"
STAT_REF = re.compile(r'hml:name="m_stat" GUID="([0-9A-F]+)\.062"')
ENTRY = re.compile(r'<STU_C0D5117B [^>]*m_60BFB2D1="(\d)"[^>]*>(.*?)</STU_C0D5117B>', re.S)
MODE_LIST = re.compile(
    r'hml:name="m_gamemode" GUID="([0-9A-F]+)\.0C5".*?dragon:name="m_stats">(.*?)</dragon:array>', re.S
)
DISPLAY_TYPES = {  # STUStatDisplayType, the 1.74 client's enum table
    "x316B0035": 0,
    "x9AFF4582": 1,
    "xE964E77F": 2,
    "xAA107C74": 3,
    "xCB522517": 4,
    "xC5E4DAF9": 5,
    "xA5416732": 6,
    "x1AF33797": 7,
    "xBE6E8B21": 8,
}


def guid(index: int, file_type: int) -> int:
    """A GUID from its index and file type: the type sits bit-reversed at bits 48..59."""
    reversed_type = int(f"{file_type - 1:012b}"[::-1], 2)
    return reversed_type << 48 | index


def entries(text: str) -> list[list]:
    """[stat GUID, no medal] of each STU_C0D5117B in a list."""
    out = []
    for flag, body in ENTRY.findall(text):
        stat = STAT_REF.search(body)
        if stat:
            out.append([f"0x{guid(int(stat.group(1), 16), 0x62):016X}", int(flag)])
    return out


def array(text: str, name: str) -> str:
    match = re.search(f'dragon:name="{name}">' + r"(.*?)\n  </dragon:array>", text, re.S)
    return match.group(1) if match else ""


def lists(text: str, default_name: str, modes_name: str) -> dict:
    modes = {
        f"0x{guid(int(mode, 16), 0xC5):016X}": entries(body)
        for mode, body in MODE_LIST.findall(array(text, modes_name))
    }
    return {"default": entries(array(text, default_name)), "modes": modes}


def display_type(folder: Path, stat: str) -> int | None:
    index = int(stat, 16) & 0xFFFFFFFFFFFF
    text = (folder / "062" / f"{index:012X}.062.xml").read_text(encoding="utf-8")
    display = re.search(r'm_displayType="([^"]+)"', text)
    return DISPLAY_TYPES.get(display.group(1)) if display else None


def score_event_metadata(path: Path, enums: EnumTables) -> dict:
    """Decode the ordered label and scaler rows without assigning meanings to anonymous fields."""
    document = StuXml(path, enums)
    if document.root.tag != "STU_A1BCAB6E":
        raise ValueError(f"{path.name} is not score event settings (STU_A1BCAB6E)")
    rows = {}
    for field, key, cls in (("m_3ED74851", "labels", "STU_E2C36BB8"),
                            ("m_AFED546B", "scalers", "StatEventScoreScaler")):
        element = document.field(document.root, field)
        values = document.value(element) if element is not None else None
        if not isinstance(values, list) or not values or any(
            not isinstance(value, dict) or value.get("$") != cls for value in values
        ):
            raise ValueError(f"{path.name} has no valid {field} array")
        rows[key] = values
    for row in rows["labels"]:
        events = row.get("m_34A798D4")
        if events is not None and (
            not isinstance(events, list) or any(type(event) is not int for event in events)
        ):
            raise ValueError(f"{path.name} contains an unresolved score event label enum")
    for row in rows["scalers"]:
        if type(row.get("m_B44A42A0")) is not int:
            raise ValueError(f"{path.name} contains an unresolved score event scaler enum")
        coefficient, option = row.get("m_9A97C666"), row.get("m_20100AF4")
        if type(coefficient) not in (int, float) or type(option) is not int or not 0 <= option <= 255:
            raise ValueError(f"{path.name} has invalid score event scaler fields")
    return {"asset": "003F.054", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **rows}


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("xml", type=Path, help="DataTool XML dump folder")
    parser.add_argument("--owlib", type=Path, help="pinned OWLib checkout for score event enum definitions")
    parser.add_argument("--score-events", type=Path, metavar="OUTPUT", help="export local score events")
    args = parser.parse_args(argv[1:])
    folder = args.xml
    if args.score_events is not None:
        if args.owlib is None:
            parser.error("--score-events requires --owlib")
        path = folder / "054" / SCORE_EVENTS_ASSET
        output = args.score_events.resolve()
        if output == path.resolve() or output.is_relative_to(ROOT):
            parser.error("--score-events needs a separate output outside the repository")
        enums = EnumTables(args.owlib / "TankLib" / "STU" / "Types")
        metadata = score_event_metadata(path, enums)
        output.write_text(json.dumps(metadata, indent=1, allow_nan=False) + "\n", encoding="utf-8")
        print(f"{len(metadata['labels'])} labels, {len(metadata['scalers'])} scalers -> {output}")
        return
    settings = (folder / "054" / PLAYER_STATS_ASSET).read_text(encoding="utf-8")
    if "<STU_13047F85 " not in settings:
        raise SystemExit(f"{PLAYER_STATS_ASSET} is not the player stats settings (STU_13047F85)")
    heroes = json.loads((DATA_DIR / "game_heroes_174.json").read_text(encoding="utf-8"))
    table = {"player": lists(settings, "m_FC833C02", "m_A38A50B4"), "heroes": {}, "stats": {}}
    for hero in heroes:
        dump = folder / "075" / f"{int(hero, 16) & 0xFFFFFFFFFFFF:012X}.075.xml"
        table["heroes"][hero] = lists(dump.read_text(encoding="utf-8"), "m_FC833C02", "m_A341183E")
    used = set()
    for group in [table["player"], *table["heroes"].values()]:
        for stats in [group["default"], *group["modes"].values()]:
            used.update(stat for stat, _ in stats)
    table["stats"] = {stat: display_type(folder, stat) for stat in sorted(used)}
    out = DATA_DIR / "game_stats_174.json"
    out.write_text(json.dumps(table, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(table['heroes'])} heroes, {len(table['stats'])} stats -> {out}")


if __name__ == "__main__":
    main(sys.argv)
