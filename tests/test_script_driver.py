"""Body statescript frames: the encoders against the client's reader layouts (bit by bit), and the driver's
frames for a scripted command sequence read back into the states the server's runtime has."""

import logging
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import statescript
from ow174.game.bits import BitReader, BitWriter
from ow174.game.script import decode, runtime
from ow174.game.script import driver as driver_module
from ow174.game.script import graph as graphs
from ow174.game.script.driver import POLLED_SWITCHES, SETTLE_FRAMES, TRUSTED_NATIVES, BodyScript
from ow174.game.script.expr import Asset, Entity, Handle, Vec3
from ow174.game.statescript import (
    Binding,
    EventWire,
    InstanceWire,
    StateWire,
    chunk,
    count,
    mask,
    owner_delta,
    write_bindings,
    write_events,
    write_payload,
    write_value,
)
from ow174.game.world import OP_CREATE, EntityUpdate

SOLDIER = 0x02E000000000006E
START = 1000
FIRE, RELOAD = 0x01, 0x400
# Active states the server has but cannot send (no payload writer for their class): none for Soldier.
UNSENDABLE: set = set()


def bitstring(bits: BitWriter) -> str:
    return "".join(str((bits.value >> k) & 1) for k in range(bits.count))


def reader(bits: BitWriter) -> BitReader:
    return BitReader(bits.getvalue())


class CodecTests(unittest.TestCase):
    def test_masks(self):
        out = BitWriter()
        mask(out, 5, {1, 4})
        self.assertEqual(bitstring(out), "01001")
        out = BitWriter()
        mask(out, 20, {10})  # 3 groups: only the second is present
        self.assertEqual(bitstring(out), "010" + "00100000")
        self.assertEqual(decode.mask(reader(out), 20), {10})

    def test_counts(self):
        for value, bits in [(0, "0"), (1, "10"), (2, "110"), (5, "111" + "0" + "1010")]:
            out = BitWriter()
            count(out, value)
            self.assertEqual(bitstring(out), bits)
            self.assertEqual(decode.count(reader(out)), value)

    def test_values_read_back(self):
        for value in [
            None, True, False, 0, 15, -1, 127, 300, -40000, 70000, 0.0, 3.0, -2.0, 0.5, 1650.0,
            Asset(0x0D80000000006161), Entity(0xA0000101), Entity(0), Handle(1, 11, 11, 0),
            Vec3(1.0, 0.0, -2.5), (False, True, False), (), "text",
        ]:  # fmt: skip
            out = BitWriter()
            write_value(out, value)
            got = decode.read_value(reader(out))
            self.assertEqual(got, value)
            self.assertIs(type(got), type(value))

    def test_small_ints_use_the_short_tags(self):
        out = BitWriter()
        write_value(out, 7)
        self.assertEqual(bitstring(out), "1000" + "1110")  # tag 1, 4 bits
        out = BitWriter()
        write_value(out, 30.0)
        self.assertEqual(bitstring(out), "0110" + "01111000")  # tag 6: a float from an int8

    def test_event_lists(self):
        # A Wait (state 51, 7 bits) at 16 * 1003 + 452 and a volley finish timer (param 2) at the same time.
        out = BitWriter()
        events = [EventWire(16 * 1003 + 452, 51), EventWire(16 * 1003 + 452, 10, param=2)]
        write_events(out, events, 16 * 1003, 7)
        expected = (
            "00" + "1100110" + "0" + "1" + "10" + "0010001110000000"  # code 0, st51, +452 (w_var 16 bits)
            + "10" + "0" + "0" + "0100" + "0101000" + "1"  # code 1, p 0, TmrU 2, st10, same time
            + "11"
        )  # fmt: skip
        self.assertEqual(bitstring(out), expected)

    def test_hit_list_and_flag_payloads(self):
        out = BitWriter()
        write_payload(out, "STU_77E13026", {"flag": True}, 16000, 16)  # quick melee's hit list
        self.assertEqual(bitstring(out), "00000" + "1")  # w_u16 0, the flag
        got = decode.read_payload(reader(out), "STU_77E13026", 16000, 16)
        self.assertEqual(got, {"hits": (), "flag": True})
        for value in (0, 5, 63, 64, 1000, 8191):
            out = BitWriter()
            write_payload(out, "STU_96F1DBCF", {"value": value}, 16000, 16)
            self.assertEqual(out.count, 7 if value < 64 else 14)
            self.assertEqual(decode.read_payload(reader(out), "STU_96F1DBCF", 16000, 16), {"value": value})

    def test_a_frame_wait_sends_the_frame_it_ends_in(self):
        # STU_D74D4F47 (01CF st120): the client builds the game's class, whose property block sends a u32,
        # the frame it finishes in (state +0x238, flags 0); its class reader reads nothing more.
        out = BitWriter()
        write_payload(out, "STU_D74D4F47", {"target": 9199}, 16 * 9199, 16)
        self.assertEqual(bitstring(out), format(9199, "032b")[::-1])
        self.assertEqual(decode.read_payload(reader(out), "STU_D74D4F47", 16 * 9199, 16), {"target": 9199})

    def test_own_instance_bindings_take_the_short_form(self):
        rifle = graphs.graph(0x0254)
        out = BitWriter()
        write_bindings(out, (Binding(11, 29),), 11, {11: rifle})
        # count 1, no path, SIID 0 (the variable's own instance), state 29 in 7 bits, no slot, no weight
        self.assertEqual(bitstring(out), "10" + "0" + "0" + "1011100" + "0" + "0" + "0")
        got = decode.read_bindings(reader(out), 11, {11: 0x0254})
        self.assertEqual(got, (decode.DecodedBinding(11, 29, 0, 0.0, True),))


