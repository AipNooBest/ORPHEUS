"""tools/ssq.py against known facts of the 1.74 graphs: the hero catalog, the graph tables of the frame
format, the VM's state classes, the mover mods and each game mode's scripts."""

import importlib.util
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ssq = load_tool("ssq")


INDEX = ssq.load_index()


def run(*argv: str) -> list[str]:
    return ssq.run(list(argv), INDEX)


def header(graph: str) -> dict:
    """The numbers `show GRAPH` prints in its head."""
    lines = run("show", graph, "--state", "0", "--depth", "0")
    text = "\n".join(lines[:8])
    numbers = re.search(
        r"(\d+) nodes \((\d+) networked, (\d+) client-only, (\d+) server-only\), (\d+) states, "
        r"(\d+) entries, (\d+) remote sync nodes",
        text,
    )
    frames = re.search(
        r"frames: (\d+) owner states, (\d+) remote states; (\d+) sync vars, (\d+) with a presence bit: (.*)",
        text,
    )
    bits = re.search(r"bits: nodes (\d+), states (\d+), remote (\d+), sync vars (\d+)", text)
    classes = next(line for line in lines if line.startswith("classes: "))[len("classes: ") :]
    return {
        "nodes": int(numbers[1]),
        "net/client/server": tuple(int(numbers[k]) for k in (2, 3, 4)),
        "states": int(numbers[5]),
        "entries": int(numbers[6]),
        "remote": int(numbers[7]),
        "owner states": int(frames[1]),
        "remote states": int(frames[2]),
        "presence": int(frames[4]),
        "presence vars": [int(var) for var in re.findall(r"#(\d+)", frames[5])],
        "bits": tuple(int(bits[k]) for k in (1, 2, 3)),
        "classes": class_counts(classes),
        "lines": lines,
    }


def class_counts(text: str) -> Counter:
    """ "StateStack x10, Switch(case subgraphs) x6" -> Counter by name."""
    counts = Counter()
    for part in text.split(", "):
        name, count = part.rsplit(" x", 1)
        counts[name] += int(count)
    return counts


# Soldier: 76's Sprint graph as a catalog row: nodes, states, entries, networked / client-only / server-only,
# remote sync nodes, bit widths and node classes.
CATALOG_0255 = (
    "| 0255 | 0 | init#5 | initial | root | - | 58 | 34 | 7 | 53/5/0 | 19 | 6/6/5 | LET_GAME_DECIDE | "
    "StateStack x10, ActionSetVar x8, StateBooleanSwitch x8, Entry x6, StateLogicalButton x5, "
    "Condition_Bool x4, ActionSendGameMessage x2, MovementMod x2, StateAnim x2, ActionCriteria x1, "
    "ActionSendClientOnlyGameMessage x1, ClientOnlySharedVars x1, EntryClientOnlySharedVars x1, "
    "STU_75D1C308 x1, STU_E9694EFD x1, StateAbility x1, StateChaseVar x1, StateEffect(StEC) x1, "
    "StateUXPresenter x1, StateWait x1 |"
)
# Per graph: m_states, H=1 states, remote, H=0 states, presence bits, and the nodes / states / remote bit
# widths.
GRAPH_TABLE = {
    "13C1": (49, 43, 20, 19, 12, (7, 6, 5)),
    "20E4": (26, 1, 1, 1, 0, (5, 5, 1)),
    "288A": (12, 6, 1, 1, 0, (4, 4, 1)),
    "288B": (146, 30, 21, 19, 4, (9, 8, 5)),
    "0C90": (138, 118, 58, 56, 29, (9, 8, 6)),
    "13C0": (22, 20, 10, 10, 3, (6, 5, 4)),
    "0C8E": (67, 48, 31, 29, 16, (8, 7, 5)),
}
# Soldier: 76's body graphs: presence bits / H=0 states.
BODY_GRAPHS = {
    "0033": (4, 7), "004B": (4, 16), "0043": (7, 13), "0251": (0, 0), "0255": (9, 18), "0257": (6, 13),
    "0259": (7, 25), "091B": (0, 0), "08B6": (2, 5), "0015": (1, 13), "0254": (23, 47), "01CF": (22, 20),
}  # fmt: skip
# Each game mode's controller graph (STUGameMode m_teams[].m_controllerScript).
CONTROLLERS = {
    "0018": "13C1", "0010": "0C90", "0014": "0C90", "0015": "0C90", "0016": "0C90", "0017": "0C90",
    "005A": "0C90", "0067": "0C90", "0003": "0CA2", "002A": "0CA2", "000F": "0D2A", "001A": "0D2A",
    "0009": "0D6F", "0046": "0D6F", "0008": "0DA5", "0025": "0F44", "001D": "0F62", "001E": "0F74",
    "0059": "0F74", "0070": "0F74", "0020": "0FC3", "0029": "11D0", "0019": "13A4", "0043": "194E",
    "0007": "2E98",
}  # fmt: skip


