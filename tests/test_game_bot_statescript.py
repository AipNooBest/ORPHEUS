"""Bot statescript delivery through the encrypted Client path and a modeled peer."""

import unittest
from unittest.mock import patch

from test_game_combat import Run
from test_game_graph_analysis import make_graph, node, output
from test_game_packet_budget import connection, decoded
from test_game_projectiles import ABILITY_2
from test_game_repeated_lifecycles import wire_frame

from ow174.game import bodytrace, bots, combat, movelog, projectiles, statescript, world
from ow174.game.body_vm_snapshot import export_component_vm
from ow174.game.content import PRACTICE_RANGE_MODE, SOLDIER
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.script import decode, graph, runtime
from ow174.game.script.bot import DIED, FIRING_GRAPH, RESET, BotScript, BotWorld
from ow174.game.script.driver import OWNER_HEALTH, Snapshot
from ow174.game.server import BOT_DESTROY_GRACE_TICKS, Client

GRAPH = 0xF0FE
PULSER = "STU_B9898052"
PULSE_KEY = (1, 0)
LIVE_VAR = 32


class AIFireWeaponTests(unittest.TestCase):
    def test_action_presses_and_releases_the_authored_slot_on_the_vm_clock(self):
        def integer(value):
            return {"$": "STUConfigVarInt", "m_value": value}

        control = make_graph(1, [node(0, "STU_D7F9E1C3", state=0, fields={
            "m_14EDDDA7": {"$": "STU_B5A0CAF0"},
            "m_20BCEE62": {"$": "STUConfigVarBool", "m_value": 1},
            "m_A60150E2": integer(3),
        })], states=[0])
        weapon = make_graph(2, [node(0, "STUStatescriptStateLogicalButton", state=0, fields={
            "m_logicalButton": integer(42),
        })], states=[0])
        weapon.fields["m_680A2CB2"] = [0]
        for buttons, end in (({"3": 42}, runtime.FINISH), ({"3": 42}, runtime.ABORT), ({}, runtime.ABORT)):
            with self.subTest(buttons=buttons, end=end):
                component = runtime.Component(bots.BOT_ENTITY, world=BotWorld(SOLDIER, 0, buttons))
                listener = component.create(weapon).state(0)
                action = component.create(control).state(0)
                listener.request(runtime.BEGIN)
                component.run_frame(10)
                action.request(runtime.BEGIN, 7)
                component.run_frame(11)
                self.assertEqual(component.held, [42] if buttons else [])
                self.assertEqual(listener.pressed_at, 182 if buttons else None)
                self.assertEqual(listener.down, bool(buttons))
                self.assertTrue(export_component_vm(component))

                action.request(end, 3)
                component.run_frame(12)
                self.assertEqual(component.held, [])
                self.assertFalse(listener.down)
                self.assertIsNone(action.button_id)

    def test_bot_death_retires_private_behavior_before_running_pending_fire(self):
        volley = "STUStatescriptStateWeaponVolley"

        def message(position, guid, target):
            return node(position, "STUStatescriptEntryGameMessage", fields={
                "m_gameMessage": {"$": "STUConfigVarGameMessage", "m_gameMessage": f"0x{guid:016X}"},
                "m_outPlug": output([target, "m_inPlug"]),
            })

        def mark(position):
            return node(position, "STUStatescriptActionSetVar", fields={
                "m_out_Var": {"$": "STUConfigVarDynamic", "m_identifier": LIVE_VAR},
                "m_value": {"$": "STUConfigVarBool", "m_value": 1},
            })

        weapon = make_graph(GRAPH, [
            node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
            node(1, "STUStatescriptStateLogicalButton", state=0, fields={
                "m_logicalButton": {"$": "STUConfigVarInt", "m_value": 20},
                "m_onGoingDownPlug": output([2, "m_beginPlug"]),
            }),
            node(2, volley, state=1, remote=0),
            message(3, RESET, 5), message(4, DIED, 5), mark(5),
        ], states=[1, 2], remote=[2], entries=[0, 3, 4])
        weapon.fields["m_680A2CB2"] = [0]
        behavior = make_graph(FIRING_GRAPH, [message(0, RESET, 1), mark(1)], entries=[0])
        definitions = {GRAPH: weapon, FIRING_GRAPH: behavior}
        body = {"graphs": [{"graph": GRAPH}], "ai_weapon_buttons": {"0": 20}}
        with (
            patch.object(graph, "entity_body", return_value=body),
            patch.object(graph, "graph", side_effect=definitions.get),
            patch.object(decode, "graph_of", side_effect=definitions.get),
        ):
            args = (0x0400000000001403, SOLDIER, bots.BOT_ENTITY, 100, 0, 200)
            control = BotScript(*args)
            control.component.edges([(20, True)])
            control.advance_live(101)
            [(shot, _, _)] = control.component.shots
            self.assertEqual((shot.node.cls, shot.index), (volley, 1))

            script = BotScript(*args, firing_position=(0.0, 0.0, 3.0))
            ai = script.behavior
            self.assertIsNotNone(ai)
            self.assertIs(ai.vars[LIVE_VAR].value(), True)
            self.assertNotIn(LIVE_VAR, script.component.instances[1].vars)
            full, snapshot = script.initial
            chunk = statescript.chunk(full, 0, 1)
            decoded = decode.read_chunk(chunk.getvalue(), chunk.count)
            self.assertEqual({number: item.graph for number, item in decoded.instances.items()}, {1: GRAPH})
            self.assertNotIn((ai.id, LIVE_VAR), snapshot.vars)

            script.component.edges([(20, True)])
            death, final, _ = script.die(101, {}, 0xA0000101)
            self.assertTrue(script.component.owner_dead)
            self.assertIsNone(script.behavior)
            self.assertTrue(ai.stopped)
            self.assertEqual(tuple(script.component.instances), (1,))
            self.assertFalse(script.component.held)
            self.assertFalse(script.component.shots)
            self.assertNotIn((1, 1), final.states)
            chunk = statescript.chunk(death, 2, 0)
            decoded = decode.read_chunk(chunk.getvalue(), chunk.count, {1: GRAPH})
            self.assertEqual(tuple(decoded.instances), (1,))
            self.assertEqual(decoded.instances[1].vars, {LIVE_VAR: (True, ())})


