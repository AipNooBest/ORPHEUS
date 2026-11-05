"""First-pulse freshness in the target build's owner and observer statescript frames."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import observers
from ow174.game.combat import ASSISTS, CRIT, DIED, KILLER, LOADOUT
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.script import expr, graph, runtime
from ow174.game.script.bot import BotScript
from ow174.game.script.decode import read_chunk
from ow174.game.script.driver import BodyScript, BodyStream

TRACER = 0x02E0000000000003
START = 1000
BODY = 0x80000101
OTHER_BODY = 0x80000201


class ClientOnlyPulserTests(unittest.TestCase):
    def test_early_death_reaches_owner_and_observer_as_a_fresh_first_pulse(self):
        script = BodyScript(SOLDIER, BODY, START, ult_seconds=0)
        instance = next(item for item in script.component.instances.values() if item.graph.index == 0x01CF)
        self.assertFalse(instance.graph.states[108].field("m_C85E2CBA"))
        known = {item: graph.index for item, graph in script.graphs().items()}

        initial_owner, = script.spawn_frames()
        script.stream.arrived(initial_owner.chunk_last)
        observer = BodyStream(BODY)
        before = observers.snapshot(script)
        initial_observer = observer.full(observers.full_frame(script, before), before)
        observer.arrived(initial_observer.chunk_last)

        script.set_health(0, 200)
        params = {KILLER: expr.Entity(OTHER_BODY), CRIT: False, LOADOUT: None, ASSISTS: ()}
        runtime.send_message(script.component, DIED, params, OTHER_BODY)

        script.command(START + 1, 0)
        (owner_update,) = script.frames()
        snapshot = observers.snapshot(script)
        frame, keys = observers.delta(script, observer, snapshot)
        observer_update = observer.delta(frame, snapshot, keys, 0)
        for update, owner in ((owner_update, True), (observer_update, False)):
            with self.subTest(owner=owner):
                chunk = update.chunk
                wire = read_chunk(chunk.getvalue(), chunk.count, known)
                self.assertIs(wire.owner, owner)
                self.assertEqual(wire.instances[instance.id].states[108], (True, {"fresh": True, "count": 1}))

        script.command(START + 33, 0)
        self.assertEqual(instance.states[108].payload(), {"fresh": False, "count": 1})

    def test_tracer_bootstrap_retains_the_nonfresh_wire_bit(self):
        script = BodyScript(TRACER, BODY, START, ult_seconds=0)
        instance = next(item for item in script.component.instances.values() if item.graph.index == 0x0024)
        state = instance.states[34]
        self.assertEqual(state.start, instance.start)
        self.assertTrue(state.node.field("m_C85E2CBA"))

        snapshot = observers.snapshot(script)
        observer = BodyStream(BODY)
        chunk = observer.full(observers.full_frame(script, snapshot), snapshot).chunk
        wire = read_chunk(chunk.getvalue(), chunk.count)
        self.assertFalse(wire.owner)
        self.assertEqual(wire.instances[instance.id].states[34], (True, {"fresh": False, "count": 1}))

    def test_death_in_the_spawn_frame_uses_the_world_freshness_gate(self):
        script = BodyScript(SOLDIER, BODY, START, ult_seconds=0)
        instance = next(item for item in script.component.instances.values() if item.graph.index == 0x01CF)
        known = {item: graph.index for item, graph in script.graphs().items()}
        initial, = script.spawn_frames()
        script.stream.arrived(initial.chunk_last)

        script.set_health(0, 200)
        params = {KILLER: expr.Entity(OTHER_BODY), CRIT: False, LOADOUT: None, ASSISTS: ()}
        runtime.send_message(script.component, DIED, params, OTHER_BODY)
        script.component.run_now()
        self.assertEqual(instance.states[108].payload(), {"fresh": False, "count": 1})

        update, = script.frames(kill_feed=True)
        wire = read_chunk(update.chunk.getvalue(), update.chunk.count, known)
        self.assertEqual(wire.instances[instance.id].states[108], (True, {"fresh": True, "count": 1}))

        script.command(START + 33, 0)
        replacement, = script.frames(kill_feed=False, replace_initial=True)
        wire = read_chunk(replacement.chunk.getvalue(), replacement.chunk.count, known)
        self.assertFalse(wire.correction)
        self.assertEqual(wire.cmfd, START + 33)
        self.assertEqual(wire.instances[instance.id].states[108], (True, {"fresh": True, "count": 1}))

    def test_old_observer_snapshot_can_clear_a_state_after_the_live_instance_is_gone(self):
        script = BodyScript(SOLDIER, BODY, START, ult_seconds=0)
        before = observers.snapshot(script)
        owner = next(item for item, (index, _) in before.instances.items() if index == 0x01CF)
        key = (owner, 108)
        before.states[key] = ("STU_B9898052", True, (("count", 1), ("fresh", True)))
        states = dict(before.states)
        del states[key]
        after = replace(before, states=states)
        stream = BodyStream(BODY)
        stream.base = before
        known = {item: index for item, (index, _) in before.instances.items()}
        live_graphs = {item: g for item, g in script.graphs().items() if item != owner}

        with patch.object(script, "graphs", return_value=live_graphs):
            frame, keys = observers.delta(script, stream, after)
        update = stream.delta(frame, after, keys, 0)
        wire = read_chunk(update.chunk.getvalue(), update.chunk.count, known)
        self.assertEqual(wire.instances[owner].states[108], (False, {}))

    def test_bot_death_in_its_spawn_tick_is_fresh_after_the_initial_frame(self):
        # Use the available body lifecycle graph to exercise the same pulser
        # semantics with a real bot stream, without the external NPC graph pack.
        definition = {"graphs": [{"graph": 0x004B}]}
        with patch.object(graph, "entity_body", return_value=definition):
            script = BotScript(0x0400000000001403, SOLDIER, BODY, START, PRACTICE_RANGE.mode_guid, 200)
        stream = BodyStream(BODY)
        full = stream.full(*script.initial)
        known = {item: item_graph.index for item, item_graph in script.graphs().items()}
        stream.arrived(full.chunk_last)

        params = {KILLER: expr.Entity(OTHER_BODY), CRIT: False, LOADOUT: None, ASSISTS: ()}
        snapshot = script.die(START, params, OTHER_BODY)
        frame, keys = observers.delta(script, stream, snapshot)
        chunk = stream.delta(frame, snapshot, keys, 0).chunk
        wire = read_chunk(chunk.getvalue(), chunk.count, known)
        feed = next(item for item in wire.instances.values() if item.graph == 0x01CF)
        self.assertFalse(wire.owner)
        self.assertEqual(feed.states[108], (True, {"fresh": True, "count": 1}))
