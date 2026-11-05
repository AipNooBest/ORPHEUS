"""Statescript values and config vars, evaluated as the client does it (interpreter 0x7FF78AA91210).

Every value a graph computes is one of these Python forms (the client's value has a type number):

    null      255   None             an unset variable reads as null: false, 0, 0.0, and null == 0
    bool        1   bool
    int         2   int              32-bit signed
    float       3   float            float32: every float result is rounded to float32, as the client
                                     computes in float32
    id          4   Asset(guid)      a 64-bit asset or identifier
    vec3        6   Vec3(x, y, z)
    handle      7   Handle(...)
    entity     10   Entity(id)
    array      12   tuple
    map        13   Map(...)
    string          str

Rules read in the client: truthy 0x7FF789E74940 (an array is its element 0; an entity is valid when it is not
0 and its low 29 bits are not all set), ToFloat 0x7FF789E74C80 and ToInt 0x7FF789E75260 (null 0, bool 0/1,
a float rounds half away from zero, an array is its element 0), equality 0x7FF789E738C0 (numbers within
1.1920929e-5, a bool against the other side's truthiness, null against a number: equal when the number
is 0), the compares (`a - eps <= b`, `a < b - eps`, ...), ADD (vector only when both sides are vectors or
entities, else float). The opcode table was read in the PS4 build and checked
against the PC interpreter.

A config var is a dict of the graph data ({"$": class, ...}). `evaluate(cfg, ctx)` returns its value; the
context (class Context) answers what the graph cannot know by itself: variables, game message
parameters, the owner, the ruleset, and the config vars the server cannot compute (world queries, UI).
Those give a default and are logged once per class.
"""

import logging
import math
import random
import struct
from dataclasses import dataclass

log = logging.getLogger("ow174.script")

EPSILON = 1.1920928955078125e-05  # the compares' and equality's tolerance (dword_7FF78B50B054)
INSTANCE, ENTITY = 0, 1  # variable scopes (STUConfigVarDynamic.m_60DB8F99)
INVALID_ENTITY = 0


def f32(value: float) -> float:
    """A number rounded to float32."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def i32(value: int) -> int:
    value = int(value) & 0xFFFFFFFF
    return value - (1 << 32) if value >> 31 else value


def round_half_away(value: float) -> int:
    """ToInt of a float: (int)(x + 0.5) or (int)(x - 0.5), in float32."""
    if math.isnan(value) or math.isinf(value):
        return 0
    if value >= 0:
        return i32(math.trunc(f32(value + 0.5)))
    return i32(math.trunc(f32(value - 0.5)))


@dataclass(frozen=True)
class Asset:
    """A 64-bit id (type 4): an asset GUID, an identifier, a game message."""

    guid: int

    def __repr__(self) -> str:
        return f"Asset({self.guid:016X})"


@dataclass(frozen=True)
class Entity:
    id: int

    def __repr__(self) -> str:
        return f"Entity({self.id:08X})"


@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float

    def __iter__(self):
        return iter((self.x, self.y, self.z))


@dataclass(frozen=True)
class Handle:
    """A handle to a state of an instance (wire tag 14): kind, instance id, state, extra."""

    kind: int
    instance: int
    state: int
    extra: int = 0


@dataclass(frozen=True)
class Map:
    """A map (type 13): (key, value) pairs in order."""

    items: tuple = ()

    def get(self, key):
        for item_key, value in self.items:
            if equal(item_key, key):
                return value
        return None


def kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, tuple):
        return "array"
    return type(value).__name__.lower()


def valid_entity(entity_id: int) -> bool:
    return entity_id != INVALID_ENTITY and (entity_id & 0x1FFFFFFF) != 0x1FFFFFFF


def truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, float):
        return value != 0.0 and not math.isnan(value)
    if isinstance(value, Asset):
        return value.guid != 0
    if isinstance(value, Handle):
        return value.instance != 0
    if isinstance(value, Vec3):
        return value.x != 0.0 or value.y != 0.0 or value.z != 0.0
    if isinstance(value, Entity):
        return valid_entity(value.id)
    if isinstance(value, tuple):
        return truthy(value[0]) if value else False
    if isinstance(value, Map):
        return bool(value.items)
    return False


def to_float(value, default: float = 0.0) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int):
        return f32(float(value))
    if isinstance(value, float):
        return value
    if isinstance(value, tuple):
        return to_float(value[0], default) if value else default
    return default


def to_int(value, default: int = 0) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round_half_away(value)
    if isinstance(value, tuple):
        return to_int(value[0], default) if value else default
    return default


def to_entity(value) -> int:
    """An entity id from a value; INVALID_ENTITY for anything else."""
    if isinstance(value, Entity):
        return value.id
    if isinstance(value, tuple) and value:
        return to_entity(value[0])
    return INVALID_ENTITY


def _close(a: float, b: float) -> bool:
    return abs(f32(a - b)) <= EPSILON


def equal(a, b) -> bool:
    """Typed equality (EQ, 0x7FF789E738C0)."""
    if isinstance(b, tuple) and not isinstance(a, tuple):
        a, b = b, a
    if isinstance(b, bool):
        return truthy(a) == b
    if isinstance(a, bool):
        return a == truthy(b)
    if a is None:
        return not truthy(b)
    if isinstance(a, (int, float)):
        if b is None:
            return not truthy(a)
        if isinstance(b, (int, float)):
            if isinstance(a, int) and isinstance(b, int):
                return a == b
            return _close(float(a), float(b))
        return False
    if isinstance(a, Vec3):
        return isinstance(b, Vec3) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, tuple):
        if isinstance(b, tuple):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b, strict=True))
        return len(a) == 1 and equal(a[0], b)
    if isinstance(a, Map):
        return (
            isinstance(b, Map)
            and len(a.items) == len(b.items)
            and all(
                equal(k1, k2) and equal(v1, v2) for (k1, v1), (k2, v2) in zip(a.items, b.items, strict=True)
            )
        )
    return type(a) is type(b) and a == b


def contains_value(container, value) -> bool:
    """Test membership in a tuple or a singleton using VM equality."""
    if isinstance(container, tuple):
        return any(equal(item, value) for item in container)
    return container is not None and equal(container, value)


def same(a, b) -> bool:
    """Exactly the same value and type: what makes a variable "changed" for the wire and watchers."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    return a == b


