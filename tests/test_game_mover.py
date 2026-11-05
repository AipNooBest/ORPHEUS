"""The server's character mover (ow174/game/mover.py) on stand-in worlds and, when their caches are built,
on the real maps; the collision data it reads (ow174/game/collision.py, casc.py)."""

import hashlib
import itertools
import logging
import math
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import casc, collision, contacts, props
from ow174.game.collision import MOVER, NOT_GROUND, World
from ow174.game.commands import CROUCH, JUMP, Command
from ow174.game.content import PRACTICE_RANGE, SOLDIER, _map_entries, heroes
from ow174.game.match import Match
from ow174.game.mover import (
    ADD,
    CROUCH_INTENT,
    CROUCHED,
    FELL,
    JUMP_LATCH,
    JUMPED,
    REAL_JUMP,
    SET,
    SPEED,
    STEP,
    TOUCHING,
    TURN_HELD,
    Mod,
    Mover,
    f32,
    modded,
    mover_data,
    statescript_mods,
)
from ow174.game.script.driver import BodyScript

TICK = 0.016
SOLDIER_BODY = 0x04000000000003CF
SHIFT = 0x08  # ability 1: Soldier: 76's Sprint


def box(x0, x1, y0, y1, z0, z1) -> list:
    """The 12 triangles of an axis-aligned box."""
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    triangles = []
    for a, b, c, d in faces:
        triangles.append((*corners[a], *corners[b], *corners[c]))
        triangles.append((*corners[a], *corners[c], *corners[d]))
    return triangles


def ramp(z0, z1, y0, degrees, x0=-20.0, x1=20.0) -> list:
    """A slope rising along +z from height y0 at z0, facing up (the engine's triangles are one-sided)."""
    y1 = y0 + (z1 - z0) * math.tan(math.radians(degrees))
    return [(x0, y0, z0, x1, y1, z1, x1, y0, z0), (x0, y0, z0, x0, y1, z1, x1, y1, z1)]


def wall(x0, z0, x1, z1, y0=0.0, y1=6.0) -> list:
    """A thin vertical wall from (x0, z0) to (x1, z1): two quads back to back, so either side stops."""
    a, b, c, d = (x0, y0, z0), (x1, y0, z1), (x1, y1, z1), (x0, y1, z0)
    return [(*a, *b, *c), (*a, *c, *d), (*a, *c, *b), (*a, *d, *c)]


FLOOR = box(-30, 30, 0, 1, -30, 30)  # its top at y = 1
REST = 4.5 - 5 * STEP  # a wall at z = 5: the engine keeps the capsule 0.5 + 5 mm out, on the 1/1024 grid


def walk(mover: Mover, ticks: int, forward=127, right=0, buttons=0, yaw=0, start=0) -> list:
    """Step the mover; the feet after each tick."""
    path = []
    for tick in range(start, start + ticks):
        mover.step(Command(tick, forward=forward, right=right, yaw=yaw, buttons=buttons), TICK, tick)
        path.append(tuple(mover.position))
    return path


def soldier(position, triangles=None, yaw=0) -> Mover:
    world = World(triangles) if triangles is not None else None
    return Mover(position, yaw, world, mover_data(SOLDIER_BODY))


class FlatFloorTests(unittest.TestCase):
    """Soldier: 76's numbers, as a 938-tick live walk replayed them."""

    def test_running_is_90_steps_of_the_grid_per_tick(self):
        mover = soldier((0.0, 1.0, 0.0))
        path = walk(mover, 40)
        self.assertEqual(mover.velocity, (0.0, 0.0, 5.5))
        steps = [round((b[2] - a[2]) / STEP) for a, b in itertools.pairwise(path[10:])]
        self.assertEqual(steps[:5], [90] * 5)
        self.assertTrue(all(feet[1] == 1.0 for feet in path))

    def test_stopping_takes_five_ticks(self):
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 40)
        z = mover.position[2]
        path = walk(mover, 5, forward=0, start=40)
        self.assertEqual(mover.velocity, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(path[-1][2] - z, 0.14, delta=0.005)

    def test_the_diagonal_is_capped_and_crouching_is_slower(self):
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 60, right=127)
        self.assertAlmostEqual(math.hypot(mover.velocity[0], mover.velocity[2]), 5.5, delta=0.002)
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 60, buttons=CROUCH)
        self.assertEqual((mover.flags, mover.velocity), (CROUCHED, (0.0, 0.0, 3.0)))

    def test_crouching_again_waits_015_s_after_standing_up(self):
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 5, forward=0, buttons=CROUCH)
        walk(mover, 1, forward=0, start=5)  # frame 5: stands up
        self.assertFalse(mover.crouched)
        walk(mover, 9, forward=0, buttons=CROUCH, start=6)  # frames 6-14: less than 0.15 s later
        self.assertFalse(mover.crouched)
        walk(mover, 1, forward=0, buttons=CROUCH, start=15)  # 10 frames, 0.16 s
        self.assertTrue(mover.crouched)

    def test_missing_command_frames_run_with_the_last_command(self):
        steady, gappy = soldier((0.0, 1.0, 0.0)), soldier((0.0, 1.0, 0.0))
        walk(steady, 40)
        walk(gappy, 30)
        gappy.step(Command(39, forward=127), TICK, 39)  # frames 30-38 never came
        self.assertEqual((gappy.position, gappy.velocity), (steady.position, steady.velocity))
        z = gappy.position[2]
        gappy.step(Command(1000, forward=127), TICK, 1000)  # a restart, not a gap: one tick only
        self.assertEqual(round((gappy.position[2] - z) / STEP), 90)

    def test_a_jump_reaches_098_m_and_lands_after_43_ticks(self):
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 1, forward=0, buttons=JUMP)
        self.assertEqual(mover.flags, JUMP_LATCH | JUMPED | REAL_JUMP)
        top, ticks = mover.position[1], 1
        while mover.airborne and ticks < 100:
            walk(mover, 1, forward=0, buttons=JUMP, start=ticks)
            top, ticks = max(top, mover.position[1]), ticks + 1
        self.assertAlmostEqual(top - 1.0, 0.98, delta=0.005)
        self.assertEqual(ticks, 43)
        self.assertEqual((mover.position[1], mover.air_ticks), (1.0, 0))
        walk(mover, 5, forward=0, buttons=JUMP, start=ticks)  # held: no hop
        self.assertFalse(mover.airborne)


