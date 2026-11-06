"""Reliable body records keep their create snapshot and cannot cross a local entity lifecycle."""

import copy
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_game_packet_budget import connection, decoded
from test_game_repeated_lifecycles import wire_frame
from test_game_server import REAPER, pick

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import heroselect, messages, world
from ow174.game.bits import BitWriter
from ow174.game.commands import FIRE, Command
from ow174.game.content import SOLDIER, heroes
from ow174.game.environment import SystemEnvironment
from ow174.game.link import ACK, KIND_CONTROL, KIND_FRAME, LinkCipher, Packet, ReceiveWindow
from ow174.game.match import Match, card_message
from ow174.game.observers import Observers
from ow174.game.script import decode, expr
from ow174.game.script.driver import BodyScript
from ow174.game.server import MAX_PAYLOAD, Client
from ow174.game.world import OP_CREATE, OP_DESTROY, OP_UPDATE, EntityUpdate, Movement


def client() -> Client:
    server = SimpleNamespace(environment=SystemEnvironment())
    player = SimpleNamespace(name="Test", entity=0xA0000100, match=SimpleNamespace(bots=[], players=[]))
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

        match.combat = SimpleNamespace(vitals={}, projectiles=SimpleNamespace(lease=leased))
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
        match.combat = SimpleNamespace(vitals={}, observers=observers)
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
    def test_old_controller_create_ack_does_not_unlock_its_replacement(self):
        connection = client()
        controller = connection.player.entity
        old, current = record(controller, OP_CREATE), record(controller, OP_CREATE)
        connection.queue_entities([old])
        sent(connection, 0, [old])
        connection.queue_entities([current])
        connection.acked(1, 1)
        self.assertFalse(connection.controller_ready)
        sent(connection, 1, [current])
        connection.acked(2, 1)
        self.assertTrue(connection.controller_ready)
        connection.queue_entities([record(controller, OP_DESTROY)])
        self.assertFalse(connection.controller_ready)

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


