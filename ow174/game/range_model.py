"""Offline, caller-driven hitscan and health for a controlled spherical-target range.

This is a simulation primitive, not a client weapon or world-collision implementation. Callers supply
the attack parameters and the nearest obstruction distance. Coordinates use y up; yaw zero faces +z,
as in commands.py. Attack history lasts for this CombatRange's lifetime, including target resets.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

Vec3 = tuple[float, float, float]


def _vector(value: Vec3) -> Vec3:
    if len(value) != 3 or not all(math.isfinite(component) for component in value):
        raise ValueError("A vector needs three finite components")
    return tuple(float(component) for component in value)


def _nonnegative(value: float, name: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")


def _identifier(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Entity, lifecycle and sequence identifiers must be nonnegative integers")


@dataclass(frozen=True, order=True)
class EntityId:
    """A reused entity slot has a new lifecycle, so an old hit identifies its original target."""

    entity_id: int
    lifecycle: int = 0

    def __post_init__(self) -> None:
        _identifier(self.entity_id)
        _identifier(self.lifecycle)


@dataclass(frozen=True)
class AttackId:
    """An authoritative attack sequence, scoped to one attacker lifecycle, not a network packet."""

    attacker: EntityId
    sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.attacker, EntityId):
            raise ValueError("An attack needs its attacker's entity and lifecycle")
        _identifier(self.sequence)


@dataclass(frozen=True)
class Ray:
    origin: Vec3
    direction: Vec3

    def __post_init__(self) -> None:
        origin, direction = _vector(self.origin), _vector(self.direction)
        scale = max(abs(component) for component in direction)
        if scale == 0:
            raise ValueError("A ray direction must be nonzero")
        direction = tuple(component / scale for component in direction)
        length = math.hypot(*direction)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "direction", tuple(component / length for component in direction))

    def point_at(self, distance: float) -> Vec3:
        _nonnegative(distance, "Distance")
        return tuple(
            origin + direction * distance
            for origin, direction in zip(self.origin, self.direction, strict=True)
        )


@dataclass
class SphereTarget:
    identity: EntityId
    center: Vec3
    radius: float
    max_health: float
    health: float = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, EntityId):
            raise ValueError("A target needs an entity and lifecycle")
        self.center = _vector(self.center)
        for value, name in ((self.radius, "Radius"), (self.max_health, "Maximum health")):
            _nonnegative(value, name)
            if value == 0:
                raise ValueError(f"{name} must be positive")
        self.health = self.max_health

    @property
    def alive(self) -> bool:
        return self.health > 0

    def reset(self) -> EntityId:
        """Respawn at the same sphere with full health and a new lifecycle."""
        self.identity = replace(self.identity, lifecycle=self.identity.lifecycle + 1)
        self.health = self.max_health
        return self.identity


@dataclass(frozen=True)
class Hit:
    target: EntityId
    distance: float
    point: Vec3


def ray_sphere_distance(ray: Ray, center: Vec3, radius: float) -> float | None:
    """Nearest distance along a normalized ray; starting inside/on the sphere hits at zero."""
    center = _vector(center)
    _nonnegative(radius, "Radius")
    offset = tuple(origin - middle for origin, middle in zip(ray.origin, center, strict=True))
    squared_radius = radius * radius
    squared_offset = sum(component * component for component in offset)
    if squared_offset <= squared_radius:
        return 0.0
    projection = sum(
        component * direction for component, direction in zip(offset, ray.direction, strict=True)
    )
    if projection >= 0:
        return None
    discriminant = projection * projection - (squared_offset - squared_radius)
    if discriminant < 0:
        return None
    return -projection - math.sqrt(discriminant)


def trace_ray(
    ray: Ray,
    targets: Iterable[SphereTarget],
    max_distance: float,
    obstruction_distance: float | None = None,
) -> Hit | None:
    """Nearest live sphere, with stable identity ordering for ties. A solid wins an exact tie.

    max_distance includes its boundary. obstruction_distance is the caller's distance to the first
    solid on this ray; this module has no map geometry and does not infer an unobstructed world.
    """
    _nonnegative(max_distance, "Maximum distance")
    if obstruction_distance is not None:
        _nonnegative(obstruction_distance, "Obstruction distance")
    hits = []
    for target in targets:
        if not target.alive:
            continue
        distance = ray_sphere_distance(ray, target.center, target.radius)
        if distance is None or distance > max_distance:
            continue
        if obstruction_distance is not None and distance >= obstruction_distance:
            continue
        hits.append(Hit(target.identity, distance, ray.point_at(distance)))
    return min(hits, key=lambda hit: (hit.distance, hit.target)) if hits else None


@dataclass(frozen=True)
class AttackEvent:
    attack_id: AttackId
    origin: Vec3
    direction: Vec3
    damage: float
    max_distance: float
    obstruction_distance: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attack_id, AttackId):
            raise ValueError("An attack needs a stable AttackId")
        ray = Ray(self.origin, self.direction)
        object.__setattr__(self, "origin", ray.origin)
        object.__setattr__(self, "direction", ray.direction)
        _nonnegative(self.damage, "Damage")
        _nonnegative(self.max_distance, "Maximum distance")
        if self.obstruction_distance is not None:
            _nonnegative(self.obstruction_distance, "Obstruction distance")


@dataclass(frozen=True)
class AttackResult:
    attack_id: AttackId
    hit: Hit | None
    damage_applied: float
    health_after: float | None
    killed: bool
    duplicate: bool = False


class CombatRange:
    """Serial simulation of caller-owned targets. Keep one instance for the whole range session."""

    def __init__(self, targets: Iterable[SphereTarget] = ()) -> None:
        self.targets: dict[int, SphereTarget] = {}
        self._attacks: dict[AttackId, tuple[AttackEvent, AttackResult]] = {}
        for target in targets:
            entity = target.identity.entity_id
            if entity in self.targets:
                raise ValueError(f"Duplicate target entity {entity}")
            self.targets[entity] = target

    def reset_target(self, entity_id: int) -> EntityId:
        return self.targets[entity_id].reset()

    def attack(self, event: AttackEvent) -> AttackResult:
        """Apply a new event once, including misses. Reusing an ID with different data is an error.

        A duplicate retains its original hit/health snapshot but reports zero applied damage and no
        new death. Resetting targets does not clear history, so an old packet cannot hurt a respawn.
        Callers validate the attacker's active lifecycle and generate authoritative attack sequences.
        """
        previous = self._attacks.get(event.attack_id)
        if previous is not None:
            original, result = previous
            if event != original:
                raise ValueError("An AttackId cannot be reused for a different event")
            return replace(result, damage_applied=0.0, killed=False, duplicate=True)
        ray = Ray(event.origin, event.direction)
        hit = trace_ray(ray, self.targets.values(), event.max_distance, event.obstruction_distance)
        damage, health, killed = 0.0, None, False
        if hit is not None:
            target = self.targets[hit.target.entity_id]
            damage = min(target.health, event.damage)
            target.health -= damage
            health, killed = target.health, not target.alive
        result = AttackResult(event.attack_id, hit, damage, health, killed)
        self._attacks[event.attack_id] = (event, result)
        return result
