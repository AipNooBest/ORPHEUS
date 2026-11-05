"""Global tick rollover through controlled production scheduling and encrypted handoffs."""

import dataclasses
import unittest
from unittest.mock import patch

from test_game_command_handoff import input_window
from test_game_repeated_lifecycles import wire_frame
from test_server_journal import MAP, FixedSource, Peer, SchedulerSource, pick

from ow174.game import bodytrace, messages, movelog, replay_values, world
from ow174.game.bits import BitReader, BitWriter
from ow174.game.collision import StaticCollisionProvider, flat_world
from ow174.game.commands import FIRE
from ow174.game.content import SOLDIER
from ow174.game.link import FIN, KIND_FRAME, SYN, peek_connection
from ow174.game.match import card_message
from ow174.game.projectiles import MAX_LEASE_FRAME
from ow174.game.server import (
    CLOCK_DRAIN_TICKS,
    FIRST_TICK,
    LEAVE_GAME,
    MAX_SIMULATION_TICK,
    TICK_SECONDS,
    GameServer,
)
from ow174.game.server_journal import RecordingServerSession, _ReplaySession, replay_server_session

ROOM = dataclasses.replace(MAP, team_sizes=(2, 1))
OWNER = (1, "Clock", SOLDIER, 0, False)
WAITING = (2, "Waiting", SOLDIER, 1, False)
ADDRESS = ("127.0.0.1", 43000)