class GraphFacts(unittest.TestCase):
    def test_sprint_graph_matches_its_catalog_row(self):
        row = [cell.strip() for cell in CATALOG_0255.strip("|").split("|")]
        found = header("0255")
        self.assertEqual(found["nodes"], int(row[6]))
        self.assertEqual(found["states"], int(row[7]))
        self.assertEqual(found["entries"], int(row[8]))
        self.assertEqual(found["net/client/server"], tuple(int(part) for part in row[9].split("/")))
        self.assertEqual(found["remote"], int(row[10]))
        self.assertEqual(found["bits"], tuple(int(part) for part in row[11].split("/")))
        self.assertEqual(
            found["classes"], class_counts(row[13])
        )  # same names: MovementMod x2, StateStack x10, ...
        self.assertIn("abilities: rid4 st1 35 ABILITY_1", found["lines"])

    def test_frame_counts_of_the_graph_tables(self):
        for graph, (states, owner, remote, remote_states, presence, bits) in GRAPH_TABLE.items():
            found = header(graph)
            with self.subTest(graph=graph):
                self.assertEqual(
                    (found["states"], found["owner states"], found["remote"], found["remote states"]),
                    (states, owner, remote, remote_states),
                )
                self.assertEqual((found["presence"], found["bits"]), (presence, bits))
        for graph, (presence, remote_states) in BODY_GRAPHS.items():
            found = header(graph)
            with self.subTest(graph=graph):
                self.assertEqual((found["presence"], found["remote states"]), (presence, remote_states))

    def test_presence_bits_in_order(self):
        # 13C1's presence bits in order, 288B's v25377, v13161, v32791, v478.
        self.assertEqual(
            header("13C1")["presence vars"],
            [2114, 8849, 8916, 12511, 8418, 8419, 8738, 13162, 10476, 8432, 10489, 9023],
        )
        self.assertEqual(header("288B")["presence vars"], [25377, 13161, 32791, 478])

    def test_288B_is_the_hero_select_screen(self):
        row = next(line for line in run("graphs") if line.startswith("288B "))
        self.assertRegex(row, r"^288B +UI +269 +146 +hero select screen$")
        found = header("288B")["lines"]
        self.assertIn("named by: 288A rid1 st0 SubScript", found)  # 288A slot 0 = st0 SubScript -> 288B
        lines = run("find", "hero", "select")
        self.assertIn("  288B  UI  hero select screen", lines)

    def test_who_starts_the_hero_select_graphs(self):
        # 13C1 st42 SubScript -> 20E4, st43 -> 288A; 288A st0 -> 288B, st1 -> 288D, st3 -> 292E.
        rows = {line[:4]: line for line in run("graphs", "--mode", "practice range")[2:]}
        self.assertRegex(rows["20E4"], r"from 13C1 rid\d+ st42 SubScript$")
        self.assertRegex(rows["288A"], r"from 13C1 rid\d+ st43 SubScript$")
        self.assertRegex(rows["288B"], r"from 288A rid\d+ st0 SubScript$")
        self.assertRegex(rows["288D"], r"from 288A rid\d+ st1 SubScript$")
        self.assertRegex(rows["292E"], r"from 288A rid\d+ st3 SubScript$")

    def test_sprint_movement_mods(self):
        # Sprint (0255) state 0 "speed add 0.5" (a dynamic config var, 0.5 from the VM), state 20 "crouch
        # intent set 0" (mover mod index 0 = speed, 21 = crouch intent).
        state0 = run("show", "0255", "--state", "0", "--depth", "0")
        self.assertIn("rid2 st0 MovementMod (remote 0)", state0)
        self.assertIn("    m_BC5E91CF=[STU_4CB0950F{m_A9561CA0=0, m_9B7A63EA=#1002}]", state0)
        state20 = run("show", "0255", "--state", "20", "--depth", "0")
        self.assertIn("    m_BC5E91CF=[STU_56DEE1BB{m_A9561CA0=21, m_9B7A63EA=0}]", state20)
        self.assertIn("  0255 rid3 ActionSetVar  m_out_Var = 0.5", run("var", "1002", "--graph", "0255"))

    def test_movement_mod_count(self):
        # 171 MovementMod states (STU_316CFEF2) in the 1.74 graphs.
        lines = run("class", "MovementMod")
        self.assertTrue(
            lines[0].startswith("STU_316CFEF2  alias MovementMod (from research catalog)  hash 316CFEF2")
        )
        self.assertTrue(any(line.startswith("nodes: 171 in ") for line in lines))
        self.assertIn("  STU_316CFEF2  MovementMod  171 nodes", run("find", "316CFEF2"))

    def test_aliases_are_unique(self):
        classes = set(INDEX["objects"]) | set(INDEX["class_nodes"])
        self.assertEqual(len({ssq.alias(cls) for cls in classes}), len(classes))


