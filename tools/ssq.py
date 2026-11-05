#!/usr/bin/env python3
"""
Query the 1.74 statescript graphs the server has (data/game_graphs_174.json.gz, read with
ow174/game/script/graph.py): which graphs a hero or a mode runs, what a graph's nodes do, who reads and
writes a variable, where a node class is used.

    py tools/ssq.py graphs [--hero NAME] [--mode NAME] [--entity GUID]   graphs, their role and size
    py tools/ssq.py show GRAPH [--state N] [--depth N]                    a graph as a tree from its entries
    py tools/ssq.py var ID_OR_NAME [--graph GRAPH]                        every use of a variable
    py tools/ssq.py class NAME_OR_HASH                                    where a class is used, its fields
    py tools/ssq.py find TEXT                                             names, aliases, hashes, GUIDs
    py tools/ssq.py hero NAME                                             a hero's body, graphs, classes, vars

    py tools/ssq.py graphs --hero soldier
    py tools/ssq.py show 0255 --state 1 --depth 1
    py tools/ssq.py var 699 --graph 0255
    py tools/ssq.py class MovementMod
    py tools/ssq.py find "hero select"

Notation: #699 is instance variable 699 and #7644e entity variable 7644 (as the VM's expr.source writes
them); rid is a node's m_nodes index (what owner frames name), st its m_states index, remote its
m_remoteSyncNodes index. Graphs are hex (0255), variables decimal (699), GUIDs index.type (02BB.01C).

Roles: body (a hero body's initial graph; ability when it has an Ability state), weapon (the weapon
component's manager and weapon scripts), mode and controller (the game modes' scripts), UI (hero select
and the HUD). A graph that other graphs name (a SubScript or PlayScript target, a graph value) has the
roles of the graphs that name it.

Variable uses: write = the node stores into it (a field the STU types declare as a variable, or the
first variable of an expression in such a field); read = it is evaluated; parent = an expression reads it
in a parent instance; id = an expression uses the identifier itself as a value or key; param = a game
message parameter; list = it is named in a variable list (ClientOnlySharedVars).

Names (class aliases, modes, buttons, variables) come from data/statescript_names_174.json. The index is
kept in cache/ssq_index.pickle and built again when the data or the code changes (a few seconds).
"""

import argparse
import collections
import json
import os
import pickle
import re
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ow174.game import content  # noqa: E402
from ow174.game.script import expr, nodes, runtime  # noqa: E402
from ow174.game.script import graph as graphs  # noqa: E402

NAMES_PATH = ROOT / "data" / "statescript_names_174.json"
CODECS_PATH = ROOT / "data" / "statescript_codecs_174.json"  # optional, tools/extract_statescript_codecs.py
CACHE_PATH = ROOT / "cache" / "ssq_index.pickle"
# Optional: TankLib/STU/Types of an OWLib checkout, for each field's declared type and offset.
OWLIB_TYPES = Path(os.environ["OWLIB_TYPES"]) if os.environ.get("OWLIB_TYPES") else None
VERSION = 5
SOURCES = (
    graphs.GRAPHS_PATH,
    NAMES_PATH,
    content.HEROES_PATH,
    Path(__file__).resolve(),
    Path(graphs.__file__),
    Path(expr.__file__),
    Path(nodes.__file__),
    Path(runtime.__file__),
)
IDENT_TYPE, MODE_TYPE = 0x0D80, 0x0230  # GUID type bits of a .01C identifier and a .0C5 game mode
ROLES = ("body", "ability", "weapon", "mode", "controller", "UI")
ABILITY = "STUStatescriptStateAbility"
EXPRESSION_DATA = "STUConfigVarExpressionData"
CONSTANT_TYPES = {"STUConfigVarInt": "int", "STUConfigVarFloat": "float", "STUConfigVarBool": "bool"}
# Objects that pair a variable identifier with a value: (identifier field, value field, what it is).
ENTRIES = {
    "STUStatescriptSchemaEntry": ("m_0D09D2D9", "m_value", "default"),
    "STU_BDFD54D7": ("m_1BF2BEEA", "m_A53100D5", "default"),
    "STU_6364CD4F": ("m_0D09D2D9", "m_value", "set"),
}
REMOTE_SYNC_VAR = "STUStatescriptRemoteSyncVar"
SYNC_LISTS = ("m_BF5B22B7", "m_8BF03679")  # STUStatescriptBase's remote sync variable lists
# The fields of the statescript base classes (OWLib: STUStatescriptBase, State, Entry, Condition, Action
# and its STU_4C2054BF), the same in every node class: `class` does not list them. show leaves out the
# bookkeeping ones, and the enums while they are 0.
BASE_FIELDS = {
    *SYNC_LISTS, "m_A2287776", "m_AED90719", "m_2BBEEAB8", "m_ADEB6E05", "m_stateGroup", "m_transitionPlug",
    "m_4F8F2F3F", "m_0B1AA8CA", "m_beginPlug", "m_F198FD3A", "m_onBeginPlug", "m_onEndPlug", "m_subgraphPlug",
    "m_outPlug", "m_5AEA0A51", "m_inPlug", "m_truePlug", "m_falsePlug",
}  # fmt: skip
BOOKKEEPING = {"m_2BBEEAB8", "m_ADEB6E05", "m_0B1AA8CA", "m_EE729DCB"}
QUIET_ZERO = {"m_A2287776", "m_AED90719", "m_5AEA0A51"}
DEFAULT_PLUGS = ("m_beginPlug", "m_inPlug")
CUT = 160  # characters of one field value in show
# Config vars expr.source writes in its own way (a variable, owner(), ruleset(), valueOr(), ...).
SOURCE_CLASSES = {
    "STUConfigVarDynamic", "STU_B5A0CAF0", "STU_3832D36C", "STU_91EFD5B1", "STU_3ACE35FB", "STU_7197C080"
}  # fmt: skip


# --- names and text -----------------------------------------------------------------------------------


@cache
def names() -> dict:
    data = json.loads(NAMES_PATH.read_text(encoding="utf-8"))
    data["buttons"] = {int(key): value for key, value in data["buttons"].items()}
    data["vars"] = {int(key): value for key, value in data["vars"].items()}
    data["graphs"] = {int(key, 16): value for key, value in data["graphs"].items()}
    data["modes"] = {int(key, 16): value for key, value in data["modes"].items()}
    return data


def alias(cls: str) -> str:
    """The short name of a class: its alias, else STUStatescript* without the prefix, else the class."""
    found = names()["classes"].get(cls, {}).get("alias")
    if found:
        return found
    if cls.startswith("STUStatescript") and len(cls) > len("STUStatescript"):
        return cls[len("STUStatescript") :]
    return cls


def class_hash(cls: str) -> str | None:
    if cls.startswith("STU_"):
        return cls[4:]
    return names()["classes"].get(cls, {}).get("hash")


def guid_type(value: int) -> int:
    """The asset type of a GUID (0x01B, 0x01C, ...): its 12 type bits reversed, plus one."""
    return int(f"{(value >> 48) & 0xFFF:012b}"[::-1], 2) + 1


def guid_text(value: int) -> str:
    return f"{value & 0xFFFFFFFFFFFF:04X}.{guid_type(value):03X}"


def var_text(var: int, scope: int | None = None) -> str:
    return f"#{var}{'e' if scope == graphs.ENTITY else ''}"


def var_name(var: int, scope: int | None = None) -> str:
    entry = names()["vars"].get(var)
    return f"{var_text(var, scope)} {entry['name']}" if entry else var_text(var, scope)


def scope_text(scope: int | None) -> str:
    return "entity" if scope == graphs.ENTITY else "instance"


def mode_name(guid: int) -> str:
    entry = names()["modes"].get(guid)
    return (entry or {}).get("name") or guid_text(guid)


def button_text(value) -> str:
    name = names()["buttons"].get(value) if isinstance(value, int) else None
    return f"{value} {name}" if name else str(value)


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class _Text(str):
    """Text that expr.source prints as it is (it prints repr() of anything that is not a config var)."""

    def __repr__(self) -> str:
        return str(self)


