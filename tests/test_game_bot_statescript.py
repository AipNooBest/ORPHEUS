"""Bot statescript delivery through the encrypted Client path and a modeled peer."""

import struct
import unittest
from unittest.mock import patch

from test_game_combat import Run
from test_game_graph_analysis import make_graph, node, output
from test_game_packet_budget import connection, decoded
from test_game_projectiles import ABILITY_2
from test_game_repeated_lifecycles import wire_frame

from ow174.game import bodytrace, bots, combat, movelog, observers, projectiles, statescript, world
from ow174.game.body_vm_snapshot import export_component_vm
from ow174.game.content import PRACTICE_RANGE_MODE, SOLDIER
from ow174.game.link import ACK, KIND_CONTROL, LinkCipher, Packet, ReceiveWindow
from ow174.game.script import decode, expr, graph, runtime
from ow174.game.script.bot import DIED, FIRING_GRAPH, RESET, BotScript, BotWorld
from ow174.game.script.driver import OWNER_HEALTH, BodyStream, Snapshot
from ow174.game.server import BOT_DESTROY_GRACE_TICKS, LEAVE_GAME, Client

GRAPH = 0xF0FE
PULSER = "STU_B9898052"
PULSE_KEY = (1, 0)
LIVE_VAR = 32
FEED_GRAPH, FEED_STATE, FEED_KILLER = 0x0C18, 8, 392
NAMEPLATE, DEATH_VOICE, BOT_WEAPON = 0x0D24, 0x0C17, 0x0BA3


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
            [(shot, _, _, _)] = control.component.shots
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
            final = script.die(101, {}, 0xA0000101)
            self.assertTrue(script.component.owner_dead)
            self.assertIsNone(script.behavior)
            self.assertTrue(ai.stopped)
            self.assertEqual(tuple(script.component.instances), (1,))
            self.assertFalse(script.component.held)
            self.assertFalse(script.component.shots)
            self.assertNotIn((1, 1), final.states)
            stream = BodyStream(script.component.entity)
            stream.base = snapshot
            death, _ = observers.delta(script, stream, final)
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
        self.initial = self.frame()

    @property
    def latest_live(self):
        return self.initial[1]

    def graphs(self):
        return {1: self.graph}

    def snapshot(self, count, tick=0, live=None):
        payload = (("count", count), ("fresh", count > 0))
        variables = {} if live is None else {LIVE_VAR: (live, ())}
        return Snapshot(
            tick, instances={1: (GRAPH, None)}, states={PULSE_KEY: (PULSER, True, payload)},
            vars={(1, var): value for var, value in variables.items()},
        )

    def frame(self, *, tick=0, live=None):
        snap = self.snapshot(0, tick, live)
        return observers.full_frame(self, snap), snap

    def live(self, value, tick):
        self.initial = self.frame(tick=tick, live=value)
        return self.initial[1]

    def die(self, frame, params, sender):
        self.deaths.append((frame, params, sender))
        return self.delta(len(self.deaths), frame)

    def delta(self, count, frame):
        snap = self.snapshot(count, frame)
        snap.vars.update(self.initial[1].vars)
        return snap

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


class NameplateScript(TimedScript):
    """Target-build H=0 graph/state identities in a small portable transport fixture."""

    def __init__(self, definitions, killer):
        self.definitions, self.killer = definitions, killer
        super().__init__(definitions[1], (6,))

    def graphs(self):
        return self.definitions

    def snapshot(self, count, tick=0, live=None):
        snapshot = Snapshot(tick, instances={
            1: (FEED_GRAPH, None), 2: (DEATH_VOICE, None), 3: (BOT_WEAPON, None), 4: (NAMEPLATE, (1, 3)),
        })
        snapshot.vars[(4, 14680)] = (False, ())
        snapshot.states[(4, 76)] = ("STU_9D7BF987", True, ())
        snapshot.states[(3, 13)] = (
            "STUStatescriptStateWeaponVolley", True, (("counter", int(live or 1)),),
        )
        if count:
            snapshot.vars[(1, FEED_KILLER)] = (expr.Entity(self.killer), ())
            snapshot.vars[(1, 1821)] = (True, ())
            snapshot.states[(1, FEED_STATE)] = (PULSER, True, (("count", 1), ("fresh", True)))
        if count > 1:
            snapshot.states[(2, 16)] = (PULSER, True, (("count", 1), ("fresh", True)))
        return snapshot