class BotNameplateTests(unittest.TestCase):
    def test_only_stock_practice_owner_filter_mounts_the_authored_nameplate(self):
        parent, nameplate = 0x0C18, 0x0D24
        enabled = {"$": "STUConfigVarDynamic", "m_identifier": 14680}
        exact_filter = {
            "$": "STUConfigVarFilter", "m_84065FD1": 0x3FFFFFFF, "m_4EA686DB": 0x100,
            "m_27CF71D5": 0, "m_condition": None,
        }
        condition = {"$": "STU_AC829876", "m_entity": {"$": "STU_B5A0CAF0"}, "m_filter": exact_filter}
        child_fields = {
            "m_36F7B64C": {"m_36F7B64C": {"m_C974E369": {"m_graph": "0x0580000000000D24"}}},
            "m_89402D74": [{"m_out_Var": enabled, "m_value": enabled}],
        }
        alternate = make_graph(0x0A5B, [
            node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
            node(1, "STU_37D754C8", state=11, fields=child_fields),
        ], states=[None] * 11 + [1], entries=[0])
        child = make_graph(nameplate, variables=[(14680, 0, 1)])
        cases = (
            (0x016B, PRACTICE_RANGE_MODE, condition, False, (1, 3)),
            (0x016C, PRACTICE_RANGE_MODE, condition, False, (1, 3)),
            (0x016B, 0x0230000000000014, condition, False, None),
            (0x0170, PRACTICE_RANGE_MODE, condition, True, (2, 11)),
            (0x016B, PRACTICE_RANGE_MODE, {**condition, "m_entity": {"$": "STU_1FF67CA6"}}, False, None),
            (0x016B, PRACTICE_RANGE_MODE,
             {**condition, "m_filter": {**exact_filter, "m_66747E7D": 2}}, False, None),
        )
        for hero, mode, predicate, has_alternate, expected in cases:
            with self.subTest(hero=hero, mode=mode, predicate=predicate):
                source = make_graph(parent, [
                    node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
                    node(1, "STUStatescriptStateBooleanSwitch", state=1, fields={
                        "m_condition": predicate, "m_trueSubgraphPlug": output([2, "m_beginPlug"]),
                    }),
                    node(2, "STU_37D754C8", state=3, fields=child_fields),
                ], states=[None, 1, None, 2], entries=[0])
                definitions = {parent: source, nameplate: child, alternate.index: alternate}
                roots = [{"graph": parent, "m_1EB5A024": [{
                    "m_0D09D2D9": "0x0D80000000003958", "m_value": {"$": "STUConfigVarBool", "m_value": 0},
                }]}]
                if has_alternate:
                    roots.append({"graph": alternate.index})
                with (
                    patch.object(graph, "entity_body", return_value={"graphs": roots}),
                    patch.object(graph, "graph", side_effect=definitions.get),
                    patch.object(decode, "graph_of", side_effect=definitions.get),
                ):
                    script = BotScript(
                        0x0400000000001403, 0x02E0000000000000 | hero, bots.BOT_ENTITY, 100, mode, 200,
                    )
                    payload, _ = script.initial
                    chunk = statescript.chunk(payload, 0, 1)
                    frame = decode.read_chunk(chunk.getvalue(), chunk.count)
                self.assertFalse(frame.owner)
                plates = [item for item in frame.instances.values() if item.graph == nameplate]
                self.assertEqual([item.parent for item in plates], [] if expected is None else [expected])
                self.assertNotIn(3, frame.instances[1].states)
                if expected == (1, 3):
                    self.assertEqual(plates[0].vars, {14680: (False, ())})


