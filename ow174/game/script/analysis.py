"""Static dependency and layout evidence; this module does not execute statescript.

Graph asset references include schemas and dormant scripts. Entry-link walks ignore
conditions, event delivery, timing and state lifetimes. Neither is execution coverage.
"""

import hashlib
import json
from collections import Counter, deque
from collections.abc import Callable, Iterable

from ow174.game.script.graph import GRAPH_TYPE, GRAPHS_PATH, Graph, Node, bodies, graph, guid

TARGET_BUILD = "1.74.0.0.104319"
GraphLoader = Callable[[int], Graph | None]


def walk_values(value, path: str = ""):
    """Yield every object and scalar with its exact path in the extracted data."""
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk_values(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for number, item in enumerate(value):
            yield from walk_values(item, f"{path}[{number}]")


def reference_closure(roots: Iterable[int], loader: GraphLoader = graph) -> dict:
    """Follow graph asset references, including cycles and missing targets, once each."""
    roots = sorted(set(roots))
    pending, visited, found, missing, edges = deque(roots), set(), [], [], []
    while pending:
        index = pending.popleft()
        if index in visited:
            continue
        visited.add(index)
        item = loader(index)
        if item is None:
            missing.append(index)
            continue
        found.append(index)
        for target in sorted(item.graph_refs()):
            edges.append([index, target])
            if target not in visited:
                pending.append(target)
    return {
        "meaning": "graph asset-reference closure; not executable or initialized instance coverage",
        "roots": roots,
        "found": sorted(found),
        "missing": sorted(missing),
        "edges": sorted(edges),
    }


def _identity(node: Node | None) -> dict | None:
    if node is None:
        return None
    return {"position": node.pos, "rid": node.rid, "type": node.cls}


def entry_link_candidates(item: Graph) -> list[dict]:
    """Walk plugs independently from every entry, without guessing when it runs."""
    entries = []
    for number, entry in enumerate(item.entries):
        pending = deque([entry.pos] if entry is not None else [])
        visited, unresolved = set(), []
        while pending:
            position = pending.popleft()
            if position in visited:
                continue
            visited.add(position)
            node = item.node(position)
            for path in sorted(node.plugs()):
                for target, plug in node.links(path):
                    if target is None or item.node(target) is None:
                        unresolved.append({"source": position, "path": path, "target": target, "plug": plug})
                    elif target not in visited:
                        pending.append(target)
        entries.append({
            "entry_index": number,
            "entry": _identity(entry),
            "trigger_semantics": "not evaluated; entry type/name is structural evidence only",
            "plug_link_candidate_positions": sorted(visited),
            "unresolved_links": unresolved,
        })
    return entries


def _field_summary(fields: dict) -> dict:
    summary: dict = {"dynamic_variables": [], "expressions": [], "literals": [], "node_references": []}
    for path, value in walk_values(fields):
        if not isinstance(value, dict):
            continue
        kind = value.get("$")
        if kind == "STUConfigVarDynamic":
            summary["dynamic_variables"].append({
                "path": path, "identifier": value.get("m_identifier"), "scope": value.get("m_60DB8F99"),
            })
        elif kind == "STUConfigVarExpressionData":
            summary["expressions"].append({
                "path": path, "opcodes": value.get("m_opcodes"), "constants": value.get("m_D99EF254"),
                "fragments": value.get("m_fragments"),
            })
        elif isinstance(kind, str) and kind.startswith("STUConfigVar") and "m_value" in value:
            summary["literals"].append({"path": path, "type": kind, "value": value["m_value"]})
        if "node" in value:
            summary["node_references"].append({"path": path, "position": value["node"]})
    summary["scalars"] = {
        key: value for key, value in fields.items()
        if value is not None and not isinstance(value, (dict, list))
    }
    return summary


def analyze_graph(item: Graph, supported_nodes: set[str] | None = None, *, include_fields=False) -> dict:
    """Describe source indexes and filtered wire slots without assuming runtime support."""
    owner = {node.pos: slot for slot, node in enumerate(item.owner_states())}
    remote = {node.pos: slot for slot, node in enumerate(item.remote_states())}
    presence = {entry.index: bit for bit, entry in enumerate(item.presence_vars())}
    nodes, unknown_links, unknown_node_refs = [], [], []
    unnamed_fields = Counter()
    for node in sorted(item.nodes, key=lambda value: value.pos):
        links = [
            {"path": path, "target": target, "input_plug": plug}
            for path in sorted(node.plugs()) for target, plug in node.links(path)
        ]
        unknown_links.extend(
            {"source": node.pos, **link} for link in links
            if link["target"] is None or item.node(link["target"]) is None
        )
        detail = {
            **_identity(node), "state_index": node.state, "remote_index": node.remote,
            "client_only": node.client_only, "server_only": node.server_only,
            "owner_slot": owner.get(node.pos), "remote_slot": remote.get(node.pos),
            "links": links, "field_summary": _field_summary(node.fields),
        }
        if include_fields:
            detail["fields"] = node.fields
        unknown_node_refs.extend(
            {"source": node.pos, **reference} for reference in detail["field_summary"]["node_references"]
            if item.node(reference["position"]) is None
        )
        unnamed_fields.update(
            value["$"] for _path, value in walk_values(node.fields)
            if isinstance(value, dict) and isinstance(value.get("$"), str) and value["$"].startswith("STU_")
        )
        nodes.append(detail)
    refs = []
    sources = [("graph", item.fields), *[(f"node[{node.pos}]", node.fields) for node in item.nodes]]
    for source, fields in sources:
        for path, value in walk_values(fields):
            asset = guid(value)
            if asset is not None and asset >> 48 == GRAPH_TYPE:
                refs.append({"source": source, "path": path, "target": asset & 0xFFFFFFFFFFFF})
    counts = Counter(node.cls for node in item.nodes)
    unknown_classes = sorted(kind for kind in counts if kind.startswith("STU_"))
    return {
        "index": item.index,
        "counts": {
            "nodes": len(item.nodes), "entries": len(item.entries), "state_entries": len(item.states),
            "owner_states": len(owner), "remote_states": len(remote), "presence_bits": len(presence),
        },
        "node_types": dict(sorted(counts.items())),
        "declared_handler_coverage": None if supported_nodes is None else {
            "meaning": "caller-declared node handlers; not verified semantics or execution coverage",
            "supported_node_count": sum(count for kind, count in counts.items() if kind in supported_nodes),
            "unsupported_types": sorted(set(counts) - supported_nodes),
        },
        "entries": entry_link_candidates(item),
        "state_layout": [
            {"state_index": number, "node": _identity(node),
             "owner_slot": owner.get(node.pos) if node else None,
             "client_only": node.client_only if node else None,
             "server_only": node.server_only if node else None}
            for number, node in enumerate(item.states)
        ],
        "remote_layout": [
            {"remote_index": number, "node": _identity(node),
             "remote_slot": remote.get(node.pos) if node else None}
            for number, node in enumerate(item.remote_nodes)
        ],
        "variables": [
            {"source_index": entry.index, "identifier": entry.var, "scope": entry.scope,
             "flag": entry.flag, "presence_bit": presence.get(entry.index)}
            for entry in item.sync_vars
        ],
        "references": refs,
        "unknowns": {
            "unread_node_rids": list(item.unknown_rids),
            "unread_entry_slots": [number for number, node in enumerate(item.entries) if node is None],
            "unread_state_slots": [number for number, node in enumerate(item.states) if node is None],
            "unread_remote_slots": [number for number, node in enumerate(item.remote_nodes) if node is None],
            "unnamed_node_types": unknown_classes,
            "unnamed_nested_field_types": dict(sorted(unnamed_fields.items())),
            "unresolved_plug_links": unknown_links,
            "unresolved_node_references": unknown_node_refs,
            "execution_reachability": "not evaluated",
            "field_and_opcode_semantics": "not established by this static analysis",
        },
        "nodes": nodes,
    }


def analyze_body(
    body: int, *, loader: GraphLoader = graph, body_table: dict | None = None,
    supported_nodes: set[str] | None = None, detail_graphs: Iterable[int] = (),
) -> dict:
    """Analyze a hero's ordered initial graphs, manager, weapons and reference closure."""
    record = (bodies() if body_table is None else body_table).get(body)
    if record is None:
        raise ValueError(f"No extracted body definition for 0x{body:016X}")
    initial = record.get("graphs") or []
    roots = [entry["graph"] for entry in initial if entry.get("graph") is not None]
    manager, weapons = record.get("manager"), record.get("weapons") or []
    roots += [index for index in [manager, *weapons] if index is not None]
    body_refs = []
    for path, value in walk_values(record):
        asset = guid(value)
        if asset is not None and asset >> 48 == GRAPH_TYPE:
            body_refs.append({"path": path, "target": asset & 0xFFFFFFFFFFFF})
    roots += [reference["target"] for reference in body_refs]
    closure = reference_closure(roots, loader)
    details = set(detail_graphs)
    graphs = [analyze_graph(loader(index), supported_nodes, include_fields=index in details)
              for index in closure["found"]]
    counts = Counter()
    for item in graphs:
        counts.update(item["node_types"])
    return {
        "format_version": 1,
        "target_build": TARGET_BUILD,
        "build_evidence": "repository target; bundle does not embed independent exact-build verification",
        "body": body, "hero": record.get("hero"), "name": record.get("name"),
        "initial_graphs": initial, "manager": manager, "weapons": weapons,
        "body_asset_references": body_refs,
        "closure": closure, "node_types": dict(sorted(counts.items())), "graphs": graphs,
        "limits": [
            "References include schemas/dormant scripts, not a list of live graph instances.",
            "Entry-link candidates ignore predicates, events, timing and state lifecycle.",
            "No expression evaluation, simulation or client acceptance is performed.",
            "Structural constants and hashed field names do not prove gameplay parameter semantics.",
        ],
    }


def source_fingerprint() -> dict:
    """Identify the exact input bundle, without treating its filename as build proof."""
    data = GRAPHS_PATH.read_bytes()
    return {"path": str(GRAPHS_PATH), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def render_text(report: dict, detail_graphs: Iterable[int] = ()) -> str:
    """A readable report; full per-node/field data is also available as CLI JSON."""
    details = set(detail_graphs)
    closure = report["closure"]
    lines = [
        "Overwatch 1.74 static statescript research report", f"Target: {report['target_build']}",
        f"Build evidence: {report['build_evidence']}",
        f"Hero: {report['name']}; body 0x{report['body']:016X}",
        "IMPORTANT: graph reference closure and entry-link candidates are NOT execution reachability.",
        f"Initial definitions (ordered): {[entry.get('graph') for entry in report['initial_graphs']]}",
        f"Weapon manager: {report['manager']}; weapon slots (null slots retained): {report['weapons']}",
        f"Loaded reference closure: {len(closure['found'])} graphs",
        "Graph indexes: " + " ".join(f"{index:04X}" for index in closure["found"]),
        "Missing graph references: " + (" ".join(f"{index:04X}" for index in closure["missing"]) or "none"),
        "Handler/execution support is unknown unless explicitly declared; declarations are not proof.",
    ]
    if "source" in report:
        lines.append("Input bundle: " + json.dumps(report["source"], ensure_ascii=False))
    lines += ["", "Node types in reference closure:"]
    lines += [f"  {kind}: {count}" for kind, count in report["node_types"].items()]
    for item in report["graphs"]:
        lines += ["", f"GRAPH {item['index']:04X} " + json.dumps(item["counts"], sort_keys=True)]
        for entry in item["entries"]:
            lines.append("  ENTRY " + json.dumps(entry, ensure_ascii=False, sort_keys=True))
        lines.append("  STATE INDEX -> OWNER WIRE SLOT (null = excluded or unread):")
        lines += ["    " + json.dumps(row, sort_keys=True) for row in item["state_layout"]]
        lines.append("  REMOTE INDEX -> REMOTE WIRE SLOT:")
        lines += ["    " + json.dumps(row, sort_keys=True) for row in item["remote_layout"]]
        lines.append("  SYNC VARIABLE SOURCE INDEX -> INSTANCE PRESENCE BIT:")
        lines += ["    " + json.dumps(row, sort_keys=True) for row in item["variables"]]
        lines.append("  UNKNOWNS " + json.dumps(item["unknowns"], ensure_ascii=False, sort_keys=True))
        if item["index"] in details:
            lines.append("  NODE/FIELD EVIDENCE (raw names/values, no inferred gameplay meaning):")
            for node in item["nodes"]:
                compact = {key: value for key, value in node.items() if key != "fields"}
                lines.append("    " + json.dumps(compact, ensure_ascii=False, sort_keys=True))
    lines += ["", "Limits:", *["  " + limit for limit in report["limits"]]]
    return "\n".join(lines) + "\n"