class PossessionDeliveryTests(unittest.TestCase):
    def queue_split_budget(self, peer, first, dependent):
        actor = peer.player
        card = card_message(actor)
        card["+0x80"] = dict(card["+0x80"])

        def card_at(length):
            card["+0x80"]["+0x40"] = "A" * length
            return [(peer.next_reliable, messages.encode(20308, card))]

        def size(reliable, entities):
            return len(peer._payload(actor.match.tick + 1, reliable, [], entities))

        low, high = 1, MAX_PAYLOAD
        while low < high:
            middle = (low + high + 1) // 2
            if size(card_at(middle), [first]) <= MAX_PAYLOAD:
                low = middle
            else:
                high = middle - 1
        reliable = card_at(low)
        self.assertLessEqual(size(reliable, [first]), MAX_PAYLOAD)
        self.assertGreater(size(reliable, [first, dependent]), MAX_PAYLOAD)
        peer.queue_reliable(20308, card)

    def test_preselected_body_must_create_before_initial_controller_with_possession(self):
        for split, shared_retry in ((False, False), (True, False), (True, True)):
            with self.subTest(split=split, shared_retry=shared_retry):
                self.check_initial_possession(split=split, shared_retry=shared_retry)

    def check_initial_possession(self, *, split, shared_retry):
        server, peer, actor, cipher = connection()
        match = actor.match
        match.tick = 100
        match.game_map = replace(match.game_map, placeables=())
        window = ReceiveWindow()
        sequence = 1

        def emit():
            match.tick += 1
            peer.send_frame(match.tick, 10.0)
            packet = decoded(self, cipher, server.sock.sent[-1][0])[0]
            return packet, wire_frame(packet)[1]

        def acknowledge(packet):
            nonlocal sequence
            self.assertTrue(window.accept(packet.seq))
            ack = Packet(peer.conn, KIND_CONTROL, sequence, window.expected, window.bits, ACK)
            sequence += 1
            server._datagram(cipher.seal(ack), peer.address, 10.0)

        def creates(records):
            return [(item.entity, fields) for item, fields in records if item.op == OP_CREATE]

        def assert_possessed(fields):
            self.assertEqual(fields[44], {0: actor.body, 1: actor.body, 2: actor.body})
            self.assertEqual(fields[75], {0: SOLDIER, 3: SOLDIER})

        payload = BitWriter()
        messages.write_channel(payload, [(0, messages.encode(21616, pick(actor.entity, SOLDIER)))])
        request = Packet(peer.conn, KIND_FRAME, 0, 0, 0, ACK, payload.getvalue())
        server._datagram(cipher.seal(request), peer.address, 10.0)
        self.assertTrue(actor.has_body)
        self.assertFalse(actor.spawned)
        updates = match.spawn_updates(actor)
        peer.queue_entities(updates)
        actor.spawned = True
        if split:
            self.queue_split_budget(peer, updates[0], updates[1])

        packet, records = emit()
        if not split:
            self.assertEqual([entity for entity, _ in creates(records)],
                             [actor.body, actor.entity, world.GAME_MODE_ENTITY])
            assert_possessed(creates(records)[1][1])
            return

        self.assertEqual([entity for entity, _ in creates(records)], [actor.body])
        # Drop the body datagram. The separate mode create can arrive and be ACKed.
        packet, records = emit()
        self.assertEqual([entity for entity, _ in creates(records)], [world.GAME_MODE_ENTITY])
        acknowledge(packet)
        retry, records = emit()
        self.assertEqual([entity for entity, _ in creates(records)], [actor.body])
        self.assertFalse(actor.body_script.create_arrived)
        acknowledge(retry)
        self.assertTrue(actor.body_script.create_arrived)
        # Drop the controller create, which carries the first 44/75 binding.
        _, records = emit()
        (entity, fields), = creates(records)
        self.assertEqual(entity, actor.entity)
        assert_possessed(fields)
        self.assertNotIn(actor.entity, peer._pending_possessions)

        model = decode.ClientBody.for_hero(SOLDIER)
        match._body_frames(actor, peer)
        tree_packet, records = emit()
        (tree,) = [item for item, _ in records if item.entity == actor.body and item.chunk]
        self.assertTrue(model.apply(tree).full)
        acknowledge(tree_packet)
        script = actor.body_script
        self.assertIsNotNone(script.stream.base)
        self.assertFalse(peer.controller_ready)

        # Send the controller retry alone and leave its outcome unknown while
        # the owner stream has a complete H0 base and its first H1 is queued.
        match._body_frames(actor, peer)
        controller, owner = peer.entities
        self.assertEqual((controller.entity, controller.op), (actor.entity, OP_CREATE))
        self.assertIs(owner.stream, script.stream)
        if not shared_retry:
            self.queue_split_budget(peer, controller, owner)
        controller_retry, records = emit()
        (entity, fields), = creates(records)
        self.assertEqual(entity, actor.entity)
        assert_possessed(fields)
        if shared_retry:
            controller_position = next(index for index, (item, _) in enumerate(records)
                                       if item.entity == actor.entity and item.op == OP_CREATE)
            owner_position = next(index for index, (item, _) in enumerate(records)
                                  if item.entity == actor.body and item.chunk)
            self.assertLess(controller_position, owner_position)
        else:
            self.assertFalse(any(item.entity == actor.body and item.chunk for item, _ in records))
            _, records = emit()
            self.assertFalse(peer.controller_ready)
            self.assertFalse(any(item.entity == actor.body and item.chunk for item, _ in records))
            self.assertEqual(script.report_count, 0)

        acknowledge(controller_retry)
        self.assertTrue(peer.controller_ready)
        if not shared_retry:
            _, records = emit()
        (initial,) = [item for item, _ in records if item.entity == actor.body and item.chunk]
        frame = model.apply(initial)
        self.assertTrue(frame.owner)
        self.assertFalse(frame.correction)
        self.assertEqual(frame.cmfd, script.frame)
        self.assertEqual(model.vars[(11, 53)], 30)
        self.assertIn((11, 70), model.on())

    def test_own_possession_shares_body_create_or_waits_for_the_exact_create_ack(self):
        server, peer, actor, cipher = connection()
        match = actor.match
        match.tick, actor.spawned = 100, True
        window = ReceiveWindow()
        sequence = 0

        def emit():
            match.tick += 1
            peer.send_frame(match.tick, 10.0)
            packet = decoded(self, cipher, server.sock.sent[-1][0])[0]
            ids, records = wire_frame(packet)
            return packet, ids, records

        def acknowledge(packet):
            nonlocal sequence
            self.assertTrue(window.accept(packet.seq))
            ack = Packet(peer.conn, KIND_CONTROL, sequence, window.expected, window.bits, ACK)
            sequence += 1
            server._datagram(cipher.seal(ack), peer.address, 10.0)

        def relevant(records, old=None):
            return [
                (item.entity, item.op, fields.get(44), fields.get(75))
                for item, fields in records
                if (item.entity == actor.body and item.op == OP_CREATE)
                or (item.entity == actor.entity and (44 in fields or 75 in fields))
                or (item.entity == old and item.op == OP_DESTROY)
            ]

        def expected(hero):
            body = actor.body
            return (actor.entity, OP_UPDATE, {0: body, 1: body, 2: body}, {0: hero, 3: hero})

        def controller_frames(records):
            return [
                (position, decode.read_chunk(item.chunk.getvalue(), item.chunk.count))
                for position, (item, _) in enumerate(records) if item.entity == actor.entity and item.chunk
            ]

        def body_chunks(records):
            return [item.chunk for item, _ in records if item.entity == actor.body and item.chunk]

        def assert_menu(records, hero, opened):
            (position, frame), = controller_frames(records)
            self.assertTrue(frame.full and frame.owner)
            self.assertEqual(frame.entity_vars[heroselect.OPEN_SELECT], (opened, ()))
            self.assertEqual(frame.entity_vars[heroselect.SELECT_SHOWN], (opened, ()))
            self.assertEqual(frame.entity_vars[heroselect.CURRENT_HERO], (expr.Asset(hero), ()))
            return position

        peer.queue_entities([
            EntityUpdate(actor.entity, OP_CREATE, lambda origin: match._player_create(origin, actor)),
        ])
        packet, _, _ = emit()
        acknowledge(packet)
        for hero in (SOLDIER, REAPER):
            old = actor.body if actor.has_body else None
            self.assertTrue(match.switch_hero(actor, heroes()[hero]))
            actor.select_open = False
            match.send_controller(actor)
            packet, _, records = emit()
            ordered = [(actor.body, OP_CREATE, None, None), expected(hero)]
            if old is not None:
                ordered.append((old, OP_DESTROY, None, None))
            self.assertEqual(relevant(records, old), ordered)
            binding = next(index for index, (_, fields) in enumerate(records) if 44 in fields)
            self.assertGreater(assert_menu(records, hero, False), binding)
            acknowledge(packet)

        old = actor.body
        self.assertTrue(match.switch_hero(actor, heroes()[SOLDIER]))
        match.send_controller(actor)
        create = next(item for item in peer.entities if item.entity == actor.body and item.op == OP_CREATE)
        components = match._possession(actor)
        possession = EntityUpdate(actor.entity, OP_UPDATE, lambda origin: world.update(origin, components))
        self.queue_split_budget(peer, create, possession)
        _, _, records = emit()  # drop the actual budget-split create datagram
        self.assertEqual(relevant(records, old), [(actor.body, OP_CREATE, None, None)])
        self.assertEqual(controller_frames(records), [])

        peer.queue_reliable(20308, card_message(actor))
        packet, ids, records = emit()
        self.assertIn(20308, ids)
        self.assertEqual(relevant(records, old), [])
        self.assertEqual(controller_frames(records), [])
        acknowledge(packet)  # confirms another packet and reports the create missing
        retry, _, records = emit()
        self.assertEqual(relevant(records, old), [(actor.body, OP_CREATE, None, None)])
        self.assertEqual(controller_frames(records), [])
        self.assertTrue(window.accept(retry.seq))
        self.assertFalse(actor.body_script.create_arrived)
        packet, _, records = emit()
        self.assertEqual(relevant(records, old), [])
        self.assertEqual(controller_frames(records), [])
        acknowledge(packet)  # includes a positive ACK for the exact create retry
        self.assertTrue(actor.body_script.create_arrived)

        # Now lose the binding itself under a second budget split. The close also
        # waits if its prerequisite was only selected for an earlier datagram.
        _, binding = peer._pending_possessions[actor.entity]
        (close,) = [item for item in peer.entities if item.stream is actor.script]
        script = actor.body_script
        match._body_frames(actor, peer)
        self.queue_split_budget(peer, binding, close)
        _, _, records = emit()
        self.assertEqual(relevant(records, old), [expected(SOLDIER)])
        self.assertEqual(controller_frames(records), [])
        self.assertEqual(body_chunks(records), [])
        packet, _, records = emit()
        self.assertEqual(relevant(records, old), [])
        self.assertEqual(controller_frames(records), [])
        (tree,) = body_chunks(records)
        frame = decode.read_chunk(tree.getvalue(), tree.count)
        self.assertTrue(frame.full)
        self.assertFalse(frame.owner)  # H0 can reach the existing body before binding
        known = {number: item.graph for number, item in frame.instances.items()}
        acknowledge(packet)
        self.assertEqual(script.stream.base_last, 1)

        command = script.frame + 1
        actor.take_commands([Command(command, buttons=FIRE)])
        self.assertEqual(script.component.instances[11].vars[53].value(), 29)
        match._body_frames(actor, peer)
        _, _, records = emit()  # lose the saturated binding retry too
        self.assertEqual(relevant(records, old), [expected(SOLDIER)])
        self.assertEqual(body_chunks(records), [])

        first = script.stream.initial_owner
        actor.take_commands([Command(command + 1, buttons=FIRE)])
        match._body_frames(actor, peer)
        self.assertIsNot(script.stream.initial_owner, first)
        self.assertNotIn(first, peer.entities)
        self.assertEqual(tuple(script.stream.pending), (script.stream.initial_owner.chunk_last,))
        packet, _, records = emit()
        self.assertEqual(relevant(records, old), [])
        self.assertEqual(controller_frames(records), [])
        self.assertEqual(body_chunks(records), [])
        self.assertEqual(script.report_count, 0)
        acknowledge(packet)
        packet, _, records = emit()
        self.assertEqual(relevant(records, old), [expected(SOLDIER)])
        self.assertEqual(controller_frames(records), [])
        self.assertEqual(body_chunks(records), [])
        acknowledge(packet)
        packet, _, records = emit()
        self.assertEqual(relevant(records, old), [(old, OP_DESTROY, None, None)])
        assert_menu(records, SOLDIER, False)
        (initial,) = body_chunks(records)
        frame = decode.read_chunk(initial.getvalue(), initial.count, known)
        self.assertEqual((frame.owner, frame.correction, frame.cmfd), (True, False, command + 1))
        self.assertTrue(frame.instances[11].states[70][0])  # authored ammo UXPresenter
        self.assertEqual(script.report_count, 1)
        acknowledge(packet)
        _, _, records = emit()
        self.assertEqual(relevant(records, old), [])

        # Reopening must reach the peer even while the selected body is absent.
        self.assertTrue(match.switch_hero(actor, heroes()[REAPER]))
        match.send_controller(actor)
        intermediate = actor.body
        create = next(item for item in peer.entities if item.entity == intermediate and item.op == OP_CREATE)
        _, binding = peer._pending_possessions[actor.entity]
        self.queue_split_budget(peer, create, binding)
        _, _, records = emit()
        self.assertEqual(relevant(records), [(intermediate, OP_CREATE, None, None)])
        actor.select_open = True
        match.send_controller(actor)
        packet, _, records = emit()
        assert_menu(records, REAPER, True)
        acknowledge(packet)
        self.assertFalse(peer.bot_create_acked(intermediate))

        # A later pick replaces the open menu only after its own new binding.
        self.assertTrue(match.switch_hero(actor, heroes()[SOLDIER]))
        actor.select_open = False
        match.send_controller(actor)
        binding_arrived = False
        for _ in range(4):
            packet, _, records = emit()
            self.assertFalse(any(item.entity == intermediate and item.op == OP_CREATE for item, _ in records))
            binding = next((index for index, (_, fields) in enumerate(records) if 44 in fields), None)
            if controller_frames(records):
                position = assert_menu(records, SOLDIER, False)
                self.assertTrue(binding_arrived or (binding is not None and binding < position))
                break
            acknowledge(packet)
            binding_arrived |= binding is not None
        else:
            self.fail("the latest selection did not close")

        # The close above was sent but not received. Positively acknowledging
        # a newer reopen reports that old packet lost without retrying its close.
        actor.select_open = True
        match.send_controller(actor)
        packet, _, records = emit()
        assert_menu(records, SOLDIER, True)
        acknowledge(packet)
        self.assertEqual(actor.script.delivered, actor.script.data_last)
        packet, _, records = emit()
        self.assertEqual(controller_frames(records), [])
        if not binding_arrived:
            self.assertIn(expected(SOLDIER), relevant(records))
        acknowledge(packet)

    def check_superseded_possession(self, *, own, lose_current=False):
        server, observer_client, observer, observer_cipher = connection()
        match = observer.match
        actor = match.add_player(2, "Target", SOLDIER, 1, False)
        key_in, key_out = b"p" * 32, b"q" * 32
        actor_client = Client(server, ("memory.test", 40002), 2, LinkCipher(key_in, key_out), actor)
        actor.client = server.clients[2] = actor_client
        actor_client.open = actor.spawned = observer.spawned = True
        viewer = actor if own else observer
        peer = actor_client if own else observer_client
        cipher = LinkCipher(key_out, key_in) if own else observer_cipher
        window = ReceiveWindow()
        sequence = 0
        match.tick = 100
        if own:
            peer.queue_entities([
                EntityUpdate(actor.entity, OP_CREATE, lambda origin: match._player_create(origin, actor)),
            ])
        else:
            match._show_players(observer)

        def emit():
            match.tick += 1
            if not own:
                match._show_players(viewer)
            peer.send_frame(match.tick, 10.0)
            packet = decoded(self, cipher, server.sock.sent[-1][0])[0]
            return packet, wire_frame(packet)[1]

        def acknowledge(packet):
            nonlocal sequence
            self.assertTrue(window.accept(packet.seq))
            ack = Packet(peer.conn, KIND_CONTROL, sequence, window.expected, window.bits, ACK)
            sequence += 1
            server._datagram(cipher.seal(ack), peer.address, 10.0)

        def possessions(records):
            return [
                (fields[44], fields[75])
                for update, fields in records if update.entity == actor.entity and 44 in fields
            ]

        def expected_possession(body, hero):
            return [({0: body, 1: body, 2: body}, {0: hero, 3: hero})]

        def switch(hero, *, deliver, lose_create=False):
            self.assertTrue(match.switch_hero(actor, heroes()[hero]))
            body = actor.body
            if not own:
                match.ensure_remote_body(viewer, actor)
            if lose_create:
                # A later packet reports both the old possession and the newest body
                # create missing. Only the current body create may retry at this point.
                _, records = emit()
                self.assertIn((body, OP_CREATE), [(item.entity, item.op) for item, _ in records])
                self.assertEqual(possessions(records), [])
                packet, records = emit()
                self.assertEqual(possessions(records), [])
                acknowledge(packet)
                packet, records = emit()
                self.assertIn((body, OP_CREATE), [(item.entity, item.op) for item, _ in records])
                self.assertEqual(possessions(records), [])
                acknowledge(packet)
            for _ in range(10):
                packet, records = emit()
                updates = possessions(records)
                if updates:
                    self.assertEqual(updates, expected_possession(body, hero))
                    if deliver:
                        acknowledge(packet)
                    return body
                acknowledge(packet)
            self.fail("the current possession was never sent")

        packet, records = emit()
        names = [
            fields[29][1] for update, fields in records
            if update.entity == actor.entity and update.op == OP_CREATE
        ]
        self.assertEqual(names, ["Target"])
        acknowledge(packet)
        old = switch(REAPER, deliver=False)
        fresh = switch(SOLDIER, deliver=not lose_current, lose_create=lose_current)
        self.assertNotEqual(old, fresh)
        if lose_current:
            packet, records = emit()
            self.assertEqual(possessions(records), [])
            acknowledge(packet)
            packet, records = emit()
            self.assertEqual(possessions(records), expected_possession(fresh, SOLDIER))
            acknowledge(packet)

        # The newest ACK reports the older packet missing. Its old 44/75 must not
        # go out again on the still-current controller after the body has changed.
        for _ in range(3):
            packet, records = emit()
            self.assertNotIn(old, [possessor[0] for possessor, _ in possessions(records)])
            acknowledge(packet)

    def test_old_remote_possession_does_not_retry_after_a_new_body_is_acknowledged(self):
        self.check_superseded_possession(own=False)

    def test_old_owner_possession_does_not_retry_after_a_new_body_is_acknowledged(self):
        self.check_superseded_possession(own=True)

    def test_current_remote_possession_retries_after_its_body_create_is_acknowledged(self):
        self.check_superseded_possession(own=False, lose_current=True)


if __name__ == "__main__":
    unittest.main()
