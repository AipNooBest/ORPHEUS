"""First-pulse freshness in the target build's owner and observer statescript frames."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import observers
from ow174.game.combat import ASSISTS, CRIT, DIED, KILLER, LOADOUT
from ow174.game.content import SOLDIER
from ow174.game.script import expr, runtime
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
