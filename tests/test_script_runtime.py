"""The statescript runtime running Soldier: 76's body graphs from button edges: the spawn's Entry results,
firing, the post-volley cooldown, reloading, and the frame window."""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.script import graph as graphs
from ow174.game.script import runtime
from ow174.game.script.driver import BodyScript, button_edges
from ow174.game.script.expr import Handle, f32

SOLDIER = 0x02E000000000006E
START = 1000
FIRE, RELOAD = 0x01, 0x400
RIFLE, MANAGER, HUD = 11, 10, 13  # instances: 0254, 0015, 01CF
# 0254's states the Entry flow leaves on (this set was seen live).
RIFLE_SPAWN = {
    0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 17, 18, 22, 25, 31, 32, 33, 34, 35, 36, 37, 44, 45, 56, 63, 64, 65,
    66, 67, 69, 70, 74, 75, 76, 83, 84,
}  # fmt: skip


def soldier() -> BodyScript:
    logging.getLogger("ow174.script").setLevel(logging.ERROR)
    return BodyScript(SOLDIER, 0xA0000101, START)


def on(body: BodyScript, instance: int) -> set[int]:
    return {state.index for state in body.component.instances[instance].states.values() if state.active}


def var(body: BodyScript, instance: int, number: int):
    found = body.component.instances[instance].vars.get(number)
    return found.value() if found is not None else None


def entity(body: BodyScript, number: int):
    found = body.component.vars.get(number)
    return found.value() if found is not None else None


def run(body: BodyScript, frames: int, buttons: int) -> None:
    for _ in range(frames):
        body.command(body.frame + 1, buttons)


class SpawnTests(unittest.TestCase):
    def test_the_instances(self):
        body = soldier()
        made = {item.id: item.graph.index for item in body.component.instances.values()}
        self.assertEqual([made[number] for number in range(1, 12)], [
            0x0033, 0x004B, 0x0043, 0x0251, 0x0255, 0x0257, 0x0259, 0x091B, 0x08B6, 0x0015, 0x0254,
        ])  # fmt: skip
        self.assertEqual(made[HUD], 0x01CF)
        hud = body.component.instances[HUD]
        self.assertEqual((hud.parent.id, hud.parent_state), (2, 3))  # 004B st3 started it

    def test_the_rifle_after_its_entries(self):
        body = soldier()
        self.assertEqual(on(body, RIFLE), RIFLE_SPAWN)
        self.assertEqual(var(body, RIFLE, 53), 30)  # ammo
        self.assertEqual(var(body, RIFLE, 198), 30)  # magazine: round(30 * clip scalar 1.0)
        self.assertEqual(var(body, RIFLE, 28), 1)  # its slot
        self.assertEqual(var(body, RIFLE, 49), Handle(1, RIFLE, 11, 0))  # the gun: CosmeticEntity st11
        self.assertEqual(var(body, RIFLE, 581), f32(1.5))

    def test_the_weapon_manager_and_the_hud(self):
        body = soldier()
        self.assertEqual(entity(body, 31), 1)  # current slot, from the default slot
        self.assertEqual(entity(body, 30), 1)
        self.assertTrue({23, 42}.issubset(on(body, MANAGER)))  # the reticle's Stack and its client-only host
        self.assertEqual(entity(body, 338), f32(1650 * f32(1.4)))  # the ult cost: 004B's 1650, 01C7's x1.4
        self.assertNotIn(14, on(body, HUD))  # 01CF st14 (the dead stack) stays off while #253e > 0