class CollisionMoveTests(unittest.TestCase):
    def test_a_wall_stops_the_body_5_mm_out(self):
        mover = soldier((0.0, 1.0, 0.0), FLOOR + box(-30, 30, 1, 6, 5, 6))
        path = walk(mover, 120)
        self.assertEqual(path[-1][2], REST)  # 4.495 on the grid: the radius and the 5 mm contact skin
        self.assertEqual(mover.velocity, (0.0, 0.0, 0.0))
        self.assertTrue(mover.flags & TOUCHING)
        self.assertTrue(all(feet[1] == 1.0 for feet in path))
        self.assertTrue(all(feet[2] <= REST for feet in path))

    def test_walking_into_a_wall_at_an_angle_slides_along_it(self):
        mover = soldier((0.0, 1.0, 0.0), FLOOR + box(-30, 30, 1, 6, 5, 6))
        path = walk(mover, 120, right=-127)  # forward and to the left (+x)
        self.assertEqual(path[-1][2], REST)
        self.assertGreater(path[-1][0] - path[60][0], 2.0)  # still going along the wall
        self.assertGreater(mover.velocity[0], 3.0)

    def test_a_long_slide_keeps_the_same_distance_every_tick(self):
        mover = soldier((-25.0, 1.0, 3.0), FLOOR + box(-30, 30, 1, 6, 5, 6), yaw=8192)  # 45 degrees to it
        path = walk(mover, 300, yaw=8192)
        touching = [feet for feet in path if feet[2] == REST]
        self.assertGreater(len(touching), 250)
        self.assertEqual(path[-1][2], REST)
        steps = {round((b[0] - a[0]) / STEP) for a, b in itertools.pairwise(touching[20:])}
        self.assertEqual(len(steps), 1)  # the same step along the wall every tick, no stutter

    def test_a_corner_holds_the_body_on_both_walls(self):
        walls = box(-30, 30, 1, 6, 5, 6) + box(-3, -2, 1, 6, -30, 30)
        mover = soldier((0.0, 1.0, 0.0), FLOOR + walls)
        path = walk(mover, 150, right=127)  # forward and to the right (-x)
        self.assertEqual((path[-1][0], path[-1][2]), (-2.0 + 0.5 + 5 * STEP, REST))
        self.assertEqual(path[-1], path[-20])  # at rest in the corner

    def test_an_acute_corner_holds_the_body_still(self):
        apex, back = (0.0, 5.0), 3.0 / math.tan(math.radians(30.0))
        sides = [(apex, (-3.0, 5.0 - back)), (apex, (3.0, 5.0 - back))]  # a 60 degree pocket opening to -z
        walls = [triangle for a, b in sides for triangle in wall(*a, *b)]
        mover = soldier((0.0, 1.0, -2.0), FLOOR + walls)
        path = walk(mover, 200)
        self.assertEqual(path[-1], path[-30])  # wedged and still, no jitter
        x, _, z = path[-1]
        self.assertLess(z, apex[1] - 0.5)
        for a, b in sides:
            self.assertGreater(segment_distance((x, z), a, b), 0.5)  # out of both walls

    def test_a_step_against_a_wall(self):
        world = FLOOR + box(-30, 30, 1, 6, 5, 6) + box(-30, 30, 1, 1.2, 4.6, 5)  # a 0.2 m kerb at its foot
        mover = soldier((0.0, 1.0, 0.0), world)
        path = walk(mover, 150)
        self.assertEqual(path[-1][2], REST)  # the capsule floats over the kerb and stops at the wall
        self.assertFalse(mover.airborne)
        self.assertEqual(path[-1], path[-10])

    def test_walking_along_a_crate_edge_slides_round_it(self):
        crate = box(-0.5, 0.5, 1, 2, 4, 5)  # a 1 m crate, its near face at z = 4, its corner at x = 0.5
        mover = soldier((0.9, 1.0, 0.0), FLOOR + crate)
        path = walk(mover, 150)
        x, _, z = path[-1]
        self.assertGreater(z, 5.0)  # pushed off the crate's corner and past it
        self.assertGreater(x, 0.9)
        for fx, _, fz in path:  # the capsule never goes into the crate
            gap = math.hypot(max(-0.5 - fx, 0.0, fx - 0.5), max(4.0 - fz, 0.0, fz - 5.0))
            self.assertGreater(gap, 0.5, (fx, fz))

    def test_walking_off_a_ledge_falls_and_lands_below(self):
        upper = box(-30, 30, 0, 1, -30, 3)
        lower = box(-30, 30, -3, -1, -30, 30)  # its top at y = -1
        mover = soldier((0.0, 1.0, 0.0), upper + lower)
        flags, heights = [], []
        for tick in range(150):
            walk(mover, 1, start=tick)
            flags.append(mover.flags)
            heights.append(mover.position[1])
        fell = next(n for n, word in enumerate(flags) if word & FELL)
        landed = next(n for n in range(fell, len(flags)) if not flags[n] & FELL)
        self.assertEqual(heights[0], 1.0)
        # Past the edge the centre ray misses and the disc holds the feet on its cast's point, 1 cm out of the
        # top (as the live client's, see PropTests).
        self.assertEqual(heights[fell - 1], 1034 * STEP)
        self.assertGreater(mover.position[2], 3.0)
        self.assertTrue(20 <= landed - fell <= 40)  # 2 m down at 17.5 m/s^2: about 0.48 s
        self.assertEqual(mover.position[1], -1.0)
        self.assertFalse(mover.airborne)

    def test_steps_keep_the_feet_on_each_tread(self):
        stairs = []
        for k in range(6):  # 0.25 m risers, 1 m treads, from z = 3
            stairs += box(-30, 30, 1, 1.25 + 0.25 * k, 3 + k, 30)
        mover = soldier((0.0, 1.0, 0.0), FLOOR + stairs)
        path = [tuple(mover.position)]
        for tick in range(200):
            walk(mover, 1, start=tick)
            self.assertFalse(mover.airborne)
            path.append(tuple(mover.position))
        # The probe at the start of a tick snaps the feet to the tread under them: the tread where the
        # feet were a tick before.
        for (_, _, z), (_, y, _) in itertools.pairwise(path):
            tread = 1.0 if z < 3 else 1.25 + 0.25 * min(5, math.floor(z - 3))
            self.assertAlmostEqual(y, tread, delta=STEP)
        self.assertEqual(path[-1][1], 2.5)

    def test_a_slope_keeps_the_feet_on_it(self):
        world = FLOOR + ramp(2.0, 22.0, 1.0, 30.0) + box(-30, 30, 1, 40, 22, 23)
        mover = soldier((0.0, 1.0, 0.0), world)
        before = mover.position[2]
        for tick in range(150):
            walk(mover, 1, start=tick)
            self.assertFalse(mover.airborne)
            _, y, z = mover.position
            if before > 2.0:  # on the slope since the last tick: the feet go along it, within the
                # spring's dead band (1/512 m, where the capsule does not follow the snap) and the grid
                on_slope = 1.0 + (z - 2.0) * math.tan(math.radians(30.0))
                self.assertAlmostEqual(y, on_slope, delta=3 * STEP, msg=f"tick {tick} at z {z}")
            before = z
        self.assertGreater(mover.position[2], 10.0)

    def test_down_a_slope_the_body_stays_on_the_ground(self):
        bottom = 1.0 - 20.0 * math.tan(math.radians(30.0))
        world = box(-30, 30, 0, 1, 2, 30) + ramp(-18.0, 2.0, bottom, 30.0)
        world += box(-30, 30, -15, bottom, -40, -18)
        mover = soldier((0.0, 1.0, 1.0), world, yaw=32768)  # facing -z
        for tick in range(120):
            walk(mover, 1, yaw=32768, start=tick)
            self.assertFalse(mover.airborne, f"tick {tick}")
        self.assertLess(mover.position[1], -4.0)  # the speed along the slope is the horizontal one

    def test_a_steep_slope_cannot_be_walked_up(self):
        world = FLOOR + ramp(2.0, 6.0, 1.0, 60.0)
        mover = soldier((0.0, 1.0, 0.0), world)
        walk(mover, 150)
        self.assertLess(mover.position[2], 2.5)
        self.assertLess(mover.position[1], 1.5)

    def test_a_low_ceiling_keeps_the_body_crouched(self):
        ceiling = box(-30, 30, 2.6, 4, 3, 8)  # 1.6 m above the floor, from z = 3 to 8
        mover = soldier((0.0, 1.0, 0.0), FLOOR + ceiling)
        walk(mover, 75, buttons=CROUCH)  # crouched, it goes under
        self.assertGreater(mover.position[2], 3.5)
        self.assertTrue(mover.crouched)
        walk(mover, 20, forward=0, start=75)  # the button is up: no room to stand
        self.assertTrue(mover.crouched)
        walk(mover, 160, start=95)  # out the other side it stands up
        self.assertGreater(mover.position[2], 8.6)
        self.assertFalse(mover.crouched)

    def test_a_jump_into_a_ceiling_comes_back_down(self):
        mover = soldier((0.0, 1.0, 0.0), FLOOR + box(-30, 30, 3.5, 5, -30, 30))  # 2.5 m above the floor
        walk(mover, 1, forward=0, buttons=JUMP)
        top = 0.0
        for tick in range(1, 80):
            walk(mover, 1, forward=0, start=tick)
            top = max(top, mover.position[1])
        self.assertLess(top, 1.0 + 3.5 - 1.0 - 2.0 + 0.01)  # the capsule's 2 m top stops under it
        self.assertEqual(mover.position[1], 1.0)
        self.assertFalse(mover.airborne)

    def test_a_body_that_falls_out_of_the_map_stops_far_below_it(self):
        mover = soldier((0.0, 1.0, 0.0), box(-1, 1, 0, 1, -1, 1))  # a 2 m platform, nothing under it
        walk(mover, 600)
        self.assertEqual((mover.position[1], mover.velocity[1]), (-100.0, 0.0))
        self.assertTrue(mover.airborne)

    def test_spawning_a_little_above_or_below_the_floor_stands_on_it(self):
        for start in (1.3, 0.97):
            mover = soldier((0.0, start, 0.0), FLOOR)
            walk(mover, 30, forward=0)
            self.assertEqual(mover.position[1], 1.0)
            self.assertFalse(mover.airborne)

    def test_a_surface_that_is_not_ground_is_not_stood_on(self):
        slippery = box(-30, 30, 0, 1, -30, 30)
        world = World(slippery + box(-30, 30, -3, -1, -30, 30), surfaces=[NOT_GROUND] * 12 + [0] * 12)
        mover = Mover((0.0, 1.0, 0.0), 0, world, mover_data(SOLDIER_BODY))
        walk(mover, 1, forward=0)
        self.assertTrue(mover.airborne)


