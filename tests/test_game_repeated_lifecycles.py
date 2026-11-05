"""Repeated backend protocol-model acceptance, using temporary loopback UDP sockets.

This does not launch a game or claim real-client acceptance. The actual encrypted
datagrams go through GameServer's router and ACK handling. A small independent world
reader extracts the wire chunks into the existing ClientModel. Ticks are advanced
explicitly so packet loss and late ACKs do not depend on thread timing.
"""

import logging
import select
import socket
import struct
import time
import unittest
from unittest.mock import patch

import test_game_server as server_tests
import test_game_to_game as transition_tests
from test_game_projectiles import read_shot
from test_game_world import read_movement
from test_script_driver import ClientModel, server_on

from ow174.game import heroselect, messages, world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.collision import StaticCollisionProvider, flat_world
from ow174.game.content import PRACTICE_RANGE, SOLDIER
from ow174.game.link import ACK, FIN, KIND_FRAME, SYN, Packet, ReceiveWindow, peek_connection
from ow174.game.match import SEND_CONTROLLER, SEND_EVERY, SEND_WORLD
from ow174.game.script import decode, expr
from ow174.game.server import PING, GameServer
from ow174.game.world import OP_CREATE, OP_DESTROY, EntityUpdate

REAPER = server_tests.REAPER


def scalar(reader, kind):
    """Read component values independently of world._write_value()."""
    if kind in ("id", "dbid"):
        values = []
        for _ in range(2 if kind == "dbid" else 1):
            if kind == "id":
                reader.align()
            mask = reader.bits(8)
            values.append(sum(reader.bits(8) << (8 * n) for n in range(8) if mask & (1 << n)))
        return tuple(values) if kind == "dbid" else values[0]
    if kind == "string":
        reader.align()
        return bytes(reader.bits(8) for _ in range(reader.bits(32))).decode("utf-8")
    if kind == "lease[]":
        reader.align()
        return [
            struct.unpack("<QIIIB", bytes(reader.bits(8) for _ in range(21))) for _ in range(reader.bits(32))
        ]
    if kind.endswith("[]"):
        reader.align()
        return [scalar(reader, kind[:-2]) for _ in range(reader.bits(32))]
    if kind.startswith("blob:"):
        return bytes(reader.bits(8) for _ in range(int(kind[5:])))
    fmt = "<I" if kind == "entity" else world.SCALAR_FORMATS[kind]
    return struct.unpack(fmt, bytes(reader.bits(8) for _ in range(struct.calcsize(fmt))))[0]


def components(reader):
    result = {}
    for _ in range(reader.bits(8)):
        index = reader.bits(8)
        reader.align()
        mask = reader.bits(reader.bits(8) * 8)
        result[index] = {
            field: scalar(reader, kind)
            for field, kind in enumerate(world.COMPONENT_FIELDS[index])
            if mask & (1 << field)
        }
    return result


def entity_record(reader, op):
    length = reader.bits(14)
    end = reader.pos + length
    if op == OP_CREATE:
        if reader.bit():  # a placeable from the map
            reader.bits(32)
            assert not reader.bit()
        else:
            reader.bits(12 if reader.bit() else 20 if reader.bit() else 32)
            flags = reader.bits(8)
            if flags & world.WITH_SKIN:
                assert flags & world.SKIN_THEME
                reader.bits(33)
            if flags & world.WITH_TRANSFORM:
                reader.bits(7 * 32)
            if flags & (world.WITH_TRANSFORM | world.WITH_SCALE):
                reader.bits(3 * 32)
            if flags & world.WITH_MOVEMENT:
                read_movement(reader, world.CREATE_BASE)
    result = components(reader)
    assert reader.pos == end, (reader.pos, end)
    return result