class DeltaTests(unittest.TestCase):
    def graphs(self):
        return {10: graphs.graph(0x0015), 11: graphs.graph(0x0254)}

    def test_a_volley_frame_reads_back(self):
        rifle = self.graphs()[11]
        item = InstanceWire(11, rifle)
        item.vars[53] = (29, ())
        item.states[8] = StateWire("STUStatescriptStateLogicalButton", True, {"counter": 1})
        item.states[13] = StateWire("STUStatescriptStateBooleanSwitch", True, {"current": False})
        item.states[29] = StateWire("STUStatescriptStateStack", True, {"top": True, "under": False})
        volley = {"start": 16 * 1004, "counter": 1}
        item.states[10] = StateWire("STUStatescriptStateWeaponVolley", True, volley)
        item.events = [EventWire(16 * 1004 + 3222, 10, param=2)]
        bindings = (Binding(11, 29, 0, -10.0, False),)
        frame = owner_delta(1005, True, [item], {33: (None, bindings)}, self.graphs())
        out = BitWriter()
        out.w_u32(1)
        out.w_var(0)
        out.bit(0)
        out.w_var(frame.count)
        out.append(frame)
        got = decode.read_chunk(out.getvalue(), out.count, {10: 0x0015, 11: 0x0254})
        self.assertEqual((got.owner, got.correction, got.cmfd), (True, True, 1005))
        rifle_got = got.instances[11]
        self.assertEqual(rifle_got.vars[53], (29, ()))
        self.assertEqual(rifle_got.states[10], (True, {**volley, "offset": 0, "volleys": 1}))
        self.assertEqual(rifle_got.states[29], (True, {"top": True, "under": False}))
        self.assertEqual(rifle_got.states[8], (True, {"counter": 1}))
        self.assertEqual(rifle_got.events, [(16 * 1004 + 3222, 10, False, 2)])
        binding = got.entity_vars[33][1][0]
        self.assertEqual(binding, decode.DecodedBinding(11, 29, 0, -10.0, False))

    def test_lbss_is_the_body_up_to_more(self):
        frame = owner_delta(5, True, [], {}, {})
        bits = bitstring(frame)
        # lead, H, C, CmFD (0 + 8 bits), LBSS (0 + 8 bits) = 7, body: Inst end (5), entity vars (1), EIns (1)
        self.assertEqual(bits[:21], "011" + "0" + "10100000" + "0" + "11100000")
        self.assertEqual(bits[21:], "00000" + "0" + "0" + "0" + "00")


def without_payloads(*classes: str):
    """The encoder as it was before these classes got their payload writers: it sends them with none."""
    real = statescript.family

    def old(cls: str) -> str:
        return "none" if cls in classes else real(cls)

    patches = [mock.patch.object(statescript, "family", old), mock.patch.object(driver_module, "family", old)]
    stack = ExitStack()
    for patch in patches:
        stack.enter_context(patch)
    return stack