def segment_distance(p, a, b) -> float:
    """The distance from a point to a segment, in the plane."""
    ex, ez = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((p[0] - a[0]) * ex + (p[1] - a[1]) * ez) / (ex * ex + ez * ez)))
    return math.hypot(p[0] - a[0] - t * ex, p[1] - a[1] - t * ez)


def hull_box(x0, x1, y0, y1, z0, z1, mask=0x1FDF) -> tuple:
    """An axis-aligned box as a polytope the way the engine keeps one: planes facing out, half edge twins
    side by side (2k, 2k + 1)."""
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    loops = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    pairs: dict[tuple, int] = {}
    for loop in loops:
        for k, a in enumerate(loop):
            b = loop[(k + 1) % 4]
            if (b, a) not in pairs and (a, b) not in pairs:
                pairs[(a, b)] = len(pairs)

    def half(a, b):
        return 2 * pairs[(a, b)] if (a, b) in pairs else 2 * pairs[(b, a)] + 1

    edges = [None] * (2 * len(pairs))
    faces, planes = [], []
    for face, loop in enumerate(loops):
        named = [half(loop[k], loop[(k + 1) % 4]) for k in range(4)]
        for k, number in enumerate(named):
            edges[number] = (1 if number % 2 == 0 else -1, loop[k], face, named[(k + 1) % 4])
        faces.append(named[0])
        axis = face // 2
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0 if face % 2 else -1.0
        planes.append((*normal, corners[loop[0]][axis] * normal[axis]))
    centroid = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    return mask, centroid, corners, planes, faces, edges