def wire_frame(packet):
    """Read the actual world datagram, including body chunks and possession references."""
    reader = BitReader(packet.payload)
    tick = reader.var_a()
    reliable, _ = messages.read_channel(reader)
    reader.var_a()  # command ACK
    assert reader.bits(4) == 0 and reader.bit() == 0
    assert reader.bit() == 1 and reader.bit() == 0
    records = []
    for _ in range(reader.bits(7)):
        update = EntityUpdate(reader.entity_id())
        assert reader.bit() == 1  # ch0
        if not reader.bit():
            records.append((update, {}))
            continue
        if reader.bit():
            assert not reader.bit()
            start = reader.pos
            decode.w_u32(reader)
            decode.w_var(reader)
            assert not reader.bit()  # no fragments
            size = decode.w_var(reader)
            reader.bits(size)
            chunk = BitWriter()
            chunk.bits(BitReader(packet.payload, start).bits(reader.pos - start), reader.pos - start)
            update.chunk = chunk
        if not reader.bit():  # ch2: a chunk can end the entity here
            records.append((update, {}))
            continue
        if reader.bit():
            first = True
            while reader.bit():
                if first:
                    reader.var_b()
                    first = False
                elif not reader.bit():
                    reader.bits(4)
                else:
                    reader.bits(32 if reader.bit() else 8)
                read_shot(reader)
        assert not reader.bit()  # these scenarios use no projectile release state
        assert reader.bit() == 1  # ch3
        if reader.bit():
            start = reader.pos
            length = reader.bits(12)
            assert not reader.bit()  # reference movement record
            read_movement(reader, tick)
            assert reader.pos == start + length
        if not reader.bit():
            records.append((update, {}))
            continue
        update.op = reader.bits(2)
        fields = {} if update.op == OP_DESTROY else entity_record(reader, update.op)
        assert reader.bit() == 0  # ch5
        records.append((update, fields))
    assert reader.left() < 8 and reader.bits(reader.left()) == 0
    return [msg_id for _, msg_id, _ in reliable], records


class ModelLink(transition_tests.Link):
    def __init__(self, game, handoff):
        super().__init__(game, handoff)
        self.window = ReceiveWindow()
        self.reliable = 0
        self.models = {}  # entity -> ClientModel; removed at the body's wire destroy
        self.fields = {}  # entity -> latest replicated component fields
        self.chunks = []  # (datagram seq, entity, decoded Frame)
        self.records = []  # the wire records, including reliable retransmissions
        self.messages = []

    def apply(self, packet):
        self.window.accept(packet.seq)
        self.got.append(packet)
        if packet.kind != KIND_FRAME:
            return
        ids, records = wire_frame(packet)
        self.messages.extend(ids)
        for update, fields in records:
            self.records.append((packet.seq, update))
            if update.op == OP_DESTROY:
                self.models.pop(update.entity, None)
                self.fields.pop(update.entity, None)
                continue
            for index, values in fields.items():
                self.fields.setdefault(update.entity, {}).setdefault(index, {}).update(values)
            if update.op == OP_CREATE and 123 in fields and update.entity not in self.models:
                self.models[update.entity] = ClientModel(fields[123][0])
            model = self.models.get(update.entity)
            if update.chunk is not None and model is not None:
                first = decode.w_u32(BitReader(update.chunk.getvalue()))
                frame = decode.read_chunk(update.chunk.getvalue(), update.chunk.count, model.graphs)
                if frame.last > model.applied and first <= model.applied + 1:
                    frame = model.apply(update)
                    self.chunks.append((packet.seq, update.entity, frame))


