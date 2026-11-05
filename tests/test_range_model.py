"""Caller-driven range geometry, health and attack retransmission behavior; no game protocol claims."""

import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game.range_model import (
    AttackEvent,
    AttackId,
    CombatRange,
    EntityId,
    Ray,
    SphereTarget,
    ray_sphere_distance,
    trace_ray,
)


def sphere(entity=10, center=(0, 1, 10), radius=1, health=100):
    return SphereTarget(EntityId(entity), center, radius, health)


def attack(sequence=0, damage=25, **kwargs):
    values = {
        "attack_id": AttackId(EntityId(1), sequence),
        "origin": (0, 1, 0),
        "direction": (0, 0, 10),
        "damage": damage,
        "max_distance": 100,
    }
    values.update(kwargs)
    return AttackEvent(**values)


class RayGeometryTests(unittest.TestCase):
    def test_direction_is_normalized_so_distances_are_world_units(self):
        ray = Ray((0, 1, 0), (0, 0, 10))
        self.assertEqual(ray.direction, (0, 0, 1))
        self.assertEqual(ray_sphere_distance(ray, (0, 1, 10), 1), 9)
        self.assertEqual(ray.point_at(9), (0, 1, 9))

    def test_y_is_vertical_and_positive_yaw_forward_can_face_positive_x(self):
        ray = Ray((0, 2, 0), (math.sin(math.pi / 2), 0, math.cos(math.pi / 2)))
        self.assertAlmostEqual(ray_sphere_distance(ray, (10, 2, 0), 1), 9)
        self.assertIsNone(ray_sphere_distance(ray, (10, 4, 0), 1))

    def test_missed_and_behind_spheres_do_not_hit(self):
        ray = Ray((0, 0, 0), (0, 0, 1))
        self.assertIsNone(ray_sphere_distance(ray, (2, 0, 10), 1))
        self.assertIsNone(ray_sphere_distance(ray, (0, 0, -10), 1))

    def test_tangent_and_origin_inside_or_on_sphere(self):
        ray = Ray((0, 0, 0), (0, 0, 1))
        self.assertEqual(ray_sphere_distance(ray, (1, 0, 10), 1), 10)
        self.assertEqual(ray_sphere_distance(ray, (0, 0, 0), 1), 0)
        self.assertEqual(ray_sphere_distance(ray, (0, 0, -1), 1), 0)

    def test_nearest_hit_is_independent_of_target_order(self):
        near, far = sphere(20, (0, 1, 5)), sphere(10)
        ray = Ray((0, 1, 0), (0, 0, 1))
        for targets in ((far, near), (near, far)):
            hit = trace_ray(ray, targets, 100)
            self.assertEqual(hit.target, near.identity)
            self.assertEqual(hit.distance, 4)
            self.assertEqual(hit.point, (0, 1, 4))

    def test_equal_distance_uses_stable_entity_order(self):
        ray = Ray((0, 1, 0), (0, 0, 1))
        self.assertEqual(trace_ray(ray, (sphere(20), sphere(10)), 100).target, EntityId(10))

    def test_range_and_obstruction_boundaries(self):
        ray, targets = Ray((0, 1, 0), (0, 0, 1)), [sphere()]
        self.assertIsNone(trace_ray(ray, targets, 8.99))
        self.assertIsNotNone(trace_ray(ray, targets, 9))
        self.assertIsNone(trace_ray(ray, targets, 100, obstruction_distance=8))
        self.assertIsNone(trace_ray(ray, targets, 100, obstruction_distance=9))
        self.assertIsNotNone(trace_ray(ray, targets, 100, obstruction_distance=9.01))

    def test_zero_nonfinite_directions_and_invalid_geometry_are_rejected(self):
        for direction in ((0, 0, 0), (math.nan, 0, 1), (math.inf, 0, 1)):
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                Ray((0, 0, 0), direction)
        for kwargs in ({"radius": 0}, {"radius": -1}, {"health": math.nan}, {"health": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                sphere(**kwargs)
        for distance in (-1, math.inf, math.nan):
            with self.subTest(distance=distance), self.assertRaises(ValueError):
                trace_ray(Ray((0, 0, 0), (0, 0, 1)), [], distance)


class CombatRangeTests(unittest.TestCase):
    def test_damage_death_clamping_and_dead_targets_are_not_hit(self):
        target = sphere(health=40)
        simulation = CombatRange([target])
        first = simulation.attack(attack(damage=25))
        self.assertEqual((first.damage_applied, first.health_after, first.killed), (25, 15, False))
        death = simulation.attack(attack(1, damage=25))
        self.assertEqual((death.damage_applied, death.health_after, death.killed), (15, 0, True))
        self.assertFalse(target.alive)
        missed = simulation.attack(attack(2))
        self.assertIsNone(missed.hit)
        self.assertEqual((missed.damage_applied, missed.health_after, missed.killed), (0, None, False))

    def test_nearest_dead_target_does_not_hide_live_target(self):
        near, far = sphere(20, (0, 1, 5), health=25), sphere(10)
        simulation = CombatRange([far, near])
        simulation.attack(attack())
        self.assertEqual(simulation.attack(attack(1)).hit.target, far.identity)
        self.assertEqual(far.health, 75)

    def test_a_retransmitted_attack_does_not_apply_damage_twice(self):
        target, event = sphere(), attack()
        simulation = CombatRange([target])
        original = simulation.attack(event)
        duplicate = simulation.attack(event)
        self.assertEqual(target.health, 75)
        self.assertFalse(original.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.hit, original.hit)
        self.assertEqual(duplicate.damage_applied, 0)
        self.assertFalse(duplicate.killed)

    def test_reset_changes_target_lifecycle_and_old_attack_cannot_hurt_respawn(self):
        target, event = sphere(health=25), attack()
        simulation = CombatRange([target])
        death = simulation.attack(event)
        self.assertTrue(death.killed)
        respawn = simulation.reset_target(target.identity.entity_id)
        self.assertEqual(respawn, EntityId(10, 1))
        self.assertEqual(target.health, 25)
        duplicate = simulation.attack(event)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.hit.target, EntityId(10, 0))
        self.assertEqual(target.health, 25)
        fresh = simulation.attack(attack(1))
        self.assertEqual(fresh.hit.target, respawn)
        self.assertTrue(fresh.killed)

    def test_new_attacker_lifecycle_or_attacker_has_an_independent_sequence(self):
        target = sphere()
        simulation = CombatRange([target])
        for identity in (EntityId(1, 0), EntityId(1, 1), EntityId(2, 0)):
            result = simulation.attack(attack(attack_id=AttackId(identity, 0)))
            self.assertFalse(result.duplicate)
        self.assertEqual(target.health, 25)

    def test_missed_or_obstructed_attacks_are_also_deduplicated(self):
        for event in (attack(direction=(1, 0, 0)), attack(obstruction_distance=2)):
            with self.subTest(event=event):
                target = sphere()
                simulation = CombatRange([target])
                self.assertIsNone(simulation.attack(event).hit)
                self.assertTrue(simulation.attack(event).duplicate)
                self.assertEqual(target.health, 100)

    def test_reusing_an_attack_id_with_different_data_is_rejected_without_damage(self):
        target, event = sphere(), attack()
        simulation = CombatRange([target])
        simulation.attack(event)
        with self.assertRaises(ValueError):
            simulation.attack(replace(event, damage=90))
        self.assertEqual(target.health, 75)

    def test_zero_damage_is_a_hit_without_health_loss(self):
        target = sphere()
        result = CombatRange([target]).attack(attack(damage=0))
        self.assertIsNotNone(result.hit)
        self.assertEqual((result.damage_applied, result.health_after, result.killed), (0, 100, False))

    def test_duplicate_target_ids_and_invalid_attacks_are_rejected(self):
        with self.assertRaises(ValueError):
            CombatRange([sphere(), sphere()])
        for kwargs in ({"damage": -1}, {"max_distance": -1}, {"obstruction_distance": math.nan}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                attack(**kwargs)
        with self.assertRaises(ValueError):
            AttackId(EntityId(1), -1)


if __name__ == "__main__":
    unittest.main()
