"""Make data/ai_spawners_174.json: every map's AI spawners (the Practice Range's training bots).

Inputs: the player's Overwatch 1.74 install (each map's ENTITY chunk; its storage keys are in
data/collision_props_174.json) and a DataTool XML dump of 075 for the bot heroes' bodies:

    py tools/extract_ai_spawners.py --stu <XML dump folder> [--game <Overwatch.exe or install folder>]

The ENTITY chunk (map chunk type 0x0B), as TankLib's teMapChunk reads it: {u32 placeable count, u32
instance data offset, u32 placeable offset, u32}; per placeable a 24-byte header {uuid, u16, u8, u8 type,
u32 size} and the body {u64 definition, u64 identifier 1, u64 identifier 2, f32 translation[3], scale[3],
rotation[4], u32 instance count, ...}; from body +112 one {u32 type, u32 offset from that entry} per
instance, each an STUv2 blob ("DUTS", u32 version, u32 offset of the instance). An AI spawner is the
instance STUECAISpawnerInstanceData (0x99257985): +8 the hero (075; an asset ref, its GUID in the second
half), +24 an identifier (01C; 0 = in every mode), +40 the offset of {u64 count, u64 offset} of its
STUStatescriptGraphWithOverrides (40 bytes each: the graph's GUID at +8, at +16 the offset of {count,
offset} of the overrides' offsets), +56 the team (TeamIndex: 0 blue, 1 red), +60 and +61 two flags (both 1
by default). An override (0x5866C45E) names a variable (+16) and the offset of its value (+24): a list
(0x06610EBC: {count, offset} at +16), an identifier (0xFF80C481: GUID at +40) or an int (0x12E434A1: at +16).
An AI hint point is STUECHintPointInstanceData (0xF199249B): its identifier at +16, its place the
placeable's translation.

Per bot hero, from the dump: the body (m_gameplayEntity), its health, armour and shields (the body's
override of the common body graph 0033's variables 199, 221 and 1736) and its run speed (the mover's
run_forward, m_CFD3E635).
"""

import argparse
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ow174.game.casc import CascError, Storage, game_root  # noqa: E402
from ow174.paths import GAME_PATH_FILE  # noqa: E402

OUT = ROOT / "data" / "ai_spawners_174.json"
PROPS = ROOT / "data" / "collision_props_174.json"
NAMES = ROOT / "data" / "extracted_heroes.json"
SPAWNER, HINT = 0x99257985, 0xF199249B
OVERRIDE, LIST, IDENTIFIER, INT = 0x5866C45E, 0x06610EBC, 0xFF80C481, 0x12E434A1
GRAPH_STRIDE = 40
HERO_TYPE = 0x02E0
ENTITY_TYPE = 0x0400
HEALTH_VARS = {"health": "0000000000C7", "armor": "0000000000DD", "shields": "0000000006C8"}  # 199, 221, 1736


def u32(data: bytes, at: int) -> int:
    return struct.unpack_from("<I", data, at)[0]


def u64(data: bytes, at: int) -> int:
    return struct.unpack_from("<Q", data, at)[0]


def offsets(data: bytes, blob: int, at: int) -> list[int]:
    """The blob offsets an inline array's {u64 count, u64 offset} at `at` lists (u64 each)."""
    total, first = struct.unpack_from("<QQ", data, at)
    return [u64(data, blob + first + 8 * n) for n in range(total)]


def value(data: bytes, blob: int, at: int):
    """An override's value: a list, an identifier GUID (hex), an int, or the type's hex when unknown."""
    kind = u32(data, at)
    if kind == LIST:
        return [value(data, blob, blob + item) for item in offsets(data, blob, blob + u64(data, at + 16))]
    if kind == IDENTIFIER:
        return f"0x{u64(data, at + 40):016X}"
    if kind == INT:
        return struct.unpack_from("<i", data, at + 16)[0]
    return f"type 0x{kind:08X}"


def placeables(data: bytes):
    """(index, translation, rotation, blob, instance) of every component instance of an ENTITY chunk."""
    count, _, offset, _ = struct.unpack_from("<4I", data, 0)
    position = offset
    for index in range(count):
        size = u32(data, position + 20)
        body = position + 24
        translation = struct.unpack_from("<3f", data, body + 24)
        rotation = struct.unpack_from("<4f", data, body + 48)
        for k in range(u32(data, body + 64)):
            entry = body + 112 + 8 * k
            blob = entry + u32(data, entry + 4)
            if data[blob : blob + 4] == b"DUTS":
                yield index, translation, rotation, blob, blob + u32(data, blob + 8)
        if size == 0:
            break
        position += size


def spawners(data: bytes) -> tuple[list[dict], dict]:
    """The AI spawners of one ENTITY chunk, in placeable order, and its hint points {identifier: place}."""
    out, hints = [], {}
    for index, translation, rotation, blob, inst in placeables(data):
        kind = u32(data, inst)
        if kind == HINT:
            hints[f"0x{u64(data, inst + 16):016X}"] = [round(v, 4) for v in translation]
        if kind != SPAWNER:
            continue
        graphs, variables = [], {}
        if u64(data, inst + 40):
            total, first = struct.unpack_from("<QQ", data, blob + u64(data, inst + 40))
            for n in range(total):  # the graphs sit in the array itself
                at = blob + first + GRAPH_STRIDE * n
                graphs.append(f"0x{u64(data, at + 8):016X}")
                if u64(data, at + 16):
                    for item in offsets(data, blob, blob + u64(data, at + 16)):
                        override = blob + item
                        if u32(data, override) == OVERRIDE:
                            name = f"0x{u64(data, override + 16):016X}"
                            variables[name] = value(data, blob, blob + u64(data, override + 24))
        identifier = u64(data, inst + 32)
        out.append(
            {
                "placeable": index,
                "hero": f"0x{u64(data, inst + 16):016X}",
                "team": struct.unpack_from("<i", data, inst + 56)[0],
                "identifier": f"0x{identifier:016X}" if identifier else None,
                "position": [round(v, 4) for v in translation],
                "rotation": [round(v, 6) for v in rotation],
                "graphs": graphs,
                "vars": variables,
                "flags": [data[inst + 60], data[inst + 61]],
            }
        )
    return out, hints


