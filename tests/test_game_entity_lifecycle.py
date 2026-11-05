"""Reliable body records keep their create snapshot and cannot cross a local entity lifecycle."""

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.bits import BitWriter
from ow174.game.content import SOLDIER, heroes
from ow174.game.environment import SystemEnvironment
from ow174.game.match import Match
from ow174.game.observers import Observers
from ow174.game.script.driver import BodyScript
from ow174.game.server import Client
from ow174.game.world import OP_CREATE, OP_DESTROY, EntityUpdate, Movement


def client() -> Client:
    server = SimpleNamespace(environment=SystemEnvironment())
    player = SimpleNamespace(name="Test", match=SimpleNamespace(bots=[]))
    return Client(server, ("127.0.0.1", 1), 1, None, player)


def sent(connection, sequence, records, reliable=()):
    connection.in_flight[sequence] = (list(reliable), list(records))
    connection.entities = [item for item in connection.entities if item not in records]


def record(entity, op=0):
    result = EntityUpdate(entity, op, chunk=BitWriter() if not op else None)
    result.stream = Mock()
    result.chunk_last = 3
    return result


class FrozenCreateTests(unittest.TestCase):
    def owner(self):
        owner = SimpleNamespace(
            body=0xA0000101, hero=heroes()[SOLDIER], team_bit=2, body_script=None,
            mover=SimpleNamespace(position=[0.0, 1.0, 2.0], yaw=0), skin=(0, False),
        )
        owner.pose = lambda: Movement(owner.mover.position, yaw=owner.mover.yaw)
        return owner

    @staticmethod
    def capture(origin, definition, components, **kwargs):
        if kwargs.get("movement") is not None:
            kwargs["movement"] = vars(kwargs["movement"])
        return origin, definition, copy.deepcopy(components), copy.deepcopy(kwargs)

    def mutate(self, owner):
        owner.hero = next(hero for hero in heroes().values() if hero.guid != SOLDIER)
        owner.body += 1
        owner.team_bit = 4
        owner.skin = (0x0A50000000001234, True)
        owner.mover.position[0] = 99.0
        owner.mover.yaw = 12000

    def test_own_create_freezes_hero_skin_pose_components_and_the_new_scripts_lease(self):
        match, owner = Match.__new__(Match), self.owner()
        lease = [[0x0400000000000001, 100, 200, 0x10000, 1]]
        callback = Mock()
        new_script = object()

        def start(player, create):
            player.body_script = new_script
            create.stream = callback

        def leased(player):
            self.assertIs(player.body_script, new_script)
            return lease

        match.combat = SimpleNamespace(projectiles=SimpleNamespace(lease=leased))
        with patch.object(match, "start_body_script", side_effect=start):
            create = match.own_body_updates(owner)[0]
        self.assertIs(create.stream, callback)
        with patch("ow174.game.match.world.create", side_effect=self.capture):
            original = create.build(0)
            self.assertEqual(original[2][90], [lease])
            self.mutate(owner)
            lease[0][1] = 9999
            self.assertEqual(create.build(0), original)
        self.assertEqual(create.entity, 0xA0000101)
        self.assertEqual(create.movement.position, (0, 1, 2))

    def test_remote_create_freezes_data_and_keeps_the_observer_arrival_gate(self):
        match, owner = Match.__new__(Match), self.owner()
        viewer = SimpleNamespace(slot=2, seen={})
        observers = Observers(SimpleNamespace())
        match.combat = SimpleNamespace(observers=observers)
        create = match.remote_body_create(viewer, owner)
        with patch("ow174.game.match.world.create", side_effect=self.capture):
            original = create.build(0)
            self.mutate(owner)
            self.assertEqual(create.build(0), original)
        self.assertNotIn(90, original[2])
        watch = observers.watches[(viewer.slot, create.entity)]
        self.assertFalse(watch.arrived)
        connection = client()
        connection.queue_entities([create])
        sent(connection, 0, [create])
        connection.acked(1, 1)
        self.assertTrue(watch.arrived)