class HeroFacts(unittest.TestCase):
    def test_soldier(self):
        # Soldier's roots and his catalog row (57 graphs, 2847 nodes, 1610 networked, 140 classes, 0F9C
        # missing).
        lines = run("hero", "soldier")
        self.assertIn(
            "initial graphs (instance 1..N): 0033 004B 0043 0251 0255 0257 0259 091B 08B6; manager 0015; "
            "weapons 0:- 1:0254 2:-",
            lines,
        )
        self.assertIn(
            "57 graphs (1 not in the data: 0F9C), 2847 nodes (1610 networked), 140 node classes", lines
        )
        for slot, graph in ((5, "0255"), (6, "0257"), (7, "0259")):
            self.assertTrue(
                any(re.match(rf"{graph} +ability .* init#{slot}$", line) for line in lines), graph
            )
        rows = run("graphs", "--hero", "soldier")
        self.assertEqual(rows[0], "Soldier: 76: body 03CF.003, 57 graphs (1 not in the data: 0F9C)")
        self.assertTrue(any(line.startswith("0F9C ") and line.endswith("(not in the data)") for line in rows))

    def test_soldier_abilities(self):
        # The buttons: 0043 st6 QUICK_WEAPON, 0255 st1 ABILITY_1, 0257 st0 ABILITY_2, 0259 st0 ABILITY_3,
        # 0254 st31 SECONDARY_FIRE.
        lines = run("hero", "soldier")
        expected = [
            "0043 rid13 st6 31 QUICK_WEAPON",
            "0255 rid4 st1 35 ABILITY_1",
            "0257 rid2 st0 36 ABILITY_2",
            "0259 rid2 st0 37 ABILITY_3",
            "0254 rid61 st31 29 SECONDARY_FIRE",
        ]
        for text in expected:
            self.assertIn(f"  ability {text}", lines)