def body_values(text: str) -> dict:
    """A body's health, armour and shields (its overrides of 0033's 199, 221, 1736; 0 when it has none)
    and its run speed."""
    start = text.find("<STUStatescriptComponent dragon:id")
    block = text[start : text.find("</STUStatescriptComponent>", start)] if start >= 0 else ""
    out = {}
    for name, identifier in HEALTH_VARS.items():
        at = block.find(f'GUID="{identifier}.01C"')
        found = re.search(r'm_value="([^"]+)"', block[at:]) if at >= 0 else None
        out[name] = float(found.group(1).replace(",", ".")) if found else 0.0
    mover = text.find("<STUCharacterMoverComponent dragon:id")
    speed = re.search(r'm_CFD3E635="([^"]+)"', text[mover:]) if mover >= 0 else None
    out["speed"] = float(speed.group(1).replace(",", ".")) if speed else 0.0
    return out


def hero_bodies(stu: Path, heroes: set[int]) -> dict:
    """Hero GUID -> {name, body, health, armor, shields, speed} from the 075 and 003 dump and
    data/extracted_heroes.json."""
    names = json.loads(NAMES.read_text(encoding="utf-8"))
    out = {}
    for hero in sorted(heroes):
        key = f"{hero & 0xFFFFFFFFFFFF:012X}.075"
        text = (stu / "075" / f"{key}.xml").read_text(encoding="utf-8")
        body = re.search(r'hml:name="m_gameplayEntity" GUID="([0-9A-F]{12})\.003"', text)
        if body is None:
            raise SystemExit(f"no m_gameplayEntity in {key}")
        body_text = (stu / "003" / f"{body.group(1)}.003.xml").read_text(encoding="utf-8")
        out[f"0x{hero:016X}"] = {
            "name": (names.get(key) or {}).get("Name", ""),
            "body": f"0x{ENTITY_TYPE << 48 | int(body.group(1), 16):016X}",
            **body_values(body_text),
        }
    return out


def dump(table: dict) -> str:
    """JSON with one hero, hint list or spawner per line."""
    heroes = ",\n".join(f"  {json.dumps(key)}: {json.dumps(item)}" for key, item in table["heroes"].items())
    lines = ["{", f' "_source": {json.dumps(table["_source"])},', ' "heroes": {', heroes, " },", ' "maps": {']
    entries = list(table["maps"].items())
    for number, (key, entry) in enumerate(entries):
        lines.append(f"  {json.dumps(key)}: {{")
        lines.append(f'   "hints": {json.dumps(entry["hints"])},')
        lines.append('   "spawners": [')
        lines.append(",\n".join(f"    {json.dumps(item)}" for item in entry["spawners"]))
        lines.append("   ]")
        lines.append("  }" + ("," if number < len(entries) - 1 else ""))
    lines += [" }", "}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stu", type=Path, required=True, help="DataTool XML dump with 075")
    parser.add_argument("--game", type=Path, help="Overwatch.exe or its folder (default: game_path.txt)")
    args = parser.parse_args()
    try:
        root = game_root(args.game or GAME_PATH_FILE.read_text(encoding="utf-8").strip())
    except (CascError, OSError) as error:
        raise SystemExit(f"no game install: {error}") from None
    storage = Storage(root)
    chunks = json.loads(PROPS.read_text(encoding="utf-8"))["maps"]
    maps, heroes = {}, set()
    for key, item in chunks.items():
        found, hints = spawners(storage.read(bytes.fromhex(item["ekey"]), bytes.fromhex(item["ckey"])))
        if found:
            maps[key] = {"hints": hints, "spawners": found}
            heroes |= {int(spawner["hero"], 16) for spawner in found}
    bad = [h for h in heroes if h >> 48 != HERO_TYPE]
    if bad:
        raise SystemExit(f"not heroes: {bad}")
    table = {
        "_source": "Build 1.74.0.0.104319: each map's ENTITY chunk (0x0DD0000B << 32 | map index), its "
        "STUECAISpawnerInstanceData and STUECHintPointInstanceData instances (tools/extract_ai_spawners.py); "
        "hero bodies from the heroes' m_gameplayEntity, their health, armor and shields from the body's "
        "overrides of 0033's vars 199, 221, 1736, speed = the mover's run_forward. team: TeamIndex (0 blue, "
        "1 red). identifier: the mode identifier the spawner needs (null: every mode). vars: the graphs' "
        "variable overrides.",
        "heroes": hero_bodies(args.stu, heroes),
        "maps": maps,
    }
    OUT.write_text(dump(table), encoding="utf-8")
    total = sum(len(entry["spawners"]) for entry in maps.values())
    print(f"{total} spawners on {len(maps)} maps, {len(heroes)} heroes -> {OUT}")


if __name__ == "__main__":
    main()