class ChunkTests(unittest.TestCase):
    """The collision chunk's format, on a hand-made chunk, and the cache."""

    @staticmethod
    def chunk() -> bytes:
        vertices = [(0, 0, 0), (1, 0, 0), (0, 0, 1), (5, 5, 5)]
        triangles = [((0, 1, 2), 0x1FDF, 0), ((0, 1, 3), 0x04C0, 0), ((1, 2, 3), 0x1B9F, 1)]
        _mask, centroid, cube, planes, faces, edges = hull_box(10.0, 11.0, 0.0, 1.0, 0.0, 1.0)
        out = bytearray(48)
        polytope_at = len(out)
        out += bytes(88)
        verts_at = len(out)
        out += b"".join(struct.pack("<3f", *corner) for corner in cube)
        planes_at = len(out)
        out += b"".join(struct.pack("<4f", *plane) for plane in planes)
        faces_at = len(out)
        out += bytes(faces)
        out += bytes(-len(out) % 4)
        edges_at = len(out)
        out += b"".join(struct.pack("<bBBB", *edge) for edge in edges)
        struct.pack_into(
            "<4Q3f3I", out, polytope_at, verts_at, planes_at, edges_at, faces_at, *centroid, 8, 6, 24
        )
        struct.pack_into("<H", out, polytope_at + 76, 0x1FDF)
        mesh_at = len(out)
        out += bytes(2 * 56)  # two meshes: the second is the first's last triangle again
        points_at = len(out)
        out += b"".join(struct.pack("<4f", *point, 0) for point in vertices)
        tris_at = len(out)
        for corners, mask, surface in triangles:
            out += struct.pack("<3I3i4H", *corners, 3, -1, -1, 0, surface, mask, 0)
        count = len(vertices)
        struct.pack_into("<3Q4I", out, mesh_at, 0, points_at, tris_at, 0, count, len(triangles) - 1, 1)
        struct.pack_into("<3Q4I", out, mesh_at + 56, 0, points_at, tris_at + 64, 0, count, 1, 1)
        struct.pack_into("<4I4Q", out, 0, 0, 0, 1, 2, 0, 0, polytope_at, mesh_at)
        return bytes(out)

    def test_only_what_stops_the_mover_is_kept_and_survives_the_cache(self):
        meshes, hulls = collision.parse_chunk(self.chunk())
        # The 0x04C0 triangle stops only shots; vertex numbers are the kept ones, wings too.
        self.assertEqual(meshes[0], ([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (5.0, 5.0, 5.0)],
                                     [(0, 1, 2, 3, -1, -1, 0x1FDF, 0)]))  # fmt: skip
        self.assertEqual(meshes[1][1], [(0, 1, 2, 2, -1, -1, 0x1B9F, 1)])
        self.assertEqual((len(hulls), hulls[0][0], len(hulls[0][2]), len(hulls[0][5])), (1, 0x1FDF, 8, 24))
        blob = collision.pack("test", meshes, hulls, {"map": "0x0"})
        header, meshes_again, hulls_again, models, placements = collision.unpack(blob)
        self.assertEqual((meshes_again, hulls_again, models, placements), (meshes, hulls, [], []))
        self.assertEqual((header["triangles"], header["hulls"]), (2, 1))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.col"
            path.write_bytes(blob)
            world = World.load(path)
        hit = world.ground(10.5, 0.5, 3.0, -1.0, MOVER)
        self.assertEqual((hit.y, hit.normal), (1.0, (0.0, 1.0, 0.0)))  # the top of the polytope cube
        kinds = [shape.kind for shape in world.engine.shapes]
        self.assertEqual(kinds, [contacts.HULL, contacts.MESH, contacts.MESH])  # polytopes first

    def test_a_prop_that_needs_an_identifier_is_kept_only_in_the_modes_that_list_it(self):
        floor = collision.soup_mesh(box(-5, 5, 0, 1, -5, 5), [0x1FDF] * 12, [0] * 12)
        crate = [("hull", *hull_box(0.0, 1.0, 1.0, 2.0, 0.0, 1.0))]
        placements = [(0, 1, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0, 1.0))]  # needs 0x1234
        meshes = [(floor.vertices, floor.records)]
        blob = collision.pack("test", meshes, [], {"map": "0x0"}, [crate], placements, [0x1234])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.col"
            path.write_bytes(blob)
            self.assertEqual(collision.cache_version(path), collision.VERSION)
            world = World.load(path)
            self.assertEqual((len(world), len(world.engine.shapes)), (12, 1))
            world = World.load(path, frozenset({0x1234}))
            self.assertEqual((len(world), len(world.engine.shapes)), (24, 2))
            self.assertEqual(world.ground(0.5, 0.5, 3.0, 0.0).y, 2.0)
            prop = world.engine.shapes[1]
            self.assertEqual(
                (prop.kind, prop.frame), (contacts.HULL, ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)))
            )
            path.write_bytes(blob[:8] + struct.pack("<I", 3) + blob[12:])  # a cache of pass 2: built again
            self.assertEqual(collision.cache_version(path), 3)
            with self.assertRaises(collision.CollisionError):
                World.load(path)

    def test_a_shape_with_one_teams_proxy_stops_only_that_team(self):
        # Mask 0x1FD5 has team 1's proxy bit only, as 58 props of Blizzard World do.
        mask, centroid, corners, planes, faces, edges = hull_box(-1.0, 1.0, -1.0, 2.0, 4.0, 6.0, 0x1FD5)
        hull = contacts.Hull(centroid, corners, planes, faces, edges, mask)
        world = contacts.StaticWorld([contacts.StaticShape(hull)])
        for category, stopped in ((collision.TEAM1, True), (collision.TEAM0, False), (MOVER, True)):
            body = contacts.CharacterBody(0.5, 1.5, world=world, category=category)
            body.teleport((0.25, 0.0, 2.0))
            for _ in range(30):
                final, _planes, _touching = body.move((0.0, 0.0, f32(0.2)), f32(0.016), 20)
            self.assertEqual(final[2] < 3.5, stopped)
            # Off the box's middle: the engine's GJK misses an overlap whose first two support points
            # line up with the origin exactly (the client's does too).
            body.teleport((0.25, 0.0, 5.25))
            self.assertEqual(body.fits(0.5, 1.5), not stopped)

    def test_a_capsule_shape_becomes_a_prism_around_it(self):
        triangles = props.prism((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), 0.5)
        corners = [corner for triangle in triangles for corner in triangle]
        sides = max(math.hypot(x, z) for x, _, z in corners)
        self.assertAlmostEqual(sides, 0.5 / math.cos(math.pi / props.SIDES), places=6)  # sides 0.5 away
        self.assertEqual((min(y for _, y, _ in corners), max(y for _, y, _ in corners)), (-0.5, 1.5))
        quarter = (0.0, math.sqrt(0.5), 0.0, math.sqrt(0.5))  # a quarter turn about up
        moved = props.place((1.0, 0.0, 0.0), (10.0, 0.0, 0.0), (2.0, 2.0, 2.0), quarter)
        self.assertEqual([round(axis, 5) for axis in moved], [10.0, 0.0, -2.0])  # scaled, turned, moved

    def test_the_storage_reader_finds_a_file_by_its_key(self):
        content = b"collision " * 1000
        blte = bytearray(b"BLTE" + struct.pack(">I", 12 + 2 * 24) + b"\x0f" + (2).to_bytes(3, "big"))
        blocks = [b"N" + content[:5000], b"Z" + zlib.compress(content[5000:])]
        for block, plain in zip(blocks, (content[:5000], content[5000:]), strict=True):
            blte += struct.pack(">II", len(block), len(plain)) + hashlib.md5(block).digest()
        blte += b"".join(blocks)
        ekey = bytes(range(16))
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data" / "casc" / "data"
            data.mkdir(parents=True)
            record = ekey[::-1] + struct.pack("<I", 30 + len(blte)) + bytes(10) + blte
            (data / "data.000").write_bytes(bytes(64) + record)
            number = casc.bucket(ekey)
            layout = struct.pack("<HBBBBBBQ", 7, number, 0, 4, 5, 9, 30, 0)
            header = struct.pack("<II", 16, 0) + layout + bytes(8)
            entry = ekey[:9] + (64).to_bytes(5, "big") + struct.pack("<I", len(record))
            index = header + struct.pack("<II", len(entry), 0) + entry
            (data / f"{number:02x}00000001.idx").write_bytes(index)
            storage = casc.Storage(folder)
            self.assertEqual(storage.read(ekey, hashlib.md5(content).digest()), content)
            with self.assertRaises(casc.CascError):
                storage.read(ekey, bytes(16))