def render(value, depth: int = 0) -> str:
    """A field value as text: variables and expressions as the VM writes them (expr.source), config vars
    as Alias(field=value), other objects as Alias{field=value}, GUIDs as index.type."""
    if isinstance(value, list):
        return "[" + ", ".join(render(item, depth + 1) for item in value) + "]"
    if isinstance(value, str) and graphs.GUID_TEXT.match(value):
        return guid_text(int(value, 16))
    if not isinstance(value, dict):
        return repr(value)
    cls = value.get("$")
    if cls is None:
        return f"node {value['node']}" if "node" in value else "editor box"
    if cls == "STUConfigVarLogicalButton":
        return f"button({button_text(value.get('m_logicalButton'))})"
    if cls in CONSTANT_TYPES:
        number = value.get("m_value") or 0
        return ("true" if number else "false") if cls == "STUConfigVarBool" else repr(number)
    if cls in expr.RESOURCES:
        name = cls.replace("STUConfigVar", "") if cls.startswith("STUConfigVar") else alias(cls)
        field = expr.RESOURCES[cls]
        guid = graphs.guid(value.get(field))
        parts = [guid_text(guid) if guid is not None else "none"] + [
            f"{key}={render(item, depth + 1)}"
            for key, item in value.items()
            if key not in ("$", "m_EE729DCB", "m_resourceKey", field) and item is not None
        ]
        return f"{name}({', '.join(parts)})"
    if depth > 8:
        return f"{alias(cls)}(...)"
    expression = isinstance(value.get("m_expression"), dict)
    if cls in SOURCE_CLASSES or expression:
        plain = {
            key: _Text(render(item, depth + 1)) if isinstance(item, (dict, list)) else item
            for key, item in value.items()
        }
        if expression:
            plain["m_expression"] = value["m_expression"]
            plain["m_configVars"] = [
                _Text(render(item, depth + 1)) for item in value.get("m_configVars") or []
            ]
        return short_floats(expr.source(plain))
    parts = [
        f"{key}={render(item, depth + 1)}"
        for key, item in value.items()
        if key not in ("$", "links", "m_EE729DCB") and item is not None
    ]
    if "m_EE729DCB" in value:  # a config var: evaluated, so written as a call
        return f"{alias(cls)}({', '.join(parts)})"
    return f"{alias(cls)}{{{', '.join(parts)}}}"


def value_type(cfg) -> str | None:
    """int / float / bool / a resource kind for a constant config var; None for a variable, an expression
    or anything computed."""
    cls = cfg.get("$") if isinstance(cfg, dict) else None
    if cls in CONSTANT_TYPES:
        return CONSTANT_TYPES[cls]
    if cls in expr.RESOURCES or (cls and "m_resourceKey" in cfg):
        return cls.replace("STUConfigVar", "") if cls.startswith("STUConfigVar") else alias(cls)
    return None


def cut(text: str, size: int = CUT) -> str:
    return text if len(text) <= size else text[: size - 3] + "..."


LONG_FLOAT = re.compile(r"(?<![\w.])-?\d+\.\d{7,}(?:e[-+]?\d+)?")


def short_floats(text: str) -> str:
    """expr.source prints float32 constants in full (0.30000001192092896): print the shortest decimal that
    is the same float32 (0.3)."""

    def shortest(match) -> str:
        value = float(match.group(0))
        for digits in range(1, 18):
            short = f"{value:.{digits}g}"
            if expr.f32(float(short)) == expr.f32(value):
                return short
        return match.group(0)

    return LONG_FLOAT.sub(shortest, text)


# --- reading a node -----------------------------------------------------------------------------------


class Refs:
    """What one node (or a graph's own fields) holds."""

    def __init__(self) -> None:
        self.vars: list[tuple] = []  # (kind, var, scope, path)
        self.graphs: list[tuple] = []  # (graph index, path)
        self.ids: list[tuple] = []  # (identifier, holder class, field, path)
        self.entries: list[tuple] = []  # (identifier, what, value config var, path)
        self.syncs: list[tuple] = []  # (identifier, scope, path)
        self.assets: list[tuple] = []  # (GUID, path)
        self.objects: list[tuple] = []  # (class, object)
        self.buttons: list = []  # logical button values


class Walker:
    """Walks a node's fields and sorts every variable, identifier, graph and asset it holds."""

    def __init__(self, var_fields: dict) -> None:
        self.var_fields = var_fields

    def run(self, fields: dict, cls: str) -> Refs:
        self.refs = Refs()
        self.obj(fields, cls, "")
        return self.refs

    def obj(self, value: dict, cls: str, path: str) -> None:
        self.refs.objects.append((cls, value))
        config = "m_EE729DCB" in value  # every STUConfigVar has it, nothing else does (checked with OWLib)
        if cls in ENTRIES:
            key, field, what = ENTRIES[cls]
            ident = graphs.guid(value.get(key))
            if ident is not None:
                self.refs.entries.append((ident & 0xFFFF, what, value.get(field), path))
        elif cls == REMOTE_SYNC_VAR:
            ident = graphs.guid(value.get("m_0D09D2D9"))
            if ident is not None:
                self.refs.syncs.append((ident & 0xFFFF, value.get("m_56341592"), path))
        elif cls == "STUConfigVarLogicalButton":
            self.refs.buttons.append(value.get("m_logicalButton"))
        for key, item in value.items():
            if key not in ("$", "links"):
                self.value(item, cls, config, key, f"{path}.{key}" if path else key)

    def value(self, item, holder: str, config: bool, field: str, path: str) -> None:
        if isinstance(item, dict):
            cls = item.get("$")
            if cls is None:
                return  # {"node": ...} or {"item": ...}
            if cls == "STUConfigVarDynamic":
                self.var(item, self.kind(holder, config, field), path)
                self.refs.objects.append((cls, item))
            elif isinstance(item.get("m_expression"), dict):
                out_field = not config and self.var_fields.get(holder, {}).get(field) == "var"
                self.expression(item, cls, path, out_field)
            else:
                self.obj(item, cls, path)
        elif isinstance(item, list):
            for number, element in enumerate(item):
                self.value(element, holder, config, field, f"{path}[{number}]")
        elif isinstance(item, str) and graphs.GUID_TEXT.match(item):
            guid = int(item, 16)
            if guid >> 48 == graphs.GRAPH_TYPE:
                self.refs.graphs.append((guid & 0xFFFFFFFFFFFF, path))
            elif guid >> 48 != IDENT_TYPE:
                self.refs.assets.append((guid, path))
            elif holder != REMOTE_SYNC_VAR and not (holder in ENTRIES and field == ENTRIES[holder][0]):
                self.refs.ids.append((guid & 0xFFFF, holder, field, path))

    def kind(self, holder: str, config: bool, field: str) -> str:
        if config:
            return "read"  # an argument of a config var
        declared = self.var_fields.get(holder, {}).get(field)
        return {"var": "write", "list": "list"}.get(declared, "read")

    def var(self, cfg: dict, kind: str, path: str) -> None:
        scope, var = expr.dynamic(cfg)
        self.refs.vars.append((kind, var, scope, path))

    def expression(self, cfg: dict, cls: str, path: str, out_field: bool) -> None:
        """The variables of an expression by what its bytecode does with them; in a variable field the
        first variable pushed is the one written (expr.lvalue)."""
        self.refs.objects.append((cls, cfg))
        data = cfg["m_expression"]
        self.refs.objects.append((EXPRESSION_DATA, data))
        uses, first = bytecode_uses(list(data.get("m_opcodes") or []))
        for number, dynamic in enumerate(data.get("m_dynamicVars") or []):
            if not isinstance(dynamic, dict) or dynamic.get("$") != "STUConfigVarDynamic":
                continue
            kinds = set(uses.get(number, {"unused"}))
            if out_field and number == first:
                kinds = (kinds - {"read"}) | {"write"}
            for kind in sorted(kinds):
                self.var(dynamic, kind, f"{path}.m_expression.m_dynamicVars[{number}]")
        for key, item in data.items():
            if key not in ("$", "m_dynamicVars"):
                self.value(item, EXPRESSION_DATA, True, key, f"{path}.m_expression.{key}")
        for key, item in cfg.items():
            if key not in ("$", "m_expression"):
                self.value(item, cls, True, key, f"{path}.{key}")