class ProtocolHarness:
    """The existing pretend game's one UDP socket, with controlled server ticks and ACK windows."""

    def __init__(self):
        self.left = []
        # These are protocol/lifecycle scenarios. Do not race 120 explicit ticks against
        # real-time map loading or depend on another test having warmed a global cache.
        self.server = GameServer(
            "127.0.0.1", 0, on_leave=self.left.append,
            collision_provider_factory=lambda: StaticCollisionProvider(flat_world(1.0)),
        )
        self.server.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server.sock.bind(("127.0.0.1", 0))
        self.server.sock.settimeout(1.0)
        self.server.port = self.server.sock.getsockname()[1]
        self.game = transition_tests.PretendGame(self.server.port)
        self.now = time.time()

    def close(self):
        self.game.close()
        self.server.sock.close()

    def link(self, handoff):
        link = ModelLink(self.game, handoff)
        self.game.links[handoff.conn] = link
        return link

    def drain(self):
        while select.select([self.game.sock], [], [], 0)[0]:
            data, _ = self.game.sock.recvfrom(4096)
            link = self.game.links[peek_connection(data)]
            packet = link.cipher.open(data)
            assert packet is not None
            link.apply(packet)

    def send(self, link, flags=ACK, payload=b"", ack=None):
        expected, bits = ack if ack is not None else (link.window.expected, link.window.bits)
        packet = Packet(link.conn, KIND_FRAME if payload else 0, link.seq, expected, bits, flags, payload)
        link.seq += 1
        self.game.sock.sendto(link.cipher.seal(packet), self.game.server)
        data, address = self.server.sock.recvfrom(4096)
        self.server._datagram(data, address, self.now)
        self.server._report_leaves()
        self.drain()

    def message(self, link, msg_id, value, ack=None):
        payload = BitWriter()
        messages.write_channel(payload, [(link.reliable, messages.encode(msg_id, value))])
        link.reliable += 1
        self.send(link, payload=payload.getvalue(), ack=ack)

    def command(self, link, buttons=0, advance=1, ack=None):
        player = self.server.clients[link.conn].player
        frame = max(player.body_script.frame, player.commands.last_frame or 0) + advance
        body = BitWriter()
        body.bits(0, 24)  # latency and key flags
        body.bits(1, 8)
        body.bits(frame, 32)
        body.bit(0)  # zero throttles
        body.bits(0, 32)  # yaw and pitch
        body.bits(buttons, 24)
        body.bits(0, 24)  # action and extra
        payload = BitWriter()
        messages.write_channel(payload, [])
        payload.bit(1)
        payload.bits(body.count, 32)
        payload.append(body)
        self.send(link, payload=payload.getvalue(), ack=ack)

    def step(self, count=1, ack_except=()):
        for _ in range(count):
            self.now += 0.016
            self.server._tick(self.now)
            self.server._report_leaves()
            self.drain()
            for link in self.game.links.values():
                if link.conn in self.server.clients and link not in ack_except:
                    self.send(link)

    def until(self, condition, ack_except=(), limit=120):
        for _ in range(limit):
            if condition():
                return
            self.step(ack_except=ack_except)
        raise AssertionError("protocol condition did not become true in controlled ticks")

    def connect(self, handoff):
        link = self.link(handoff)
        self.send(link, SYN)
        assert link.got[-1].flags & (SYN | ACK) == SYN | ACK
        self.send(link)
        self.until(lambda: 20300 in link.messages)
        self.message(link, 21601, {})
        player = self.server.clients[link.conn].player
        self.until(lambda: player.steps_done == 4 and player.entity in link.fields)
        return link, player


class RepeatedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.run = ProtocolHarness()
        self.addCleanup(self.run.close)
        self.script_log = logging.getLogger("ow174.script")
        old_level = self.script_log.level
        self.script_log.setLevel(logging.ERROR)
        self.addCleanup(self.script_log.setLevel, old_level)

    def start_loading(self):
        (handoff,) = self.run.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        link = self.run.link(handoff)
        self.run.send(link, SYN)
        self.run.send(link)
        self.run.until(lambda: 20300 in link.messages)
        self.run.message(link, 21601, {})
        return link, self.run.server.clients[link.conn].player

    @staticmethod
    def reliable_messages(packet):
        if packet.kind != KIND_FRAME:
            return []
        reader = BitReader(packet.payload)
        reader.var_a()
        return messages.read_channel(reader)[0]

    def assert_world_not_admitted(self, link, player):
        self.assertFalse(any(item.op == OP_CREATE for _, item in link.records))
        self.assertFalse(player.spawned)

    def test_lost_ownership_waits_for_its_actual_retry_ack_before_world_create(self):
        link, player = self.start_loading()
        apply = link.apply
        dropped = []

        def lose_ownership(packet):
            if not dropped and any(kind == 20302 for _, kind, _ in self.reliable_messages(packet)):
                dropped.append(packet)
            else:
                apply(packet)

        with patch.object(link, "apply", side_effect=lose_ownership):
            self.run.until(lambda: dropped, ack_except=(link,))
        self.run.until(lambda: self.run.now - player.loaded_at > SEND_WORLD + SEND_EVERY * 0.016,
                       ack_except=(link,))
        self.assert_world_not_admitted(link, player)
        self.assertNotIn(20302, link.messages)

        self.run.send(link)  # later datagrams acknowledge the hole in the receive window
        self.run.until(lambda: 20302 in link.messages, ack_except=(link,))
        self.assert_world_not_admitted(link, player)
        self.run.send(link)  # ACK the packet that actually delivered ownership
        self.run.until(lambda: player.steps_done == 4 and player.entity in link.fields)
        self.assertIn(world.GAME_MODE_ENTITY, link.fields)

    def test_ownership_packet_ack_still_waits_for_an_earlier_reliable_ordinal(self):
        link, player = self.start_loading()
        self.run.message(link, PING, {"+0x78": 9, "+0x80": [11, 22]})
        apply = link.apply
        missing = []

        def lose_pong(packet):
            pong = next(
                (ordinal for ordinal, kind, _ in self.reliable_messages(packet) if kind == 20306), None
            )
            if pong is None:
                apply(packet)
            else:
                missing.append(pong)

        with patch.object(link, "apply", side_effect=lose_pong):
            self.run.until(lambda: 20302 in link.messages, ack_except=(link,))
            self.assertEqual(len(missing), 1)
            (ordinal, kind, value), = self.reliable_messages(link.got[-1])
            self.assertEqual((kind, value["+0x78"]["+0x0"]), (20302, player.entity))
            self.assertLess(missing[0], ordinal)
            owner_seq = link.got[-1].seq
            self.run.send(link)
            self.assertNotIn(owner_seq, player.client.in_flight)  # actual positive ownership ACK
            self.run.until(lambda: self.run.now - player.loaded_at > SEND_WORLD + SEND_EVERY * 0.016,
                           ack_except=(link,))
            self.assertNotIn(20306, link.messages)
            self.assert_world_not_admitted(link, player)

        self.run.send(link)
        self.run.until(lambda: 20306 in link.messages, ack_except=(link,))
        self.assert_world_not_admitted(link, player)
        self.run.send(link)  # the retransmitted predecessor completes the acknowledged prefix
        self.run.until(lambda: player.steps_done == 4 and player.entity in link.fields)
        self.assertIn(world.GAME_MODE_ENTITY, link.fields)

    @staticmethod
    def own_controller_chunks(link, player):
        return [
            (seq, item.chunk)
            for seq, item in link.records
            if item.entity == player.entity and item.chunk is not None
        ]

    def assert_open_controller_full(self, chunk, hero=None):
        frame = decode.read_chunk(chunk.getvalue(), chunk.count)
        self.assertTrue(frame.full and frame.owner)
        self.assertEqual((frame.first, frame.last), (0, 1))
        self.assertEqual([instance.graph for instance in frame.instances.values()],
                         [0x13C1, 0x288A, 0x288B, 0x20E4, 0x288D])
        self.assertEqual(frame.entity_vars[heroselect.OPEN_SELECT], (True, ()))
        self.assertEqual(frame.entity_vars[heroselect.SELECT_SHOWN], (True, ()))
        if hero is not None:
            self.assertEqual(frame.entity_vars[heroselect.CURRENT_HERO], (expr.Asset(hero), ()))

    def hold_own_controller_create_ack(self):
        link, player = self.start_loading()
        # With ownership ACK delayed, the world and controller time thresholds are both already due.
        self.run.until(lambda: self.run.now - player.loaded_at > SEND_CONTROLLER + SEND_EVERY * 0.016,
                       ack_except=(link,))
        self.assertIn(20302, link.messages)
        self.run.send(link)
        apply = link.apply
        dropped, unrelated, early_chunks, early_boards = [], [], [], []

        def lose_controller_create(packet):
            if packet.kind == KIND_FRAME:
                _, records = wire_frame(packet)
                own = [(item, fields) for item, fields in records if item.entity == player.entity]
                early_chunks.extend(packet.seq for item, _ in own if item.chunk is not None)
                early_boards.extend(
                    packet.seq for item, fields in own if item.op != OP_CREATE and 64 in fields
                )
                if any(item.op == OP_CREATE for item, _ in own):
                    dropped.append(packet.seq)
                    return
                if dropped and any(item.op == OP_CREATE for item, _ in records):
                    unrelated.append(packet.seq)
            apply(packet)

        with patch.object(link, "apply", side_effect=lose_controller_create):
            self.run.until(lambda: unrelated)
        self.assertIn(unrelated[0], {packet.seq for packet in link.got})
        self.assertNotIn(unrelated[0], player.client.in_flight)  # this world datagram was positively ACKed
        self.assertEqual(early_chunks, [])
        self.assertEqual(early_boards, [])

        def own_create():
            return any(item.entity == player.entity and item.op == OP_CREATE for _, item in link.records)

        self.assertFalse(own_create())
        unrelated_ack = link.window.expected, link.window.bits
        self.run.until(own_create, ack_except=(link,))
        return link, player, unrelated_ack

    def test_controller_full_waits_for_its_own_create_ack_after_other_world_packets(self):
        link, player, _ = self.hold_own_controller_create_ack()
        self.run.step(SEND_EVERY, ack_except=(link,))
        self.assertEqual(self.own_controller_chunks(link, player), [])
        self.run.send(link)
        self.run.until(lambda: self.own_controller_chunks(link, player))
        (_, full), = self.own_controller_chunks(link, player)
        self.assert_open_controller_full(full)
        self.assertEqual(player.script.delivered, 1)

    def test_early_hero_pick_keeps_latest_controller_and_possession_behind_own_create_ack(self):
        link, player, unrelated_ack = self.hold_own_controller_create_ack()
        last_before_pick = link.got[-1].seq
        self.run.message(link, 21616, server_tests.pick(player.entity, SOLDIER), ack=unrelated_ack)
        self.assertTrue(player.has_body)
        self.assertFalse(player.select_open)
        reopen = server_tests.pick(player.entity, SOLDIER)
        reopen.update({"+0x80": b"\x01\x01" + bytes(8), "+0xA8": heroselect.OPEN_OR_CLOSE})
        self.run.message(link, 21616, reopen, ack=unrelated_ack)
        self.assertTrue(player.select_open)

        self.run.step(2 * SEND_EVERY, ack_except=(link,))
        for packet in link.got:
            if packet.kind != KIND_FRAME or packet.seq <= last_before_pick:
                continue
            _, records = wire_frame(packet)
            for item, fields in records:
                if item.entity == player.entity and item.op != OP_CREATE:
                    self.assertIsNone(item.chunk)
                    self.assertFalse(fields.keys() & {44, 64, 75})

        self.run.send(link)
        self.run.until(lambda: self.own_controller_chunks(link, player) and 44 in link.fields[player.entity])
        (_, full), = self.own_controller_chunks(link, player)
        self.assert_open_controller_full(full, hero=SOLDIER)
        self.assertEqual(link.fields[player.entity][44], {0: player.body, 1: player.body, 2: player.body})
        self.assertEqual(link.fields[player.entity][75][0], SOLDIER)

    def test_healthy_ownership_keeps_world_timing_and_soldier_input(self):
        link, player = self.start_loading()
        self.run.until(lambda: player.entity in link.fields)
        self.assertIn(20302, link.messages)
        self.assertIn(world.GAME_MODE_ENTITY, link.fields)
        elapsed = self.run.now - player.loaded_at
        self.assertGreaterEqual(elapsed, SEND_WORLD)
        self.assertLess(elapsed, SEND_WORLD + SEND_EVERY * 0.016)
        create_seq, = (
            seq for seq, item in link.records if item.entity == player.entity and item.op == OP_CREATE
        )
        self.run.until(lambda: self.own_controller_chunks(link, player))
        (controller_seq, full), = self.own_controller_chunks(link, player)
        self.assertGreater(controller_seq, create_seq)
        self.assert_open_controller_full(full)
        elapsed = self.run.now - player.loaded_at
        self.assertGreaterEqual(elapsed, SEND_CONTROLLER)
        # Initial world entities can occupy the first eligible packet.
        self.assertLess(elapsed, SEND_CONTROLLER + 2 * SEND_EVERY * 0.016)

        self.run.message(link, 21616, server_tests.pick(player.entity, SOLDIER))
        script = player.body_script
        self.assertIsNotNone(script)
        self.run.until(lambda: script.stream.base is not None)
        self.run.command(link)
        self.run.until(lambda: any(entity == player.body and frame.owner for _, entity, frame in link.chunks))
        self.run.command(link, buttons=1)
        self.assertEqual(script.component.instances[11].vars[53].value(), 29)
        self.run.command(link)
        self.run.command(link, advance=25)  # settle the owner frame clock with neutral input
        self.run.until(lambda: link.models[player.body].vars.get((11, 53)) == 29)

    def initialize(self, link, player, hero, *, pick=True, leave_full_unacked=False):
        if pick:
            self.run.message(link, 21616, server_tests.pick(player.entity, hero))
        script = player.body_script
        self.assertIsNotNone(script)
        self.run.until(lambda: player.body in link.models, ack_except=(link,))
        self.assertFalse(script.create_arrived)
        self.assertEqual(script.stream.last, 0)
        self.run.step(6, ack_except=(link,))
        self.assertEqual(script.stream.last, 0)  # create ACK is a real transport gate
        creates = [
            (seq, item) for seq, item in link.records if item.entity == player.body and item.op == OP_CREATE
        ]
        # Loss of a current create must still retransmit normally. The late-ACK
        # suppression tested below must not disable delivery of the next lifecycle.
        self.run.send(link, ack=(creates[-1][0] + 1, 0))
        self.assertFalse(script.create_arrived)
        self.run.until(
            lambda: (
                sum(item.entity == player.body and item.op == OP_CREATE for _, item in link.records)
                > len(creates)
            ),
            ack_except=(link,),
        )
        self.assertFalse(script.create_arrived)
        self.assertEqual(script.stream.last, 0)
        self.run.send(link)
        self.run.until(lambda: script.stream.last == 1, ack_except=(link,))
        model = link.models[player.body]
        self.assertEqual(model.applied, 1)
        self.assertIsNone(script.stream.base)
        full_packet = next(
            seq for seq, entity, frame in reversed(link.chunks) if entity == player.body and frame.full
        )
        original = script.stream.full_update.chunk
        chunks_before = sum(item.entity == player.body and item.chunk is not None for _, item in link.records)
        self.run.send(link, ack=(full_packet + 1, 0))
        self.assertIn(script.stream.full_update, player.client.entities)
        self.run.until(
            lambda: (
                sum(item.entity == player.body and item.chunk is not None for _, item in link.records)
                > chunks_before
            ),
            ack_except=(link,),
        )
        full_packet, resent = next(
            (seq, item)
            for seq, item in reversed(link.records)
            if item.entity == player.body and item.chunk is not None
        )
        self.assertEqual((resent.chunk.getvalue(), resent.chunk.count), (original.getvalue(), original.count))
        self.assertEqual(model.applied, 1)  # a duplicate full does not rebuild the instance table
        self.run.command(link, ack=(0, 0))
        self.run.step(6, ack_except=(link,))
        self.assertEqual(script.stream.last, 1)  # full ACK gates the first owner frame too
        if leave_full_unacked:
            return script, full_packet
        self.run.send(link)
        self.run.until(lambda: any(entity == player.body and frame.owner for _, entity, frame in link.chunks))
        self.assertEqual(
            model.graphs, {item.id: item.graph.index for item in script.component.instances.values()}
        )
        self.assertEqual(model.on(), server_on(script))
        self.assertEqual(link.fields[player.entity][44], {0: player.body, 1: player.body, 2: player.body})
        self.assertEqual(link.fields[player.entity][75][0], hero)
        self.assertEqual(link.fields[player.body][123][0], hero)
        if hero == SOLDIER:
            self.assertEqual(model.vars[(11, 53)], 30)
            self.assertEqual(model.vars[(0, 29)], (False, True, False))
            self.assertEqual(script.component.instances[11].vars[53].value(), 30)
        return script, full_packet

    def test_ten_hero_round_trips_retire_old_records_and_reset_soldiers_weapon(self):
        own, remote = self.run.server.create_match(
            PRACTICE_RANGE, [(1, "Alpha", 0, 0, False), (2, "Observer", 0, 0, False)]
        )
        link, player = self.run.connect(own)
        observer_link, observer = self.run.connect(remote)
        self.initialize(link, player, SOLDIER)
        for cycle in range(10):
            with self.subTest(cycle=cycle + 1):
                old_soldier, old_body = player.body_script, player.body
                self.run.command(link, buttons=1)
                self.run.command(link, buttons=1, advance=25)
                self.run.command(link, advance=1)
                self.run.step(6)
                self.assertLess(old_soldier.component.instances[11].vars[53].value(), 30)
                self.assertEqual(
                    link.models[old_body].vars[(11, 53)], old_soldier.component.instances[11].vars[53].value()
                )
                reaper, full_packet = self.initialize(link, player, REAPER, leave_full_unacked=True)
                self.assertTrue(old_soldier.retired)
                self.assertNotIn(old_body, link.models)
                reaper_body = player.body
                self.run.until(lambda body=reaper_body: body in observer_link.models, ack_except=(link,))
                self.assertNotIn(old_body, observer.seen)
                self.assertNotIn((observer.slot, old_body), player.match.combat.observers.watches)
                # Switch before Reaper's full arrives at the server's ACK boundary. Then
                # alternate a late positive ACK and loss feedback for that obsolete full.
                self.run.message(link, 21616, server_tests.pick(player.entity, SOLDIER), ack=(0, 0))
                self.assertTrue(reaper.retired)
                self.assertFalse(reaper.stream.full_update.resend)
                self.assertIsNone(reaper.stream.base)
                self.run.send(link, ack=(full_packet + 1, 1 if cycle % 2 == 0 else 0))
                self.assertIsNone(reaper.stream.base)
                self.assertEqual(reaper.frames(), [])
                self.assertFalse(
                    any(
                        item.entity == reaper_body and item.op != OP_DESTROY
                        for item in player.client.entities
                    )
                )
                soldier, _ = self.initialize(link, player, SOLDIER, pick=False)
                self.assertIsNot(soldier, old_soldier)
                self.assertIsNot(soldier.component, old_soldier.component)
                self.run.until(
                    lambda: observer_link.fields.get(player.entity, {}).get(44, {}).get(0) == player.body
                )
                self.assertNotIn(reaper_body, link.models)
                self.assertNotIn(reaper_body, observer_link.models)
                self.assertNotIn(reaper_body, observer.seen)
                self.assertNotIn((observer.slot, reaper_body), player.match.combat.observers.watches)
                self.assertEqual(observer_link.fields[player.entity][44][0], player.body)
                self.assertEqual(observer_link.fields[player.entity][75][0], SOLDIER)

    def test_ten_leave_rejoins_keep_old_connection_packets_out_of_the_new_lifecycle(self):
        (handoff,) = self.run.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
        link, player = self.run.connect(handoff)
        self.initialize(link, player, SOLDIER)
        for cycle in range(10):
            with self.subTest(cycle=cycle + 1):
                old_link, old_player = link, player
                old_client, old_script = player.client, player.body_script
                old_model = link.models[player.body]
                old_match = player.match
                (handoff,) = self.run.server.create_match(PRACTICE_RANGE, [(1, "Alpha", 0, 0, False)])
                if cycle % 2:
                    link, player = self.run.connect(handoff)  # SYN before the old FIN
                self.run.send(old_link, FIN)
                self.assertEqual(old_link.got[-1].flags & (FIN | ACK), FIN | ACK)
                if not cycle % 2:
                    link, player = self.run.connect(handoff)  # FIN before the new SYN
                self.assertTrue(old_client.closed and old_player.gone and old_match.ended)
                self.assertIsNone(old_player.client)
                self.assertFalse(old_player.spawned)
                self.assertIsNone(old_player.body_script)
                self.assertTrue(old_script.retired)
                self.assertEqual(old_script.frames(), [])
                self.assertNotIn(old_link.conn, self.run.server.clients)
                self.assertNotIn(old_link.conn, self.run.server.handoffs)
                self.initialize(link, player, SOLDIER)
                self.assertIsNot(link.models[player.body], old_model)
                new_client, new_script = player.client, player.body_script
                old_sequence = old_client.seq
                before = (player.body, new_script, new_script.stream.base_last, new_client.expected_in)
                # The same socket and reused player/body ids make the connection/key route
                # essential: the previous match's ACK, FIN, and hero pick must all be inert.
                self.run.send(old_link)
                self.run.send(old_link, FIN)
                self.run.message(old_link, 21616, server_tests.pick(old_player.entity, REAPER))
                self.assertEqual(
                    before,
                    (player.body, player.body_script, new_script.stream.base_last, new_client.expected_in),
                )
                self.assertTrue(new_client.open and not new_client.closed)
                self.assertEqual(new_client.address, old_client.address)
                self.assertIsNot(new_client.cipher, old_client.cipher)
                self.assertIsNot(new_client.window, old_client.window)
                self.assertEqual(self.run.left.count(old_player), 1)
                self.run.step(3)
                self.assertEqual(old_client.seq, old_sequence)  # no dropped link retransmits
                self.assertNotIn(old_match, self.run.server.matches)
                self.assertEqual(len(self.run.server.matches), 1)
                self.assertEqual(len(self.run.server.clients), 1)
                self.assertEqual(self.run.server.handoffs, {})
        self.assertEqual(len(self.run.left), 10)


if __name__ == "__main__":
    unittest.main()