def cached(map_guid: int) -> World | None:
    path = collision.cache_path(map_guid)
    return World.load(path) if collision.cache_version(path) == collision.VERSION else None


def client_mover(world, position, velocity, yaw, pitch, throttles, frame) -> Mover:
    """Soldier: 76 (team 0) in a state the live client predicted: on the ground, the spring at rest."""
    mover = Mover(position, yaw, world, mover_data(SOLDIER_BODY), category=collision.TEAM0)
    mover.velocity = velocity
    mover.pitch = pitch
    mover.flags_word = TURN_HELD | REAL_JUMP
    mover.spring = mover.spring2 = 3.0517578125e-05  # step 23406 of 32768: the rest
    mover.spring_steps = (23406, 23406)
    mover.throttles = mover.input_throttles = throttles
    mover.input_frame = frame
    return mover


class RealMapTests(unittest.TestCase):
    """On the maps' own collision; skipped until tools/build_collision.py (or a match) built the cache."""

    def spawn_tests(self, map_guid: int, spawns) -> None:
        world = cached(map_guid)
        if world is None:
            self.skipTest(f"no collision cache for {map_guid:#x}")
        for position in spawns:
            mover = Mover(position, 0, world, mover_data(SOLDIER_BODY))
            walk(mover, 20, forward=0)
            x, y, z = mover.position
            ground = world.ground(x, z, y + 1.0, y - 1.0)
            self.assertFalse(mover.airborne, position)
            self.assertAlmostEqual(y, ground.y, delta=STEP, msg=position)

    def test_the_practice_range_spawns_stand_on_the_floor(self):
        self.spawn_tests(PRACTICE_RANGE.map_guid, [spawn.position for spawn in PRACTICE_RANGE.spawns])

    def test_the_spawn_rooms_of_kings_row_stand_on_the_floor(self):
        entry = _map_entries().get(0x08000000000000D4)
        if entry is None:
            self.skipTest("no map data")
        self.spawn_tests(0x08000000000000D4, [tuple(spawn["position"]) for spawn in entry["spawns"][:12]])

    def test_walking_out_of_the_practice_range_spawn(self):
        world = cached(PRACTICE_RANGE.map_guid)
        if world is None:
            self.skipTest("no collision cache for the Practice Range")
        spawn = PRACTICE_RANGE.spawns[0]
        yaw = round(spawn.yaw * 65536 / 360)
        mover = Mover(spawn.position, yaw, world, mover_data(SOLDIER_BODY))
        for tick in range(300):  # out of the spawn room, over a 2.4 cm step
            walk(mover, 1, yaw=yaw, start=tick)
            self.assertFalse(mover.airborne, f"tick {tick}")
            x, y, z = mover.position
            ground = world.ground(x, z, y + 0.6, y - 0.6)
            self.assertLess(abs(y - ground.y), 0.1)  # it stands there from the next tick on
        walk(mover, 10, forward=0, yaw=yaw, start=300)
        x, y, z = mover.position
        self.assertAlmostEqual(y, world.ground(x, z, y + 0.6, y - 0.6).y, delta=STEP)
        self.assertGreater(math.dist(spawn.position, mover.position), 20.0)


