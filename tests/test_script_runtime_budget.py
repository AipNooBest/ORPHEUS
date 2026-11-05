"""Exhausted simulation budgets fail the whole body; they cannot produce successful frames or shots."""

import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.content import SOLDIER
from ow174.game.script import runtime
from ow174.game.script.driver import BodyScript
from ow174.game.script.graph import Graph
from ow174.game.script.runtime import ScriptBudgetError


def node(position, cls, *, state=None, **fields):
    return {
        "pos": position,
        "class": cls,
        "uid": position,
        "rid": position,
        "remote": -1,
        "state": state,
        "client_only": False,
        "server_only": False,
        "fields": {"m_2BBEEAB8": state is not None, "m_ADEB6E05": state is None, **fields},
    }


def plug(*links):
    return {"$": "STUStatescriptOutputPlug", "links": [list(item) for item in links]}


def setter(position, value, *links):
    return node(
        position,
        "STUStatescriptActionSetVar",
        m_out_Var={"$": "STUConfigVarDynamic", "m_identifier": 7, "m_60DB8F99": 0},
        m_value={"$": "STUConfigVarInt", "m_value": value},
        m_outPlug=plug(*links),
    )


def graph(nodes=(), entries=()):
    return Graph(
        {
            "index": 1,
            "header": {},
            "fields": {},
            "items": len(nodes),
            "containers": {},
            "nodes": list(nodes),
            "unknown_nodes": [],
            "states": [item["pos"] for item in nodes if item["state"] is not None],
            "entries": list(entries),
            "remote": [],
            "sync_vars": [],
        }
    )


def cross_events():
    """Two native instances exchange one event at a time, below each drain's local budget."""
    component = runtime.Component(123)
    first = component.create(graph(), init=False)
    second = component.create(graph(), init=False)
    first.initialized = second.initialized = True
    seen = []
    event_node = SimpleNamespace(cls="AuditCross")

    def cross(instance, _node, _caller):
        seen.append(instance.id)
        other = second if instance is first else first
        other.post(runtime.Event(component.now, 0, runtime.ENTRY, node=event_node))
        return True

    first.post(runtime.Event(0, 0, runtime.ENTRY, node=event_node))
    return component, seen, cross