class ReliableLifecycleTests(unittest.TestCase):
    def test_record_objects_shared_by_clients_keep_independent_delivery_generations(self):
        first, second = client(), client()
        first.queue_entities([record(42, OP_CREATE)])
        first.queue_entities([EntityUpdate(42, OP_DESTROY)])
        shared = record(42, OP_CREATE)
        first.queue_entities([shared])
        second.queue_entities([shared])
        self.assertEqual(first.entities, [shared])
        self.assertEqual(second.entities, [shared])
        sent(first, 0, [shared])
        sent(second, 0, [shared])
        first.queue_entities([EntityUpdate(42, OP_DESTROY)])
        first.acked(1, 1)
        shared.stream.arrived.assert_not_called()
        second.acked(1, 1)
        shared.stream.arrived.assert_called_once_with(shared.chunk_last)

    def test_destroy_discards_unsent_create_and_state_records(self):
        connection = client()
        create, chunk = record(42, OP_CREATE), record(42)
        connection.queue_entities([create, chunk])
        destroy = EntityUpdate(42, OP_DESTROY)
        connection.queue_entities([destroy])
        self.assertEqual(connection.entities, [destroy])
        connection.queue_entities([record(42)])
        self.assertEqual(connection.entities, [destroy])

    def test_destroy_does_not_resend_old_inflight_create_or_state_after_packet_loss(self):
        connection = client()
        create, chunk = record(42, OP_CREATE), record(42)
        connection.queue_entities([create, chunk])
        sent(connection, 0, [create, chunk], reliable=[(5, b"keep this message")])
        destroy = EntityUpdate(42, OP_DESTROY)
        connection.queue_entities([destroy])
        sent(connection, 1, [destroy])
        connection.acked(2, 0b1)  # destroy arrived, older create/chunk packet was lost
        self.assertEqual(connection.entities, [])
        self.assertEqual(connection.reliable, [(5, b"keep this message")])
        create.stream.arrived.assert_not_called()
        chunk.stream.arrived.assert_not_called()

    def test_late_positive_ack_cannot_activate_the_old_script_after_destroy(self):
        connection = client()
        create, chunk = record(42, OP_CREATE), record(42)
        connection.queue_entities([create, chunk])
        sent(connection, 0, [create, chunk])
        connection.queue_entities([EntityUpdate(42, OP_DESTROY)])
        connection.acked(1, 1)
        create.stream.arrived.assert_not_called()
        chunk.stream.arrived.assert_not_called()

    def test_reused_id_cannot_be_destroyed_or_activated_by_the_old_generation(self):
        connection = client()
        old, new = record(42, OP_CREATE), record(42, OP_CREATE)
        connection.queue_entities([old])
        sent(connection, 0, [old])
        destroy = EntityUpdate(42, OP_DESTROY)
        connection.queue_entities([destroy])
        sent(connection, 1, [destroy])
        connection.queue_entities([new])
        sent(connection, 2, [new])
        connection.acked(3, 0b101)  # old/new creates arrived, obsolete destroy was lost
        self.assertEqual(connection.entities, [])
        old.stream.arrived.assert_not_called()
        new.stream.arrived.assert_called_once_with(new.chunk_last)
        connection.queue_entities([old])  # retrying an existing old record cannot give it a new generation
        self.assertEqual(connection.entities, [])

    def test_current_generation_loss_still_resends_and_acks_other_entities_normally(self):
        connection = client()
        old, current = record(42, OP_CREATE), record(43, OP_CREATE)
        connection.queue_entities([old, current])
        sent(connection, 0, [old])
        sent(connection, 1, [current])
        connection.queue_entities([EntityUpdate(42, OP_DESTROY)])
        connection.acked(2, 0b10)  # old create arrived but retired; current create was lost
        self.assertIn(current, connection.entities)
        old.stream.arrived.assert_not_called()
        sent(connection, 2, [current])
        connection.acked(3, 1)
        current.stream.arrived.assert_called_once_with(current.chunk_last)

    def test_native_body_create_ack_still_unlocks_the_initial_reliable_full_frame(self):
        script = BodyScript(SOLDIER, 0xA0000101, 100)
        create = EntityUpdate(script.entity, OP_CREATE)
        script.created(create)
        connection = client()
        connection.queue_entities([create])
        self.assertEqual(script.frames(), [])
        sent(connection, 0, [create])
        connection.acked(1, 1)
        self.assertTrue(script.create_arrived)
        frames = script.frames()
        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].resend)
        self.assertIs(frames[0].stream, script.stream)


if __name__ == "__main__":
    unittest.main()