# --- vectors ---------------------------------------------------------------------------------------


def _vec(value) -> Vec3:
    return value if isinstance(value, Vec3) else Vec3(0.0, 0.0, 0.0)


def _vec_op(a: Vec3, b: Vec3, op) -> Vec3:
    return Vec3(*(f32(op(x, y)) for x, y in zip(a, b, strict=True)))


def _length(v: Vec3) -> float:
    return f32(math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z))


# --- the context ---------------------------------------------------------------------------------


class Context:
    """What an evaluation reads outside its expression. This base answers like an empty world: no
    variables, no parameters, no owner; a subclass gives the runtime's answers."""

    def __init__(self, seed: int = 1) -> None:
        self.rng = random.Random(seed)

    def var(self, scope: int, var: int):
        return None

    def proxy_var(self, levels: int, scope: int, var: int):
        """A variable of the instance `levels` steps up the parent chain (a SubScript's parent)."""
        return None

    def proxy_entity(self, levels: int):
        return None

    def param(self, var: int):
        """A parameter of the game message, CycleIndex candidate, ... being handled."""
        return None

    def owner(self):
        return None

    def member(self, entity: int, var: int):
        """`entity.#var`: another entity's entity-scope variable."""
        return None

    def position(self, entity: int) -> Vec3:
        return Vec3(0.0, 0.0, 0.0)

    def ruleset(self, key: int):
        """GetGameRulesetValue: the hero's or mode's ruleset value for a key (an identifier)."""
        return None

    def would_stack_to_top(self, var: tuple[int, int], priority: tuple[float, bool]):
        return True

    def native(self, cfg: dict):
        """A config var the server cannot compute (a world query, the UI): its default."""
        return default_native(cfg)


_warned: set[str] = set()


def warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.info("[script] " + message, *args)


# Config vars that need the client's world or UI. The server answers what the owning client would see in
# a normal game, so its prediction and our frames agree.
NATIVE_DEFAULTS = {
    "STU_CC623180": True,  # IsHUDEnabled
    "STU_98E9109B": False,  # CustomGameLogicEnabled
    "STU_5E75FF05": 0,  # ReticleType
    "STU_C76C63AF": 0.0,  # ReticleNumericSetting
}


def default_native(cfg: dict):
    cls = cfg.get("$", "?")
    if cls in NATIVE_DEFAULTS:
        return NATIVE_DEFAULTS[cls]
    warn_once(cls, "config var %s is not computed on the server: null", cls)
    return None


