"""The client's statescript codec table (data/statescript_codecs_174.json, read in the client code by
tools/extract_statescript_codecs.py) and the encoder against it: every payload the encoder writes must read
back by the table, whole and with the same values."""

import sys
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import statescript
from ow174.game.bits import BitReader, BitWriter
from ow174.game.script import codecs, coverage, decode
from ow174.game.script import graph as graphs
from ow174.game.script.expr import Vec3, f32

FRAME_TIME, FRAME_MS = 16 * 1000, 16
# Payloads as the VM gives them, per encoder writer.
SAMPLES = {
    "none": [{}],
    "switch": [{"current": True}, {"current": False}],
    "stack": [{"top": True, "under": False}, {"top": False, "under": True}],
    "button": [{"counter": 0}, {"counter": 13}, {"counter": 21}],
    "volley": [
        {"start": FRAME_TIME - 160, "counter": 5},
        {"start": FRAME_TIME + 3, "offset": -7, "subindex": 2, "volleys": 3, "counter": 70},
    ],
    "anim": [{"counter": 6}, {"counter": 9}],
    "chase": [
        {"cur": 1.5, "reached": False, "remaining": None, "last": None},
        {"cur": Vec3(1.0, -2.0, 0.5), "reached": True, "remaining": 70000, "last": FRAME_TIME + 5},
        {"cur": 0.25, "remaining": 300, "last": FRAME_TIME - 40},
    ],
    "ability": [
        {"flags": 5, "cur": 0.0},
        {"flags": 2, "cur": 3.5, "rate": 1.0, "last": FRAME_TIME - 40},
        {"flags": 7, "cur": 1.25, "rate": 0.5, "last": FRAME_TIME + 16},
    ],
    "targets": [{}],
    "subscript": [{"child": 13}, {"child": 300}],
    "message": [{"stacked": True}, {"stacked": False}],
    "send": [{}],
    "link": [{}],
    "hits": [{"flag": True}, {"flag": False}],
    "flags13": [{"value": 0}, {"value": 63}, {"value": 64}, {"value": 8191}],
    "frames": [{"target": 9199}],
    "counter2": [{"counter": 0}, {"counter": 3}, {"counter": 6}],
    "pulser": [{"fresh": False, "count": 0}, {"fresh": True, "count": 300}],
}


def expected(kind: str, payload: dict) -> dict:
    """What the client takes from a payload the encoder wrote (decode.read_payload's form)."""
    get = payload.get
    if kind == "switch":
        return {"current": get("current", False)}
    if kind == "stack":
        return {"top": get("top", False), "under": get("under", False)}
    if kind in ("button", "anim", "counter2", "volley"):
        width = {"button": 15, "anim": 7, "counter2": 3, "volley": 63}[kind]
        out = {"counter": get("counter", 0) & width}
        if kind == "volley":
            out.update(start=get("start", FRAME_TIME), offset=get("offset", 0), volleys=get("volleys", 1))
            if get("offset"):
                out["subindex"] = get("subindex", 0)
        return out
    if kind == "chase":
        cur = get("cur", 0.0)
        cur = Vec3(f32(cur.x), f32(cur.y), f32(cur.z)) if isinstance(cur, Vec3) else f32(cur)
        return {
            "cur": cur,
            "reached": get("reached", False),
            "remaining": get("remaining"),
            "last": get("last"),
        }
    if kind == "ability":
        out = {"flags": get("flags", 0), "cur": f32(get("cur", 0.0)), "rate": 1.0}
        if get("cur", 0.0) > 0:
            out.update(rate=f32(get("rate", 1.0)), last=get("last", FRAME_TIME + FRAME_MS))
        return out
    if kind == "targets":
        return {"targets": ()}
    if kind == "subscript":
        return {"child": get("child", 0)}
    if kind == "message":
        return {"sender": None, "stacked": get("stacked", False)}
    if kind == "hits":
        return {"hits": (), "flag": get("flag", False)}
    if kind == "flags13":
        return {"value": get("value", 0)}
    if kind == "frames":
        return {"target": get("target", 0)}
    if kind == "pulser":
        return {"fresh": get("fresh", False), "count": get("count", 0) & 0xFF}
    return {}


def graph_classes() -> set[str]:
    return {
        node.cls
        for index in graphs.graph_indexes()
        for node in graphs.graph(index).states
        if node is not None
    }