class PropTests(unittest.TestCase):
    """The Practice Range's props, which its collision chunk does not have (props.py): what the body
    collides with in the client. Skipped until the map's cache is built."""

    def setUp(self):
        self.world = cached(PRACTICE_RANGE.map_guid)
        if self.world is None:
            self.skipTest("no collision cache for the Practice Range")

    def test_a_railing_keeps_the_body_on_the_balcony(self):
        # The balcony north of the spawn (its floor at y = 7 up to x = 47.8, then a 6 m drop) has a 0.9 m
        # railing (definition 2600) at x = 47.5; without it the body walks off and falls to the floor.
        mover = Mover((46.5, 7.0, 43.3), 16384, self.world, mover_data(SOLDIER_BODY))  # facing +x
        walk(mover, 90, yaw=16384)
        x, y, _ = mover.position
        self.assertLess(x, 47.5)
        self.assertAlmostEqual(y, 7.0, delta=0.05)
        self.assertFalse(mover.airborne)
        self.assertTrue(mover.flags & TOUCHING)

    def test_the_office_chairs_on_wheels_are_not_solid(self):
        # 04AA (a chair on five wheels) is a dynamic body in the client (category 0x100, read from the running
        # client's physics world), which the mover does not meet: the one at the desks (44.15, 1.0, 33.47)
        # is no ground (its seat used to be, at 1.72 m) and no shape of the engine.
        self.assertIsNone(self.world.ground(44.15, 33.47, 1.9, 1.2))
        chair = [s for s in self.world.engine.shapes if abs(s.fat_low[0] - 43.658) < 0.01]
        self.assertFalse([s for s in chair if abs(s.fat_low[2] - 32.936) < 0.01])

    def test_a_body_on_the_desk_stops_at_the_monitor_where_the_client_does(self):
        # The desk 04AB's hull hangs on its model's node 0x8B, 1.62 m up: a box from the desk top to 3 m in
        # front of the wall monitor. The live client stopped at (46.01171875, 31.1826171875) walking along the
        # wall on the desk (session 070300_3A33, frame 50668); without the node the hull was under the desk.
        hulls = [
            s for s in self.world.engine.shapes if s.kind == contacts.HULL and 43.5 < s.fat_low[0] < 43.7
        ]
        self.assertTrue(
            any(abs(s.fat_low[1] - 1.805) < 0.01 and abs(s.fat_high[1] - 3.123) < 0.01 for s in hulls)
        )
        for z in (31.18, 31.2):
            mover = Mover((46.40, 1.9326171875, z), -16384, self.world, mover_data(SOLDIER_BODY))
            walk(mover, 60, yaw=-16384)
            self.assertEqual(mover.position[0::2], [46.01171875, 31.1826171875])
            self.assertTrue(mover.flags & TOUCHING)

    def test_the_disc_holds_the_feet_1_cm_over_a_ledge_where_the_client_does(self):
        # Stepping up onto a seat (1.6899) and the desk top (1.9307) the centre ray misses the ledge and the
        # ground disc catches it: the live client's feet stand on the cast's point, 1 cm out of the mesh
        # (session 070300_3A33, its own states and commands: frames 50315..50322 and 51438..51439; the
        # server's eight rays had 1.689453125 and 1.9296875).
        steps = [
            ((45.8525390625, 1.0, 32.9755859375), (-3.6005859375, 0.0, -4.1572265625), -25323, 5756, (0, 127),
             50314, [(127, 0, -25215, 6008), (127, 0, -25143, 6242), (127, 0, -25034, 6495),
                     (127, 0, -24980, 6657), (127, 0, -24890, 6855), (127, 0, -24854, 6999),
                     (127, 0, -24818, 7107), (127, 0, -24782, 7180)],
             [45.54296875, 1.7001953125, 32.548828125]),
            ((45.0029296875, 1.7529296875, 32.1494140625), (-4.890625, 0.0, 2.5166015625), -27810, 15932,
             (-127, 0), 51437, [(0, -127, -27810, 15896), (0, -127, -27810, 15860)],
             [44.84765625, 1.9404296875, 32.2294921875]),
        ]  # fmt: skip
        for position, velocity, yaw, pitch, throttles, frame, commands, feet in steps:
            mover = client_mover(self.world, position, velocity, yaw, pitch, throttles, frame)
            for k, (forward, right, yaw, pitch) in enumerate(commands, frame + 1):
                mover.step(Command(k, forward, right, yaw, pitch), TICK, k)
            self.assertEqual(mover.position, feet)

    def test_the_things_on_the_desk_are_not_ground(self):
        # 04A3 (capsules, a hull and a sphere on the desk) is dynamic in the client; the server's body used to
        # step onto its hull (2.1888 m) at the desk's left end where the client's stood on the desk.
        self.assertLess(self.world.ground(43.0745, 32.0777, 2.5, 1.5).y, 1.94)  # the desk top, 1.9328
        self.assertFalse(
            [s for s in self.world.engine.shapes if s.kind in (contacts.CAPSULE, contacts.SPHERE)]
        )