# --- config vars ---------------------------------------------------------------------------------


def guid_of(value) -> int:
    """An asset GUID of the graph data ("0x..." text) as an int, 0 for none."""
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    return 0


def dynamic(cfg) -> tuple[int, int] | None:
    """(scope, var id) of a STUConfigVarDynamic, None for anything else."""
    if isinstance(cfg, dict) and cfg.get("$") == "STUConfigVarDynamic":
        return (ENTITY if cfg.get("m_60DB8F99") else INSTANCE), int(cfg.get("m_identifier") or 0)
    return None


def _number(cfg: dict, convert):
    value = cfg.get("m_value")
    return convert(value if value is not None else 0)


CONSTANTS = {
    "STUConfigVarBool": lambda cfg: bool(cfg.get("m_value")),
    "STUConfigVarInt": lambda cfg: _number(cfg, i32),
    "STUConfigVarFloat": lambda cfg: _number(cfg, lambda v: f32(float(v))),
    "STUConfigVarLogicalButton": lambda cfg: int(cfg.get("m_logicalButton") or 0),
}
# Resource config vars: their value is the resource's id.
RESOURCES = {
    "STUConfigVarIdentifier": "m_identifier",
    "STUConfigVarGameMessage": "m_gameMessage",
    "STUConfigVarEffect": "m_effect",
    "STUConfigVarLoadout": "m_loadout",
    "STUConfigVarHardPoint": "m_hardPoint",
    "STUConfigVarGameMode": "m_gamemode",
    "STU_BCD1C634": "m_id",  # Enumeration: an identifier of an enum type
    "STU_8A491388": "m_priority",  # StackPriority: a .024 asset
}
EXPRESSIONS = {"STU_919FD47C", "STUConfigVarExpression"}


def evaluate(cfg, ctx: Context):
    """The value of a config var (None for no config var)."""
    if cfg is None:
        return None
    if not isinstance(cfg, dict):
        return cfg
    cls = cfg.get("$")
    if cls == "STUConfigVarDynamic":
        scope, var = dynamic(cfg)
        return ctx.var(scope, var)
    if cls in CONSTANTS:
        return CONSTANTS[cls](cfg)
    if cls in EXPRESSIONS:
        return run(cfg, ctx)
    if cls == "STU_45EDF12D" and cfg.get("m_key") is None:
        container = evaluate(cfg.get("m_814F8EC3"), ctx)
        value = evaluate(cfg.get("m_value"), ctx)
        return contains_value(container, value)
    if cls in RESOURCES:
        return Asset(guid_of(cfg.get(RESOURCES[cls])))
    if cls == "STU_B5A0CAF0":  # GetOwner
        return ctx.owner()
    if cls == "STU_3832D36C":  # GetGameRulesetValue
        key = cfg.get("m_value")
        found = dynamic(key)
        return ctx.ruleset(found[1] if found else to_int(evaluate(key, ctx)))
    if cls == "STU_91EFD5B1":  # ValueOrDefault
        value = evaluate(cfg.get("m_value"), ctx)
        return value if value is not None else evaluate(cfg.get("m_default"), ctx)
    if cls == "STU_3ACE35FB":  # WouldVarStackToTop
        var = dynamic(cfg.get("m_E50CF556"))
        return ctx.would_stack_to_top(var, stack_priority(cfg.get("m_012B7AF7")))
    if cls == "STU_7197C080":
        return cooldown_duration(cfg, ctx)
    if cls == "STU_BE0BD267":  # Random float between the asset's bounds (0B92's waits, 0C17's chance gate).
        low = f32(to_float(cfg.get("m_min")))
        high = f32(to_float(cfg.get("m_max")))
        return f32(ctx.rng.uniform(low, high))
    if cls == "STUConfigVarVec3":
        value = cfg.get("m_value") or {}
        # The target build's DataTool export preserves the teVec3 field casing.
        return Vec3(*(f32(float(value.get(axis, value.get(axis.lower())) or 0.0)) for axis in "XYZ"))
    if "m_resourceKey" in cfg:  # any other resource config var: the resource's id
        for key, value in cfg.items():
            if key != "m_resourceKey" and guid_of(value):
                return Asset(guid_of(value))
    return ctx.native(cfg)