def bytecode_uses(code: list) -> tuple[dict, int | None]:
    """What an expression's bytecode does with each m_dynamicVars entry (the ops of expr.run), following
    its jumps (code after an END is only reached by a jump), and the entry its first push reads."""
    uses: dict[int, set] = {}
    first = None
    seen = set()
    todo = [0]
    while todo:
        pc = todo.pop()
        while 0 <= pc < len(code) and pc not in seen:
            seen.add(pc)
            op = code[pc]
            arg = code[pc + 1] if pc + 1 < len(code) else 0
            if op == 0:
                break
            if op == 1:
                pc += arg
                continue
            if op in (2, 3, 4, 5):  # and, or, jump if false, jump if true: both ways
                todo.append(pc + arg)
                pc += 2
                continue
            if op in (8, 9, 10):
                number = code[pc + 2] if op == 10 and pc + 2 < len(code) else arg
                uses.setdefault(number, set()).add("parent" if op == 10 else "read")
                first = number if first is None else first
            elif op in (7, 13):
                uses.setdefault(arg, set()).add("id" if op == 7 else "param")
            pc += 1 + expr.OPERANDS.get(op, 0)
    return uses, first


def label(value) -> str:
    """What kind of value a field holds, for the field lists of `class`."""
    if isinstance(value, dict):
        if "$" in value:
            return alias(value["$"])
        return "node link" if "node" in value else "editor box"
    if isinstance(value, list):
        kinds = sorted({label(item) for item in value if item is not None})
        return f"[{', '.join(kinds)}]" if kinds else "[]"
    if isinstance(value, str):
        return f"GUID .{guid_type(int(value, 16)):03X}" if graphs.GUID_TEXT.match(value) else "text"
    return type(value).__name__


# --- the index ----------------------------------------------------------------------------------------