class MoverStateTests(unittest.TestCase):
    """The movement state's fields the owner's client compares exactly (correction.py), as its mover
    writes them."""

    def test_the_pitch_is_held_to_89_degrees_and_that_is_remembered(self):
        mover = soldier((0.0, 1.0, 0.0))
        mover.step(Command(1, pitch=9000), TICK, 1)
        self.assertEqual((mover.pitch, mover.flags & TURN_HELD), (9000, 0))
        mover.step(Command(2, pitch=17000), TICK, 2)
        self.assertEqual((mover.pitch, mover.flags & TURN_HELD), (16202, TURN_HELD))
        mover.step(Command(3, pitch=-20000), TICK, 3)
        self.assertEqual(mover.pitch, -16202)
        mover.step(Command(4), TICK, 4)
        self.assertEqual((mover.pitch, mover.flags), (0, TURN_HELD))  # 0x200000 is never cleared

    def test_the_state_keeps_the_throttle_bytes_the_way_the_client_rounds_them(self):
        mover = soldier((0.0, 1.0, 0.0))
        mover.step(Command(7, forward=104, right=-127), TICK, 70)
        self.assertEqual((mover.throttles, mover.input_frame, mover.input_tick), ((-127, 103), 7, 70))
        mover.step(Command(8, forward=127, right=9), TICK, 71)
        self.assertEqual(mover.input_throttles, (8, 127))

    def test_the_spring_offsets_go_as_steps_of_their_range_and_the_second_follows_the_first(self):
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 5, forward=0)
        rest = 23406  # Soldier's range [-2.5, 1.0]: 2.5 / 3.5 of 32768
        self.assertEqual((mover.spring_steps, mover.spring), ((rest, rest), 2.0**-15))
        steps = []
        for tick in range(5, 20):  # crouched, the capsule stays put: 0.25 m above its new rest
            walk(mover, 1, forward=0, buttons=CROUCH, start=tick)
            steps.append(mover.spring_steps)
        self.assertGreater(steps[0][0], rest + 1500)
        snap = next(k for k, (first, _) in enumerate(steps) if first == rest)
        self.assertTrue(all(first == second for first, second in steps[:snap]))
        self.assertGreater(steps[snap][1], rest)  # it keeps the first one's last move, then rests too
        self.assertEqual(steps[-1], (rest, rest))
        self.assertEqual((mover.spring_speed, mover.crouch_end), (0.0, None))
        walk(mover, 1, forward=0, start=20)
        self.assertEqual(mover.crouch_end, 20)

    def test_gravitys_part_of_the_velocity_follows_the_fall(self):
        mover = soldier((0.0, 1.0, 0.0))
        walk(mover, 1, forward=0, buttons=JUMP)
        falls = []
        while mover.airborne:
            walk(mover, 1, forward=0, start=len(falls) + 1)
            falls.append((mover.velocity[1], mover.fall))
        self.assertTrue(all(fall == 0.0 for speed, fall in falls if speed > 0.0))  # going up: none
        # Falling it is the fall speed (before the grid), or what gravity added if that is less.
        self.assertTrue(all(abs(fall - speed) < 2 * STEP for speed, fall in falls if speed < 0.0))
        self.assertLess(falls[-2][1], -5.0)  # the last tick in the air
        self.assertEqual(mover.fall, 0.0)  # the landing hands it to the body


