"""Stored server replay of a fixed world MoveTo through encrypted commands."""

import unittest
from unittest.mock import patch

import test_server_journal as journal_tests
from test_game_graph_analysis import make_graph, node, output

from ow174.game.content import SOLDIER
from ow174.game.script import graph
from ow174.game.server_journal import _ReplaySession


class MoveToReplayTests(unittest.TestCase):
    def test_fixed_world_move_replays_active_state_and_arrival(self):
        shift = 0x08  # Command bit for logical button 35.
        target = (0.0, 0.0, 1.5)
        definition = make_graph(
            0xF108,
            [
                node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
                node(
                    1,
                    "STUStatescriptStateLogicalButton",
                    state=0,
                    fields={
                        "m_logicalButton": 35,
                        "m_onGoingDownPlug": output([2, "m_beginPlug"]),
                    },
                ),
                node(
                    2,
                    "STUStatescriptStateMoveTo",
                    state=1,
                    fields={
                        "m_B060F688": 0,
                        "m_865F3D01": {
                            "$": "STUConfigVarVec3",
                            "m_value": dict(zip("XYZ", target, strict=True)),
                        },
                        "m_speed": 10.0,
                    },
                ),
            ],
            states=[1, 2],
            entries=[0],
        )
        definition.header["m_statesBitCount"] = 1  # the two states also travel in the owner stream
        definition.fields["m_680A2CB2"] = [0]  # Receive logical button edges in state 0.
        bodies = graph.bodies()
        body_key, body = next((key, item) for key, item in bodies.items() if int(item["hero"], 16) == SOLDIER)
        health = next(item for item in body["graphs"] if item["graph"] == 0x0033)
        bodies[body_key] = dict(body, graphs=[health, {"graph": definition.index}], manager=None, weapons=[])
        load = graph.graph

        def load_graph(index):
            return definition if index == definition.index else load(index)

        def move_state(player):
            component = player.body_script.component
            instance = next(
                item for item in component.instances.values() if item.graph.index == definition.index
            )
            return instance.states[1]

        with (
            patch.object(graph, "bodies", return_value=bodies),
            patch.object(graph, "graph", side_effect=load_graph),
        ):
            fixture = journal_tests.ServerJournalTests()
            session, sent = fixture.make(verify_stored_state=True)
            (peer,), (player,) = fixture.connected(session, sent, two=False)
            first = player.body_script.frame + 1

            peer.commands(first, 1, shift, now=101.0)
            self.assertEqual(player.mover.position[2], 0.0)
            state = move_state(player)
            self.assertTrue(state.active)
            peer.commands(first + 1, 3, 0, now=101.048)
            self.assertTrue(state.active)
            self.assertGreater(player.mover.position[2], 0.0)
            self.assertLess(player.mover.position[2], target[2])
            peer.commands(first + 4, 12, 0, now=101.240)
            self.assertFalse(state.active)
            self.assertAlmostEqual(player.mover.position[2], target[2], delta=1 / 1024)

            replay = _ReplaySession(session.finish(), session.registry.resolve)
            result = replay.run()

        self.assertTrue(result["stored_state_verified"])
        (restored,) = replay.server.matches[0].players
        self.assertEqual(restored.mover.history, player.mover.history)
        self.assertEqual(restored.mover.position, player.mover.position)
        replayed = move_state(restored)
        self.assertEqual((replayed.active, replayed.counter), (False, state.counter))


if __name__ == "__main__":
    unittest.main()
