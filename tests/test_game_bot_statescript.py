"""Bot statescript delivery through the encrypted Client path and a modeled peer."""

import unittest
from unittest.mock import patch

from test_game_graph_analysis import make_graph, node
from test_game_packet_budget import connection, decoded
from test_game_repeated_lifecycles import wire_frame

from ow174.game import bodytrace, bots, combat, movelog, statescript, world
from ow174.game.content import SOLDIER
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.script import decode
from ow174.game.script.driver import Snapshot
from ow174.game.server import BOT_DESTROY_GRACE_TICKS, Client

GRAPH = 0xF0FE
PULSER = "STU_B9898052"
PULSE_KEY = (1, 0)


class SyntheticScript:
    """A verified H=0 pulse layout with a synthetic graph, independent of NPC asset extraction."""

    def __init__(self, graph):
        self.graph = graph
        self.deaths = []
        self.initial = self.frame(0, full=True)

    def frame(self, count, *, full, tick=0):
        payload = {"fresh": count > 0, "count": count}
        snap = Snapshot(tick, instances={1: (GRAPH, None)}, states={PULSE_KEY: (PULSER, True, payload)})
        state = statescript.StateWire(PULSER, True, payload)
        item = statescript.InstanceWire(1, self.graph, states={0: state})
        encode = statescript.remote_full_frame if full else statescript.remote_delta
        return encode([item], {}, {1: self.graph}), snap

    def die(self, frame, params, sender):
        self.deaths.append((frame, params, sender))
        data, snap = self.frame(len(self.deaths), full=False, tick=frame)
        return data, snap, {("state", *PULSE_KEY)}


class WirePeer:
    def __init__(self, client, cipher):
        self.client, self.cipher = client, cipher
        self.window = ReceiveWindow()
        self.seq = 0
        self.models = {}
        self.pulses = []

    def receive(self, packet, records):
        if not self.window.accept(packet.seq):
            return
        for record, _ in records:
            if record.op == world.OP_CREATE:
                self.models.setdefault(record.entity, decode.ClientBody())
            elif record.op == world.OP_DESTROY:
                self.models.pop(record.entity, None)
            if record.chunk is None:
                continue
            model = self.models[record.entity]
            frame = decode.read_chunk(record.chunk.getvalue(), record.chunk.count, model.graphs)
            if frame.last <= model.applied:
                continue
            if not frame.full and frame.first > model.applied + 1:
                raise AssertionError("a bot delta arrived before the receiver's baseline")
            model.apply(record)
            pulse = frame.instances[1].states.get(0)
            if pulse is not None and pulse[0] and pulse[1].get("fresh"):
                self.pulses.append((record.entity, frame.last, pulse[1]["count"]))

    def ack(self):
        packet = Packet(
            self.client.conn, KIND_CONTROL, self.seq, self.window.expected, self.window.bits, ACK
        )
        self.seq += 1
        self.client.server._datagram(self.cipher.seal(packet), self.client.address, 10.0)


class BotStatescriptTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)
        self.graph = make_graph(GRAPH, [node(0, PULSER, state=0, remote=0)], states=[0], remote=[0])
        original_graph = decode.graph_of
        graph_patch = patch.object(decode, "graph_of", lambda index: self.graph if index == GRAPH
                                   else original_graph(index))
        graph_patch.start()
        self.addCleanup(graph_patch.stop)
        self.server, client, self.shooter, cipher = connection()
        self.match = self.shooter.match
        self.shooter.spawned = self.shooter.has_body = True
        self.tick = self.match.tick = 100
        self.bot = bots.Bot(
            bots.BOT_ENTITY,
            {"hero": f"0x{SOLDIER:X}", "team": 1, "position": (0, 0, 3),
             "rotation": (0, 0, 0, 1), "graphs": []},
            {"name": "Fixture bot", "body": "0x0400000000001403", "health": 200},
            {},
        )
        self.bot.body_script = SyntheticScript(self.graph)
        self.match.bots = [self.bot]
        self.match.combat.next_bot = self.bot.entity + 1
        self.shooting_peer = WirePeer(client, cipher)
        self.watching_peer = self.add_observer(2)
        for peer in (self.shooting_peer, self.watching_peer):
            self.show_bots(peer)

    def add_observer(self, number):
        player = self.match.add_player(number, "Observer", SOLDIER, 0, False)
        key_in, key_out = bytes([number]) * 32, bytes([number + 16]) * 32
        client = Client(
            self.server, ("memory.test", 40000 + number), number, LinkCipher(key_in, key_out), player
        )
        player.client, player.spawned, client.open = client, True, True
        self.server.clients[number] = client
        return WirePeer(client, LinkCipher(key_out, key_in))

    def show_bots(self, peer):
        self.match.bots_shown[peer.client.player.slot] = self.match.tick
        peer.client.queue_entities([self.match._bot_create_update(bot) for bot in self.match.bots])

    def send(self, peer, *, receive=True):
        self.tick += 3
        self.match.tick = self.tick
        peer.client.send_frame(self.tick, 10.0)
        packet, tick, reliable, _, _, count = decoded(self, peer.cipher, self.server.sock.sent[-1][0])
        ids, records = wire_frame(packet)
        self.assertEqual((tick, ids, len(records)), (self.tick, [number for _, number, _ in reliable], count))
        if receive:
            peer.receive(packet, records)
        return packet, reliable, records

    @staticmethod
    def entities(records, op):
        return [record.entity for record, _ in records if record.op == op]

    @staticmethod
    def chunks(records, entity):
        return [record.chunk for record, _ in records if record.entity == entity and record.chunk is not None]

    def only_chunk(self, records, entity):
        chunks = self.chunks(records, entity)
        self.assertEqual(len(chunks), 1)
        return chunks[0]

    def assert_pulse(self, chunk, *, first, last, count):
        frame = decode.read_chunk(chunk.getvalue(), chunk.count, {1: GRAPH})
        self.assertEqual((frame.owner, frame.first, frame.last), (False, first, last))
        self.assertEqual(frame.instances[1].graph, GRAPH)
        self.assertEqual(frame.instances[1].states[0], (True, {"fresh": count > 0, "count": count}))

    def assert_same_chunk(self, original, retry):
        self.assertEqual((original.count, original.getvalue()), (retry.count, retry.getvalue()))

    def kill_bot(self):
        target = next(target for target in self.match.combat._targets() if target.entity == self.bot.entity)
        death_frame = self.match.tick
        self.match.combat._damage(self.shooter, target, 200, False, None, death_frame)
        self.match.combat._damage(self.shooter, target, 200, False, None, death_frame)
        self.match.combat._send_health()
        self.assertEqual(
            [(frame, sender) for frame, _, sender in self.bot.body_script.deaths],
            [(death_frame, self.shooter.body)],
        )

    def replacement(self, entity):
        fresh = bots.respawned(self.bot, entity, self.match.tick)
        fresh.body_script = SyntheticScript(self.graph)
        return fresh

    def respawn(self):
        replacement = self.replacement(self.match.combat.next_bot)
        with patch.object(bots, "respawned", return_value=replacement):
            self.match.combat._respawn_bot(self.bot)
        return replacement.entity

    def test_initial_bot_scripts_are_prepared_one_per_update_before_world_spawn(self):
        batch = [bots.respawned(self.bot, self.bot.entity + offset, 0) for offset in range(3)]
        scripts = [SyntheticScript(self.graph) for _ in batch]
        bot_ids = [bot.entity for bot in batch]
        with (
            patch.object(bots, "make", return_value=batch),
            patch("ow174.game.match.graph.entity_body", return_value={"graphs": []}),
            patch("ow174.game.match.BotScript", side_effect=scripts) as prepare,
        ):
            _, client, player, _ = connection()
            match = player.match
            match.map_loaded(player, 10.0)
            for offset in range(len(batch)):
                match.update(11.0 + offset * 0.016, 100 + offset)
                self.assertEqual(prepare.call_count, offset + 1)
                if offset + 1 < len(batch):
                    self.assertEqual(player.steps_done, 2)
                    self.assertFalse(player.spawned)
                    self.assertFalse(client.entities)
            self.assertEqual((player.spawned, player.steps_done), (True, 4))
            self.assertEqual(
                [item.entity for item in client.entities
                 if item.op == world.OP_CREATE and item.entity in bot_ids],
                bot_ids,
            )
            match.update(11.1, 104)
            self.assertEqual(prepare.call_count, len(batch))

    def test_each_receiver_retries_create_full_and_death_before_retiring_old_bot(self):
        peers = (self.shooting_peer, self.watching_peer)
        old = self.bot.entity
        lost_full = {}
        for peer in peers:
            _, _, lost = self.send(peer, receive=False)
            self.assertEqual(self.entities(lost, world.OP_CREATE), [old])
            self.assertEqual(self.chunks(lost, old), [])
            _, _, gap = self.send(peer)
            self.assertEqual(self.entities(gap, world.OP_CREATE), [])
            self.assertEqual(self.chunks(gap, old), [])
            peer.ack()
            _, _, retry = self.send(peer)
            self.assertEqual(self.entities(retry, world.OP_CREATE), [old])
            self.assertEqual(self.chunks(retry, old), [])
            peer.ack()
            peer.ack()  # duplicate positive ACK must not enqueue a second full
            _, _, records = self.send(peer, receive=False)
            lost_full[peer] = self.only_chunk(records, old)
            self.assert_pulse(lost_full[peer], first=0, last=1, count=0)

        self.kill_bot()

        lost_death = {}
        for peer in peers:
            _, reliable, gap = self.send(peer)
            self.assertEqual(self.chunks(gap, old), [])  # death is gated on this peer's full ACK
            notices = [number for _, number, _ in reliable if number in (combat.HIT, combat.KILL_NOTICE)]
            self.assertEqual(notices, [combat.HIT, combat.KILL_NOTICE] if peer is self.shooting_peer else [])
            self.assertTrue(any(record.entity == old and 51 in fields for record, fields in gap))
            peer.ack()
            _, _, retry = self.send(peer)
            self.assert_same_chunk(lost_full[peer], self.only_chunk(retry, old))
            self.assertEqual(peer.pulses, [])
            _, _, before_full_ack = self.send(peer)
            self.assertEqual(self.chunks(before_full_ack, old), [])
            peer.ack()
            peer.ack()
            packet, _, records = self.send(peer, receive=False)
            lost_death[peer] = packet, records
            self.assert_pulse(self.only_chunk(records, old), first=2, last=2, count=1)

        late = self.add_observer(3)
        self.show_bots(late)  # the corpse exists, but this peer did not witness its death
        _, reliable, create = self.send(late)
        self.assertEqual(self.entities(create, world.OP_CREATE), [old])
        self.assertEqual(reliable, [])
        late.ack()
        _, _, full = self.send(late)
        self.assert_pulse(self.only_chunk(full, old), first=0, last=1, count=0)
        late.ack()
        _, _, no_history = self.send(late)
        self.assertEqual(self.chunks(no_history, old), [])
        self.assertEqual(late.pulses, [])

        fresh = self.respawn()
        self.assertNotEqual(old, fresh)
        for peer in peers:
            _, _, gap = self.send(peer)
            self.assertEqual(self.entities(gap, world.OP_CREATE), [fresh])
            self.assertNotIn(old, self.entities(gap, world.OP_DESTROY))
            self.assertEqual(self.chunks(gap, old), [])
            peer.ack()
            _, _, retry = self.send(peer)
            self.assertNotIn(old, self.entities(retry, world.OP_DESTROY))
            old_packet, old_records = lost_death[peer]
            self.assert_same_chunk(self.only_chunk(old_records, old), self.only_chunk(retry, old))
            self.assert_pulse(self.only_chunk(retry, fresh), first=0, last=1, count=0)
            peer.receive(old_packet, old_records)  # late original must not apply the pulse again
            self.assertEqual(peer.pulses, [(old, 2, 1)])

        self.shooting_peer.ack()
        _, _, shooter_done = self.send(self.shooting_peer)
        self.assertEqual(self.entities(shooter_done, world.OP_DESTROY), [old])
        _, _, observer_waits = self.send(self.watching_peer)
        self.assertNotIn(old, self.entities(observer_waits, world.OP_DESTROY))
        self.watching_peer.ack()
        _, _, observer_done = self.send(self.watching_peer)
        self.assertEqual(self.entities(observer_done, world.OP_DESTROY), [old])
        for peer, records in zip(peers, (shooter_done, observer_done), strict=True):
            self.assertEqual(self.chunks(records, old), [])
            self.assertEqual(peer.pulses, [(old, 2, 1)])

        _, reliable, create = self.send(late)
        self.assertEqual(self.entities(create, world.OP_CREATE), [fresh])
        self.assertEqual(self.entities(create, world.OP_DESTROY), [old])
        self.assertEqual(reliable, [])
        late.ack()
        _, _, full = self.send(late)
        self.assert_pulse(self.only_chunk(full, fresh), first=0, last=1, count=0)
        self.assertNotIn(old, late.models)
        self.assertEqual(late.pulses, [])

    def test_expiry_retires_pending_script_and_old_full_ack_cannot_unlock_reused_id(self):
        peer, old = self.watching_peer, self.bot.entity
        _, _, create = self.send(peer)
        self.assertEqual(self.entities(create, world.OP_CREATE), [old])
        peer.ack()
        _, _, pending_full = self.send(peer)
        self.assert_pulse(self.only_chunk(pending_full, old), first=0, last=1, count=0)
        self.kill_bot()
        fresh = self.respawn()
        expiry = self.match.tick + BOT_DESTROY_GRACE_TICKS

        self.tick = expiry - 4
        _, _, before_expiry = self.send(peer)
        self.assertIn(fresh, self.entities(before_expiry, world.OP_CREATE))
        self.assertNotIn(old, self.entities(before_expiry, world.OP_DESTROY))
        self.tick = expiry - 3
        _, _, expired = self.send(peer)
        self.assertEqual(self.entities(expired, world.OP_DESTROY), [old])
        self.assertEqual(self.chunks(expired, old), [])

        reused = self.replacement(old)
        self.match.bots.append(reused)
        peer.client.queue_entities([self.match._bot_create_update(reused)])
        peer.ack()  # includes the old full, but has never seen the new create
        _, _, new_create = self.send(peer)
        self.assertEqual(self.entities(new_create, world.OP_CREATE), [old])
        self.assertEqual(self.chunks(new_create, old), [])
        peer.ack()
        _, _, new_full = self.send(peer)
        self.assert_pulse(self.only_chunk(new_full, old), first=0, last=1, count=0)
        peer.ack()
        _, _, final = self.send(peer)
        self.assertEqual(self.chunks(final, old), [])
        self.assertEqual(peer.pulses, [])