def cooldown_duration(cfg: dict, ctx: Context) -> float:
    """AbilityCooldownDuration (evaluator 0x7FF789BE2E80, field offsets from its type's field list):
    max(base * scalar, least); the scalar defaults to 1 (a cooldown rule of the mode or hero)."""
    base = to_float(evaluate(cfg.get("m_01AFADB6"), ctx))
    least = to_float(evaluate(cfg.get("m_EA70ACC2"), ctx))
    scalar = cfg.get("m_69A20070")
    product = f32((to_float(evaluate(scalar, ctx)) if scalar is not None else 1.0) * base)
    return product if product > least else least


# StackPriority assets (.024): priority and the flag that puts a new entry above the entries with the
# same priority (DataTool dump of 1.74: every .024 there is).
STACK_PRIORITIES = {
    0x0001: (0, 0), 0x0002: (-10, 0), 0x0003: (10, 0), 0x0015: (20, 0), 0x003D: (0, 1), 0x003E: (-5, 1),
    0x0051: (30, 0), 0x0066: (10, 0), 0x0079: (-5, 0), 0x008D: (2, 0), 0x008E: (0, 0), 0x008F: (1, 0),
    0x0090: (-1, 0), 0x0091: (3, 0), 0x00A1: (-20, 0), 0x00B5: (5, 0), 0x00C9: (-2, 0), 0x00DD: (-10, 1),
    0x00F1: (25, 0), 0x00F2: (16, 0), 0x00F3: (99, 0), 0x00F4: (4, 0), 0x00F6: (14, 1), 0x00F7: (9, 0),
    0x00F9: (5, 0), 0x00FA: (15, 0), 0x00FE: (19, 0),
}  # fmt: skip


def stack_priority(cfg) -> tuple[float, bool]:
    """(priority, above equals) of a StackPriority config var; no asset: 0.0 and above."""
    guid = guid_of(cfg.get("m_priority")) if isinstance(cfg, dict) else 0
    if not guid:
        return 0.0, True
    priority, above = STACK_PRIORITIES.get(guid & 0xFFFFFFFF, (0, 0))
    return float(priority), bool(above)


def lvalue(cfg) -> tuple[int, int] | None:
    """(scope, var id) that an out var writes: a STUConfigVarDynamic, or an expression that is a single
    PUSH_VAR (the client's lvalue mode records the first push)."""
    found = dynamic(cfg)
    if found is not None:
        return found
    if isinstance(cfg, dict) and cfg.get("$") in EXPRESSIONS:
        code, dynamics, _ = _parts(cfg)
        if len(code) >= 2 and code[0] in (8, 9) and (len(code) == 2 or code[2] == 0):
            return dynamic(dynamics[code[1]])
    return None


# --- the bytecode --------------------------------------------------------------------------------


class ExpressionError(Exception):
    pass


def _parts(cfg: dict):
    data = cfg.get("m_expression") or {}
    return (
        list(data.get("m_opcodes") or []),
        list(data.get("m_dynamicVars") or []),
        list(data.get("m_D99EF254") or []),
    )


# Operand bytes per opcode (the rest have none).
OPERANDS = dict.fromkeys((1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 19, 20, 21), 1)
OPERANDS[10] = 2
MAX_EXPRESSION_STEPS = 4096  # far above the extracted expressions; bounds malformed loops/streams
COMPARES = {
    33: lambda a, b: a - EPSILON <= b,
    34: lambda a, b: a < b - EPSILON,
    35: lambda a, b: a + EPSILON >= b,
    36: lambda a, b: a > b + EPSILON,
}


def _index(obj, index):
    """MEMBER_BY_INDEX: an array element (null past the end), the value itself at index 0 of a
    non-array, or the element equal to a non-numeric index."""
    if isinstance(index, (bool, int, float)) or index is None:
        number = to_int(index)
        if isinstance(obj, tuple):
            return obj[number] if 0 <= number < len(obj) else None
        return obj if number == 0 else None
    if isinstance(obj, tuple):
        for item in obj:
            if equal(item, index):
                return item
    return None


def _member(obj, key: int, ctx: Context):
    if isinstance(obj, Entity) or (isinstance(obj, tuple) and obj and isinstance(obj[0], Entity)):
        entity = to_entity(obj)
        return ctx.member(entity, key) if valid_entity(entity) else None
    if isinstance(obj, Map):
        return obj.get(key)
    return None