class WirePeer:
    def __init__(self, client, cipher):
        self.client, self.cipher = client, cipher
        self.window = ReceiveWindow()
        self.seq = 0
        self.models = {}
        self.pulses = []
        self.feed_pulses = []

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
            for instance in frame.instances.values():
                if instance.graph == GRAPH:
                    state, pulses = 0, self.pulses
                elif instance.graph == FEED_GRAPH:
                    state, pulses = FEED_STATE, self.feed_pulses
                else:
                    continue
                pulse = instance.states.get(state)
                if pulse is not None and pulse[0] and pulse[1].get("fresh"):
                    pulses.append((record.entity, frame.last, pulse[1]["count"]))

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
            # The fixture starts with the mode UI loaded and tests NPC stream loss.
            peer.client._mode_create_acked = True
            peer.client.player.mode_script.delivered = 1
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

    def install_kill_feed_bot(self):
        def death_graph(index, state, params=(), variables=()):
            entry = node(0, "STUStatescriptEntryGameMessage", fields={
                "m_gameMessage": {"$": "STUConfigVarGameMessage", "m_gameMessage": f"0x{DIED:016X}"},
                "m_params": list(params),
                "m_outPlug": output([1, "m_beginPlug"]),
            })
            return make_graph(index, [entry, node(1, PULSER, state=state, remote=0)],
                              states=[None] * state + [1], remote=[1], entries=[0], variables=variables)

        killer = {"m_B5051BCE": "0x0A40000000000012", "m_out_Var": {
            "$": "STUConfigVarDynamic", "m_identifier": FEED_KILLER,
        }}
        feed = death_graph(FEED_GRAPH, FEED_STATE, (killer,), ((FEED_KILLER, 0, 1),))
        ordinary = death_graph(GRAPH, 0)
        definitions = {feed.index: feed, ordinary.index: ordinary}
        original_graph, original_decode = graph.graph, decode.graph_of
        load = patch.object(graph, "graph", lambda index: definitions.get(index) or original_graph(index))
        read = patch.object(
            decode, "graph_of", lambda index: definitions.get(index) or original_decode(index),
        )
        for edit in (load, read):
            edit.start()
            self.addCleanup(edit.stop)
        with patch.object(graph, "entity_body", return_value={"graphs": [{"graph": i} for i in definitions]}):
            self.bot.body_script = BotScript(
                self.bot.body, self.bot.hero, self.bot.entity, self.tick, self.match.game_map.mode_guid,
                self.bot.health,
            )

        watcher = self.watching_peer
        watcher.client._mode_create_acked = False
        watcher.client.player.mode_script.delivered = 0
        watcher.client.queue_entities([
            world.EntityUpdate(world.GAME_MODE_ENTITY, world.OP_CREATE, self.match._game_mode_create),
        ])
        for peer in (self.shooting_peer, watcher):
            self.show_bots(peer)
            _, _, create = self.send(peer)
            self.assertIn(self.bot.entity, self.entities(create, world.OP_CREATE))
            peer.ack()
            _, _, full = self.send(peer)
            chunk = self.only_chunk(full, self.bot.entity)
            frame = decode.read_chunk(chunk.getvalue(), chunk.count)
            self.assertTrue(frame.full)
            self.assertEqual({item.graph for item in frame.instances.values()}, set(definitions))
            peer.ack()

    def kill_feed_bot(self):
        target = next(target for target in self.match.combat._targets() if target.owner is self.bot)
        self.match.combat._damage(self.shooter, target, 200, False, None, self.tick)
        self.match.combat._send_health()
        self.assertEqual(self.bot.body_script.death_frame, self.tick)

    def install_nameplate_bots(self):
        def remote_graph(index, state, kind, variables=()):
            return make_graph(index, [node(0, kind, state=state, remote=0)],
                              states=[None] * state + [0], remote=[0], variables=variables)

        definitions = {
            1: make_graph(FEED_GRAPH, [
                node(0, "STU_37D754C8", state=3), node(1, PULSER, state=FEED_STATE, remote=0),
            ], states=[None] * 3 + [0] + [None] * 4 + [1], remote=[1],
                variables=((FEED_KILLER, 0, 1), (1821, 0, 1))),
            2: remote_graph(DEATH_VOICE, 16, PULSER),
            3: remote_graph(BOT_WEAPON, 13, "STUStatescriptStateWeaponVolley"),
            4: remote_graph(NAMEPLATE, 76, "STU_9D7BF987", ((14680, 0, 1),)),
        }
        by_index = {definition.index: definition for definition in definitions.values()}
        native_graph = decode.graph_of
        graphs = patch.object(decode, "graph_of", lambda index: by_index.get(index) or native_graph(index))
        graphs.start()
        self.addCleanup(graphs.stop)
        self.bot.spawner["hero"] = "0x02E000000000016B"
        self.bot.hero = int(self.bot.spawner["hero"], 16)
        living = bots.respawned(self.bot, self.bot.entity + 1, self.tick)
        self.match.combat.next_bot = living.entity + 1
        self.match.bots.append(living)
        for bot in self.match.bots:
            bot.body_script = NameplateScript(definitions, self.shooter.body)

        for peer in (self.watching_peer, self.shooting_peer):
            client = peer.client
            client._mode_create_acked = False
            client.player.mode_script.delivered = 0
            client.queue_entities([
                world.EntityUpdate(world.GAME_MODE_ENTITY, world.OP_CREATE, self.match._game_mode_create),
            ])
            self.show_bots(peer)
            _, _, creates = self.send(peer)
            self.assertEqual(set(self.entities(creates, world.OP_CREATE)),
                             {self.bot.entity, living.entity, world.GAME_MODE_ENTITY})
            peer.ack()
            _, _, records = self.send(peer)
            for bot in self.match.bots:
                frame = self.bot_frame(peer, records, bot.entity)
                self.assertTrue(frame.full)
                self.assertFalse(frame.owner)
                self.assertEqual(frame.instances[3].states[13][1]["counter"], 1)
                self.assertNotIn(NAMEPLATE, {item.graph for item in frame.instances.values()})
            peer.ack()
        return living

    def bot_frame(self, peer, records, entity):
        chunk = self.only_chunk(records, entity)
        return decode.read_chunk(chunk.getvalue(), chunk.count, peer.models[entity].graphs)

    def assert_nameplate_child(self, frame):
        child, = (item for item in frame.instances.values() if item.graph == NAMEPLATE and item.descriptor)
        self.assertEqual(child.parent, (1, 3))
        self.assertEqual(child.vars[14680], (False, ()))
        self.assertEqual(child.states[76], (True, {}))

    def test_nameplate_waits_for_each_mode_ack_without_delaying_damage_or_death_frames(self):
        living = self.install_nameplate_bots()
        victim = self.bot.entity
        self.show_shooter_identity()
        watcher, shooter = self.watching_peer, self.shooting_peer
        for peer in (watcher, shooter):
            peer.client.queue_mode_frame(combat.mode_frame({}))
        _, _, ready = self.send(watcher)
        mode = self.only_chunk(ready, world.GAME_MODE_ENTITY)
        self.assertEqual(decode.read_chunk(mode.getvalue(), mode.count).instances[1].graph, combat.MODE_UI)
        self.assertEqual(self.chunks(ready, victim) + self.chunks(ready, living.entity), [])
        watcher.ack()
        _, _, dropped = self.send(shooter, receive=False)
        lost_mode = self.only_chunk(dropped, world.GAME_MODE_ENTITY)
        self.assertEqual(self.chunks(dropped, victim) + self.chunks(dropped, living.entity), [])

        self.advance_combat(10.0, self.tick)
        target = next(target for target in self.match.combat._targets() if target.owner is self.bot)
        death_tick = self.tick
        self.match.combat._damage(self.shooter, target, 200, True, None, death_tick)
        self.match.combat._send_health()
        for peer in (watcher, shooter):
            _, reliable, death = self.send(peer)
            self.assert_terminal_health(death)
            frame = self.bot_frame(peer, death, victim)
            self.assertEqual(frame.last, 2, "nameplate must not consume the first post-baseline death slot")
            self.assertEqual(frame.instances[1].vars[FEED_KILLER], (expr.Entity(self.shooter.body), ()))
            self.assertEqual(frame.instances[1].vars[1821], (True, ()))
            if peer is watcher:
                self.assert_nameplate_child(frame)
                self.assert_nameplate_child(self.bot_frame(peer, death, living.entity))
                self.assertEqual(watcher.feed_pulses, [(victim, 2, 1)])
            else:
                self.assertNotIn(NAMEPLATE, peer.models[victim].graphs.values())
                self.assertEqual(self.chunks(death, living.entity), [])
                self.assertEqual(shooter.feed_pulses, [])
                hit, = (value for _, number, value in reliable if number == combat.HIT)
                self.assertEqual(hit["+0x78"], {
                    "+0x0": {"+0x0": self.bot.entity}, "+0x4": 200, "+0x8": combat.CRITICAL | combat.KILLING,
                })
                self.assertEqual(hit["+0x84"], death_tick)
                self.assertNotIn(combat.KILL_NOTICE, [number for _, number, _ in reliable])
            peer.ack()

        self.advance_combat(10.0 + 6 * combat.TICK, death_tick + 6)
        _, _, voiced = self.send(shooter)
        self.assert_same_chunk(lost_mode, self.only_chunk(voiced, world.GAME_MODE_ENTITY))
        frame = self.bot_frame(shooter, voiced, victim)
        self.assertEqual(frame.instances[2].states[16], (True, {"fresh": True, "count": 1}))
        self.assertNotIn(NAMEPLATE, shooter.models[victim].graphs.values())
        self.assertEqual(self.chunks(voiced, living.entity), [])
        shooter.ack()
        _, reliable, named = self.send(shooter)
        self.assert_nameplate_child(self.bot_frame(shooter, named, living.entity))
        self.assertNotIn(NAMEPLATE, shooter.models[victim].graphs.values())
        self.assertEqual(shooter.feed_pulses, [(victim, 4, 1)])
        self.assertIn(combat.KILL_NOTICE, [number for _, number, _ in reliable])
        shooter.ack()
        _, _, settled = self.send(shooter)
        self.assertEqual(self.chunks(settled, victim), [])
        self.assertEqual(self.chunks(settled, living.entity), [])

        self.tick = self.match.tick = death_tick + 32  # past the pulse's 500 ms freshness window
        aged = self.bot.body_script.delta(2, self.tick)
        aged.states[(2, 16)] = (PULSER, True, (("count", 1), ("fresh", False)))
        shooter.client.queue_bot_death_update(victim, aged)
        _, _, terminal = self.send(shooter)
        frame = self.bot_frame(shooter, terminal, victim)
        self.assertEqual(frame.instances[2].states[16], (True, {"fresh": False, "count": 1}))
        self.assertNotIn(NAMEPLATE, shooter.models[victim].graphs.values())
        self.assertEqual(shooter.feed_pulses, [(victim, 4, 1)])

    def test_lost_nameplate_only_chunk_does_not_block_the_next_terminal_snapshot(self):
        self.install_nameplate_bots()
        shooter, victim = self.shooting_peer, self.bot.entity
        shooter.client.queue_mode_frame(combat.mode_frame({}))
        _, _, mode = self.send(shooter)
        self.only_chunk(mode, world.GAME_MODE_ENTITY)
        shooter.ack()
        old_packet, _, old_records = self.send(shooter, receive=False)
        child_only = self.bot_frame(shooter, old_records, victim)
        self.assertEqual((child_only.first, child_only.last), (2, 2))
        self.assert_nameplate_child(child_only)
        self.assertNotIn(NAMEPLATE, shooter.models[victim].graphs.values())

        self.advance_combat(10.0, self.tick)
        target = next(target for target in self.match.combat._targets() if target.owner is self.bot)
        self.match.combat._damage(self.shooter, target, 200, True, None, self.tick)
        self.match.combat._send_health()
        _, _, terminal = self.send(shooter)
        self.assert_terminal_health(terminal)
        self.assertEqual(
            [
                (51 in fields, record.chunk is not None)
                for record, fields in terminal
                if record.entity == victim and (51 in fields or record.chunk is not None)
            ],
            [(True, False), (False, True)],
        )
        frame = self.bot_frame(shooter, terminal, victim)
        self.assertEqual((frame.first, frame.last), (2, 3))
        self.assert_nameplate_child(frame)
        self.assertEqual(frame.instances[1].vars[FEED_KILLER], (expr.Entity(self.shooter.body), ()))
        self.assertEqual(frame.instances[1].vars[1821], (True, ()))
        self.assertEqual(shooter.feed_pulses, [(victim, 3, 1)])
        shooter.ack()

        shooter.receive(old_packet, old_records)
        shooter.ack()
        _, _, settled = self.send(shooter)
        self.assertEqual(self.chunks(settled, victim), [])
        self.assertEqual(shooter.models[victim].applied, 3)
        self.assertEqual(shooter.models[victim].records[4], (NAMEPLATE, (1, 3)))
        self.assertEqual(shooter.feed_pulses, [(victim, 3, 1)])

    def test_unacked_nameplate_alone_does_not_hold_bot_destroy(self):
        self.install_nameplate_bots()
        shooter, old = self.shooting_peer, self.bot.entity
        shooter.client.queue_mode_frame(combat.mode_frame({}))
        _, _, mode = self.send(shooter)
        self.only_chunk(mode, world.GAME_MODE_ENTITY)
        shooter.ack()
        _, _, pending = self.send(shooter)
        self.assert_nameplate_child(self.bot_frame(shooter, pending, old))

        fresh = self.respawn()
        _, _, retired = self.send(shooter)
        self.assertIn(old, self.entities(retired, world.OP_DESTROY))
        self.assertIn(fresh, self.entities(retired, world.OP_CREATE))
        self.assertEqual(self.chunks(retired, old), [])
        shooter.ack()  # positively ACK the old presenter only after the destroy arrived

        _, _, settled = self.send(shooter)
        self.assertEqual(self.chunks(settled, old), [])
        self.assertNotIn(old, shooter.models)
        self.assertIsNone(shooter.client.bot_stream(old))
        self.assert_pulse(self.only_chunk(settled, fresh), first=0, last=1, count=0)

    def assert_terminal_health(self, records):
        values = [
            struct.unpack_from("<Iff", fields[51][0])[2]
            for record, fields in records if record.entity == self.bot.entity and 51 in fields
        ]
        self.assertEqual(values, [0.0])

    def shooter_possession(self, records):
        return [
            (fields[44], fields[75][0])
            for record, fields in records if record.entity == self.shooter.entity and 44 in fields
        ]

    def show_shooter_identity(self, *, receive_possession=True):
        watcher = self.watching_peer
        viewer = watcher.client.player
        self.match._show_players(viewer)
        self.match.ensure_remote_body(viewer, self.shooter)
        _, _, creates = self.send(watcher)
        self.assertIn(self.shooter.body, self.entities(creates, world.OP_CREATE))
        names = [
            fields[29][1] for record, fields in creates
            if record.entity == self.shooter.entity and record.op == world.OP_CREATE
        ]
        self.assertEqual(names, [self.shooter.name])
        watcher.ack()
        self.match._show_players(viewer)
        _, _, records = self.send(watcher, receive=receive_possession)
        possession = self.shooter_possession(records)
        body = self.shooter.body
        self.assertEqual(possession, [({0: body, 1: body, 2: body}, self.shooter.hero.guid)])
        if receive_possession:
            watcher.ack()
        return possession

    def test_kill_feed_pulse_waits_for_mode_ack_without_delaying_other_death_state(self):
        self.install_kill_feed_bot()
        self.show_shooter_identity()
        shooter, watcher, old = self.shooting_peer, self.watching_peer, self.bot.entity
        watcher.client.queue_mode_frame(combat.mode_frame({}))
        _, _, lost_mode = self.send(watcher, receive=False)
        mode = self.only_chunk(lost_mode, world.GAME_MODE_ENTITY)
        self.kill_feed_bot()
        death_tick = self.tick

        _, _, immediate = self.send(watcher)
        self.assert_terminal_health(immediate)
        self.assertEqual(watcher.pulses, [(old, 2, 1)])
        self.assertEqual(watcher.feed_pulses, [])
        _, _, shooter_death = self.send(shooter)
        self.assert_terminal_health(shooter_death)
        self.assertEqual(shooter.pulses, [(old, 2, 1)])
        self.assertEqual(shooter.feed_pulses, [(old, 2, 1)])

        watcher.ack()
        _, _, mode_retry = self.send(watcher)
        self.assert_same_chunk(mode, self.only_chunk(mode_retry, world.GAME_MODE_ENTITY))
        self.assertEqual(self.chunks(mode_retry, old), [])
        watcher.ack()
        watcher.ack()
        _, _, lost_feed = self.send(watcher, receive=False)
        lost = self.only_chunk(lost_feed, old)
        self.tick = death_tick + 29  # The admitted pulse is still retryable after 500 ms.
        _, _, gap = self.send(watcher)
        self.assertEqual(self.chunks(gap, old), [])
        watcher.ack()
        _, _, feed = self.send(watcher)
        chunk = self.only_chunk(feed, old)
        self.assert_same_chunk(lost, chunk)
        model = watcher.models[old]
        frame = decode.read_chunk(chunk.getvalue(), chunk.count, model.graphs)
        feed_id, = (number for number, index in model.graphs.items() if index == FEED_GRAPH)
        self.assertEqual(model.vars[(feed_id, FEED_KILLER)], expr.Entity(self.shooter.body))
        self.assertEqual(watcher.feed_pulses, [(old, frame.last, 1)])
        self.assertEqual(watcher.pulses, [(old, 2, 1)])
        watcher.ack()
        _, _, after = self.send(watcher)
        self.assertEqual(self.chunks(after, old), [])

    def test_expired_kill_feed_pulse_is_not_released_by_a_late_mode_ack(self):
        self.install_kill_feed_bot()
        self.show_shooter_identity()
        watcher, old = self.watching_peer, self.bot.entity
        watcher.client.queue_mode_frame(combat.mode_frame({}))
        _, _, mode = self.send(watcher)
        self.only_chunk(mode, world.GAME_MODE_ENTITY)
        self.kill_feed_bot()
        death_tick = self.tick
        _, _, immediate = self.send(watcher)
        self.assert_terminal_health(immediate)
        self.assertEqual(watcher.pulses, [(old, 2, 1)])
        self.assertEqual(watcher.feed_pulses, [])

        self.tick = death_tick + 29  # ACK arrives 32 ticks (512 ms) after death, before respawn.
        self.send(watcher)
        watcher.ack()
        self.assertEqual(watcher.client.player.mode_script.delivered, 1)
        _, _, after = self.send(watcher)
        self.assertEqual(self.chunks(after, old), [])
        self.assertIn(old, watcher.models)
        self.assertEqual(watcher.feed_pulses, [])

        self.respawn()
        _, _, retired = self.send(watcher)
        self.assertIn(old, self.entities(retired, world.OP_DESTROY))
        self.assertEqual(self.chunks(retired, old), [])

    def kill_with_lost_shooter_possession(self):
        self.install_kill_feed_bot()
        watcher, old = self.watching_peer, self.bot.entity
        watcher.client.queue_mode_frame(combat.mode_frame({}))
        _, _, mode = self.send(watcher)
        self.only_chunk(mode, world.GAME_MODE_ENTITY)
        watcher.ack()
        lost = self.show_shooter_identity(receive_possession=False)
        self.kill_feed_bot()

        _, _, immediate = self.send(watcher)
        self.assert_terminal_health(immediate)
        self.assertEqual(watcher.pulses, [(old, 2, 1)])
        self.assertEqual(watcher.feed_pulses, [])
        return lost

    def test_npc_kill_feed_waits_for_remote_killer_possession_ack(self):
        lost = self.kill_with_lost_shooter_possession()
        shooter, watcher, old = self.shooting_peer, self.watching_peer, self.bot.entity
        _, _, local = self.send(shooter)
        self.assert_terminal_health(local)
        self.assertEqual(shooter.pulses, [(old, 2, 1)])
        self.assertEqual(shooter.feed_pulses, [(old, 2, 1)])
        shooter.ack()

        watcher.ack()  # Reports the lost possession while confirming ordinary terminal state.
        _, _, retry = self.send(watcher)
        self.assertEqual(self.shooter_possession(retry), lost)
        self.assertEqual(self.chunks(retry, old), [])
        _, _, before_ack = self.send(watcher)
        self.assertEqual(self.chunks(before_ack, old), [])
        self.assertEqual(watcher.feed_pulses, [])
        watcher.ack()
        _, _, restored = self.send(watcher)
        chunk = self.only_chunk(restored, old)
        model = watcher.models[old]
        frame = decode.read_chunk(chunk.getvalue(), chunk.count, model.graphs)
        feed_id, = (number for number, index in model.graphs.items() if index == FEED_GRAPH)
        self.assertFalse(frame.owner)
        self.assertEqual(model.vars[(feed_id, FEED_KILLER)], expr.Entity(self.shooter.body))
        self.assertEqual(watcher.feed_pulses, [(old, frame.last, 1)])
        self.assertEqual(watcher.pulses, [(old, 2, 1)])
        watcher.ack()
        _, _, after = self.send(watcher)
        self.assertEqual(self.chunks(after, old), [])

    def test_late_killer_possession_ack_does_not_admit_feed_after_go_home(self):
        lost = self.kill_with_lost_shooter_possession()
        watcher, old = self.watching_peer, self.bot.entity
        watcher.client.go_home(10.0)
        watcher.ack()  # The old loss is still processed while leaving.
        _, reliable, retry = self.send(watcher)
        self.assertIn(LEAVE_GAME, [msg_id for _, msg_id, _ in reliable])
        self.assertEqual(self.shooter_possession(retry), lost)
        self.assertEqual(self.chunks(retry, old), [])

        watcher.ack()  # Positive ACK of the current association after go_home.
        _, _, after = self.send(watcher)
        self.assertEqual(self.chunks(after, old), [])
        self.assertEqual(watcher.feed_pulses, [])

    def test_deferred_npc_feed_does_not_name_a_destroyed_local_killer_body(self):
        self.install_kill_feed_bot()
        shooter, player = self.shooting_peer, self.shooter
        old = player.body
        shooter.client._mode_create_acked = False
        player.mode_script.delivered = 0
        shooter.client.queue_entities([
            *self.match.own_body_updates(player),
            world.EntityUpdate(
                player.entity, world.OP_CREATE, lambda origin: self.match._player_create(origin, player)
            ),
            world.EntityUpdate(world.GAME_MODE_ENTITY, world.OP_CREATE, self.match._game_mode_create),
        ])
        _, _, creates = self.send(shooter)
        self.assertIn(old, self.entities(creates, world.OP_CREATE))
        self.assertEqual(self.shooter_possession(creates), [({0: old, 1: old, 2: old}, player.hero.guid)])
        shooter.ack()

        self.kill_feed_bot()
        _, _, terminal = self.send(shooter)
        self.assert_terminal_health(terminal)
        self.assertEqual(shooter.feed_pulses, [])
        shooter.ack()
        self.assertTrue(self.match.switch_hero(player, player.hero))
        _, _, retired = self.send(shooter)
        self.assertIn(old, self.entities(retired, world.OP_DESTROY))
        self.assertNotIn(old, shooter.models)
        self.assertNotEqual(player.body, old)
        shooter.ack()

        # The mode becomes ready just a few ticks after death. The body named
        # in that pending pulse is already gone from this peer.
        shooter.client.queue_mode_frame(combat.mode_frame({}))
        _, _, mode = self.send(shooter)
        self.only_chunk(mode, world.GAME_MODE_ENTITY)
        shooter.ack()
        self.assertEqual(player.mode_script.delivered, 1)
        _, _, after = self.send(shooter)
        self.assertEqual(self.chunks(after, self.bot.entity), [])
        self.assertEqual(shooter.feed_pulses, [])

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
                if offset == 0:
                    sequence = client.seq
                    client.send_frame(match.tick, 11.0)
                    client.acked(sequence + 1, 1)
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

    def test_respawn_discards_a_lost_create_for_only_the_unacknowledged_peer(self):
        shooter, watcher, old = self.shooting_peer, self.watching_peer, self.bot.entity
        self.acknowledge_baseline(watcher, old)
        lost, _, records = self.send(shooter, receive=False)
        self.assertEqual(self.entities(records, world.OP_CREATE), [old])
        _, _, gap = self.send(shooter)
        self.assertEqual(self.entities(gap, world.OP_CREATE), [])
        shooter.ack()  # later frame reports the old create lost and queues its retry
        self.assertTrue(
            any(item.entity == old and item.op == world.OP_CREATE for item in shooter.client.entities)
        )
        self.assertFalse(shooter.client.bot_create_acked(old))

        self.kill_bot()
        pending = shooter.client._combat_notices[old]
        self.assertEqual([number for number, _ in pending], [combat.HIT, combat.KILL_NOTICE])
        fresh = self.respawn()

        _, reliable, replacement = self.send(shooter)
        self.assertEqual(self.entities(replacement, world.OP_CREATE), [fresh])
        self.assertEqual(self.entities(replacement, world.OP_DESTROY), [old])
        self.assertEqual(self.chunks(replacement, old), [])
        self.assertFalse(any(number in (combat.HIT, combat.KILL_NOTICE) for _, number, _ in reliable))
        shooter.window.accept(lost.seq)  # stale positive ACK cannot reactivate this body
        shooter.ack()
        self.assertNotIn(old, shooter.client._bot_scripts)
        self.assertFalse(shooter.client.bot_create_acked(old))
        self.assertTrue(shooter.client.bot_create_acked(fresh))
        _, _, full = self.send(shooter)
        self.assert_pulse(self.only_chunk(full, fresh), first=0, last=1, count=0)

        _, _, terminal = self.send(watcher)
        self.assertEqual(self.entities(terminal, world.OP_CREATE), [fresh])
        self.assertNotIn(old, self.entities(terminal, world.OP_DESTROY))
        self.assert_pulse(self.only_chunk(terminal, old), first=2, last=2, count=1)
        watcher.ack()
        _, _, retired = self.send(watcher)
        self.assertIn(old, self.entities(retired, world.OP_DESTROY))

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

    def test_live_full_is_encoded_for_viewers_and_retries_keep_the_registered_baseline(self):
        child = make_graph(GRAPH - 1)
        definition = make_graph(GRAPH, [
            node(0, "STUStatescriptEntry", fields={"m_outPlug": output([1, "m_beginPlug"])}),
            node(1, "STU_37D754C8", state=0, fields={
                "m_36F7B64C": {"m_36F7B64C": {"m_C974E369": {"m_graph": f"0x058000000000{child.index:04X}"}}},
            }),
            node(2, "STUStatescriptEntryGameMessage", fields={
                "m_gameMessage": {"$": "STUConfigVarGameMessage", "m_gameMessage": f"0x{DIED:016X}"},
                "m_outPlug": output([1, runtime.ABORT_PLUG]),
            }),
        ], states=[1], entries=[0, 2], variables=[(LIVE_VAR, 0, 1)])
        definitions = {GRAPH: definition, child.index: child}
        old, first, later = self.bot.entity, self.shooting_peer, self.watching_peer
        with (
            patch.object(graph, "entity_body", return_value={"graphs": [{"graph": GRAPH}]}),
            patch.object(graph, "graph", side_effect=definitions.get),
            patch.object(decode, "graph_of", side_effect=definitions.get),
            patch.object(observers, "full_frame", wraps=observers.full_frame) as encode,
        ):
            script = self.bot.body_script = BotScript(
                self.bot.body, self.bot.hero, old, self.tick, self.match.game_map.mode_guid, self.bot.health,
            )
            instance = script.component.instances[1]
            instance.write((runtime.INSTANCE, LIVE_VAR), 10.0)
            snapshot = script.advance_live(self.tick + 1)
            self.assertEqual(encode.call_count, 1)  # only the bootstrap frame
            self.assertIs(script.latest_live, snapshot)

            self.show_bots(first)
            self.assertEqual(encode.call_count, 2)
            _, _, create = self.send(first)
            self.assertIn(old, self.entities(create, world.OP_CREATE))
            first.ack()
            _, _, dropped = self.send(first, receive=False)
            frozen = self.only_chunk(dropped, old)
            self.assert_live(frozen, first=0, last=1, value=10.0)

            for value in (20.0, 30.0):
                instance.write((runtime.INSTANCE, LIVE_VAR), value)
                script.advance_live(script.frame + 1)
            self.assertEqual(encode.call_count, 2)
            self.show_bots(later)
            self.assertEqual(encode.call_count, 3)
            self.assertIs(script.initial[1], script.latest_live)
            self.assertEqual(encode.call_count, 3)

            _, _, gap = self.send(first)
            self.assertEqual(self.chunks(gap, old), [])
            first.ack()
            _, _, retry = self.send(first)
            self.assert_same_chunk(frozen, self.only_chunk(retry, old))
            _, _, create = self.send(later)
            self.assertIn(old, self.entities(create, world.OP_CREATE))
            later.ack()
            _, _, full = self.send(later)
            self.assert_live(self.only_chunk(full, old), first=0, last=1, value=30.0)
            self.assertEqual(encode.call_count, 3)

            # Death aborts the child, but a late viewer's baseline still describes
            # the last live graph tree without the terminal transition.
            instance.write((runtime.INSTANCE, LIVE_VAR), 40.0)
            script.advance_live(self.tick)
            self.assertEqual(encode.call_count, 3)
            terminal = script.die(self.tick + 1, {}, self.shooter.body)
            self.assertNotIn(2, terminal.instances)
            payload, snapshot = script.initial
            self.assertEqual(encode.call_count, 4)
            self.assertIn(2, snapshot.instances)
            wire = statescript.chunk(payload, 0, 1)
            self.assert_live(wire, first=0, last=1, value=40.0)
            frame = decode.read_chunk(wire.getvalue(), wire.count)
            self.assertEqual((frame.instances[2].graph, frame.instances[2].parent), (child.index, (1, 0)))

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
        peer.client.queue_bot_live(old, script.snapshot(0, tick=self.tick, live=99.0))
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