class RuntimeBudgetTests(unittest.TestCase):
    def assert_failed(self, component):
        self.assertIsInstance(component.fault, ScriptBudgetError)
        self.assertEqual(component.shots, [])
        self.assertEqual(component.held, [])
        for instance in component.instances.values():
            self.assertTrue(instance.stopped)
            self.assertEqual(instance.queue, [])
            self.assertEqual(instance.deferred, [])
            self.assertFalse(any(state.active for state in instance.states.values()))

    def test_event_overflow_invalidates_a_real_active_rifle(self):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        body = BodyScript(SOLDIER, 0xA0000101, 1000)
        body.command(1001, 1)
        rifle = body.component.instances[11]
        self.assertTrue(rifle.state(10).active)
        self.assertTrue(body.component.shots)
        event_node = SimpleNamespace(cls="AuditCount")
        seen = []

        def count(_instance, _node, _caller):
            seen.append(True)
            return True

        with (
            mock.patch.object(runtime, "EVENTS_PER_TIME", 8),
            mock.patch.dict(runtime.HANDLERS, {"AuditCount": count}),
        ):
            for _ in range(10):
                rifle.post(runtime.Event(body.component.now, 0, runtime.ENTRY, node=event_node))
            with self.assertRaisesRegex(ScriptBudgetError, "event budget 8"):
                body.command(1002, 0)
        self.assertEqual(body.frame, 1001)  # the driver did not commit the failed command
        self.assertEqual(len(seen), 8)
        self.assert_failed(body.component)
        for callback in (
            lambda: body.command(1002, 0),
            lambda: body.component.run_frame(1002),
            body.component.run_now,
            body.snapshot,
            body.frames,
            body.spawn_frames,
            body._report_shots,
        ):
            with self.subTest(callback=callback), self.assertRaises(ScriptBudgetError):
                callback()
            self.assertEqual(body.component.shots, [])

    def test_run_now_cannot_succeed_with_same_time_events_left(self):
        component, seen, cross = cross_events()
        with (
            mock.patch.object(runtime, "MAX_COMPONENT_UPDATES", 3),
            mock.patch.dict(runtime.HANDLERS, {"AuditCross": cross}),
            self.assertRaisesRegex(ScriptBudgetError, "current-time update budget 3"),
        ):
            component.run_now()
        self.assertEqual(len(seen), 6)
        self.assert_failed(component)

    def test_run_frame_cannot_advance_to_its_end_after_exhaustion(self):
        component, seen, cross = cross_events()
        with (
            mock.patch.object(runtime, "MAX_COMPONENT_UPDATES", 3),
            mock.patch.dict(runtime.HANDLERS, {"AuditCross": cross}),
            self.assertRaisesRegex(ScriptBudgetError, "frame update budget 3"),
        ):
            component.run_frame(1)
        self.assertEqual(len(seen), 6)
        self.assertEqual(component.now, 16)  # not the unprocessed window's end, 31
        self.assert_failed(component)

    def test_work_budget_is_shared_across_instances(self):
        component, seen, cross = cross_events()
        with (
            mock.patch.object(runtime, "MAX_SCRIPT_WORK", 12),
            mock.patch.dict(runtime.HANDLERS, {"AuditCross": cross}),
            self.assertRaisesRegex(ScriptBudgetError, "script work budget 12"),
        ):
            component.run_now()
        self.assertLess(len(seen), 10)
        self.assert_failed(component)

    def test_deferred_update_exhaustion_is_not_a_success(self):
        component = runtime.Component(123)
        instance = component.create(graph([node(0, "AuditDefer", state=0)]), init=False)
        instance.initialized = True

        class Requeue(runtime.State):
            def timer(self, _param):
                self.instance.deferred.append((0.0, 0, self))

        state = Requeue(instance, instance.graph.node(0), 0)
        state.active = True
        instance.states[0] = state
        instance.deferred.append((0.0, 0, state))
        with (
            mock.patch.object(runtime, "MAX_INSTANCE_UPDATES", 3),
            self.assertRaisesRegex(ScriptBudgetError, "instance 1 update budget 3"),
        ):
            instance.update()
        self.assert_failed(component)

    def test_immediate_action_cycle_hits_our_depth_limit_before_python_recursion(self):
        component = runtime.Component(123)
        instance = component.create(graph([setter(0, 1, (0, "m_inPlug"))]), init=False)
        with self.assertRaisesRegex(ScriptBudgetError, f"plug depth budget {runtime.MAX_FOLLOW_DEPTH}"):
            instance.run_node(instance.graph.node(0), None)
        self.assert_failed(component)

    def test_supported_actions_keep_link_order(self):
        component = runtime.Component(123)
        instance = component.create(
            graph(
                [
                    node(0, "STUStatescriptEntry", m_outPlug=plug((1, "m_inPlug"), (3, "m_inPlug"))),
                    setter(1, 1, (2, "m_inPlug")),
                    setter(2, 2),
                    setter(3, 3),
                ],
                entries=[0],
            )
        )
        component.run_frame(0)
        self.assertIsNone(component.fault)
        self.assertEqual(instance.vars[7].value(), 3)

    def test_state_begins_remain_queued_while_actions_run_immediately(self):
        component = runtime.Component(123)
        instance = component.create(
            graph(
                [
                    node(0, "STUStatescriptEntry", m_outPlug=plug((1, "m_beginPlug"), (2, "m_inPlug"))),
                    node(1, "STUStatescriptState", state=0, m_onBeginPlug=plug((3, "m_inPlug"))),
                    setter(2, 2),
                    setter(3, 3),
                ],
                entries=[0],
            )
        )
        component.run_frame(0)
        self.assertIsNone(component.fault)
        self.assertTrue(instance.state(0).active)
        self.assertEqual(instance.vars[7].value(), 3)


if __name__ == "__main__":
    unittest.main()