def _arith(op: int, a, b, ctx: Context):
    vector = all(isinstance(x, (Vec3, Entity)) for x in (a, b))
    if op in (23, 24) and vector:
        va = ctx.position(a.id) if isinstance(a, Entity) else a
        vb = ctx.position(b.id) if isinstance(b, Entity) else b
        return _vec_op(va, vb, (lambda x, y: x + y) if op == 23 else (lambda x, y: x - y))
    if op == 25 and (isinstance(a, Vec3) or isinstance(b, Vec3)):
        if isinstance(a, Vec3) and isinstance(b, Vec3):
            return _vec_op(a, b, lambda x, y: x * y)
        vec, scalar = (a, b) if isinstance(a, Vec3) else (b, a)
        s = to_float(scalar)
        return Vec3(*(f32(x * s) for x in vec))
    if op == 26 and isinstance(a, Vec3):
        if isinstance(b, Vec3):
            return Vec3(*(f32(x / y) if y else 0.0 for x, y in zip(a, b, strict=True)))
        s = to_float(b)
        if abs(s) < 1.18e-36:
            return Vec3(0.0, 0.0, 0.0)
        inverse = f32(1.0 / s)
        return Vec3(*(f32(x * inverse) for x in a))
    x, y = to_float(a), to_float(b)
    if op == 23:
        return f32(x + y)
    if op == 24:
        return f32(x - y)
    if op == 25:
        return f32(x * y)
    if op == 26:
        return 0.0 if y == 0.0 or math.isnan(y) else f32(x / y)
    if op == 27:
        return 0.0 if y == 0.0 else f32(math.fmod(x, y))
    if op == 28:
        try:
            return f32(math.pow(x, y)) if x > 0 else 0.0
        except OverflowError:
            return math.inf
    raise ExpressionError(f"opcode {op}")


def _unary(op: int, a, ctx: Context):
    if op == 29:
        return Vec3(-a.x, -a.y, -a.z) if isinstance(a, Vec3) else f32(-to_float(a))
    if op == 30:
        return not truthy(a)
    if op in (50, 51):
        if isinstance(a, (Vec3, Entity)):
            return _length(ctx.position(a.id) if isinstance(a, Entity) else a)
        return f32(abs(to_float(a)))
    if op == 52:
        v = _vec(a)
        return f32(math.hypot(v.x, v.z))
    if op == 57:
        v = _vec(a)
        length = _length(v)
        return Vec3(*(f32(x / length) for x in v)) if length else Vec3(0.0, 0.0, 0.0)
    if op in (60, 61, 62):
        return getattr(a, "xyz"[op - 60]) if isinstance(a, Vec3) else 0.0
    x = to_float(a)
    if op == 39:
        return f32(math.sqrt(x)) if x > 0 else 0.0
    if op == 40:
        return f32(math.radians(x))
    if op == 41:
        return f32(math.degrees(x))
    if op in (42, 43, 44):
        return f32((math.sin, math.cos, math.tan)[op - 42](x))
    if op in (46, 47, 48):
        return f32((math.sin, math.cos, math.tan)[op - 46](math.radians(x)))
    if op == 66:
        return f32(math.floor(x)) if math.isfinite(x) else x
    if op == 67:
        return f32(math.ceil(x)) if math.isfinite(x) else x
    if op == 68:
        return round_half_away(x)
    raise ExpressionError(f"opcode {op}")


def _jump_target(pc: int, offset: int, size: int) -> int:
    target = pc + offset
    if offset == 0:
        raise ExpressionError(f"non-progressing jump at byte {pc}")
    if not 0 <= target < size:
        raise ExpressionError(f"jump at byte {pc} leaves the stream: target {target}, size {size}")
    return target