class ModTests(unittest.TestCase):
    def test_a_mod_acts_from_the_frame_after_the_statescript_turned_it_on_up_to_the_one_it_went_off(self):
        mover = soldier((0.0, 1.0, 0.0))
        mover.track_mods({(5, 0, 0): (SPEED, ADD, 0.5, 0.0)}, 10)
        mod = mover.mods[(5, 0, 0)]
        self.assertEqual((mod.start, mod.end), (11, None))
        mover.track_mods({}, 20)
        self.assertEqual(mod.end, 21)
        self.assertEqual([mod.acts(frame) for frame in (10, 11, 20, 21)], [False, True, True, False])
        mover.track_mods({}, 21)
        self.assertEqual(mover.mods, {})

    def test_the_speed_scalar_changes_the_speeds_and_the_friction_base_speed(self):
        data = mover_data(SOLDIER_BODY)
        self.assertIs(modded(data, []), data)
        sprint = modded(data, [Mod(SPEED, ADD, 0.5, 0), Mod(CROUCH_INTENT, SET, 0.0, 0)])
        self.assertEqual((sprint.run_forward, sprint.run_strafe, sprint.crouch_forward), (8.25, 8.25, 4.5))
        self.assertEqual(sprint.run_backward, f32(4.95 * 1.5))
        self.assertEqual(sprint.decel_ground_base_speed, f32(1.5 * 6.2))
        self.assertEqual((sprint.accel_ground, sprint.decel_ground_base), (35.0, 15.0))


class SprintTests(unittest.TestCase):
    """Soldier: 76's Sprint, run by the statescript runtime: its MovementMod states put +0.5 on the speed
    scalar and the crouch intent to 0 while it is on."""

    def setUp(self):
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        self.body = BodyScript(SOLDIER, 0xA0000101, 999)
        self.mover = soldier((0.0, 1.0, 0.0))
        for frame in range(1000, 1030):
            self.frame(frame, forward=0)

    def frame(self, frame: int, buttons: int = 0, forward: int = 127) -> float:
        """One command frame as the server runs it: the mover, then the statescript."""
        self.mover.step(Command(frame, forward=forward, buttons=buttons), TICK, frame)
        self.body.command(frame, buttons, forward=forward)
        self.mover.track_mods(statescript_mods(self.body.component), frame)
        return math.hypot(self.mover.velocity[0], self.mover.velocity[2])

    def test_sprint_runs_at_one_and_a_half_times_the_run_speed_from_the_next_frame(self):
        speeds = [self.frame(frame, SHIFT if frame in (1050, 1060) else 0) for frame in range(1030, 1070)]
        # The client's numbers: run 5.5 * 1.5 = 8.25 and acceleration 35 * 8.25 m/s^2 against friction
        # 15 * 8.25 (its base speed 6.2 * 1.5); after it, friction 15 * 5.5 + (8.25 - 6.2) * 2 * 5.5.
        self.assertEqual(speeds[19:22], [5.5, 5.5, 8.1396484375])  # frames 1049-1051: on in 1050
        self.assertEqual(speeds[22:31], [8.25] * 9)
        self.assertEqual(speeds[30:33], [8.25, 6.5693359375, 5.5])  # off in 1060
        self.assertEqual([mod for mod in self.mover.mods.values() if mod.value], [])  # a HUD mod adds -0

    def test_crouching_waits_for_the_frame_after_sprint_ends(self):
        for frame in range(1030, 1050):
            self.frame(frame, SHIFT if frame == 1040 else 0)
        self.frame(1050, CROUCH)  # the crouch ends Sprint in this frame, but its mods act in it
        self.assertFalse(self.mover.crouched)
        self.frame(1051, CROUCH)
        self.assertTrue(self.mover.crouched)


class MatchMoverTests(unittest.TestCase):
    def test_a_players_body_walks_on_the_maps_collision_with_its_heros_values(self):
        if cached(PRACTICE_RANGE.map_guid) is None:
            self.skipTest("no collision cache for the Practice Range")
        collision.world_for(PRACTICE_RANGE.map_guid)  # loaded, as the match's background load does
        match = Match(PRACTICE_RANGE)
        player = match.add_player(1, "Alpha", SOLDIER, 0, False)
        player.has_body = True
        self.assertTrue(player.mover.on_map)
        yaw = round(PRACTICE_RANGE.spawns[0].yaw * 65536 / 360)
        player.take_commands([Command(7000 + frame, forward=127, yaw=yaw) for frame in range(60)])
        self.assertEqual((player.mover.position[1], player.mover.data.run_forward), (1.0, 5.5))
        self.assertGreater(math.dist(player.mover.position, PRACTICE_RANGE.spawns[0].position), 4.0)
        match.switch_hero(player, heroes()[0x02E0000000000003])  # Tracer: a new body at the spawn
        player.take_commands([Command(7060 + frame, forward=127, yaw=yaw) for frame in range(60)])
        self.assertEqual(player.mover.data.run_forward, 6.0)
        vx, vy, vz = player.mover.velocity
        self.assertEqual(vy, 0.0)
        self.assertAlmostEqual(math.hypot(vx, vz), 6.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