def soldier() -> BodyScript:
    logging.getLogger("ow174.script").setLevel(logging.ERROR)
    return BodyScript(SOLDIER, 0xA0000101, START)


class ClientModel(decode.ClientBody):
    """What the client has of the body after applying the frames in order: the instances it built itself,
    then what the chunks make; each chunk is read by that table, as the client reads it (a chunk that
    would crash the client raises decode.ClientCrash)."""

    def __init__(self, hero: int = SOLDIER) -> None:
        super().__init__(decode.initial_graphs(hero))


def server_on(body: BodyScript) -> set:
    """The networked states the server has on, less the uncertain switches it keeps off the client."""
    on = {
        (item.id, state.index)
        for item in body.component.instances.values()
        for state in item.states.values()
        if state.active and state.networked
    }
    return on - uncertain(body)


def uncertain(body: BodyScript) -> set:
    return {
        (item.id, state.index)
        for item in body.component.instances.values()
        for state in item.states.values()
        if state.node.cls in POLLED_SWITCHES and state.natives - TRUSTED_NATIVES
    }


def spawned(start: int = START) -> tuple[BodyScript, ClientModel]:
    """A Soldier body whose full frame the client has applied."""
    logging.getLogger("ow174.script").setLevel(logging.ERROR)
    body = BodyScript(SOLDIER, 0xA0000101, start)
    client = ClientModel()
    [tree] = body.spawn_frames()
    client.apply(tree)
    body.stream.arrived(tree.chunk_last)
    return body, client


def chunk_payload(update: EntityUpdate) -> tuple[int, int]:
    """A chunk's payload bits (as an int) and its length."""
    reader = BitReader(update.chunk.getvalue())
    decode.w_u32(reader)
    decode.w_var(reader)
    reader.bit()
    size = decode.w_var(reader)
    return int.from_bytes(update.chunk.getvalue(), "little") >> reader.pos & (1 << size) - 1, size


def apply_instance_list(client: ClientModel, update: EntityUpdate) -> None:
    """The instances a delta makes and destroys: the client applies its list before it reads any payload."""
    reader = BitReader(update.chunk.getvalue())
    first = decode.w_u32(reader)
    decode.w_var(reader)
    reader.bit()
    decode.w_var(reader)
    reader.bit()  # lead
    if reader.bit():  # H: C, CmFD and in a delta LBSS
        reader.bit()
        decode.w_var(reader)
        if first:
            decode.w_var(reader)
    index = 0
    while step := decode.w_u16(reader):
        index += step
        if reader.bit():
            client.instances.pop(index, None)
            client.records.pop(index, None)
        elif reader.bit():
            item = decode.DecodedInstance(index)
            decode._descriptor(reader, item)
            client.instances[index] = item.graph
            client.records[index] = (item.graph, item.parent)


def owner_chunk(body: BitWriter, cmfd: int, first: int) -> EntityUpdate:
    """An owner delta (C = 1) around a hand-made body (the instance list up to the event lists)."""
    out = BitWriter()
    out.bits(0b110, 3)  # lead 0, H 1, C 1
    out.w_var(cmfd)
    out.w_var(body.count)
    out.append(body)
    out.bits(0, 3)  # no more sub-frames, no frame events
    return EntityUpdate(0xA0000101, chunk=chunk(out, first, 0))


