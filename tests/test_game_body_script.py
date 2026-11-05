"""Body creation/delivery gates and the boundary between diagnostics and wire state."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ow174.game.commands import Command
from ow174.game.content import SOLDIER, heroes
from ow174.game.script.body import BodyScript
from ow174.game.script.graph import graph
from ow174.game.script.research_runtime import RuntimeValue


class SupportedRuntime:
    """A complete zero-state graph fixture; this is not a Soldier weapon model."""

    def __init__(self, *args, **kwargs):
        self.frame = None
        self.complete = True
        self.entity_vars = {123: RuntimeValue("int", 5)}
        self.instances = [SimpleNamespace(
            index=1, graph=graph(0x288D), parent=None, variables={}, active=set(), children={},
        )]
        self.diagnostics = []

    def initialize(self, frame):
        self.frame = frame

    def advance(self, command):
        self.frame = command.frame

    def snapshot(self):
        return {"complete": self.complete, "diagnostics": self.diagnostics}


class BodyScriptTests(unittest.TestCase):
    def fixture(self, mode="experimental"):
        with patch("ow174.game.script.body.BodyRuntime", SupportedRuntime):
            return BodyScript(0xA0000101, 1, 16000, generation=1, mode=mode, frame=100)

    def test_data_waits_for_the_body_create_datagram_to_arrive(self):
        body = self.fixture()
        self.assertIsNone(body.pending(1.0))
        self.assertEqual(body.stream.last, 0)
        body.on_created()
        update = body.pending(1.0)
        self.assertTrue(update.resend)
        self.assertIs(update.stream, body.stream)
        self.assertEqual(body.stream.cmfd, 100)

    def test_ack_cannot_cover_unreceived_initial_data(self):
        body = self.fixture()
        body.on_created()
        initial = body.pending(1.0)
        body.advance(Command(101))
        self.assertIsNone(body.pending(2.0))
        body.stream.arrived(initial.chunk_last)
        ack = body.pending(2.0)
        self.assertFalse(ack.resend)
        self.assertEqual(body.stream.cmfd, 101)
        self.assertIsNone(body.pending(3.0))  # never invent a later processed input frame

    def test_changed_data_is_a_reliable_full_snapshot(self):
        body = self.fixture()
        body.on_created()
        first = body.pending(1.0)
        body.stream.arrived(first.chunk_last)
        body.advance(Command(101))
        body.runtime.entity_vars[123] = RuntimeValue("int", 4)
        second = body.pending(2.0)
        self.assertTrue(second.resend)
        self.assertGreater(second.chunk_last, first.chunk_last)

    def test_diagnostic_mode_never_emits_owner_frames(self):
        body = self.fixture("diagnostic")
        body.on_created()
        self.assertIsNone(body.pending(1.0))
        self.assertFalse(body.snapshot()["wire_data_sent"])

    def test_real_incomplete_soldier_initialization_is_not_sent(self):
        body = BodyScript(
            0xA0000101, heroes()[SOLDIER].body, 16000,
            generation=1, mode="experimental", frame=100,
        )
        body.on_created()
        self.assertIsNone(body.pending(1.0))
        self.assertFalse(body.runtime.complete)
        self.assertIn("unresolved", body.error)
        self.assertFalse(body.snapshot()["wire_data_sent"])


if __name__ == "__main__":
    unittest.main()