class SyntheticScript:
    """A verified H=0 pulse layout with a synthetic graph, independent of NPC asset extraction."""

    def __init__(self, graph):
        self.graph = graph
        self.deaths = []
        self.initial = self.frame(0, full=True)

    def graphs(self):
        return {1: self.graph}

    def frame(self, count, *, full, tick=0, live=None):
        payload = {"fresh": count > 0, "count": count}
        variables = {} if live is None else {LIVE_VAR: (live, ())}
        snap = Snapshot(
            tick, instances={1: (GRAPH, None)}, states={PULSE_KEY: (PULSER, True, payload)},
            vars={(1, var): value for var, value in variables.items()},
        )
        state = statescript.StateWire(PULSER, True, payload)
        item = statescript.InstanceWire(1, self.graph, vars=variables, states={0: state})
        encode = statescript.remote_full_frame if full else statescript.remote_delta
        return encode([item], {}, self.graphs()), snap

    def live(self, value, tick):
        self.initial = self.frame(0, full=True, tick=tick, live=value)
        return self.initial[1]

    def die(self, frame, params, sender):
        self.deaths.append((frame, params, sender))
        return self.delta(len(self.deaths), frame)

    def delta(self, count, frame):
        data, snap = self.frame(count, full=False, tick=frame)
        snap.vars.update(self.initial[1].vars)
        return data, snap, {("state", *PULSE_KEY)}

    def advance_death(self, frame, lifetime_ticks):
        return ()