def run(cfg: dict, ctx: Context, *, max_steps: int = MAX_EXPRESSION_STEPS):
    """Run an expression's bytecode. A broken stream or an opcode we do not know gives null, as the client
    leaves the result untouched on an unknown opcode. ``max_steps`` bounds executed instructions;
    invalid instructions and exhausted budgets use the same once-per-expression error report.

    Validate only the path actually executed: conditional streams can enter bytes that a linear
    walk from zero would mistake for operands in an untaken branch.
    """
    code, dynamics, floats = _parts(cfg)
    configs = list(cfg.get("m_configVars") or [])
    stack: list = []
    pc = 0
    try:
        if type(max_steps) is not int or max_steps < 1:
            raise ExpressionError("max_steps must be a positive integer")
        steps = 0
        while pc < len(code):
            if steps >= max_steps:
                raise ExpressionError(f"instruction budget {max_steps} exhausted at byte {pc}")
            steps += 1
            op = code[pc]
            if type(op) is not int or not 0 <= op <= 255:
                raise ExpressionError(f"invalid opcode byte {op!r} at {pc}")
            operands = OPERANDS.get(op, 0)
            if pc + 1 + operands > len(code):
                raise ExpressionError(f"truncated opcode {op} at byte {pc}: needs {operands} operand bytes")
            for index in range(pc + 1, pc + 1 + operands):
                operand = code[index]
                if type(operand) is not int or not 0 <= operand <= 255:
                    raise ExpressionError(f"invalid operand byte {operand!r} at {index}")
            arg = code[pc + 1] if operands else 0
            if op == 0:
                return stack[-1] if stack else None
            if op == 1:
                pc = _jump_target(pc, arg, len(code))
                continue
            if op in (2, 3):
                keep = truthy(stack[-1]) != (op == 2)
                if keep:
                    pc = _jump_target(pc, arg, len(code))
                else:
                    stack.pop()
                    pc += 2
                continue
            if op in (4, 5):
                jump = truthy(stack.pop()) == (op == 5)
                pc = _jump_target(pc, arg, len(code)) if jump else pc + 2
                continue
            if op == 6:
                stack.append(f32(float(floats[arg])))
            elif op == 7:
                stack.append(int(dynamics[arg].get("m_identifier") or 0))
            elif op in (8, 9):
                scope, var = dynamic(dynamics[arg])
                stack.append(ctx.var(scope, var))
            elif op == 10:
                scope, var = dynamic(dynamics[code[pc + 2]])
                stack.append(ctx.proxy_var(arg, scope, var))
            elif op in (11, 12):
                key = to_int(stack.pop())
                stack.append(ctx.proxy_var(arg, ENTITY if op == 12 else INSTANCE, key))
            elif op == 13:
                stack.append(ctx.param(int(dynamics[arg].get("m_identifier") or 0)))
            elif op == 14:
                stack.append(evaluate(configs[arg] if arg < len(configs) else None, ctx))
            elif op in (15, 16):
                key = stack.pop()
                key = key.guid & 0xFFFF if isinstance(key, Asset) else to_int(key)
                stack.append(_member(stack.pop(), key, ctx))
            elif op == 17:
                index = stack.pop()
                stack.append(_index(stack.pop(), index))
            elif op == 18:
                top = stack.pop()
                stack.append(len(top) if isinstance(top, tuple) else 0)
            elif op == 19:
                pairs = stack[len(stack) - 2 * arg :]
                del stack[len(stack) - 2 * arg :]
                stack.append(Map(tuple((to_int(pairs[k]), pairs[k + 1]) for k in range(0, len(pairs), 2))))
            elif op == 20:
                items = tuple(stack[len(stack) - arg :]) if arg else ()
                del stack[len(stack) - arg :]
                stack.append(items)
            elif op == 21:
                stack.append(ctx.proxy_entity(arg))
            elif op == 22:
                b, a = stack.pop(), stack.pop()
                left = a if isinstance(a, tuple) else (() if a is None else (a,))
                right = b if isinstance(b, tuple) else (() if b is None else (b,))
                stack.append(left + right)
            elif op in (23, 24, 25, 26, 27, 28):
                b, a = stack.pop(), stack.pop()
                stack.append(_arith(op, a, b, ctx))
            elif op in (31, 32):
                key = to_int(stack.pop())
                stack.append(ctx.var(ENTITY if op == 32 else INSTANCE, key))
            elif op in COMPARES:
                b, a = to_float(stack.pop()), to_float(stack.pop())
                stack.append(COMPARES[op](a, b))
            elif op in (37, 38):
                b, a = stack.pop(), stack.pop()
                stack.append(equal(a, b) == (op == 37))
            elif op in (45, 49):
                x, y = to_float(stack.pop()), to_float(stack.pop())
                angle = math.atan2(y, x)
                stack.append(f32(math.degrees(angle) if op == 49 else angle))
            elif op in (53, 54):
                b, a = stack.pop(), stack.pop()
                va = ctx.position(a.id) if isinstance(a, Entity) else _vec(a)
                vb = ctx.position(b.id) if isinstance(b, Entity) else _vec(b)
                d = _vec_op(va, vb, lambda x, y: x - y)
                stack.append(_length(d) if op == 53 else f32(math.hypot(d.x, d.z)))
            elif op in (55, 56):
                b, a = _vec(stack.pop()), _vec(stack.pop())
                if op == 55:
                    stack.append(f32(a.x * b.x + a.y * b.y + a.z * b.z))
                else:
                    x = f32(a.y * b.z - a.z * b.y)
                    y = f32(a.z * b.x - a.x * b.z)
                    stack.append(Vec3(x, y, f32(a.x * b.y - a.y * b.x)))
            elif op == 58:
                z, y, x = to_float(stack.pop()), to_float(stack.pop()), to_float(stack.pop())
                stack.append(Vec3(x, y, z))
            elif op in (63, 64):
                b, a = to_float(stack.pop()), to_float(stack.pop())
                stack.append(min(a, b) if op == 63 else max(a, b))
            elif op == 65:
                hi, lo, v = to_float(stack.pop()), to_float(stack.pop()), to_float(stack.pop())
                stack.append(max(min(v, max(lo, hi)), min(lo, hi)))
            elif op in (69, 70):
                b, a = stack.pop(), stack.pop()
                if op == 70 or (isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool)):
                    low, high = sorted((to_int(a), to_int(b)))
                    stack.append(ctx.rng.randint(low, high))
                else:
                    stack.append(f32(ctx.rng.uniform(to_float(a), to_float(b))))
            elif op == 71:
                t, b, a = to_float(stack.pop()), to_float(stack.pop()), to_float(stack.pop())
                stack.append(f32(a + (b - a) * t))
            elif op in (29, 30, 39, 40, 41, 42, 43, 44, 46, 47, 48, 50, 51, 52, 57, 60, 61, 62, 66, 67, 68):
                stack.append(_unary(op, stack.pop(), ctx))
            else:
                warn_once(f"op{op}", "expression opcode %d is not modelled: null", op)
                return None
            pc += 1 + OPERANDS.get(op, 0)
        return stack[-1] if stack else None
    except (IndexError, ExpressionError, KeyError, TypeError) as error:
        warn_once(f"broken{id(cfg)}", "expression %s could not run (%s): null", code, error)
        return None


