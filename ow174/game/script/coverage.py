"""Which statescript state classes of a hero the server cannot send yet.

For every networked state (neither client-only nor server-only) in the graphs a hero body can make (its
initial graphs, the weapon manager, its weapon scripts and every graph they name), the classes whose payload
the encoder has no writer for (statescript.family is "unknown") or whose layout the client table does not
have (unverified). Such a state is sent only while it is off (and never when it is an StEC class); the client
then keeps its own prediction of it. tools/codec_coverage.py prints the report.
"""

from dataclasses import dataclass, field

from ow174.game import statescript
from ow174.game.script import codecs, graph


@dataclass
class Missing:
    cls: str
    codec: str | None
    status: str
    stec: bool
    states: list = field(default_factory=list)  # (graph index, m_states index)


def hero_body(hero: str | int) -> dict:
    """A hero's body record (graph.bodies()) by its hero GUID or a name prefix (case does not matter)."""
    for record in graph.bodies().values():
        if isinstance(hero, int) and int(record["hero"], 16) == hero:
            return record
        if isinstance(hero, str) and record["name"].lower().startswith(hero.lower()):
            return record
    raise KeyError(f"no hero body for {hero!r}")


def hero_graphs(record: dict) -> list[int]:
    """The graphs the body can make: its roots and every graph they name, the ones the data has."""
    roots = [item.get("graph") for item in record["graphs"]] + [
        record.get("manager"),
        *(record.get("weapons") or []),
    ]
    seen: set[int] = set()
    todo = [index for index in roots if index is not None]
    while todo:
        index = todo.pop()
        found = graph.graph(index)
        if index in seen or found is None:
            continue
        seen.add(index)
        todo.extend(found.graph_refs() - seen)
    return sorted(seen)


def missing(hero: str | int) -> list[Missing]:
    """The classes of the hero's networked states the encoder cannot write, most used first."""
    found: dict[str, Missing] = {}
    for index in hero_graphs(hero_body(hero)):
        for node in graph.graph(index).owner_states():
            if statescript.family(node.cls) != "unknown":
                continue
            item = codecs.codec(node.cls)
            row = found.setdefault(
                node.cls,
                Missing(
                    node.cls,
                    item.codec if item else None,
                    item.status if item else "absent",
                    bool(item and item.stec),
                ),
            )
            row.states.append((index, node.state))
    return sorted(found.values(), key=lambda row: (-len(row.states), row.cls))


def report(hero: str | int) -> str:
    record = hero_body(hero)
    rows = missing(hero)
    count = len(hero_graphs(record))
    lines = [f"{record['name']}: {count} graphs, {len(rows)} state classes the encoder cannot write"]
    for row in rows:
        where = ", ".join(f"{g:04X} st{s}" for g, s in row.states[:6]) + (
            " ..." if len(row.states) > 6 else ""
        )
        kind = "StEC, " if row.stec else ""
        lines.append(
            f"  {row.cls}: {len(row.states)} states, codec {row.codec} ({kind}{row.status}): {where}"
        )
    return "\n".join(lines)