class ClientModelTests(unittest.TestCase):
    """The client's instance table and variable lookup (decode.ClientBody), checked on frames it cannot
    read."""

    def test_the_client_builds_the_initial_graphs_and_the_full_frame_the_rest(self):
        client = ClientModel()
        self.assertEqual(list(client.graphs.items())[:3], [(1, 0x0033), (2, 0x004B), (3, 0x0043)])
        self.assertEqual(len(client.graphs), 9)
        body, client = spawned()
        server = {item.id: item.graph.index for item in body.component.instances.values()}
        self.assertEqual(client.graphs, server)

    def test_a_variable_without_an_id_crashes_it(self):
        _, client = spawned()
        rig = graphs.graph(0x02C5)  # instance 14
        padding = next(entry.index for entry in rig.sync_vars if entry.var is None)
        for bindings, where in (((Binding(14, 3),), "0x7FF78991D6DB"), ((), "0x7FF78AAB78A0")):
            out = BitWriter()
            out.w_u16(14)
            out.bits(0, 2)  # not gone, its record
            out.w_u16(0)
            out.bit(0)  # no entity variables
            mask(out, len(rig.sync_vars), {padding})  # an m_syncVars entry without an identifier
            write_value(out, 1.5)
            write_bindings(out, bindings, 14, {14: rig})
            out.bit(0)
            mask(out, len(rig.states), set())
            out.bit(0)  # no event lists
            with self.assertRaises(decode.ClientCrash) as caught:
                client.apply(owner_chunk(out, START + 20, 3))
            self.assertEqual(caught.exception.where, where)

    def test_unknown_instances(self):
        _, client = spawned()
        field = graphs.graph(0x0257)
        bound = owner_delta(START + 20, True, [], {33: (True, (Binding(25, 0, 1),))}, {25: field})
        with self.assertRaises(decode.ClientCrash) as caught:  # a binding to an instance it does not have
            client.apply(EntityUpdate(0xA0000101, chunk=chunk(bound, 3, 0)))
        self.assertEqual(caught.exception.where, "0x7FF78991D6E5")
        listed = InstanceWire(25, field)
        listed.vars[472] = (15.0, ())
        refused = owner_delta(START + 21, True, [listed], {}, {25: field})
        with self.assertRaises(decode.FrameRefused):  # named without a descriptor, no record
            client.apply(EntityUpdate(0xA0000101, chunk=chunk(refused, 3, 0)))