# --- reading expressions back ------------------------------------------------------------------------

BINARY = {17: "[]", 22: "++", 23: "+", 24: "-", 25: "*", 26: "/", 27: "%"}
BINARY.update({33: "<=", 34: "<", 35: ">=", 36: ">", 37: "==", 38: "!="})
FUNCTIONS = {
    18: "count", 28: "pow", 29: "-", 30: "!", 39: "sqrt", 40: "rad", 41: "deg", 42: "sinr", 43: "cosr",
    44: "tanr", 45: "atan2r", 46: "sin", 47: "cos", 48: "tan", 49: "atan2", 50: "abs", 51: "mag",
    52: "magxz", 53: "dist", 54: "distxz", 55: "dot", 56: "cross", 57: "norm", 58: "vec3", 60: "x",
    61: "y", 62: "z", 63: "min", 64: "max", 65: "clamp", 66: "floor", 67: "ceil", 68: "round", 69: "random",
    70: "randomInt", 71: "lerp",
}  # fmt: skip
ARITY = {28: 2, 45: 2, 49: 2, 53: 2, 54: 2, 55: 2, 56: 2, 58: 3, 63: 2, 64: 2, 65: 3, 69: 2, 70: 2, 71: 3}


def _var_text(cfg) -> str:
    found = dynamic(cfg)
    if found is None:
        return "?"
    return f"#{found[1]}{'e' if found[0] == ENTITY else ''}"