class FireTests(unittest.TestCase):
    def test_holding_fire_counts_the_ammo_down(self):
        body = soldier()
        body.command(START + 1, FIRE)
        self.assertIn(10, on(body, RIFLE))  # WeaponVolley
        self.assertEqual(var(body, RIFLE, 53), 29)  # shot 0 at the start
        run(body, 62, FIRE)  # one second after the press: the frame at 16 * (F + 62) + 15
        elapsed = 62 * 16 + 15
        shots = int(f32(f32(f32(elapsed) * f32(0.001)) * 9.0))
        self.assertEqual(var(body, RIFLE, 53), 30 - (shots + 1))

    def test_release_freezes_the_ammo_and_starts_the_cooldown(self):
        body = soldier()
        body.command(START + 1, FIRE)
        run(body, 9, FIRE)  # 10 frames held
        body.command(body.frame + 1, 0)
        self.assertNotIn(10, on(body, RIFLE))
        self.assertIn(4, on(body, RIFLE))  # the post-volley cooldown ChaseVar on #173
        held = 10 * 16  # the volley started at 16 * (START + 1) and ended at the release frame's start
        shots = int(f32(f32(f32(held + 1) * f32(0.001)) * 9.0))
        self.assertEqual(var(body, RIFLE, 53), 30 - (shots + 1))
        run(body, 12, 0)
        self.assertNotIn(4, on(body, RIFLE))
        self.assertEqual(var(body, RIFLE, 173), 0.0)
        self.assertIn(8, on(body, RIFLE))  # the fire button state is back

    def test_reload_refills_and_ends(self):
        body = soldier()
        body.command(START + 1, FIRE)
        run(body, 20, FIRE)
        run(body, 20, 0)
        self.assertLess(var(body, RIFLE, 53), 30)
        body.command(body.frame + 1, RELOAD)
        pressed = body.frame
        self.assertIs(var(body, RIFLE, 51), True)
        self.assertTrue({28, 51, 39}.issubset(on(body, RIFLE)))  # Stack #33e, the refill Wait, the Anim
        refill = 16 * pressed + 767  # st51: R(1.5 * 0.511 * 1000)
        run(body, refill // 16 - body.frame - 1, 0)
        self.assertLess(var(body, RIFLE, 53), 30)
        body.command(body.frame + 1, 0)
        self.assertEqual(var(body, RIFLE, 53), 30)
        run(body, 60, 0)
        self.assertIs(var(body, RIFLE, 51), False)  # the Anim (1.5 s) ended the reload
        self.assertIn(8, on(body, RIFLE))

    def test_an_empty_magazine_reloads_by_itself(self):
        body = soldier()
        body.command(START + 1, FIRE)
        run(body, 199, FIRE)  # 30 shots at 9 per second: the last at 3222 ms
        self.assertEqual(var(body, RIFLE, 53), 1)
        run(body, 3, FIRE)
        self.assertEqual(var(body, RIFLE, 53), 0)
        run(body, 66, FIRE)  # the 160 ms cooldown, then the 767 ms refill
        self.assertEqual(var(body, RIFLE, 53), 30)  # refilled while still held
        self.assertIs(var(body, RIFLE, 51), True)


class MechanicsTests(unittest.TestCase):
    def test_button_edges(self):
        self.assertEqual(button_edges(0, FIRE), [(20, True), (111, True)])
        self.assertEqual(button_edges(FIRE, RELOAD), [(20, False), (14, True), (111, False)])
        self.assertEqual(button_edges(0, 0, 2), [(16, True), (16, False)])

    def test_stack_order(self):
        var = runtime.Var((0, 1))

        class Fake:
            def __init__(self, name):
                self.name = name
                self.active = True

        a, b, c, d = Fake("a"), Fake("b"), Fake("c"), Fake("d")
        runtime.insert_link(var, runtime.Link(a, 0, -10.0, False))
        runtime.insert_link(var, runtime.Link(b, 0, -10.0, False))  # equal, below
        runtime.insert_link(var, runtime.Link(c, 0, -10.0, True))  # equal, above
        runtime.insert_link(var, runtime.Link(d, 0, 99.0, False))
        self.assertEqual([link.state.name for link in var.links], ["b", "a", "c", "d"])
        self.assertFalse(runtime.would_stack_to_top(var, (-5.0, True)))
        self.assertTrue(runtime.would_stack_to_top(var, (99.0, True)))

    def test_a_timer_inside_the_window_runs_in_its_frame(self):
        # 0015's weapon-switch Wait (0.5 s), begun in frame F: it ends in the frame that holds 16F + 500.
        body = soldier()
        manager = body.component.instances[MANAGER]
        wait = manager.state(18)
        body.component.now = 16 * (START + 1)
        wait.begin()
        timers = [event.time for event in manager.queue if event.state is wait]
        self.assertEqual(timers, [16 * (START + 1) + 500])
        body.command(START + 31, 0)  # frame 1031 runs [16496, 16511]
        self.assertTrue(wait.active)
        body.command(START + 32, 0)  # frame 1032 runs [16512, 16527], which holds 16516
        self.assertFalse(wait.active)

    def test_graph_data_is_complete_for_the_soldier(self):
        for index in (0x0015, 0x0254, 0x004B, 0x01CF):
            self.assertIsNotNone(graphs.graph(index))


if __name__ == "__main__":
    unittest.main()
