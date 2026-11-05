"""Predicted projectiles (projectiles.py): the lease, confirmations, the explosion release, the shot records
the other clients get, and the Biotic Field's entity, frame and heal; the other bodies' statescript
(observers.py)."""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_game_combat import KANEZAKA_DM, Run
from test_game_world import read_movement

from ow174.game import combat, projectiles, world
from ow174.game.bits import BitReader
from ow174.game.commands import Command
from ow174.game.content import SOLDIER, heroes
from ow174.game.script.decode import ClientBody, read_chunk
from ow174.game.world import HEALTH_POOL, OP_CREATE, OP_DESTROY

HELIX, HELIX_VISOR, CANISTER = 0x040000000000072C, 0x0400000000001C86, 0x040000000000072D
SECONDARY, ABILITY_2 = 0x02, 0x10


def bits_of(writer) -> BitReader:
    return BitReader(writer.getvalue())


def read_shot(r: BitReader) -> dict:
    """A shot record as the client's reader 0x7FF789C91F80 reads it (form 2 only)."""

    def counter():
        if not r.bit():
            return r.bits(4)
        value = r.bits(8)
        return r.bits(32) if value == 0xFF else value

    def w16():
        if not r.bit():
            return r.bits(4)
        return r.bits(16) if r.bit() else r.bits(8)

    def entity():
        kind = r.bits(2)
        if not r.bit():
            value = r.bits(8)
        else:
            value = r.bits(16)
            if value == 0xFFFF:
                value = r.bits(32)
        return 0x80000000 | kind << 29 | value

    out = {"shot": counter()}
    if r.bit():
        out["spread"] = r.bits(32)
    assert not r.bit(), "form 1"
    out.update(instance=w16(), state=w16(), activation=r.bits(6))
    out["volleys"] = 1 if r.bit() else w16()
    out["extra"] = w16()
    out["origin"] = tuple(struct.unpack("<f", struct.pack("<I", r.bits(32)))[0] for _ in range(3))
    out["yaw16"], out["pitch16"] = r.bits(16), r.bits(16)
    ids = []
    if r.bit():
        count = w16()
        ids.append(entity())
        for _ in range(count - 1):
            if r.bit():
                ids.append(ids[-1] + 1)
            else:
                negative = r.bit()
                step = counter()
                ids.append(ids[-1] - step if negative else ids[-1] + step)
    out["ids"] = ids
    return out


class LeaseTests(unittest.TestCase):
    def test_soldiers_predicted_definitions_and_ids(self):
        run = Run()
        player = run.players[0]
        fights = run.match.combat.projectiles
        self.assertEqual(projectiles.definitions(player.body_script), [CANISTER, HELIX_VISOR, HELIX])
        blocks = fights.lease(player)
        self.assertEqual(len(blocks), 6)  # two windows of each
        guid, start, stop, base, per = blocks[4]
        self.assertEqual((guid, stop - start, per), (HELIX, projectiles.LEASE_SPAN, 1))
        frame = start + 1234
        # the allocator 0x7FF789AE59D0: 0xA0000000 | base + (f - start) * perFrame
        self.assertEqual(fights.lease_id(player, HELIX, frame), 0xA0000000 | (base + 1234))
        self.assertIsNone(
            fights.lease_id(player, 0x04000000000003D4, frame)
        )  # the rifle: sync NONE, no lease

    def test_the_lease_rides_in_the_owners_body_create_only(self):
        run = Run(KANEZAKA_DM, ("A", "B"))
        a = run.players[0]
        blocks = run.match.combat.projectiles.lease(a)
        record = world.update(0, {90: [blocks]})
        data = record.getvalue()
        # 8 bits count, 8 bits index 90, pad, u8 mask length 1, mask 1, aligned u32 count, 21 bytes a block
        self.assertEqual(data[:4], bytes([1, 90, 1, 1]))
        self.assertEqual(struct.unpack_from("<I", data, 4)[0], len(blocks))
        guid, start, stop, base, per = struct.unpack_from("<QIIIB", data, 8)
        self.assertEqual((guid, start, stop, base, per), blocks[0])
        self.assertEqual(len(data), 8 + 21 * len(blocks))
        own = run.match._body_create(0, a, None, own=True)
        other = run.match._body_create(0, a, None)
        self.assertEqual(own.count - other.count, 8 * (len(data) - 1))  # the same create, plus component 90


class HelixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = Run(KANEZAKA_DM, ("A", "B"))
        self.a, self.b = self.run.players
        for _ in range(20):
            self.run.step({})
        self.a.client.entities.clear()
        self.b.client.entities.clear()
        self.frame = self.run.frame + 1
        self.run.step({self.a: (0, 0, SECONDARY)})
        for _ in range(150):
            self.run.step({})

    def test_the_owners_rocket_is_confirmed_then_released(self):
        lease = self.run.match.combat.projectiles.lease_id(self.a, HELIX, self.frame)
        creates = [item for item in self.a.client.entities if item.op == OP_CREATE and item.entity == lease]
        self.assertEqual(len(creates), 1)
        destroys = [item for item in self.a.client.entities if item.op == OP_DESTROY and item.entity == lease]
        self.assertEqual(len(destroys), 1)
        r = bits_of(destroys[0].ch2)
        self.assertEqual((r.bit(), r.bit()), (0, 1))  # no events, a projectile state
        self.assertEqual(r.var_b(), projectiles.NO_BASELINE)
        r.var_b()
        self.assertEqual(r.bit(), 0)  # alive: the client plays its own held explosion (+4 = 0)

    def test_the_other_client_gets_a_server_rocket_and_its_shot_record(self):
        creates = [item for item in self.b.client.entities if item.op == OP_CREATE]
        self.assertEqual([item.entity for item in creates], [projectiles.SEEN_FIRST])
        events = [
            item for item in self.b.client.entities if item.entity == self.a.body and item.ch2 is not None
        ]
        self.assertEqual(len(events), 1)
        r = bits_of(events[0].ch2)
        self.assertEqual((r.bit(), r.bit()), (1, 1))  # events, one event
        self.assertEqual(r.var_b(), self.frame)
        shot = read_shot(r)
        self.assertEqual((shot["instance"], shot["state"], shot["ids"]), (11, 16, [projectiles.SEEN_FIRST]))
        self.assertEqual((r.bit(), r.bit()), (0, 0))  # the end of the events, no projectile state
        destroy = [item for item in self.b.client.entities if item.op == OP_DESTROY]
        self.assertEqual([item.entity for item in destroy], [projectiles.SEEN_FIRST])
        self.assertIsNotNone(destroy[0].ch2)

    def test_the_direction_reads_back(self):
        for direction in ((0.0, 0.0, 1.0), (0.6, 0.0, 0.8), (0.0, -1.0, 0.0), (-0.48, 0.6, -0.64)):
            yaw16, pitch16 = projectiles.direction16(direction)
            import math

            a = (yaw16 - 32768) * math.pi / 32768
            b = (pitch16 - 32768) * math.pi / 65536
            back = (math.sin(a) * math.cos(b), math.sin(b), math.cos(a) * math.cos(b))
            for x, y in zip(direction, back, strict=True):
                self.assertAlmostEqual(x, y, delta=2e-4)


class ProjectileRangeTests(unittest.TestCase):
    def test_helix_sweeps_to_its_range_boundary(self):
        for z, hit in ((100.05, True), (100.7, False)):
            with self.subTest(target_feet_z=z):
                run = Run(KANEZAKA_DM, ("A", "B"))
                shooter, victim = run.players
                fight = run.match.combat
                run.match.bots.clear()
                y = run.match.game_map.spawns[0].position[1]
                shooter.mover.position = (0.0, y, 0.0)
                victim.mover.position = (0.0, y, z)
                frame = shooter.body_script.frame + 1
                shooter.body_script.command(frame, SECONDARY)
                fight.command(shooter, Command(frame, buttons=SECONDARY))
                [rocket] = fight.rockets
                reach = rocket.speed * rocket.left
                self.assertEqual(reach, 100.0)
                target = next(target for target in fight._targets() if target.owner is victim)
                contact = combat.ray_capsule(
                    rocket.position, rocket.direction, *target.segment(), target.radius,
                )
                self.assertGreater(contact, reach - rocket.speed * combat.TICK)
                self.assertEqual(contact < reach, hit)
                vitals = fight.vitals_of(target)
                before = vitals.total
                for tick in range(126):
                    fight.update(20.0 + tick * combat.TICK, frame + tick, frames=False)
                self.assertFalse(fight.rockets)
                self.assertEqual(vitals.total < before, hit)
                self.assertAlmostEqual(rocket.flown * rocket.speed, min(contact, reach))


