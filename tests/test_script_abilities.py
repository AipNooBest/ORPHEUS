"""Soldier: 76's abilities and HUD run by the statescript runtime: Sprint, Helix Rockets, Biotic Field,
Tactical Visor and quick melee from button edges, their flags and cooldowns, the ultimate's charge, and the
owner frames that carry all of it to the client."""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_script_driver import ClientModel, server_on

from ow174.game.script.driver import ULT_SECONDS, BodyScript
from ow174.game.script.expr import f32

SOLDIER = 0x02E000000000006E
START = 1000
FIRE, SECONDARY, SHIFT, E, Q, MELEE = 0x01, 0x02, 0x08, 0x10, 0x20, 0x800
MELEE_GRAPH, SPRINT, BIOTIC, VISOR, RIFLE = 3, 5, 6, 7, 11  # instances: 0043, 0255, 0257, 0259, 0254
RUNNING, PUSHED, HELD = 1, 2, 4


class Session:
    """A body whose frames go to a client model, every one acknowledged."""

    def __init__(self, ult_seconds: float = ULT_SECONDS) -> None:
        logging.getLogger("ow174.script").setLevel(logging.ERROR)
        self.body = BodyScript(SOLDIER, 0xA0000101, START, ult_seconds=ult_seconds)
        self.client = ClientModel()
        [tree] = self.body.spawn_frames()
        self.client.apply(tree)
        self.body.stream.arrived(tree.chunk_last)
        self.run(30)

    def run(self, frames: int, buttons: int = 0, forward: int = 0) -> None:
        for _ in range(frames):
            body = self.body
            body.command(body.frame + 1, buttons, forward=forward)
            for update in body.frames():
                self.client.apply(update)
                body.stream.arrived(update.chunk_last)
            assert self.client.on() == server_on(body), body.frame  # the client confirms the server

    def ability(self, instance: int, state: int):
        return self.body.component.instances[instance].states[state]

    def var(self, instance: int, number: int):
        found = self.body.component.instances[instance].vars.get(number)
        return found.value() if found is not None else None

    def on(self, instance: int) -> set[int]:
        states = self.body.component.instances[instance].states.values()
        return {state.index for state in states if state.active}


class SprintTests(unittest.TestCase):
    def test_shift_toggles_sprint_while_moving_forward(self):
        session = Session()
        sprint = session.ability(SPRINT, 1)
        session.run(5, 0, forward=127)
        session.run(3, SHIFT, forward=127)
        self.assertEqual(sprint.flags, RUNNING | HELD)
        self.assertEqual(session.var(SPRINT, 699), 1)  # the HUD's "in use"
        session.run(10, 0, forward=127)
        self.assertEqual(sprint.flags, RUNNING)
        session.run(3, SHIFT, forward=127)  # a second press stops it and stays latched while down
        self.assertEqual(sprint.flags, HELD)
        self.assertEqual(session.var(SPRINT, 699), 0)
        session.run(5, 0, forward=127)
        self.assertEqual(sprint.flags, 0)

    def test_sprint_stops_when_the_player_stops_moving(self):
        session = Session()
        sprint = session.ability(SPRINT, 1)
        session.run(1, SHIFT, forward=127)
        session.run(5, 0, forward=127)
        self.assertEqual(sprint.flags, RUNNING)
        session.run(5, 0, forward=0)
        self.assertEqual(sprint.flags, 0)

    def test_no_sprint_standing_still(self):
        session = Session()
        session.run(3, SHIFT)
        self.assertEqual(session.ability(SPRINT, 1).flags & RUNNING, 0)