def source(cfg, depth: int = 0) -> str:
    """A config var as text, for logs and dumps: `#53` (instance var), `#31e` (entity var), `param(12)`,
    `x.#5918`, `a[i]`, `(c ? t : f)`."""
    if cfg is None:
        return "null"
    if not isinstance(cfg, dict):
        return repr(cfg)
    cls = cfg.get("$", "?")
    if cls == "STUConfigVarDynamic":
        return _var_text(cfg)
    if cls in CONSTANTS:
        value = CONSTANTS[cls](cfg)
        return f"button({value})" if cls == "STUConfigVarLogicalButton" else repr(value)
    if cls in RESOURCES:
        return f"{cls.replace('STUConfigVar', '')}({guid_of(cfg.get(RESOURCES[cls])):X})"
    if cls == "STU_B5A0CAF0":
        return "owner()"
    if cls == "STU_3832D36C":
        return f"ruleset({source(cfg.get('m_value'), depth + 1)})"
    if cls == "STU_91EFD5B1":
        return f"valueOr({source(cfg.get('m_value'), depth + 1)}, {source(cfg.get('m_default'), depth + 1)})"
    if cls == "STU_3ACE35FB":
        return f"wouldStackToTop({source(cfg.get('m_E50CF556'), depth + 1)})"
    if cls == "STU_7197C080":
        parts = (source(cfg.get(key), depth + 1) for key in ("m_01AFADB6", "m_EA70ACC2", "m_69A20070"))
        return "cooldown({}, {}, {})".format(*parts)
    if cls not in EXPRESSIONS:
        return f"{cls}()"
    if depth > 6:
        return "expr(...)"
    code, dynamics, floats = _parts(cfg)
    configs = list(cfg.get("m_configVars") or [])
    try:
        return _decompile(code, dynamics, floats, configs, depth)
    except (IndexError, KeyError, TypeError):
        return f"expr{code}"


def _decompile(code, dynamics, floats, configs, depth) -> str:
    def run_text(pc: int, stack: list, stop: int | None = None) -> tuple[str, int, str]:
        """Text of the code from pc up to an END ("end"), a JUMP ("jump", pc = its target) or `stop`."""
        while pc < len(code) and pc != stop:
            op = code[pc]
            arg = code[pc + 1] if pc + 1 < len(code) else 0
            if op == 0:
                return (stack[-1] if stack else "?"), pc, "end"
            if op == 1:
                return (stack[-1] if stack else "?"), pc + arg, "jump"
            if op in (2, 3):
                left = stack.pop()
                right, _, _ = run_text(pc + 2, [], pc + arg)
                stack.append(f"({left} {'&&' if op == 2 else '||'} {right})")
                pc += arg
                continue
            if op in (4, 5):
                cond = stack.pop()
                if op == 5:
                    cond = f"!{cond}"
                then, merge, how = run_text(pc + 2, [])
                if how != "jump":  # the true branch ends the expression: so does the false one
                    other, end, how = run_text(pc + arg, [])
                    return f"({cond} ? {then} : {other})", end, how
                other, _, _ = run_text(pc + arg, [], merge)
                stack.append(f"({cond} ? {then} : {other})")
                pc = merge
                continue
            if op == 6:
                stack.append(repr(f32(float(floats[arg]))))
            elif op == 7:
                stack.append(f"id({dynamics[arg].get('m_identifier')})")
            elif op in (8, 9):
                stack.append(_var_text(dynamics[arg]))
            elif op == 10:
                stack.append(f"parent{arg}.{_var_text(dynamics[code[pc + 2]])}")
            elif op == 13:
                stack.append(f"param({dynamics[arg].get('m_identifier')})")
            elif op == 14:
                stack.append(source(configs[arg], depth + 1))
            elif op in (15, 16):
                key = stack.pop()
                stack.append(f"{stack.pop()}.{key}")
            elif op == 17:
                index = stack.pop()
                stack.append(f"{stack.pop()}[{index}]")
            elif op in (19, 20):
                count = arg * (2 if op == 19 else 1)
                items = stack[len(stack) - count :] if count else []
                del stack[len(stack) - count :]
                stack.append(("map" if op == 19 else "array") + f"({', '.join(items)})")
            elif op == 21:
                stack.append(f"parentEntity{arg}")
            elif op in (31, 32):
                stack.append(f"{'globalDeref' if op == 32 else 'deref'}({stack.pop()})")
            elif op in BINARY:
                b, a = stack.pop(), stack.pop()
                stack.append(f"({a} {BINARY[op]} {b})")
            elif op in FUNCTIONS:
                arity = ARITY.get(op, 1)
                args = stack[len(stack) - arity :]
                del stack[len(stack) - arity :]
                name = FUNCTIONS[op]
                stack.append(f"{name}{args[0]}" if op in (29, 30) else f"{name}({', '.join(args)})")
            else:
                stack.append(f"op{op}")
            pc += 1 + OPERANDS.get(op, 0)
        return (stack[-1] if stack else "?"), pc, "stop"

    return run_text(0, [])[0]