class BioticFieldTests(unittest.TestCase):
    def test_the_default_field_create_keeps_its_wire_bytes(self):
        # These pre-theme records cover both paths, with the same non-zero observer position.
        for parent, size, expected in (
            (
                0xA0100042, 547,
                "728f030000000000000000000000000000000000000000000000000000e00f0000e00f0000"
                "e00f0000e04f0c7000800400004800008004000000a010000428000000000000",
            ),
            (
                None, 350,
                "720f030000e80f00000830000010100000000000000000000000000000e00f0000e00f0000"
                "e00f0000e00f00",
            ),
        ):
            with self.subTest(parent=parent):
                record = projectiles.field_create(parent, (1.25, -2.5, 3.0))(0)
                self.assertEqual((record.count, record.getvalue().hex()), (size, expected))

    def test_the_skin_theme_reaches_the_owner_and_observer_field(self):
        run = Run(KANEZAKA_DM, ("Owner", "Observer"))
        owner, observer = run.players
        grillmaster = 0x0A50000000001BC8  # replaces the field's 0B06.00D ring
        run.match.skin_of = lambda account, _hero: (
            (grillmaster, True) if account == owner.account_lo else (0, False)
        )
        run.match.switch_hero(owner, heroes()[SOLDIER])
        self.assertEqual(owner.skin, (grillmaster, True))
        for _ in range(20):
            run.step({})
        run.step({owner: (0, 0, ABILITY_2)})
        for _ in range(60):
            run.step({})
        [shot] = run.match.combat.projectiles.shots
        self.assertTrue(shot.created)
        for viewer, parents in ((owner, shot.created), (observer, [None])):
            with self.subTest(viewer=viewer.name):
                creates = [
                    item for item in viewer.client.entities
                    if item.op == OP_CREATE and item.entity >> 16 == 0xA008
                ]
                self.assertEqual(len(creates), len(parents))
                for item, parent in zip(creates, parents, strict=True):
                    record = item.build(0)
                    reader = bits_of(record)
                    self.assertEqual((reader.bit(), reader.bit(), reader.bits(12)), (0, 1, 0x03DC))
                    flags = world.WITH_TRANSFORM | world.WITH_SCALE | world.WITH_SKIN | world.SKIN_THEME
                    if parent is not None:
                        flags |= world.WITH_MOVEMENT
                    self.assertEqual(reader.bits(8), flags)
                    self.assertEqual((reader.bits(32), reader.bit()), (0x1BC8, 0))
                    position = struct.unpack("<3f", reader.bits(96).to_bytes(12, "little"))
                    reader.bits(7 * 32)  # rotation and scale
                    if parent is None:
                        self.assertEqual(position, shot.where)
                    else:
                        self.assertEqual(position, (0.0, 0.0, 0.0))
                        movement = read_movement(reader, world.CREATE_BASE, parent=parent)
                        self.assertEqual(movement["flags"], projectiles.KEEP_ROTATION)
                    self.assertEqual(reader.bits(8), 0)  # no components
                    self.assertEqual(reader.pos, record.count)

    def test_the_canister_the_field_entity_its_frame_and_the_heal(self):
        run = Run(KANEZAKA_DM, ("A", "B"))
        a, b = run.players
        for _ in range(20):
            run.step({})
        vitals = run.match.combat.vitals_of(next(t for t in run.match.combat._targets() if t.owner is a))
        vitals.damage(100.0)
        a.client.entities.clear()
        b.client.entities.clear()
        run.step({a: (0, 0, ABILITY_2)})
        for _ in range(60):
            run.step({})
        fights = run.match.combat.projectiles
        field_creates = [
            item for item in a.client.entities if item.op == OP_CREATE and item.entity >> 16 == 0xA008
        ]
        self.assertTrue(field_creates)
        record = field_creates[0].build(0)
        self.assertEqual(record.count > 0, True)
        frames = [
            item for item in a.client.entities if item.chunk is not None and item.entity >> 16 == 0xA008
        ]
        self.assertTrue(frames)
        decoded = read_chunk(frames[0].chunk.getvalue(), frames[0].chunk.count, {1: projectiles.FIELD_GRAPH})
        self.assertEqual(decoded.instances[1].graph, projectiles.FIELD_GRAPH)
        on = {index for index, (active, _) in decoded.instances[1].states.items() if active}
        self.assertEqual(on, {2, 3, 5})
        self.assertTrue(
            [item for item in b.client.entities if item.op == OP_CREATE and item.entity >> 16 == 0xA008]
        )
        self.assertGreater(vitals.now[HEALTH_POOL], 100.0)  # 35 a second on the owner standing in it
        for _ in range(int(projectiles.FIELD_SECONDS / 0.016) + 5):
            run.step({})
        self.assertFalse(fights.shots)
        destroyed = {item.entity for item in a.client.entities if item.op == OP_DESTROY}
        self.assertTrue({item.entity for item in field_creates} <= destroyed)


class ObserverTests(unittest.TestCase):
    def test_the_other_client_sees_the_weapon_and_the_shots(self):
        run = Run(KANEZAKA_DM, ("A", "B"))
        a, b = run.players
        for _ in range(12):
            run.step({})
        model = ClientBody.for_hero(SOLDIER)
        for item in b.client.entities:
            if item.chunk is not None and item.entity == a.body:
                model.apply(item)
        on = model.on()
        self.assertIn((11, 11), on)  # 0254 st11: the weapon in his hands (CosmeticEntity)
        self.assertNotIn((11, 70), on)  # no HUD presenter of his
        b.client.entities.clear()
        for _ in range(10):
            run.step({a: (0, 0, 0x01)})
        for item in b.client.entities:
            if item.chunk is not None and item.entity == a.body:
                model.apply(item)
        self.assertIn((11, 10), model.on())  # his rifle volley: B's client shoots its tracers itself


if __name__ == "__main__":
    unittest.main()