class GlobalClockTests(unittest.TestCase):
    def setUp(self):
        sockets = patch("socket.socket", side_effect=AssertionError("must not create a socket"))
        sockets.start()
        self.addCleanup(sockets.stop)
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)

    @staticmethod
    def session(source=None, *, stored=False):
        sent = []

        def send(data, address):
            sent.append((data, address))
            return len(data)

        session = RecordingServerSession(
            environment=FixedSource() if source is None else source,
            collision_provider=StaticCollisionProvider(flat_world(0.0)),
            sendto=send,
            verify_stored_state=stored,
        )
        return session, sent

    @staticmethod
    def frames(peer):
        for data, address in peer.sent:
            if peek_connection(data) != peer.handoff.conn:
                continue
            packet = peer.cipher.open(data)
            if packet is None or address != peer.address:
                raise AssertionError("the peer must authenticate its emitted datagrams")
            if packet.kind == KIND_FRAME:
                reader = BitReader(packet.payload)
                tick = reader.var_a()
                reliable, _ = messages.read_channel(reader)
                _, records = wire_frame(packet)
                yield packet.seq, tick, reliable, records

    def until(self, session, now, condition, *, ack_peer=None, limit=80):
        for _ in range(limit):
            if condition():
                return now
            now += TICK_SECONDS
            session.tick(now)
            if ack_peer is not None:
                ack_peer.drain()
                ack_peer.send(now=now)
        self.fail("the controlled tick condition did not become true")

    def connect_soldier(self, session, sent, handoff, now):
        peer = Peer(session, handoff, sent, ADDRESS)
        peer.send(flags=SYN, now=now)
        peer.send(now=now)
        peer.message(21601, {}, now=now)
        player = session.server.clients[handoff.conn].player
        now = self.until(session, now, lambda: player.steps_done == 4, ack_peer=peer)
        peer.message(21616, pick(player.entity, SOLDIER), now=now)
        script = player.body_script
        self.assertIsNotNone(script)
        now = self.until(session, now, lambda: script.stream.base is not None, ack_peer=peer)
        return peer, player, now

    def leave_attempts(self, peer):
        return [row for row in self.frames(peer) if any(kind == LEAVE_GAME for _, kind, _ in row[2])]

    def test_production_loop_huge_miss_resets_idle_clock_and_replays(self):
        late = 10.0 + (MAX_SIMULATION_TICK - FIRST_TICK + CLOCK_DRAIN_TICKS + 5) * TICK_SECONDS
        source = SchedulerSource(
            ("start", 10.0), ("timeout", 10.0), ("due", late), ("next_tick", late), ("tick", 100.0)
        )
        session, sent = self.session(source, stored=True)
        session.run_loop([None])
        self.assertEqual(session.server.tick, FIRST_TICK)
        self.assertEqual(sent, [])
        replay = replay_server_session(session.finish(), resolver=session.registry.resolve)
        self.assertTrue(replay["stored_state_verified"])

    def test_scheduler_records_clock_ready_after_old_leave_then_replays_new_handoff(self):
        class RolloverSource(SchedulerSource):
            handoff_times = iter((100.0, 160.03))

            def wall_time(self, *, callsite):
                if callsite == "server.handoff.deadline":
                    return next(self.handoff_times)
                return super().wall_time(callsite=callsite)

        source = RolloverSource(
            ("start", 10.0), ("timeout", 10.0), ("due", 10.020), ("next_tick", 10.020), ("tick", 160.01),
            ("timeout", 10.020), ("due", 10.040), ("next_tick", 10.040), ("tick", 160.03),
        )
        session, sent = self.session(source, stored=True)
        old, = session.create_match(ROOM, [OWNER])
        session.tick(100.0, missed=MAX_SIMULATION_TICK - FIRST_TICK)
        args = {"game_map": dataclasses.asdict(ROOM), "players": [WAITING], "card": 0}
        session.run_loop([None, None], callback_actions=[("clock_ready", None, "create_match", args)])
        self.assertEqual(session.server.tick, FIRST_TICK)
        self.assertEqual(sent, [])
        (handoff, player, _), = session.server.handoffs.values()
        self.assertNotEqual(handoff.conn, old.conn)
        self.assertEqual(player.account_lo, WAITING[0])

        peer = Peer(session, handoff, sent, ADDRESS)
        peer.send(flags=SYN, now=160.04)
        peer.send(now=160.04)
        session.report_callbacks()
        document = session.finish()
        self.assertEqual([row["state_ref"] for row in document["collision"]["reads"]], [0, 0])
        scheduler, = (row for row in document["roots"] if row["operation"] == "run_loop")
        effects = [replay_values.decode(effect)[1] for effect in scheduler["effects"]]
        self.assertEqual(
            [
                effect["kind"] for effect in effects
                if effect["kind"] not in ("socket_receive", "socket_timeout")
            ],
            ["left", "clock_ready", "callback_action", "collision_poll", "callback_result", "socket_close"],
        )
        self.assertEqual(next(effect for effect in effects if effect["kind"] == "left")["player"],
                         (old.match_id, 0))
        result = next(effect["result"] for effect in effects if effect["kind"] == "callback_result")
        self.assertEqual(result, [dataclasses.asdict(handoff)])
        replay = _ReplaySession(document, session.registry.resolve)
        self.assertTrue(replay.run()["stored_state_verified"])
        for server in (session.server, replay.server):
            self.assertEqual(server.handoffs, {})
            self.assertEqual(set(server.clients), {handoff.conn})
            self.assertTrue(server.clients[handoff.conn].open)

    def test_drain_delivers_reliable_leave_with_bad_world_queue_and_pending_handoff(self):
        late = 10.0 + 2 * CLOCK_DRAIN_TICKS * TICK_SECONDS
        drain_now = 102.0
        source = SchedulerSource(
            ("start", 10.0), ("timeout", 10.0), ("due", late), ("next_tick", late), ("tick", drain_now)
        )
        session, sent = self.session(source)
        server = session.server
        session.tick(100.0, missed=MAX_SIMULATION_TICK - FIRST_TICK - 101)
        old, pending = session.create_match(ROOM, [OWNER, WAITING], card=12)
        owner, player, _ = self.connect_soldier(session, sent, old, 100.1)
        session.report_callbacks()
        client, match, script = player.client, player.match, player.body_script
        self.assertIsNone(player.commands.last_frame)
        self.assertTrue(server.open_matches(12))
        deadline = server.handoffs[pending.conn][2]
        last_simulation = match.tick
        client.queue_reliable(20308, card_message(player))
        bad_lease = [(0x0400000000000001, 0, 1 << 32, 1, 1)]
        client.queue_entities([
            world.EntityUpdate(
                player.body, world.OP_UPDATE, lambda origin: world.update(origin, {90: [bad_lease]})
            )
        ])

        session.run_loop([None])
        now = drain_now
        self.assertEqual(match.tick, last_simulation)
        self.assertLessEqual(server.tick, MAX_LEASE_FRAME)
        self.assertIsNone(session.create_match(ROOM, [OWNER], card=12))
        self.assertIsNone(session.apply("join_match", {"match_id": old.match_id, "players": [OWNER]}))
        self.assertEqual(server.open_matches(12), [])
        self.assertEqual(server.handoffs[pending.conn][2], deadline)
        waiting_player = server.handoffs[pending.conn][1]

        now = self.until(session, now, lambda: bool(self.leave_attempts(owner)))
        (first,) = self.leave_attempts(owner)
        self.assertEqual([kind for _, kind, _ in first[2]], [20308, LEAVE_GAME])
        self.assertEqual(first[3], [])
        owner.drain(lose=True)
        now = self.until(session, now, lambda: any(seq > first[0] for seq, *_ in self.frames(owner)))
        later = next(row for row in self.frames(owner) if row[0] > first[0])
        self.assertEqual(later[2:], ([], []))
        owner.drain()
        owner.send(now=now)  # ACK the later frame with a gap for the lost leave packet
        now = self.until(session, now, lambda: len(self.leave_attempts(owner)) == 2)
        _, resent = self.leave_attempts(owner)
        self.assertGreater(resent[0], first[0])
        self.assertGreater(resent[1], first[1])
        self.assertEqual(resent[2:], first[2:])
        self.assertLessEqual(resent[1], MAX_LEASE_FRAME)
        owner.drain()

        waiting = Peer(session, pending, sent, ADDRESS)
        payload = BitWriter()
        messages.write_channel(payload, [(0, messages.encode(21601, {}))])
        waiting.send(flags=SYN, payload=payload.getvalue(), now=now)
        self.assertIsNone(waiting_player.loaded_at)
        waiting.send(now=now)
        now = self.until(session, now, lambda: bool(self.leave_attempts(waiting)))
        ids = [kind for _, _, reliable, _ in self.frames(waiting) for _, kind, _ in reliable]
        self.assertEqual(ids, [LEAVE_GAME])
        waiting.drain()
        waiting.send(flags=FIN, now=now)
        owner.send(flags=FIN, now=now)
        self.assertTrue(script.retired)
        self.assertEqual(server.clients, {})
        self.assertIsNone(session.create_match(ROOM, [OWNER]))
        session.report_callbacks()
        now = self.until(session, now, lambda: server.tick == FIRST_TICK)
        self.assertEqual((server.matches, server.handoffs), ([], {}))

        (fresh,) = session.create_match(ROOM, [OWNER])
        new, current, now = self.connect_soldier(session, sent, fresh, now)
        self.assertNotEqual(fresh.conn, old.conn)
        self.assertNotEqual((fresh.key_in, fresh.key_out), (old.key_in, old.key_out))
        self.assertEqual(current.client.address, client.address)
        self.assertLess(current.body_script.frame, script.frame)
        owner.send(payload=input_window(MAX_SIMULATION_TICK, 1, buttons=FIRE), now=now)
        self.assertIsNone(current.commands.last_frame)
        new.commands(current.body_script.frame + 1, 1, FIRE, now=now)
        self.assertEqual(current.body_script.component.instances[11].vars[53].value(), 29)

    def test_pending_only_drain_denial_is_recorded_without_creating_a_match(self):
        session, _ = self.session(stored=True)
        (pending,) = session.create_match(ROOM, [OWNER])
        session.tick(100.0, missed=MAX_SIMULATION_TICK - FIRST_TICK + 5)
        self.assertIsNone(session.create_match(ROOM, [WAITING]))
        self.assertEqual(list(session.server.handoffs), [pending.conn])
        self.assertEqual(len(session.server.matches), 1)
        replay = replay_server_session(session.finish(), resolver=session.registry.resolve)
        self.assertTrue(replay["stored_state_verified"])

    def test_ready_callback_reopens_admission_only_after_old_callbacks(self):
        session, _ = self.session()
        server = session.server
        events = []

        def left(player):
            events.append(("left", player.account_lo, server.lock._is_owned()))
            with server.lock:
                server._tick(160.02)  # a reentrant tick still sees the active callback
            events.append(("old_admission", server.create_match(ROOM, [WAITING])))

        def ready():
            tick, locked = server.tick, server.lock._is_owned()
            tickets = server.create_match(ROOM, [WAITING])
            events.append(("ready", tick, locked, len(tickets or ())))

        server.on_leave, server.on_clock_ready = left, ready
        session.create_match(ROOM, [OWNER])
        session.tick(100.0, missed=MAX_SIMULATION_TICK - FIRST_TICK)
        session.tick(160.01)
        session.report_callbacks()
        self.assertEqual(events, [("left", OWNER[0], False), ("old_admission", None)])
        session.tick(160.03)
        session.report_callbacks()
        session.report_callbacks()
        self.assertEqual(events[-1], ("ready", FIRST_TICK, False, 1))
        self.assertEqual(len(events), 3)

    def test_hard_cap_closes_pending_handoff_despite_backward_wall_clock(self):
        left = []
        server = GameServer(
            environment=FixedSource(), on_leave=left.append,
            collision_provider_factory=lambda: StaticCollisionProvider(flat_world(0.0)),
        )
        with server.lock:
            server._tick(100.0, missed=MAX_SIMULATION_TICK - FIRST_TICK - 1)
            (pending,) = server.create_match(ROOM, [OWNER])
            player = server.handoffs[pending.conn][1]
            for _ in range(CLOCK_DRAIN_TICKS):
                server._tick(99.0, missed=CLOCK_DRAIN_TICKS * 2)
            self.assertEqual(server.tick, MAX_LEASE_FRAME)
            self.assertIn(pending.conn, server.handoffs)
            server._tick(98.0, missed=CLOCK_DRAIN_TICKS * 2)
            self.assertEqual(server.tick, MAX_LEASE_FRAME)
            self.assertEqual(server.handoffs, {})
            self.assertIsNone(server.create_match(ROOM, [OWNER]))
        server._report_leaves()
        self.assertEqual(left, [player])
        with server.lock:
            server._tick(98.0)
            self.assertEqual(server.tick, FIRST_TICK)
