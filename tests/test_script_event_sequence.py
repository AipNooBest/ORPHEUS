"""Explicit event counter preserves the old iterator and FIFO/fault contracts."""

import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script import runtime
from ow174.game.script.graph import Graph


def empty_graph():
    return Graph({
        "index": 1, "header": {}, "fields": {}, "items": 0, "containers": {}, "nodes": [],
        "unknown_nodes": [], "states": [], "entries": [], "remote": [], "sync_vars": [],
    })


class EventSequenceTests(unittest.TestCase):
    def test_matches_count_without_a_machine_integer_limit(self):
        for start in (0, 37, 1 << 200):
            sequence, previous = runtime.EventSequence(start), itertools.count(start)
            self.assertIs(iter(sequence), sequence)
            for _ in range(1000):
                self.assertEqual(sequence.next_value, next(previous))
                self.assertEqual(next(sequence), sequence.next_value - 1)

    def test_observation_is_read_only_and_never_calls_next_or_reduce(self):
        sequence = runtime.EventSequence(12)
        with mock.patch.object(runtime.EventSequence, "__next__", side_effect=AssertionError("consumed")):
            self.assertEqual((sequence.next_value, sequence.next_value), (12, 12))
            self.assertEqual(repr(sequence), "EventSequence(next_value=12)")
        with self.assertRaises(AttributeError):
            sequence.next_value = 0
        for start in (-1, True, 1.0, None):
            with self.subTest(start=start), self.assertRaises(ValueError):
                runtime.EventSequence(start)

    def test_same_time_events_keep_fifo_including_events_queued_during_dispatch(self):
        component = runtime.Component(123)
        component.now = 100
        instance = component.create(empty_graph(), init=False)
        first = runtime.Event(99, 99, runtime.ENTRY)
        second = runtime.Event(100, 99, runtime.ENTRY)
        instance.post(first)
        instance.post(second)
        seen = []

        def dispatch(event):
            seen.append(event.seq)
            if event is first:
                instance.post(runtime.Event(0, 99, runtime.ENTRY))

        with mock.patch.object(instance, "dispatch", side_effect=dispatch):
            instance.drain()
        self.assertEqual((first.time, second.time), (100, 100))
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(component.seq.next_value, 3)

    def test_cross_instance_posts_share_sequence_and_delayed_queue_order(self):
        component = runtime.Component(123)
        first = component.create(empty_graph(), init=False)
        second = component.create(empty_graph(), init=False)
        first.post(runtime.Event(20, 99, runtime.ENTRY))
        second.post(runtime.Event(10, 99, runtime.ENTRY))
        first.post(runtime.Event(10, 99, runtime.ENTRY))
        first.post(runtime.Event(20, 99, runtime.ENTRY))
        self.assertEqual([(e.time, e.seq) for e in first.queue], [(10, 2), (20, 0), (20, 3)])
        self.assertEqual([(e.time, e.seq) for e in second.queue], [(10, 1)])
        self.assertEqual(component.seq.next_value, 4)

    def test_fault_does_not_reset_or_consume_the_counter(self):
        component = runtime.Component(123)
        instance = component.create(empty_graph(), init=False)
        instance.post(runtime.Event(0, 0, runtime.ENTRY))
        with self.assertRaises(runtime.ScriptBudgetError) as first:
            component.budget_exceeded("probe")
        self.assertEqual((component.seq.next_value, instance.queue), (1, []))
        with self.assertRaises(runtime.ScriptBudgetError) as second:
            component.run_now()
        self.assertIs(first.exception, second.exception)
        self.assertEqual(component.seq.next_value, 1)


if __name__ == "__main__":
    unittest.main()