class BodyTests(unittest.TestCase):
    def test_spawn_frames(self):
        body = soldier()
        [tree] = body.spawn_frames()
        client = ClientModel()
        frame = client.apply(tree)
        self.assertTrue(frame.full and not frame.owner)
        self.assertEqual(client.graphs[11], 0x0254)
        self.assertEqual(frame.instances[13].parent, (2, 3))
        self.assertEqual(client.vars[(11, 53)], 30)
        self.assertEqual(client.vars[(0, 29)], (False, True, False))
        self.assertLess(tree.chunk.count, 8 * 1200)
        self.assertEqual(body.frames(), [])  # nothing before the full frame arrived
        body.stream.arrived(tree.chunk_last)
        body.command(START + 1, 0)
        [first] = body.frames()
        frame = client.apply(first)
        self.assertEqual((frame.owner, frame.correction, frame.cmfd), (True, False, START + 1))
        self.assertEqual(frame.first, 2)
        self.assertLess(first.chunk.count, 8 * 1200)
        self.assertEqual(client.on(), server_on(body) - UNSENDABLE)
        self.assertEqual(frame.instances[2].states[3], (True, {"child": 13}))  # 004B st3 -> 01CF

    def test_the_live_first_owner_frame(self):
        # The frame that crashed both clients of a Practice Range match: Soldier picked at command frame
        # 9198, the first owner frame at 9199. The client must read it whole and end with our states.
        body, client = spawned(9198)
        body.command(9199, 0)
        [first] = body.frames()
        frame = client.apply(first)
        self.assertEqual((frame.owner, frame.correction, frame.cmfd), (True, False, 9199))
        self.assertEqual(client.on(), server_on(body) - UNSENDABLE)

    def test_the_client_model_crashes_where_the_client_did(self):
        """That frame as the server sent it live (01CF st120 a plain state without a payload): the client
        took the 32 bits after st120 as its frame, so instance 14's (02C5) variable mask picked an
        m_syncVars entry without an id, whose own-instance binding read the bag of a null variable
        (0x7FF78991D6DB, 'reading 0x90' at payload bit 2842 in both crash dumps)."""
        with mock.patch.dict(runtime.STATE_CLASSES, {"STU_D74D4F47": runtime.State}):
            body, client = spawned(9198)
            body.command(9199, 0)
            with without_payloads("STU_D74D4F47"):
                [first] = body.frames()
        with self.assertRaises(decode.ClientCrash) as caught:
            client.apply(first)
        self.assertEqual((caught.exception.pos, caught.exception.where), (2842, "0x7FF78991D6DB"))
        self.assertIn("id-less variable of instance 14", caught.exception.what)

    def test_sprint_sends_its_effect_counter(self):
        """0255 st9 (an Effect, StEC) begins with Sprint. Its payload is 2 bits, the activation counter & 3
        (reader 0x7FF789894540); the encoder sent none before (the second live crash), so the client read on 2
        bits late."""

        def run():
            body, client = spawned()
            same = True
            for step in range(1, 61):
                body.command(START + step, 0x08 if step == 30 else 0, 0, 127, 0)
                for update in body.frames():
                    client.apply(update)
                    body.stream.arrived(update.chunk_last)
                same &= client.on() == server_on(body) - UNSENDABLE
            return body, same

        body, same = run()
        self.assertTrue(body.component.instances[5].states[9].counter)  # the Effect began
        self.assertTrue(same)
        with without_payloads("STU_691BFA55"):
            try:
                _, same = run()
            except decode.DecodeError:
                same = False
        self.assertFalse(same)

    def test_the_second_crash_frame(self):
        """Dump J of the second live crash (2026-10-02 00:52:18): CmFD 4094, a 977-bit frame, the client's
        reader at bit 857 with bits 832-895 = 0x2098989898989898, crashed in the variable store
        (0x7FF789E761E4 <- 0x7FF78AAB78DF <- one_var 0x7FF78991D484). With these commands (the
        best fit: fire, release at 4093, running) the encoder as it was live (Effect 0254 st21 without its 2
        bits) makes exactly that frame, and the client's table reads it to an id-less variable of instance 12
        whose store crashes, its reader at bit 857. Now the frame reads whole."""

        def frame_at_4094():
            body = BodyScript(SOLDIER, 0xA0000101, 3880)
            client = ClientModel()
            [tree] = body.spawn_frames()
            client.apply(tree)
            body.stream.arrived(tree.chunk_last)
            k = 3880
            while k < 4094:
                for _ in range(2):
                    k += 1
                    fire = 3990 <= k < 4010 or 4069 <= k < 4093
                    body.command(k, FIRE if fire else 0, 0, 127 if k > 3900 else 0, 0)
                for update in body.frames():
                    if k == 4094:
                        return update, client
                    try:
                        client.apply(update)
                    except decode.DecodeError:  # misread as live: its instance list still applies
                        apply_instance_list(client, update)
                    body.stream.arrived(update.chunk_last)

        with without_payloads("STU_691BFA55"):
            update, client = frame_at_4094()
        bits, size = chunk_payload(update)
        self.assertEqual((size, bits >> 832 & (1 << 64) - 1), (977, 0x2098989898989898))
        positions = []
        original = decode.read_bindings

        def bindings(reader, own, graphs, unresolved=None):
            found = original(reader, own, graphs, unresolved)
            if unresolved:
                positions.append(decode._pos(reader))
            return found

        with (
            mock.patch.object(decode, "read_bindings", bindings),
            self.assertRaises(decode.ClientCrash) as caught,
        ):
            client.apply(update)
        self.assertEqual((caught.exception.where, positions), ("0x7FF78AAB78A0", [857]))
        self.assertIn("id-less variable of instance 12", caught.exception.what)
        update, client = frame_at_4094()
        client.apply(update)

    def test_a_frame_wait_finishes_and_sends_its_frame(self):
        # 01CF st120 (STU_D74D4F47) is on at the spawn; the client's class finishes it m_3016B9A1 (1) frames
        # after its begin and then begins st78, whose SubScript st29 makes 0D24.
        body, client = spawned()
        [first] = body.frames()  # before any new command: at the spawn frame, st120 still on
        frame = client.apply(first)
        self.assertEqual(frame.instances[13].states[120], (True, {"target": START + 1}))
        self.assertEqual(client.on(), server_on(body) - UNSENDABLE)
        body.stream.arrived(first.chunk_last)
        for frame_number in range(START + 1, START + SETTLE_FRAMES + 1):
            body.command(frame_number, 0)
            for update in body.frames():
                client.apply(update)
                body.stream.arrived(update.chunk_last)
        self.assertFalse(client.states[(13, 120)])
        self.assertTrue(client.states[(13, 78)] and client.states[(13, 29)])
        self.assertEqual(client.graphs[20], 0x0D24)
        self.assertEqual(client.on(), server_on(body) - UNSENDABLE)

    def test_uncertain_switches_stay_off_the_client(self):
        body = soldier()
        # 004B st12-14 ask whether the owner passes entity filters (STU_AC829876): the server cannot answer.
        self.assertTrue({(2, 12), (2, 13), (2, 14)} <= uncertain(body))
        self.assertTrue({(10, 23), (11, 8), (6, 7)}.isdisjoint(uncertain(body)))  # trusted answers
        [tree] = body.spawn_frames()
        client = ClientModel()
        client.apply(tree)
        body.stream.arrived(tree.chunk_last)
        body.command(START + 1, 0)
        [first] = body.frames()
        client.apply(first)
        self.assertTrue(client.on().isdisjoint(uncertain(body)))

    def test_frames_follow_the_server_through_fire_and_reload(self):
        body = soldier()
        client = ClientModel()
        [tree] = body.spawn_frames()
        client.apply(tree)
        body.stream.arrived(tree.chunk_last)
        script = {START + 30: FIRE, START + 90: 0, START + 120: RELOAD, START + 122: 0}
        buttons = 0
        sent = 0
        was_on = set()
        for frame_number in range(START + 1, START + 260):
            buttons = script.get(frame_number, buttons)
            body.command(frame_number, buttons)
            for update in body.frames():
                frame = client.apply(update)
                sent += 1
                if frame.cmfd >= START + 1 + SETTLE_FRAMES:
                    self.assertTrue(frame.correction)
                self.assertLess(update.chunk.count, 8 * 1200)
                body.stream.arrived(update.chunk_last)
            if body.frame >= START + 1 + SETTLE_FRAMES:
                self.assertEqual(client.on(), server_on(body) - UNSENDABLE, frame_number)
                ammo = body.component.instances[11].vars[53].value()
                if (11, 10) in client.on() and (11, 10) not in was_on:
                    ammo = body.component.instances[11].states[10].ammo0  # the volley's first frame
                self.assertEqual(client.vars.get((11, 53)), ammo)
            was_on = client.on()
        self.assertGreater(sent, 200)

    def test_a_lost_frame_is_carried_by_the_next(self):
        body = soldier()
        client = ClientModel()
        [tree] = body.spawn_frames()
        client.apply(tree)
        body.stream.arrived(tree.chunk_last)
        for frame_number in range(START + 1, START + 20):
            body.command(frame_number, 0)
            for update in body.frames():
                client.apply(update)
                body.stream.arrived(update.chunk_last)
        body.command(START + 20, FIRE)
        [lost] = body.frames()  # never arrives
        body.command(START + 21, FIRE)
        body.command(START + 22, 0)
        [next_frame] = body.frames()
        frame = client.apply(next_frame)
        self.assertEqual(frame.first, lost.chunk_last)  # right after the last acknowledged chunk
        self.assertEqual(client.on(), server_on(body) - UNSENDABLE)
        self.assertNotIn((11, 10), client.on())  # the volley began and ended in the lost frame's time

    def test_the_full_frame_waits_for_the_create(self):
        body = soldier()
        create = body.created(EntityUpdate(0xA0000101, OP_CREATE))
        self.assertEqual(body.frames(), [])
        create.stream.arrived(create.chunk_last)  # the transport's ack of the create's datagram
        [tree] = body.frames()
        self.assertEqual((tree.chunk_last, tree.resend), (1, True))
        self.assertEqual(body.frames(), [])  # nothing more until the full frame arrived
        tree.stream.arrived(tree.chunk_last)
        body.command(START + 1, 0)
        [first] = body.frames()
        self.assertEqual(first.chunk_last, 2)
        body.retire()  # a hero switch
        self.assertFalse(tree.resend)
        body.command(START + 40, 0)
        self.assertEqual(body.frames(), [])

    def test_hero_bodies_spawn(self):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        for body in graphs.bodies().values():
            script = BodyScript(int(body["hero"], 16), 0xA0000101, START)
            [tree] = script.spawn_frames()
            self.assertLess(tree.chunk.count, 8 * 1264, body["name"])
            script.stream.arrived(tree.chunk_last)
            script.command(START + 1, 0)
            [first] = script.frames()
            self.assertLess(first.chunk.count, 8 * 1264, body["name"])


if __name__ == "__main__":
    unittest.main()