class TimedScript(SyntheticScript):
    """Schedule known pulse deltas so the test measures production Combat and Client ordering."""

    def __init__(self, graph, offsets, *, immediate=True):
        super().__init__(graph)
        self.offsets, self.immediate = offsets, immediate
        self.advances, self.emitted = [], []

    def die(self, frame, params, sender):
        payload = super().die(frame, params, sender)
        return payload if self.immediate else None

    def advance_death(self, frame, lifetime_ticks):
        self.advances.append((frame, lifetime_ticks))
        death = self.deaths[0][0]
        result = []
        for offset in self.offsets[len(self.emitted):]:
            if offset > lifetime_ticks or death + offset > frame:
                break
            self.emitted.append(death + offset)
            result.append(self.delta(len(self.emitted) + int(self.immediate), self.emitted[-1]))
        return result


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
        # Keep the synthetic scripts in the fixture even when an external pack has NPC definitions.
        catalog = patch.object(graph, "entity_body", return_value=None)
        catalog.start()
        self.addCleanup(catalog.stop)
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

    def assert_live(self, chunk, *, first, last, value):
        frame = decode.read_chunk(chunk.getvalue(), chunk.count, {1: GRAPH})
        self.assertEqual((frame.owner, frame.first, frame.last), (False, first, last))
        self.assertEqual(frame.instances[1].vars, {LIVE_VAR: (value, ())})

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

    def acknowledge_baseline(self, peer, entity):
        _, _, create = self.send(peer)
        self.assertIn(entity, self.entities(create, world.OP_CREATE))
        peer.ack()
        _, _, full = self.send(peer)
        self.assert_pulse(self.only_chunk(full, entity), first=0, last=1, count=0)
        peer.ack()

    def advance_combat(self, now, tick):
        self.tick = self.match.tick = tick
        self.match.combat.update(now, tick, frames=False)

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

    def test_live_state_waits_for_create_and_full_acks_and_late_view_gets_current_full(self):
        old, peer = self.bot.entity, self.watching_peer
        script = self.bot.body_script
        peer.client.queue_bot_live(old, script.live(10.0, self.tick))
        _, _, lost_create = self.send(peer, receive=False)
        self.assertEqual(self.entities(lost_create, world.OP_CREATE), [old])
        self.assertEqual(self.chunks(lost_create, old), [])
        _, _, gap = self.send(peer)
        self.assertEqual(self.chunks(gap, old), [])
        peer.ack()
        _, _, create = self.send(peer)
        self.assertEqual(self.entities(create, world.OP_CREATE), [old])
        self.assertEqual(self.chunks(create, old), [])
        peer.ack()

        _, _, full = self.send(peer)
        self.assert_pulse(self.only_chunk(full, old), first=0, last=1, count=0)
        peer.client.queue_bot_live(old, script.live(20.0, self.tick))
        _, _, before_full_ack = self.send(peer)
        self.assertEqual(self.chunks(before_full_ack, old), [])
        peer.ack()
        _, _, live = self.send(peer)
        self.assert_live(self.only_chunk(live, old), first=2, last=2, value=20.0)
        peer.ack()
        _, _, after = self.send(peer)
        self.assertEqual(self.chunks(after, old), [])

        late = self.add_observer(3)
        self.show_bots(late)
        _, _, late_create = self.send(late)
        self.assertEqual(self.entities(late_create, world.OP_CREATE), [old])
        self.assertEqual(self.chunks(late_create, old), [])
        late.ack()
        _, _, late_full = self.send(late)
        self.assert_live(self.only_chunk(late_full, old), first=0, last=1, value=20.0)
        late.ack()
        _, _, late_after = self.send(late)
        self.assertEqual(self.chunks(late_after, old), [])
        self.assertEqual((peer.pulses, late.pulses), ([], []))

    def test_lost_live_retries_before_death_while_replacement_gets_its_own_stream(self):
        old, peer = self.bot.entity, self.watching_peer
        self.acknowledge_baseline(peer, old)
        script = self.bot.body_script
        peer.client.queue_bot_live(old, script.live(10.0, self.tick))
        _, _, lost = self.send(peer, receive=False)
        live = self.only_chunk(lost, old)
        self.assert_live(live, first=2, last=2, value=10.0)

        peer.client.queue_bot_live(old, script.live(20.0, self.tick))
        peer.client.queue_bot_live(old, script.live(30.0, self.tick))
        self.kill_bot()
        fresh = self.respawn()
        peer.client.queue_bot_live(old, script.frame(0, full=False, tick=self.tick, live=99.0)[1])
        _, _, gap = self.send(peer)
        self.assertEqual(self.entities(gap, world.OP_CREATE), [fresh])
        self.assertNotIn(old, self.entities(gap, world.OP_DESTROY))
        self.assertEqual(self.chunks(gap, old), [])
        peer.ack()

        _, _, retry = self.send(peer)
        self.assert_same_chunk(live, self.only_chunk(retry, old))
        self.assert_pulse(self.only_chunk(retry, fresh), first=0, last=1, count=0)
        self.assertNotIn((1, LIVE_VAR), peer.models[fresh].vars)
        peer.ack()
        _, _, latest = self.send(peer)
        self.assert_live(self.only_chunk(latest, old), first=3, last=3, value=30.0)
        _, _, before_live_ack = self.send(peer)
        self.assertEqual(self.chunks(before_live_ack, old), [])
        self.assertNotIn(old, self.entities(before_live_ack, world.OP_DESTROY))
        peer.ack()

        _, _, death = self.send(peer)
        self.assert_pulse(self.only_chunk(death, old), first=4, last=4, count=1)
        self.assertEqual(peer.pulses, [(old, 4, 1)])
        peer.ack()
        _, _, retired = self.send(peer)
        self.assertEqual(self.entities(retired, world.OP_DESTROY), [old])
        self.assertEqual(self.chunks(retired, old), [])
        self.assertNotIn(old, peer.models)
        self.assertIn(fresh, peer.models)

    def test_skipped_death_ticks_deliver_reliable_deltas_before_retirement(self):
        old = self.bot.entity
        shooter, watcher = self.shooting_peer, self.watching_peer
        lifetime = int(combat.BOT_RESPAWN / combat.TICK)
        script = self.bot.body_script = TimedScript(self.graph, (10, 14, lifetime + 1))
        for peer in (shooter, watcher):
            self.acknowledge_baseline(peer, old)
        self.advance_combat(10.0, self.tick)
        self.kill_bot()
        death_tick = self.tick

        _, _, lost_immediate = self.send(shooter, receive=False)
        immediate = self.only_chunk(lost_immediate, old)
        self.assert_pulse(immediate, first=2, last=2, count=1)
        _, _, witnessed = self.send(watcher)
        self.assert_pulse(self.only_chunk(witnessed, old), first=2, last=2, count=1)
        watcher.ack()
        late = self.add_observer(3)
        self.show_bots(late)
        self.acknowledge_baseline(late, old)

        # A single late tick reaches the respawn deadline. Both due deltas must
        # be queued before the old lifetime is retired, and the one past the cap omitted.
        expiry_tick = death_tick + lifetime + 5
        replacement = self.replacement(self.match.combat.next_bot)
        fresh = replacement.entity
        with patch.object(bots, "respawned", return_value=replacement):
            self.advance_combat(10.0 + combat.BOT_RESPAWN, expiry_tick)
        self.assertEqual(script.advances, [(expiry_tick, lifetime)])
        self.assertEqual(script.emitted, [death_tick + 10, death_tick + 14])
        self.assertEqual(self.match.bots, [replacement])

        # The watcher acknowledged the immediate pulse, so it is already free
        # to receive the first timed delta while the shooter's original was lost.
        _, _, first_watched = self.send(watcher)
        self.assertIn(fresh, self.entities(first_watched, world.OP_CREATE))
        self.assertNotIn(old, self.entities(first_watched, world.OP_DESTROY))
        self.assert_pulse(self.only_chunk(first_watched, old), first=3, last=3, count=2)
        watcher.ack()
        _, _, second_watched = self.send(watcher)
        self.assert_pulse(self.only_chunk(second_watched, old), first=4, last=4, count=3)
        watcher.ack()
        _, _, finished_watcher = self.send(watcher)
        self.assertIn(old, self.entities(finished_watcher, world.OP_DESTROY))

        _, _, shooter_gap = self.send(shooter)
        self.assertIn(fresh, self.entities(shooter_gap, world.OP_CREATE))
        self.assertNotIn(old, self.entities(shooter_gap, world.OP_DESTROY))
        self.assertEqual(self.chunks(shooter_gap, old), [])
        shooter.ack()
        _, _, retried_immediate = self.send(shooter)
        self.assert_same_chunk(immediate, self.only_chunk(retried_immediate, old))
        shooter.ack()

        _, _, lost_timed = self.send(shooter, receive=False)
        first_timed = self.only_chunk(lost_timed, old)
        self.assert_pulse(first_timed, first=3, last=3, count=2)
        _, _, timed_gap = self.send(shooter)
        self.assertEqual(self.chunks(timed_gap, old), [])
        self.assertNotIn(old, self.entities(timed_gap, world.OP_DESTROY))
        shooter.ack()
        _, _, retried_timed = self.send(shooter)
        self.assert_same_chunk(first_timed, self.only_chunk(retried_timed, old))
        _, _, before_ack = self.send(shooter)
        self.assertEqual(self.chunks(before_ack, old), [])
        self.assertNotIn(old, self.entities(before_ack, world.OP_DESTROY))
        shooter.ack()

        shooter.client.queue_bot_death_update(old, script.delta(4, self.tick))
        _, _, last_timed = self.send(shooter)
        self.assert_pulse(self.only_chunk(last_timed, old), first=4, last=4, count=3)
        shooter.ack()
        _, _, finished_shooter = self.send(shooter)
        self.assertIn(old, self.entities(finished_shooter, world.OP_DESTROY))
        self.assertEqual(self.chunks(finished_shooter, old), [])
        for peer in (shooter, watcher):
            self.assertEqual(peer.pulses, [(old, 2, 1), (old, 3, 2), (old, 4, 3)])

        _, _, late_view = self.send(late)
        self.assertIn(old, self.entities(late_view, world.OP_DESTROY))
        self.assertIn(fresh, self.entities(late_view, world.OP_CREATE))
        self.assertEqual(self.chunks(late_view, old), [])
        self.assertEqual(late.pulses, [])

    def test_delayed_first_delta_reaches_only_viewers_registered_at_death(self):
        old, shooter = self.bot.entity, self.shooting_peer
        script = self.bot.body_script = TimedScript(self.graph, (15,), immediate=False)
        self.acknowledge_baseline(shooter, old)
        self.advance_combat(10.0, self.tick)
        self.kill_bot()
        death_tick = self.tick
        _, _, after_death = self.send(shooter)
        self.assertEqual(self.chunks(after_death, old), [])
        shooter.ack()

        late = self.add_observer(3)
        self.show_bots(late)
        self.acknowledge_baseline(late, old)
        self.advance_combat(10.0 + 15 * combat.TICK, death_tick + 15)
        self.assertEqual(script.emitted, [death_tick + 15])
        _, _, original_view = self.send(shooter)
        self.assert_pulse(self.only_chunk(original_view, old), first=2, last=2, count=1)
        _, _, late_view = self.send(late)
        self.assertEqual(self.chunks(late_view, old), [])
        self.assertEqual(late.pulses, [])