class CooldownTests(unittest.TestCase):
    def test_helix_rockets_start_their_cooldown(self):
        session = Session()
        self.assertEqual(session.var(RIFLE, 472), 6.0)  # the cooldown's length: AbilityCooldownDuration
        session.run(1, SECONDARY)
        self.assertEqual(session.var(RIFLE, 61), f32(6.0 - f32(15 * 0.001)))  # set to 6, then 15 ms of it
        session.run(100, 0)
        self.assertAlmostEqual(session.var(RIFLE, 61), 6.0 - 1.615, places=3)
        session.run(300, 0)
        self.assertEqual(session.var(RIFLE, 61), 0.0)

    def test_biotic_field_throws_then_cools_down(self):
        session = Session()
        field = session.ability(BIOTIC, 0)
        session.run(1, E)
        self.assertEqual(field.flags, RUNNING | PUSHED | HELD)  # on the stack variable #33e, then started
        session.run(24, 0)  # the throw comes at 0.4 s
        self.assertEqual(session.var(BIOTIC, 61), 0.0)
        session.run(1, 0)
        self.assertIn(5, session.on(BIOTIC))  # the Wait after the throw volley
        self.assertGreater(session.var(BIOTIC, 61), 14.9)  # #61 = #472 = 15 at the throw
        session.run(15, 0)  # ActionStopAbility at 0.635 s
        self.assertEqual(field.flags, 0)
        session.run(1000, 0)
        self.assertEqual(session.var(BIOTIC, 61), 0.0)

    def test_quick_melee_and_its_cooldown(self):
        session = Session()
        melee = session.ability(MELEE_GRAPH, 6)
        session.run(1, MELEE)
        self.assertEqual(melee.flags, RUNNING | PUSHED | HELD)
        self.assertIn(24, session.on(MELEE_GRAPH))  # the hit list state: sent with an empty list
        self.assertGreater(session.var(MELEE_GRAPH, 61), 0.9)
        session.run(70, 0)
        self.assertEqual((melee.flags, session.var(MELEE_GRAPH, 61)), (0, 0.0))


class UltimateTests(unittest.TestCase):
    def test_the_charge_fills_by_itself(self):
        session = Session(ult_seconds=2.0)
        body = session.body
        cost = body.component.vars[338].value()
        self.assertEqual(cost, f32(1650 * f32(1.4)))
        self.assertAlmostEqual(body.component.vars[342].value(), cost * 30 * 16 / 2000, delta=1.0)
        session.run(100, 0)
        self.assertEqual(body.component.vars[342].value(), cost)
        self.assertGreater(ULT_SECONDS, 0)

    def test_tactical_visor_needs_a_full_charge(self):
        session = Session(ult_seconds=0)
        visor = session.ability(VISOR, 0)
        session.run(3, Q)
        self.assertEqual(visor.flags & RUNNING, 0)
        session.run(3, 0)
        session.body.set_ult_charge(session.body.component.vars[338].value())
        session.run(1, Q)
        self.assertEqual(visor.flags, RUNNING | HELD)
        self.assertEqual(session.body.component.vars[342].value(), 0)  # spent
        session.run(int(7.2 * 1000 / 16), 0)  # 1.2 s, then the 6 s ChaseVar #215
        self.assertEqual(visor.flags, 0)

    def test_no_charge_while_the_ultimate_runs(self):
        session = Session()
        session.body.set_ult_charge(session.body.component.vars[338].value())
        session.run(1, Q)
        session.run(100, 0)
        self.assertEqual(session.body.component.vars[342].value(), 0)


class HudTests(unittest.TestCase):
    def test_every_hud_presenter_is_on_after_spawn(self):
        session = Session()
        on = session.client.on()
        presenters = {
            (10, 23): "the reticle's Stack #3772e",
            (10, 42): "the reticle's client-only host",
            (RIFLE, 70): "the weapon with its ammo",
            (RIFLE, 69): "Helix Rockets",
            (SPRINT, 31): "Sprint",
            (BIOTIC, 16): "Biotic Field",
            (VISOR, 32): "the ultimate's meter",
        }
        for key, name in presenters.items():
            self.assertIn(key, on, name)

    def test_the_hud_variables_reach_the_client(self):
        session = Session()
        got = session.client.vars
        self.assertEqual((got[(RIFLE, 53)], got[(RIFLE, 198)]), (30, 30))  # ammo / magazine
        self.assertEqual(got[(RIFLE, 472)], 6.0)
        self.assertEqual(got[(BIOTIC, 472)], 15.0)
        self.assertEqual(got[(0, 338)], f32(1650 * f32(1.4)))  # the ultimate's cost; #342e its charge
        self.assertIn((0, 342), got)
        self.assertEqual((got[(0, 1732)], got[(0, 1737)]), (200.0, 200.0))  # the health number


if __name__ == "__main__":
    unittest.main()