class VariableFacts(unittest.TestCase):
    def test_699_is_written_by_sprint_and_read_by_its_icon(self):
        # Sprint 0255: #699 from the Ability's start/stop var lists; st31's icon reads
        # `#699 ? 0 : (#2575 || #2575e)` and `#699 == 1`.
        lines = run("var", "699", "--graph", "0255")
        self.assertIn("  0255 rid4 st1 StateAbility  m_EF3C5C73[0].m_out_Var = 1", lines)
        self.assertIn("  0255 rid4 st1 StateAbility  m_19899EAC[0].m_out_Var = 0", lines)
        reads = [line for line in lines if line.startswith("  0255 rid53 st31 StateUXPresenter")]
        self.assertTrue(any(line.endswith(": (#699 ? 0.0 : (#2575 || #2575e))") for line in reads), reads)
        self.assertTrue(any(line.endswith(": (#699 == 1.0)") for line in reads), reads)
        self.assertIn("  0255 m_syncVars[59]: instance, presence bit 7, m_AC9480C7=0", lines)
        self.assertEqual(lines[lines.index("write (3 in 1 graph):") + 1][:28], "  0255 rid4 st1 StateAbility")

    def test_entity_scoped_bools_of_hero_select(self):
        # v31435 and v2438 are entity-scoped bools written with STUConfigVarBool.
        lines = run("var", "31435", "--graph", "288B")
        self.assertRegex(lines[1], r"^scope: entity x12 \(")
        self.assertEqual(lines[2], "type: bool x5 (from defaults and constant writes)")
        # Every constant write of v2438 in 288B is an entity bool too; its instance uses are where game
        # message 1360.025 lands (rid22) before rid201 copies it into the entity variable.
        lines = run("var", "2438", "--graph", "288B")
        writes = lines[lines.index("write (10 in 1 graph):") + 1 : lines.index("read (10 in 1 graph):")]
        constant = [line for line in writes if line.endswith(("= true [entity]", "= false [entity]"))]
        self.assertEqual(len(constant), 8)
        self.assertTrue(lines[2].startswith("type: bool x8 "), lines[2])
        self.assertIn("  288B rid201 ActionSetVar  m_out_Var = #2438 [entity]", writes)
        self.assertTrue(
            writes[0].startswith("  288B rid22 EntryGameMessage  m_params[0].m_out_Var: "), writes[0]
        )

    def test_names_and_notation(self):
        self.assertTrue(
            run("var", "CURRENT_HERO")[0].startswith("#940  identifier 03AC.01C  name CURRENT_HERO")
        )
        self.assertEqual(ssq.guid_text(0x0580000000000255), "0255.01B")
        self.assertEqual(ssq.guid_text(0x0D800000000002BB), "02BB.01C")
        self.assertEqual(ssq.guid_text(0x02E000000000006E), "006E.075")
        self.assertEqual(ssq.guid_text(0x04000000000003CF), "03CF.003")
        self.assertEqual(ssq.guid_text(0x0230000000000018), "0018.0C5")
        self.assertEqual([ssq.parse_var(text) for text in ("699", "#699", "#7644e", "0x2BB", "02BB.01C")],
                         [699, 699, 7644, 699, 699])  # fmt: skip
        self.assertEqual(ssq.parse_guid("0186.00D"), 0x0300000000000186)
        self.assertEqual(ssq.parse_guid("0255.01B"), 0x0580000000000255)
        found = run("find", "03CF.003")
        self.assertIn("  Soldier: 76  hero 006E.075  body 03CF.003  Damage", found)
        found = run("find", "0018.0C5")
        self.assertTrue(
            any(line.startswith("  Practice Range  0018.0C5  script 13C0") for line in found), found
        )

    def test_expressions_are_read_by_their_jumps(self):
        # The code after an END is reached by a jump only (0255 st31: 8 0 4 6 6 0 0 8 8 1 3 4 8 2 0).
        uses, first = ssq.bytecode_uses([8, 0, 4, 6, 6, 0, 0, 8, 8, 1, 3, 4, 8, 2, 0])
        self.assertEqual((uses, first), ({0: {"read"}, 1: {"read"}, 2: {"read"}}, 0))
        kinds = Counter(kind for uses in INDEX["vars"].values() for _gi, _rid, kind, _scope, _path in uses)
        self.assertNotIn("unused", kinds)


class ModeFacts(unittest.TestCase):
    def test_each_mode_names_its_controller(self):
        for mode, controller in CONTROLLERS.items():
            rows = run("graphs", "--mode", mode)
            with self.subTest(mode=mode):
                self.assertTrue(
                    any(line.startswith(controller + " ") and " controller" in line for line in rows[2:]),
                    rows[:6],
                )
        rows = run("graphs", "--mode", "001E")
        self.assertTrue(any(line.startswith("1058 ") and "FFA body script" in line for line in rows))
        practice = run("graphs", "--mode", "Practice Range")
        self.assertTrue(any(line.startswith("0C90 ") and "TeamRed controller" in line for line in practice))


if __name__ == "__main__":
    unittest.main()