class BotHealthTests(unittest.TestCase):
    def setUp(self):
        for module in (bodytrace, movelog):
            edit = patch.object(module, "ENABLED", False)
            edit.start()
            self.addCleanup(edit.stop)
        with patch.object(bots, "make", return_value=[]):
            self.run = Run(names=("Healer", "Attacker"))
        self.healer, self.attacker = self.run.players
        match = self.run.match
        self.fight = match.combat
        x, y, z = self.healer.mover.position
        self.bot = bots.Bot(
            bots.BOT_ENTITY,
            {"hero": f"0x{SOLDIER:X}", "team": self.healer.team, "position": (x, y, z + 1),
             "rotation": (0, 0, 0, 1), "graphs": []},
            {"name": "Friendly bot", "body": "0x040000000000143E", "health": 225},
            {},
        )
        match.bots = [self.bot]
        definition = make_graph(GRAPH)
        graph_of = graph.graph
        with (
            patch.object(graph, "entity_body", return_value={"graphs": [{"graph": GRAPH}]}),
            patch.object(graph, "graph", side_effect=lambda index: definition if index == GRAPH else
                         graph_of(index)),
        ):
            match._prepare_bot_script(self.bot)
        self.script = self.bot.body_script
        self.assertIsInstance(self.script, BotScript)
        self.target = next(target for target in self.fight._targets() if target.owner is self.bot)
        self.vitals = self.fight.vitals_of(self.target)

    def test_nonlethal_damage_updates_scripted_bot_health_at_its_existing_frame(self):
        start = self.script.component.now
        self.fight._damage(self.attacker, self.target, 20, False, None, self.run.frame)
        self.assertEqual(self.vitals.total, 205.0)
        self.assertEqual(self.script.component.entity_var(OWNER_HEALTH).value(), self.vitals.total)
        self.assertEqual(self.script.component.now, start)
        self.assertFalse(self.script.component.owner_dead)

    def test_biotic_field_keeps_healing_a_friendly_scripted_bot(self):
        self.vitals.damage(20)
        self.run.step({self.healer: (0, 0, ABILITY_2)})
        for _ in range(60):
            self.run.step({})

        shots = self.fight.projectiles.shots
        self.assertEqual(len(shots), 1, "healing an NPC must not remove the active field")
        (field,) = shots
        self.assertIsNotNone(field.field_tick)
        one_heal = projectiles.HEAL_PER_SECOND * projectiles.FRAME_MS / 1000
        self.assertGreater(self.vitals.total, 205.0 + one_heal)
        self.assertEqual(self.script.component.entity_var(OWNER_HEALTH).value(), self.vitals.total)
