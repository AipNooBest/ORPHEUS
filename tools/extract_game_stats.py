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
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PLAYER_STATS_ASSET = "000000000051.054.xml"
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


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit(__doc__)
    folder = Path(argv[1])
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