def signature() -> tuple:
    files = [(path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in SOURCES if path.is_file()]
    owlib = OWLIB_TYPES.stat().st_mtime_ns if OWLIB_TYPES and OWLIB_TYPES.is_dir() else None
    return VERSION, tuple(files), owlib


def load_index(rebuild: bool = False) -> dict:
    key = signature()
    if not rebuild and CACHE_PATH.is_file():
        try:
            index = pickle.loads(CACHE_PATH.read_bytes())
        except (pickle.UnpicklingError, EOFError, AttributeError, ValueError):
            index = None
        if isinstance(index, dict) and index.get("key") == key:
            return index
    index = build_index()
    index["key"] = key
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_name(f"{CACHE_PATH.name}.{os.getpid()}.tmp")
    temporary.write_bytes(pickle.dumps(index, protocol=pickle.HIGHEST_PROTOCOL))
    os.replace(temporary, CACHE_PATH)
    return index


def graph_summary(graph: graphs.Graph) -> dict:
    return {
        "nodes": len(graph.nodes),
        "states": sum(node is not None for node in graph.states),
        "entries": sum(node is not None for node in graph.entries),
        "remote": sum(node is not None for node in graph.remote_nodes),
        "unknown": len(graph.unknown_rids),
        "networked": sum(node.is_networked for node in graph.nodes),
        "client_only": sum(node.client_only for node in graph.nodes),
        "server_only": sum(node.server_only for node in graph.nodes),
        "owner_states": len(graph.owner_states()),
        "remote_states": len(graph.remote_states()),
        "sync_vars": len(graph.sync_vars),
        "presence": [entry.var for entry in graph.presence_vars()],
        "bits": (graph.nodes_bits, graph.states_bits, graph.remote_bits, graph.sync_vars_bits),
        "prediction": graph.header.get("m_predictionBehavior"),
        "classes": collections.Counter(node.cls for node in graph.nodes),
        "nodes_info": {
            node.rid: (node.state, node.cls, node.client_only, node.server_only) for node in graph.nodes
        },
        "abilities": [],  # (rid, st, logical buttons) of each Ability state
    }


def build_index() -> dict:
    walker = Walker(names()["var_fields"])
    lists = ("vars", "syncs", "sync_vars", "entries", "ids", "assets", "class_nodes", "class_values")
    index = {key: collections.defaultdict(list) for key in lists}
    # vars: var -> (graph, rid, kind, scope, path); syncs: var -> (graph, rid, scope, path) of the nodes'
    # remote sync var lists; sync_vars: var -> (graph, m_syncVars entry, scope, m_AC9480C7, presence bit);
    # entries: var -> (what, graph, rid, where, value text, type); ids: var -> (graph, rid, holder, field,
    # path); assets: GUID -> (graph, rid, path); class_nodes / class_values: class -> (graph, rid).
    index.update(graphs={}, edges=[], fields=collections.defaultdict(dict), objects=collections.Counter())
    index["constant_writes"] = collections.defaultdict(collections.Counter)  # var -> (graph, scope, type)
    for gi in graphs.graph_indexes():
        graph = graphs.graph(gi)
        summary = index["graphs"][gi] = graph_summary(graph)
        add_refs(index, gi, None, walker.run(graph.fields, "graph"))
        for node in graph.nodes:
            refs = walker.run(node.fields, node.cls)
            add_refs(index, gi, node.rid, refs)
            index["class_nodes"][node.cls].append((gi, node.rid))
            if node.cls == ABILITY:
                buttons = sorted({value for value in refs.buttons if isinstance(value, int)})
                summary["abilities"].append((node.rid, node.state, buttons))
        for entry in graph.sync_vars:
            if entry.var is not None:
                bit = graph.presence_bit(entry.var) if entry.has_presence_bit else None
                index["sync_vars"][entry.var].append((gi, entry.index, entry.scope, entry.flag, bit))
    index["attach"] = attachments(index)
    index["roles"] = roles(index)
    index["owlib"] = owlib_types(set(index["objects"]) | set(index["class_nodes"]))
    for key in (*lists, "fields", "constant_writes"):
        index[key] = dict(index[key])
    return index


def add_refs(index: dict, gi: int, rid: int | None, refs: Refs) -> None:
    for kind, var, scope, path in refs.vars:
        index["vars"][var].append((gi, rid, kind, scope, path))
    for child, path in refs.graphs:
        if child != gi:
            index["edges"].append((gi, rid, child, path))
    for ident, holder, field, path in refs.ids:
        index["ids"][ident].append((gi, rid, holder, field, path))
    for ident, what, cfg, path in refs.entries:
        index["entries"][ident].append((what, gi, rid, path, render(cfg), value_type(cfg)))
    for ident, scope, path in refs.syncs:
        index["syncs"][ident].append((gi, rid, scope, path))
    for guid, path in refs.assets:
        index["assets"][guid].append((gi, rid, path))
    seen = set()
    for number, (cls, obj) in enumerate(refs.objects):
        index["objects"][cls] += 1
        if (number or rid is None) and cls not in seen:  # a node's own class is not a value of it
            seen.add(cls)
            index["class_values"][cls].append((gi, rid))
        stats = index["fields"][cls]
        for key, item in obj.items():
            if key != "$":
                entry = stats.setdefault(key, [0, 0, collections.Counter()])
                entry[0] += 1
                if item is not None:
                    entry[1] += 1
                    entry[2][label(item)] += 1
        written = expr.dynamic(obj.get("m_out_Var"))
        if (
            written is not None
            and isinstance(obj.get("m_value"), dict)
            and obj["m_value"].get("$") in CONSTANT_TYPES
        ):
            index["constant_writes"][written[1]][(gi, written[0], CONSTANT_TYPES[obj["m_value"]["$"]])] += 1


def attachments(index: dict) -> dict:
    """graph -> [(role, owner, slot)]: hero bodies (initial graphs, weapon component), game modes (script,
    team controllers and body scripts) and the named UI graphs. Body overrides go to index["entries"]."""
    attach = collections.defaultdict(list)
    for body, record in graphs.bodies().items():
        hero = record.get("name") or guid_text(body)
        for slot, entry in enumerate(record["graphs"]):
            if entry.get("graph") is None:
                continue
            attach[entry["graph"]].append(("body", hero, f"init#{slot + 1}"))
            for item in entry.get("m_1EB5A024") or []:
                ident = graphs.guid(item.get("m_0D09D2D9")) if isinstance(item, dict) else None
                if ident is not None:
                    cfg = item.get("m_value")
                    where = f"{hero} init#{slot + 1}"
                    index["entries"][ident & 0xFFFF].append(
                        ("override", entry["graph"], None, where, render(cfg), value_type(cfg))
                    )
        if record.get("manager") is not None:
            attach[record["manager"]].append(("weapon", hero, "manager"))
        for slot, weapon in enumerate(record.get("weapons") or []):
            if weapon is not None:
                attach[weapon].append(("weapon", hero, f"weapon[{slot}]"))
    for guid, mode in names()["modes"].items():
        owner = mode.get("name") or guid_text(guid)
        for field in ("script", "m_F88BA3B9"):
            if mode.get(field):
                attach[int(mode[field], 16)].append(("mode", owner, field))
        for team in mode["teams"]:
            if team.get("controller"):
                attach[int(team["controller"], 16)].append(
                    ("controller", owner, f"{team['team']} controller")
                )
            if team.get("body"):
                attach[int(team["body"], 16)].append(("mode", owner, f"{team['team']} body script"))
    for gi, entry in names()["graphs"].items():
        attach[gi].append((entry["role"], entry["name"], ""))
    for gi, summary in index["graphs"].items():
        if summary["abilities"] and gi in attach:
            attach[gi] = [
                ("ability" if role == "body" else role, owner, slot) for role, owner, slot in attach[gi]
            ]
    return dict(attach)


def roles(index: dict) -> dict:
    """Each graph's roles: its own attachments, else the roles of the graphs that name it."""
    parents = collections.defaultdict(set)
    for parent, _rid, child, _path in index["edges"]:
        parents[child].add(parent)
    found = {gi: {role for role, _owner, _slot in entries} for gi, entries in index["attach"].items()}
    every = set(index["graphs"]) | set(parents)
    changed = True
    while changed:
        changed = False
        for gi in every - set(index["attach"]):
            new = set().union(*(found.get(parent, set()) for parent in parents[gi]))
            if new != found.get(gi, set()):
                found[gi] = new
                changed = True
    return {gi: [role for role in ROLES if role in value] for gi, value in found.items()}


STU_CLASS = re.compile(
    r"\[STU\((0x[0-9A-F]+)(?:,\s*\d+)?\)\]\s*public class (\w+)(?:\s*:\s*(\w+))?\s*\{(.*?)\n    \}", re.S
)
STU_FIELD = re.compile(
    r"\[STUField\(0x[0-9A-F]+(?:,\s*(\d+))?[^\]]*\)\][^\n]*\n\s*public ([\w<>\[\]]+) (m_\w+)"
)


def owlib_types(wanted: set) -> dict:
    """class -> (hash, parent, [(field, type, offset)]) from OWLib's STU types for the classes of the data
    and their parents; {} without OWLib."""
    if not OWLIB_TYPES or not OWLIB_TYPES.is_dir():
        return {}
    every = {}
    for path in sorted(OWLIB_TYPES.rglob("*.cs")):
        for hash_text, name, parent, body in STU_CLASS.findall(path.read_text(encoding="utf-8")):
            fields = [
                (field, kind, int(offset) if offset else None)
                for offset, kind, field in STU_FIELD.findall(body)
            ]
            every[name] = (hash_text[2:].upper().zfill(8), parent or None, fields)
    out = {}
    for cls in wanted:
        while cls in every and cls not in out:
            out[cls] = every[cls]
            cls = every[cls][1]
    return out


# --- shared views -------------------------------------------------------------------------------------


def parents_of(index: dict) -> dict:
    found = collections.defaultdict(list)
    for parent, rid, child, path in index["edges"]:
        found[child].append((parent, rid, path))
    return found


def children_of(index: dict) -> dict:
    found = collections.defaultdict(list)
    for parent, rid, child, path in index["edges"]:
        found[parent].append((child, rid, path))
    return found


def node_index(index: dict, gi: int, rid: int) -> str:
    """rid12 st7 for a node of a graph."""
    info = index["graphs"][gi]["nodes_info"].get(rid)
    return f"rid{rid}" + (f" st{info[0]}" if info is not None and info[0] is not None else "")


def node_label(index: dict, gi: int, rid: int | None) -> str:
    """rid12 st7 Alias [client-only] for a node of a graph, "graph field" for rid None."""
    if rid is None:
        return "graph field"
    info = index["graphs"][gi]["nodes_info"].get(rid)
    if info is None:
        return f"rid{rid}"
    _state, cls, client_only, server_only = info
    flags = [flag for flag, on in (("client", client_only), ("server", server_only)) if on]
    return f"{node_index(index, gi, rid)} {alias(cls)}" + (f" [{'+'.join(flags)}-only]" if flags else "")


def roles_text(index: dict, gi: int) -> str:
    return "+".join(index["roles"].get(gi, [])) or "-"


def attach_text(entries: list) -> str:
    """Where a graph is attached, short: slots shared by many heroes or modes are counted."""
    by_slot = collections.OrderedDict()
    for role, owner, slot in entries:
        by_slot.setdefault((role, slot), []).append(owner)
    parts = []
    for (role, slot), owners in by_slot.items():
        owners = list(dict.fromkeys(owners))
        if not slot:
            parts.append(", ".join(owners))
        elif len(owners) <= 2:
            parts.append(f"{', '.join(owners)} {slot}")
        else:
            parts.append(
                f"{slot} of {len(owners)} {'heroes' if role in ('body', 'ability', 'weapon') else 'modes'}"
            )
    return "; ".join(parts)


def where_text(index: dict, gi: int, parents: dict) -> str:
    if gi in index["attach"]:
        return attach_text(index["attach"][gi])
    found = list(dict.fromkeys((parent, rid) for parent, rid, _path in parents.get(gi, [])))
    if not found:
        return "-"
    text = ", ".join(f"{parent:04X} {node_label(index, parent, rid)}" for parent, rid in found[:3])
    return "from " + text + (f" (+{len(found) - 3} more)" if len(found) > 3 else "")


def closure(index: dict, roots: list[int]) -> list[tuple]:
    """(graph, parent, rid) for the roots and every graph they name, breadth first."""
    children = children_of(index)
    found = set()
    order = []
    queue = collections.deque((root, None, None) for root in roots)
    while queue:
        gi, parent, rid = queue.popleft()
        if gi in found:
            continue
        found.add(gi)
        order.append((gi, parent, rid))
        queue.extend(
            (child, gi, child_rid) for child, child_rid, _path in children.get(gi, []) if child not in found
        )
    return order


def graph_rows(index: dict, order: list[tuple], how: dict) -> list[str]:
    rows = [f"{'GRAPH':6} {'ROLE':16} {'NODES':>5} {'STATES':>6}  HOW"]
    for gi, parent, rid in order:
        text = how.get(gi) or (
            f"from {parent:04X} {node_label(index, parent, rid)}" if parent is not None else "-"
        )
        summary = index["graphs"].get(gi)
        size = f"{summary['nodes']:5} {summary['states']:6}" if summary else f"{'-':>5} {'-':>6}"
        rows.append(
            f"{gi:04X}   {roles_text(index, gi):16} {size}  {text}"
            + ("" if summary else " (not in the data)")
        )
    return rows


HEX = re.compile(r"(?:0x)?([0-9A-Fa-f]+)(?:\.[0-9A-Fa-f]{3})?$")


def parse_hex(text: str) -> int | None:
    """0255, 0x255, 0x0580000000000255 or 0255.01B as a number."""
    match = HEX.match(text.strip())
    return int(match.group(1), 16) if match else None


def type_bits(kind: int) -> int:
    """The 12 type bits of an asset type (0x01B -> 0x580): the type minus one, reversed."""
    return int(f"{kind - 1:012b}"[::-1], 2)


def parse_guid(text: str) -> int | None:
    """A full GUID from 0x0300000000000186 or 0186.00D; None for a bare index."""
    match = re.fullmatch(r"(?:0x)?([0-9A-Fa-f]+)\.([0-9A-Fa-f]{3})", text.strip())
    if match:
        return (type_bits(int(match.group(2), 16)) << 48) | int(match.group(1), 16)
    number = parse_hex(text)
    return number if number is not None and number >> 48 else None


def parse_graph(text: str) -> int | None:
    number = parse_hex(text)
    if number is None:
        return None
    return number & 0xFFFFFFFFFFFF if number >> 48 == graphs.GRAPH_TYPE else number


def parse_var(text: str) -> int | None:
    """699, #699, #7644e, v699, 0x2BB, 02BB.01C, 0x0D800000000002BB or a known name."""
    raw = text.strip()
    match = re.fullmatch(r"[#v]?(\d+)e?", raw, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if raw.lower().startswith("0x") or "." in raw:
        number = parse_hex(raw)
        if number is not None:
            return number & 0xFFFF if number >> 48 == IDENT_TYPE else number
    return next((var for var, entry in names()["vars"].items() if entry["name"].lower() == raw.lower()), None)


def find_body(text: str) -> tuple[int, dict] | None:
    """A hero body by hero name, hero GUID or body GUID / index."""
    bodies = graphs.bodies()
    hero = content.find_hero(text)
    if hero is not None and hero.body in bodies:
        return hero.body, bodies[hero.body]
    number = parse_hex(text)
    if number is None:
        return None
    for body, record in bodies.items():
        hero_guid = int(record["hero"], 16) if record.get("hero") else None
        if number in (body, hero_guid) or (number >> 48 == 0 and number == body & 0xFFFFFFFFFFFF):
            return body, record
    return None


def body_roots(record: dict) -> tuple[list[int], dict]:
    roots, how = [], {}
    for slot, entry in enumerate(record["graphs"]):
        if entry.get("graph") is not None:
            roots.append(entry["graph"])
            how.setdefault(entry["graph"], f"init#{slot + 1}")
    if record.get("manager") is not None:
        roots.append(record["manager"])
        how.setdefault(record["manager"], "manager")
    for slot, weapon in enumerate(record.get("weapons") or []):
        if weapon is not None:
            roots.append(weapon)
            how.setdefault(weapon, f"weapon[{slot}]")
    return roots, how


def find_mode(text: str) -> tuple[int, dict] | None:
    """A game mode by name (whole or the start) or GUID / index; LookupError when a name fits several."""
    modes = names()["modes"]
    wanted = text.strip().lower()
    found = [(guid, mode) for guid, mode in modes.items() if (mode.get("name") or "").lower() == wanted]
    if not found:
        found = [
            (guid, mode)
            for guid, mode in modes.items()
            if (mode.get("name") or "").lower().startswith(wanted)
        ]
    if len(found) > 1:
        raise LookupError(
            f"{text!r} is several modes: " + ", ".join(guid_text(guid) for guid, _mode in found)
        )
    if found:
        return found[0]
    number = parse_hex(text)
    if number is not None:
        guid = number if number >> 48 else (MODE_TYPE << 48) | number
        if guid in modes:
            return guid, modes[guid]
    return None


# --- graphs -------------------------------------------------------------------------------------------


def cmd_graphs(index: dict, args) -> list[str]:
    if not (args.hero or args.mode or args.entity):
        parents = parents_of(index)
        missing = " ".join(f"{gi:04X}" for gi in graphs.missing()) or "-"
        out = [f"{len(index['graphs'])} graphs in the data; named by them but not in the data: {missing}"]
        out.append(f"{'GRAPH':6} {'ROLE':16} {'NODES':>5} {'STATES':>6}  WHERE")
        for gi, summary in sorted(index["graphs"].items()):
            where = where_text(index, gi, parents)
            out.append(
                f"{gi:04X}   {roles_text(index, gi):16} {summary['nodes']:5} {summary['states']:6}  {where}"
            )
        return out
    out = []
    for text in (args.hero, args.entity):
        if not text:
            continue
        found = find_body(text)
        if found is None:
            out.append(f"no hero body for {text!r} (the data has the 32 hero bodies)")
            continue
        body, record = found
        roots, how = body_roots(record)
        order = closure(index, roots)
        missing = [gi for gi, _parent, _rid in order if gi not in index["graphs"]]
        missing_text = f" ({len(missing)} not in the data: {' '.join(f'{gi:04X}' for gi in missing)})"
        out.append(
            f"{record['name']}: body {guid_text(body)}, {len(order)} graphs"
            + (missing_text if missing else "")
        )
        out += [*graph_rows(index, order, how), ""]
    if args.mode:
        try:
            found = find_mode(args.mode)
        except LookupError as error:
            return [*out, str(error)]
        if found is None:
            known = ", ".join(mode_name(guid) for guid in sorted(names()["modes"]))
            return [*out, f"no game mode {args.mode!r}; modes: {known}"]
        guid, mode = found
        roots, how = [], collections.defaultdict(list)
        for field in ("script", "m_F88BA3B9"):
            if mode.get(field):
                roots.append(int(mode[field], 16))
                how[roots[-1]].append(field)
        for team in mode["teams"]:
            for field, slot in (("controller", "controller"), ("body", "body script")):
                if team.get(field):
                    roots.append(int(team[field], 16))
                    how[roots[-1]].append(f"{team['team']} {slot}")
        order = closure(index, roots)
        missing = sum(gi not in index["graphs"] for gi, _parent, _rid in order)
        out.append(f"{mode_name(guid)} ({guid_text(guid)}): {len(order)} graphs"
                   + (f", {missing} not in the data" if missing else ""))  # fmt: skip
        out += graph_rows(index, order, {gi: ", ".join(slots) for gi, slots in how.items()})
    return out


# --- show ---------------------------------------------------------------------------------------------


class Shower:
    def __init__(self, index: dict, graph: graphs.Graph, depth: int | None) -> None:
        self.index = index
        self.graph = graph
        self.depth = depth
        self.walker = Walker(names()["var_fields"])
        self.done: set[int] = set()
        self.out: list[str] = []

    def label(self, rid: int) -> str:
        return node_label(self.index, self.graph.index, rid)

    def head(self) -> None:
        index, gi, out = self.index, self.graph.index, self.out
        summary = index["graphs"][gi]
        parents = parents_of(index)
        out.append(f"{gi:04X}.01B  {roles_text(index, gi)}: {where_text(index, gi, parents)}")
        line = (
            f"{summary['nodes']} nodes ({summary['networked']} networked, {summary['client_only']} "
            f"client-only, {summary['server_only']} server-only), {summary['states']} states, "
            f"{summary['entries']} entries, {summary['remote']} remote sync nodes"
        )
        out.append(
            line + (f", {summary['unknown']} nodes DataTool could not read" if summary["unknown"] else "")
        )
        presence = ", ".join(var_name(var) for var in summary["presence"]) or "-"
        out.append(
            f"frames: {summary['owner_states']} owner states, {summary['remote_states']} remote states; "
            f"{summary['sync_vars']} sync vars, {len(summary['presence'])} with a presence bit: {presence}"
        )
        nodes_bits, states_bits, remote_bits, vars_bits = summary["bits"]
        out.append(
            f"bits: nodes {nodes_bits}, states {states_bits}, remote {remote_bits}, sync vars {vars_bits}; "
            f"m_predictionBehavior {summary['prediction']}"
        )
        named_by = sorted(
            {(parent, -1 if rid is None else rid) for parent, rid, _path in parents.get(gi, [])}
        )
        text = ", ".join(
            f"{parent:04X} {node_label(index, parent, None if rid < 0 else rid)}" for parent, rid in named_by
        )
        out.append(f"named by: {text or '-'}")
        children = sorted({child for child, _rid, _path in children_of(index).get(gi, [])})
        out.append("names: " + (" ".join(f"{child:04X}" for child in children) or "-"))
        counts = sorted(summary["classes"].items(), key=lambda item: (-item[1], alias(item[0])))
        out.append("classes: " + ", ".join(f"{alias(cls)} x{count}" for cls, count in counts))
        if summary["abilities"]:
            out.append("abilities: " + "; ".join(ability_text(*ability) for ability in summary["abilities"]))

    def node(self, node: graphs.Node, level: int, how: str = "") -> None:
        pad = "  " * level
        text = f"{pad}{how}{self.label(node.rid)}" + (f" (remote {node.remote})" if node.remote >= 0 else "")
        if node.rid in self.done:
            self.out.append(text + " (above)")
            return
        self.done.add(node.rid)
        self.out.append(text)
        refs = self.walker.run(node.fields, node.cls)
        fields = []
        for key, value in node.fields.items():
            if value is None or graphs.is_plug(value) or key in SYNC_LISTS or key in BOOKKEEPING:
                continue
            plugs = isinstance(value, list) and value and all(map(graphs.is_plug, value))
            if not plugs and not (key in QUIET_ZERO and not value):
                fields.append(f"{key}={cut(render(value))}")
        if fields:
            self.out.append(f"{pad}    " + " | ".join(fields))
        uses = collections.defaultdict(list)
        for kind, var, scope, _path in refs.vars:
            if var_text(var, scope) not in uses[kind]:
                uses[kind].append(var_text(var, scope))
        if uses:
            self.out.append(
                f"{pad}    vars: "
                + "; ".join(f"{kind} {' '.join(found)}" for kind, found in sorted(uses.items()))
            )
        syncs = sorted({var_text(ident, scope) for ident, scope, _path in refs.syncs})
        if syncs:
            self.out.append(f"{pad}    remote sync vars: {' '.join(syncs)}")
        for child, path in refs.graphs:
            if child != self.graph.index:
                missing = "" if child in self.index["graphs"] else " (not in the data)"
                self.out.append(f"{pad}    graph {child:04X}{missing} at {path}")
        for path, plug in node.plugs().items():
            for target, plug_path in plug["links"]:
                end = self.graph.node(target) if target is not None else None
                into = "" if plug_path in DEFAULT_PLUGS else f"({plug_path}) "
                if end is None:
                    self.out.append(f"{pad}  {path} -> {into}a node DataTool could not read")
                elif self.depth is not None and level >= self.depth:
                    self.out.append(f"{pad}  {path} -> {into}{self.label(end.rid)} ...")
                else:
                    self.node(end, level + 1, f"{path} -> {into}")


def ability_text(rid: int, st: int, buttons: list) -> str:
    return f"rid{rid} st{st} " + (" ".join(button_text(value) for value in buttons) or "no button")


def cmd_show(index: dict, args) -> list[str]:
    gi = parse_graph(args.graph)
    graph = graphs.graph(gi) if gi is not None else None
    if graph is None:
        return [f"graph {args.graph!r} is not in the data"]
    shower = Shower(index, graph, args.depth)
    shower.head()
    if args.state is not None:
        node = graph.state(args.state)
        if node is None:
            return [*shower.out, f"{gi:04X} has no state {args.state} (m_states has {len(graph.states)})"]
        shower.out.append("")
        shower.node(node, 0)
        return shower.out
    for node in graph.entries:
        if node is not None:
            shower.out.append("")
            shower.node(node, 0, "entry ")
    rest = [node for node in graph.nodes if node.rid not in shower.done]
    if rest:
        shower.out += ["", f"not reached from an entry ({len(rest)}):"]
        for node in rest:
            if node.rid not in shower.done:
                shower.node(node, 0)
    return shower.out


# --- var ----------------------------------------------------------------------------------------------


def use_line(index: dict, gi: int, rid: int | None, path: str) -> str:
    """One use of a variable: the node, the field path, and the value written (an m_out_Var's m_value) or
    the expression or object it sits in."""
    node = graphs.graph(gi).by_rid(rid) if rid is not None else None
    shown = ""
    if node is not None:
        written = (
            graphs.field_at(node.fields, path[: -len("m_out_Var")] + "m_value")
            if path.endswith("m_out_Var")
            else None
        )
        if written is not None:
            shown = f" = {cut(render(written), 100)}"
        elif ".m_expression." in path:
            shown = f": {cut(render(graphs.field_at(node.fields, path.split('.m_expression.')[0])), 100)}"
        elif "." in path:
            holder = graphs.field_at(node.fields, path.rsplit(".", 1)[0])
            shown = f": {cut(render(holder), 100)}" if holder is not None else ""
    return f"  {gi:04X} {node_label(index, gi, rid)}  {path}{shown}"


def cmd_var(index: dict, args) -> list[str]:
    var = parse_var(args.var)
    if var is None:
        return [f"{args.var!r} is not a variable id or a known variable name"]
    only = parse_graph(args.graph) if args.graph else None
    entity_only = bool(re.fullmatch(r"[#v]?\d+e", args.var.strip(), re.IGNORECASE))

    def keep(gi: int, scope: int | None = None) -> bool:
        return (only is None or gi == only) and (not entity_only or scope in (None, graphs.ENTITY))

    uses = [use for use in index["vars"].get(var, []) if keep(use[0], use[3])]
    entry = names()["vars"].get(var)
    head = f"{var_text(var)}  identifier {guid_text((IDENT_TYPE << 48) | var)}"
    head += f"  name {entry['name']} ({entry['from']})" if entry else "  no name known"
    head += (f"  (only {only:04X})" if only is not None else "") + (
        "  (entity scope only)" if entity_only else ""
    )
    out = [head]
    if uses:
        scopes = collections.Counter(scope for _gi, _rid, _kind, scope, _path in uses)
        counts = ", ".join(f"{scope_text(scope)} x{count}" for scope, count in sorted(scopes.items()))
        out.append(f"scope: {counts} (an instance variable is one per instance: the same id in two graphs is "
                   "two variables)")  # fmt: skip
    defaults = [item for item in index["entries"].get(var, []) if keep(item[1])]
    types = collections.Counter(item[5] for item in defaults if item[5])
    for (gi, scope, kind), count in index["constant_writes"].get(var, {}).items():
        if keep(gi, scope):
            types[kind] += count
    found_types = ", ".join(f"{kind} x{count}" for kind, count in types.most_common()) or "unknown"
    out.append(f"type: {found_types} (from defaults and constant writes)")
    out.append(f"defaults and set values ({len(defaults)}):" if defaults else "defaults: none")
    for what, gi, rid, where, text, kind in defaults:
        place = where if what == "override" else f"{node_label(index, gi, rid)} {where}"
        out.append(f"  {what:8} {gi:04X} {place}: {text}" + (f" ({kind})" if kind else ""))
    by_kind = collections.defaultdict(list)
    for gi, rid, kind, scope, path in uses:
        by_kind[kind].append((gi, rid, scope, path))
    for kind in ("write", "read", "parent", "id", "param", "list", "unused"):
        found = sorted(
            by_kind.get(kind, []), key=lambda item: (item[0], -1 if item[1] is None else item[1], item[3])
        )
        if found:
            out.append(f"{kind} ({len(found)} in {plural(len({item[0] for item in found}), 'graph')}):")
            for gi, rid, scope, path in found:
                out.append(use_line(index, gi, rid, path) + (" [entity]" if scope == graphs.ENTITY else ""))
    sync = [item for item in index["sync_vars"].get(var, []) if keep(item[0], item[2])]
    out.append(f"graph sync vars (m_syncVars, {len(sync)}):" if sync else "graph sync vars: none")
    for gi, number, scope, flag, bit in sync:
        where = f"presence bit {bit}" if bit is not None else "no presence bit"
        out.append(f"  {gi:04X} m_syncVars[{number}]: {scope_text(scope)}, {where}, m_AC9480C7={flag}")
    remote = [item for item in index["syncs"].get(var, []) if keep(item[0], item[2])]
    out.append(f"node remote sync vars ({len(remote)}):" if remote else "node remote sync vars: none")
    for gi, rid, scope, path in remote:
        out.append(f"  {gi:04X} {node_label(index, gi, rid)}  {path} ({scope_text(scope)})")
    ids = [item for item in index["ids"].get(var, []) if keep(item[0])]
    if ids:
        kinds = collections.Counter(f"{alias(holder)}.{field}" for _gi, _rid, holder, field, _path in ids)
        out.append(f"the identifier as a value ({len(ids)}): "
                   + ", ".join(f"{name} x{count}" for name, count in kinds.most_common()))  # fmt: skip
        out += [f"  {gi:04X} {node_label(index, gi, rid)}  {path}" for gi, rid, _holder, _field, path in ids]
    if not (uses or defaults or sync or remote or ids):
        out.append("not used in the data")
    return out


# --- class --------------------------------------------------------------------------------------------


def find_class(index: dict, text: str) -> str | None:
    known = sorted(set(index["objects"]) | set(index["class_nodes"]))
    raw = text.strip()
    if raw in known:
        return raw
    for cls in known:
        client = names()["classes"].get(cls, {}).get("client") or ""
        if raw.lower() in (alias(cls).lower(), client.lower()):
            return cls
    hex_text = raw.upper().removeprefix("STU_").removeprefix("0X")
    if re.fullmatch(r"[0-9A-F]{1,8}", hex_text):
        hex_text = hex_text.zfill(8)
        for cls in known:
            if (class_hash(cls) or index["owlib"].get(cls, ("",))[0]) == hex_text:
                return cls
    return None


def codec_entry(cls: str):
    """The class's entry in data/statescript_codecs_174.json ("classes" by hash, or searched by class name
    or hash in any other layout); None without the file or the entry."""
    if not CODECS_PATH.is_file():
        return None
    data = json.loads(CODECS_PATH.read_text(encoding="utf-8"))
    hashed = class_hash(cls)
    table = data.get("classes") if isinstance(data, dict) else None
    if isinstance(table, dict):
        for key in (hashed, cls, f"STU_{hashed}"):
            if key and key in table:
                return table[key]
        return next(
            (item for item in table.values() if isinstance(item, dict) and item.get("name") == cls), None
        )
    keys = {key.upper() for key in (cls, hashed, f"STU_{hashed}") if key}

    def search(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.upper() in keys:
                    return item
            if any(str(value.get(field, "")).upper() in keys for field in ("hash", "cls", "class", "name")):
                return value
            value = list(value.values())
        if isinstance(value, list):
            for item in value:
                found = search(item) if isinstance(item, (dict, list)) else None
                if found is not None:
                    return found
        return None

    return search(data)


def vm_text(cls: str, is_state: bool) -> str:
    """How the server's VM runs a node class (the registries of ow174/game/script/runtime.py)."""
    if cls in runtime.STATE_CLASSES:
        state = runtime.STATE_CLASSES[cls]
        return f"state class {state.__name__} ({state.__module__})"
    if cls in runtime.HANDLERS:
        function = runtime.HANDLERS[cls]
        return f"handler {function.__name__} ({function.__module__})"
    return "a plain State (no model of its own)" if is_state else "no model: it only follows m_outPlug"


def cmd_class(index: dict, args) -> list[str]:
    cls = find_class(index, args.name)
    if cls is None:
        return [f"no class {args.name!r} in the data (try: py tools/ssq.py find {args.name})"]
    info = names()["classes"].get(cls, {})
    owlib = index["owlib"].get(cls)
    hashed = class_hash(cls) or (owlib[0] if owlib else None)
    head = f"{cls}  alias {alias(cls)}" + (f" (from {info['from']})" if info.get("from") else "")
    head += (f"  hash {hashed}" if hashed else "") + (
        f"  client name {info['client']}" if info.get("client") else ""
    )
    out = [head]
    if owlib:
        chain, parent = [], owlib[1]
        while parent:
            chain.append(parent)
            parent = index["owlib"].get(parent, (None, None))[1]
        out.append("parents (OWLib): " + (" > ".join(chain) or "-"))
    node_uses = index["class_nodes"].get(cls, [])
    if node_uses:
        gi, rid = node_uses[0]
        out.append(f"VM: {vm_text(cls, index['graphs'][gi]['nodes_info'][rid][0] is not None)}")
    if node_uses:
        by_graph = collections.OrderedDict()
        for gi, rid in node_uses:
            by_graph.setdefault(gi, []).append(rid)
        net = sum(not any(index["graphs"][gi]["nodes_info"][rid][2:]) for gi, rid in node_uses)
        out.append(f"nodes: {len(node_uses)} in {plural(len(by_graph), 'graph')} ({net} networked)")
        for gi, rids in by_graph.items():
            out.append(
                f"  {gi:04X} ({roles_text(index, gi)}): "
                + ", ".join(node_index(index, gi, rid) for rid in rids)
            )
    value_uses = index["class_values"].get(cls, [])
    if value_uses:
        by_graph = collections.OrderedDict()
        for gi, rid in value_uses:
            by_graph.setdefault(gi, []).append(rid)
        fields = sum(rid is None for _gi, rid in value_uses)
        places = [plural(len(value_uses) - fields, "node")] if len(value_uses) > fields else []
        places += [plural(fields, "graph field")] if fields else []
        out.append(f"as a value: in {' and '.join(places)} of {plural(len(by_graph), 'graph')}")
        for gi, rids in by_graph.items():
            out.append(f"  {gi:04X}: " + ", ".join(node_label(index, gi, rid) for rid in rids))
    declared = {field: (kind, offset) for field, kind, offset in owlib[2]} if owlib else {}
    var_fields = names()["var_fields"].get(cls, {})
    base = BASE_FIELDS if node_uses else set()
    note = ("; declared type and offset from OWLib" if owlib else "") + (
        "; the statescript base classes' fields are left out" if node_uses else ""
    )
    out.append(f"fields ({index['objects'].get(cls, 0)} objects in the data{note}):")
    shown = {*base, "m_EE729DCB", "links"}
    for field, (count, filled, kinds) in index["fields"].get(cls, {}).items():
        if field in shown:
            continue
        shown.add(field)
        kind, offset = declared.get(field, ("", None))
        place = f"+{offset} {kind}" if kind else ""
        values = ", ".join(f"{name} x{number}" for name, number in kinds.most_common(4))
        extra = {"var": "  (a variable: written)", "list": "  (a variable list)"}.get(
            var_fields.get(field), ""
        )
        out.append(
            f"  {field:24} {place:28} set in {filled}/{count}" + (f": {values}" if values else "") + extra
        )
    for field, (kind, offset) in declared.items():
        if field not in shown:
            out.append(f"  {field:24} {f'+{offset} {kind}':28} not in the data")
    entry = codec_entry(cls)
    if entry is not None:
        out.append("codec (data/statescript_codecs_174.json):")
        for key, value in entry.items() if isinstance(entry, dict) else [("entry", entry)]:
            out.append(
                f"  {key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
            )
    elif CODECS_PATH.is_file():
        out.append("codec: no entry in data/statescript_codecs_174.json")
    return out


# --- find ---------------------------------------------------------------------------------------------


def cmd_find(index: dict, args) -> list[str]:
    text = " ".join(args.text).strip()
    wanted = text.lower()
    if not wanted:
        return ["nothing to find"]
    number = parse_hex(text)
    guid = parse_guid(text)
    hits = collections.defaultdict(list)
    for cls in sorted(set(index["objects"]) | set(index["class_nodes"])):
        info = names()["classes"].get(cls, {})
        words = [
            cls,
            alias(cls),
            info.get("client") or "",
            class_hash(cls) or index["owlib"].get(cls, ("",))[0],
        ]
        if any(wanted in word.lower() for word in words if word):
            count = len(index["class_nodes"].get(cls, []))
            what = plural(count, "node") if count else plural(index["objects"].get(cls, 0), "object")
            hits["class"].append(f"{cls}  {alias(cls)}  {what}")
    for cls, stats in sorted(index["fields"].items()):
        for field, (count, filled, _kinds) in stats.items():
            if wanted in field.lower():
                hits["field"].append(f"{field} in {alias(cls)} ({cls}): set in {filled}/{count}")
    for gi in sorted(set(index["graphs"]) | set(graphs.missing())):
        where = attach_text(index["attach"][gi]) if gi in index["attach"] else ""
        if (number is not None and parse_graph(text) == gi) or (where and wanted in where.lower()):
            missing = "" if gi in index["graphs"] else " (not in the data)"
            hits["graph"].append(f"{gi:04X}  {roles_text(index, gi)}  {where or '-'}{missing}")
    for hero in content.heroes().values():
        if wanted in hero.name.lower() or guid in (hero.guid, hero.body):
            hits["hero"].append(
                f"{hero.name}  hero {guid_text(hero.guid)}  body {guid_text(hero.body)}  {hero.role}"
            )
    for mode_guid, mode in sorted(names()["modes"].items()):
        if wanted in (mode.get("name") or "").lower() or guid == mode_guid:
            teams = " ".join(
                f"{team['team']} {team['controller'] or '-'}/{team['body'] or '-'}" for team in mode["teams"]
            )
            name, script = mode_name(mode_guid), mode["script"] or "-"
            hits["mode"].append(
                f"{name}  {guid_text(mode_guid)}  script {script}  controller/body script: {teams}"
            )
    for map_guid, entry in sorted(content._map_entries().items()):
        if wanted in (entry.get("name") or "").lower() or guid == map_guid:
            modes = ", ".join(mode_name(int(mode, 16)) for mode in entry.get("modes", []))
            hits["map"].append(f"{entry.get('name')}  {guid_text(map_guid)}  modes: {modes or '-'}")
    for var, entry in sorted(names()["vars"].items()):
        if wanted in entry["name"].lower():
            hits["var"].append(f"{var_name(var)}  ({entry['from']})")
    var = parse_var(text)
    if var is not None and var in index["vars"] and var not in names()["vars"]:
        uses = index["vars"][var]
        hits["var"].append(
            f"{var_name(var)}  {len(uses)} uses in {plural(len({use[0] for use in uses}), 'graph')}"
        )
    for value, name in sorted(names()["buttons"].items()):
        if wanted in name.lower():
            hits["button"].append(f"{value} {name}")
    found = index["assets"].get(guid, []) if guid is not None else []
    if found:
        places = ", ".join(f"{gi:04X} {node_label(index, gi, rid)} {path}" for gi, rid, path in found[:10])
        more = " ..." if len(found) > 10 else ""
        hits["asset"].append(f"{guid_text(guid)} in {plural(len(found), 'place')}: {places}{more}")
    out = []
    for kind in ("class", "field", "graph", "hero", "mode", "map", "var", "button", "asset"):
        lines = hits.get(kind)
        if lines:
            out.append(f"{kind} ({len(lines)}):")
            out += [f"  {line}" for line in lines[: args.limit]]
            if len(lines) > args.limit:
                out.append(f"  ... {len(lines) - args.limit} more (--limit)")
    return out or [f"nothing matches {text!r}"]


# --- hero ---------------------------------------------------------------------------------------------


def cmd_hero(index: dict, args) -> list[str]:
    found = find_body(args.name)
    if found is None:
        return [f"no hero {args.name!r}"]
    body, record = found
    hero = content.heroes().get(int(record["hero"], 16)) if record.get("hero") else None
    roots, how = body_roots(record)
    order = closure(index, roots)
    present = [gi for gi, _parent, _rid in order if gi in index["graphs"]]
    missing = [gi for gi, _parent, _rid in order if gi not in index["graphs"]]
    classes = collections.Counter()
    for gi in present:
        classes.update(index["graphs"][gi]["classes"])
    out = [f"{record['name']}  hero {guid_text(hero.guid) if hero else '-'}  {hero.role if hero else ''}  "
           f"body {guid_text(body)}"]  # fmt: skip
    initial = " ".join(
        f"{entry['graph']:04X}" if entry.get("graph") is not None else "-" for entry in record["graphs"]
    )
    weapons = " ".join(
        f"{slot}:{weapon:04X}" if weapon is not None else f"{slot}:-"
        for slot, weapon in enumerate(record.get("weapons") or [])
    )
    manager = f"{record['manager']:04X}" if record.get("manager") is not None else "-"
    out.append(f"initial graphs (instance 1..N): {initial}; manager {manager}; weapons {weapons or '-'}")
    node_count = sum(index["graphs"][gi]["nodes"] for gi in present)
    networked = sum(index["graphs"][gi]["networked"] for gi in present)
    missing_text = (
        f" ({len(missing)} not in the data: {' '.join(f'{gi:04X}' for gi in missing)})" if missing else ""
    )
    out.append(f"{len(order)} graphs{missing_text}, {node_count} nodes ({networked} networked), "
               f"{len(classes)} node classes")  # fmt: skip
    for slot, entry in enumerate(record["graphs"]):
        for item in entry.get("m_1EB5A024") or []:
            ident = graphs.guid(item.get("m_0D09D2D9")) if isinstance(item, dict) else None
            if ident is not None:
                value = render(item.get("m_value"))
                out.append(
                    f"  init#{slot + 1} {entry['graph']:04X} override {var_name(ident & 0xFFFF)} = {value}"
                )
    out += ["", *graph_rows(index, order, how)]
    for gi in present:
        out += [
            f"  ability {gi:04X} {ability_text(*ability)}" for ability in index["graphs"][gi]["abilities"]
        ]
    counts = sorted(classes.items(), key=lambda item: (-item[1], alias(item[0])))
    out += ["", "classes: " + ", ".join(f"{alias(cls)} x{count}" for cls, count in counts), ""]
    graph_set = set(present)
    shared = collections.defaultdict(lambda: collections.defaultdict(set))
    for var, uses in index["vars"].items():
        for gi, _rid, kind, scope, _path in uses:
            if gi in graph_set and scope == graphs.ENTITY:
                shared[var][kind].add(gi)
    rows = []
    for var, kinds in sorted(shared.items()):
        if len(set().union(*kinds.values())) >= 2:
            parts = [
                f"{kind} {' '.join(f'{gi:04X}' for gi in sorted(found))}"
                for kind, found in sorted(kinds.items())
            ]
            rows.append(f"  {var_name(var, graphs.ENTITY)}: " + "; ".join(parts))
    out.append(f"entity vars used by 2 or more of these graphs ({len(rows)}):")
    return out + rows


# --- main ---------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    main.add_argument("--rebuild", action="store_true", help="build the cached index again")
    commands = main.add_subparsers(dest="command", required=True)
    command = commands.add_parser("graphs", help="graphs with their role and size")
    command.add_argument("--hero", help="a hero's body graphs and the graphs they name")
    command.add_argument("--mode", help="a game mode's graphs (name or GUID)")
    command.add_argument("--entity", help="a body entity (003 GUID or index)")
    command = commands.add_parser("show", help="a graph as a tree")
    command.add_argument("graph", help="graph index (hex) or GUID")
    command.add_argument("--state", type=int, help="start at this m_states index")
    command.add_argument("--depth", type=int, help="follow links this deep")
    command = commands.add_parser("var", help="every use of a variable")
    command.add_argument("var", help="699, #699, #7644e (entity scope only), 0x2BB, 02BB.01C or a name")
    command.add_argument("--graph", help="only the uses in this graph")
    command = commands.add_parser("class", help="where a class is used and its fields")
    command.add_argument("name", help="alias, class name or hash")
    command = commands.add_parser("find", help="names, aliases, hashes, GUIDs")
    command.add_argument("text", nargs="+")
    command.add_argument("--limit", type=int, default=40, help="lines per kind (default 40)")
    command = commands.add_parser("hero", help="a hero's body, graphs, classes and shared vars")
    command.add_argument("name")
    return main


COMMANDS = {
    "graphs": cmd_graphs, "show": cmd_show, "var": cmd_var, "class": cmd_class, "find": cmd_find,
    "hero": cmd_hero,
}  # fmt: skip


def run(argv: list[str], index: dict | None = None) -> list[str]:
    """The lines a command prints; `index` skips loading the cached index (tests)."""
    args = parser().parse_args(argv)
    return COMMANDS[args.command](index if index is not None else load_index(args.rebuild), args)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # hero names such as Torbjörn in any console
    print("\n".join(run(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