class TableTests(unittest.TestCase):
    def test_every_state_class_of_the_graph_data_has_an_entry(self):
        missing = sorted(cls for cls in graph_classes() if codecs.codec(cls) is None)
        self.assertEqual(missing, [])

    def test_the_winner_follows_the_registry_rule(self):
        # The builder (0x7FF78AA9FE60) walks the list from its head and replaces an entry when a record's
        # priority is >= the entry's: the highest priority wins, on a tie the record nearer the list's end.
        groups = defaultdict(list)
        for record in codecs.table()["records"]:
            groups[record["typeinfo"]].append(record)
        for records in groups.values():
            top = max(record["priority"] for record in records)
            winner = max(record["index"] for record in records if record["priority"] == top)
            self.assertEqual([r["index"] for r in records if r["winner"]], [winner])

    def test_a_codec_name_means_one_layout(self):
        layouts = defaultdict(set)
        for entry in codecs.table()["classes"].values():
            if entry["layout"] is not None:
                layouts[entry["codec"]].add(repr(entry["layout"]))
        self.assertEqual({name: len(found) for name, found in layouts.items() if len(found) > 1}, {})

    def test_stec_classes(self):
        self.assertEqual(
            codecs.stec_classes(),
            {
                "STUStatescriptStateLogicalButton",
                "STU_691BFA55",
                "STU_2F07848F",
                "STUStatescriptStateWeaponVolley",
            },
        )

    def test_classes_read_in_the_client(self):
        entries = codecs.table()["classes"]
        frames = entries["D74D4F47"]  # the live crash of 2026-10-01: the game's class (priority 1) wins
        self.assertEqual((frames["winner"]["priority"], frames["winner"]["vtable"]), (1, "0x7ff78b5836c8"))
        self.assertEqual(frames["layout"], [{"op": "bits", "n": 32, "f": "target", "block": "3D70B9F1"}])
        effect = entries["691BFA55"]  # the second crash: 2 bits in every frame kind (0x7FF789894540)
        self.assertEqual((effect["stec"], effect["winner"]["vtable"]), (True, "0x7ff78b53dba8"))
        self.assertEqual(effect["layout"], [{"op": "bits", "n": 2, "f": "counter"}])
        self.assertEqual([op["n"] for op in entries["B9898052"]["layout"]], [1, 8])  # ClientOnlyPulser
        self.assertEqual(
            entries["C38B92B4"]["layout"][0], {"op": "bits", "n": 8, "f": "flags", "block": "7EEFB57A"}
        )
        self.assertEqual(entries["594C2D80"]["codec"], "stack")
        self.assertEqual(entries["9D7BF987"]["layout"], [])  # ClientOnlySharedVars: the plain State

    def test_the_layout_reader_reads_conditions_and_lists(self):
        out = BitWriter()
        out.bit(1)  # has_list
        out.w_u16(2)
        for entity, age in ((0xA0000101, 5), (0x80000002, 300)):
            out.bits(entity, 32)
            out.var_b(age)
        layout = codecs.codec("STUStatescriptStateTrackTargets").layout
        got = codecs.read(BitReader(out.getvalue()), layout, True, False)
        self.assertEqual(
            got["targets"], [{"entity": 0xA0000101, "age": 5}, {"entity": 0x80000002, "age": 300}]
        )


class EncoderTests(unittest.TestCase):
    def test_every_writer_reads_back_by_the_table(self):
        """Each class the encoder can write: its payloads read back by the client's table to the same values
        and to the last bit (owner deltas: H = 1, full = 0)."""
        checked = defaultdict(int)
        for cls in sorted(graph_classes()):
            kind = statescript.family(cls)
            if kind == "unknown":
                continue
            for payload in SAMPLES[kind]:
                out = BitWriter()
                statescript.write_payload(out, cls, payload, FRAME_TIME, FRAME_MS)
                reader = BitReader(out.getvalue())
                got = decode.read_payload(reader, cls, FRAME_TIME, FRAME_MS, owner=True, full=False)
                self.assertEqual(reader.pos, out.count, (cls, payload))
                self.assertEqual(got, expected(kind, payload), (cls, payload))
                checked[kind] += 1
        self.assertEqual(set(checked), set(statescript.WRITERS))

    def test_unwritable_classes_are_never_sent_on(self):
        self.assertFalse(statescript.sendable("STU_9935FBC8", True))
        self.assertTrue(statescript.sendable("STU_9935FBC8", False))
        self.assertFalse(
            statescript.sendable("STU_2F07848F", False)
        )  # StEC: its payload goes with X even off
        self.assertTrue(statescript.sendable("STU_691BFA55", True))

    def test_coverage_report(self):
        rows = {row.cls: row for row in coverage.missing("soldier")}
        self.assertEqual(set(rows), {"STU_9935FBC8", "STU_861D9015"})
        self.assertIn((0x02CF, 1), rows["STU_861D9015"].states)
        self.assertIn("2 state classes the encoder cannot write", coverage.report(0x02E000000000006E))
        for record in graphs.bodies().values():
            for row in coverage.missing(int(record["hero"], 16)):
                self.assertEqual(statescript.family(row.cls), "unknown")


if __name__ == "__main__":
    unittest.main()
