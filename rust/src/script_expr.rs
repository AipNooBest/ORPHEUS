//! Typed config and expression evaluation for server-side graph initialization.

use std::{error::Error, fmt};

use serde_json::Value as Json;

pub use crate::script_wire::Value;
use crate::{accounts::parse_guid, script_graph::asset_guid};

pub(crate) const EPSILON: f64 = 1.192_092_895_507_812_5e-5;
const MAX_CONFIG_DEPTH: usize = 64;
pub const MAX_EXPRESSION_STEPS: usize = 4096;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Scope {
    Instance,
    Entity,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Variable {
    pub scope: Scope,
    pub id: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StackPriority {
    pub level: i8,
    pub above_equals: bool,
}

/// Priority assets documented by the 104319 reference evaluator.
pub fn stack_priority(config: &Json) -> Result<StackPriority, EvalError> {
    let asset = child(config, "m_priority");
    if asset.is_null() {
        return Ok(StackPriority {
            level: 0,
            above_equals: true,
        });
    }
    let guid = resource(asset).ok_or(EvalError::MalformedConfig("stack priority asset"))?;
    let (level, above_equals) = match guid as u32 {
        0x0000 | 0x003d => (0, true),
        0x0001 | 0x008e => (0, false),
        0x0002 => (-10, false),
        0x0003 | 0x0066 => (10, false),
        0x0015 => (20, false),
        0x003e => (-5, true),
        0x0051 => (30, false),
        0x0079 => (-5, false),
        0x008d => (2, false),
        0x008f => (1, false),
        0x0090 => (-1, false),
        0x0091 => (3, false),
        0x00a1 => (-20, false),
        0x00b5 | 0x00f9 => (5, false),
        0x00c9 => (-2, false),
        0x00dd => (-10, true),
        0x00f1 => (25, false),
        0x00f2 => (16, false),
        0x00f3 => (99, false),
        0x00f4 => (4, false),
        0x00f6 => (14, true),
        0x00f7 => (9, false),
        0x00fa => (15, false),
        0x00fe => (19, false),
        _ => {
            return Err(EvalError::UnsupportedConfig(format!(
                "stack priority {guid:016X}"
            )));
        }
    };
    Ok(StackPriority {
        level,
        above_equals,
    })
}

/// External reads may register runtime watchers. An unset variable is null.
pub trait Context {
    fn variable(&mut self, _variable: Variable) -> Value {
        Value::Null
    }

    fn parent_variable(&mut self, _levels: u8, _variable: Variable) -> Value {
        Value::Null
    }

    fn parent_entity(&mut self, _levels: u8) -> Value {
        Value::Null
    }

    fn parameter(&mut self, _id: u32) -> Value {
        Value::Null
    }

    fn owner(&mut self) -> Value {
        Value::Null
    }

    fn entity_member(&mut self, _entity: u32, _id: u32) -> Value {
        Value::Null
    }

    fn position(&mut self, _entity: u32) -> [f64; 3] {
        [0.0; 3]
    }

    fn body_scale(&mut self, _entity: u32) -> Option<f64> {
        None
    }

    fn ruleset(&mut self, _id: u32) -> Value {
        Value::Null
    }

    fn would_stack_to_top(
        &mut self,
        _variable: Variable,
        _priority: StackPriority,
    ) -> Option<bool> {
        None
    }

    /// None means the native is unavailable. Some(Null) is a computed result.
    fn native(&mut self, _config: &Json) -> Option<Value> {
        None
    }
}

#[derive(Default)]
pub struct EmptyContext;

impl Context for EmptyContext {}

#[derive(Debug, Eq, PartialEq)]
pub enum EvalError {
    UnsupportedConfig(String),
    UnsupportedOpcode { opcode: u8, offset: usize },
    MalformedConfig(&'static str),
    MalformedBytecode { offset: usize, reason: &'static str },
    BudgetExceeded { offset: usize, limit: usize },
    NestingExceeded,
}

impl fmt::Display for EvalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedConfig(class) => write!(f, "unsupported statescript config {class}"),
            Self::UnsupportedOpcode { opcode, offset } => {
                write!(
                    f,
                    "unsupported statescript opcode {opcode} at byte {offset}"
                )
            }
            Self::MalformedConfig(reason) => write!(f, "malformed statescript config: {reason}"),
            Self::MalformedBytecode { offset, reason } => {
                write!(
                    f,
                    "malformed statescript bytecode at byte {offset}: {reason}"
                )
            }
            Self::BudgetExceeded { offset, limit } => {
                write!(
                    f,
                    "statescript instruction budget {limit} exhausted at byte {offset}"
                )
            }
            Self::NestingExceeded => f.write_str("statescript config nesting limit exceeded"),
        }
    }
}

impl Error for EvalError {}

pub fn evaluate(config: &Json, context: &mut impl Context) -> Result<Value, EvalError> {
    evaluate_with_budget(config, context, MAX_EXPRESSION_STEPS)
}

/// The instruction budget is shared by nested expressions.
pub fn evaluate_with_budget(
    config: &Json,
    context: &mut impl Context,
    max_steps: usize,
) -> Result<Value, EvalError> {
    if max_steps == 0 {
        return Err(EvalError::MalformedConfig(
            "instruction budget must be positive",
        ));
    }
    Evaluator {
        limit: max_steps,
        steps: 0,
    }
    .config(config, context, 0)
}

pub fn dynamic(config: &Json) -> Result<Option<Variable>, EvalError> {
    if class(config) != Some("STUConfigVarDynamic") {
        return Ok(None);
    }
    let scope = if json_truthy(child(config, "m_60DB8F99")) {
        Scope::Entity
    } else {
        Scope::Instance
    };
    Ok(Some(Variable {
        scope,
        id: identifier(config)?,
    }))
}

/// Return a direct dynamic variable or the bytecode form of a single variable push.
pub fn lvalue(config: &Json) -> Result<Option<Variable>, EvalError> {
    if let Some(variable) = dynamic(config)? {
        return Ok(Some(variable));
    }
    if !is_expression(class(config)) {
        return Ok(None);
    }
    let parts = Expression::read(config)?;
    if parts.code.len() < 2
        || !matches!(parts.code[0].as_u64(), Some(8 | 9))
        || (parts.code.len() > 2 && parts.code[2].as_u64() != Some(0))
    {
        return Ok(None);
    }
    let index = byte(&parts.code[1], 1)?;
    let entry = table(parts.dynamics, index, 0)?;
    dynamic(entry)
}

pub fn valid_entity(id: u32) -> bool {
    id != 0 && (id & 0x1fff_ffff) != 0x1fff_ffff
}

fn first_scalar(mut value: &Value) -> Option<&Value> {
    while let Value::Array(items) = value {
        value = items.first()?;
    }
    Some(value)
}

pub fn truthy(value: &Value) -> bool {
    match first_scalar(value) {
        Some(Value::Bool(value)) => *value,
        Some(Value::Integer(value)) => *value != 0,
        Some(Value::Float(value)) => *value != 0.0 && !value.is_nan(),
        Some(Value::Asset(guid)) => *guid != 0,
        Some(Value::Entity(id)) => valid_entity(*id),
        Some(Value::Vector(parts)) => parts.iter().any(|&part| part != 0.0),
        Some(Value::Handle { instance, .. }) => *instance != 0,
        _ => false,
    }
}

pub fn to_float(value: &Value) -> f64 {
    match first_scalar(value) {
        Some(Value::Bool(value)) => f64::from(u8::from(*value)),
        Some(Value::Integer(value)) => rounded(*value as f64),
        Some(Value::Float(value)) => *value,
        _ => 0.0,
    }
}

pub fn to_int(value: &Value, default: i64) -> i64 {
    match first_scalar(value) {
        Some(Value::Null) => 0,
        Some(Value::Bool(value)) => i64::from(*value),
        Some(Value::Integer(value)) => *value,
        Some(Value::Float(value)) => i64::from(round_half_away(*value)),
        _ => default,
    }
}

fn rounded(value: f64) -> f64 {
    f64::from(value as f32)
}

pub fn round_half_away(value: f64) -> i32 {
    let number = rounded(if value >= 0.0 {
        value + 0.5
    } else {
        value - 0.5
    })
    .trunc();
    if number.is_finite() {
        number.rem_euclid(4_294_967_296.0) as u32 as i32
    } else {
        0
    }
}

fn close(a: f64, b: f64) -> bool {
    rounded(a - b).abs() <= EPSILON
}

pub fn equal(a: &Value, b: &Value) -> bool {
    let (a, b) = if matches!(b, Value::Array(_)) && !matches!(a, Value::Array(_)) {
        (b, a)
    } else {
        (a, b)
    };
    match (a, b) {
        (a, Value::Bool(b)) => truthy(a) == *b,
        (Value::Bool(a), b) => *a == truthy(b),
        (Value::Null, b) => !truthy(b),
        (Value::Integer(_) | Value::Float(_), Value::Null) => !truthy(a),
        (Value::Integer(a), Value::Float(b)) | (Value::Float(b), Value::Integer(a)) => {
            close(*a as f64, *b)
        }
        (Value::Float(a), Value::Float(b)) => close(*a, *b),
        (Value::Vector(a), Value::Vector(b)) => a.iter().zip(b).all(|(&a, &b)| close(a, b)),
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| equal(a, b))
        }
        (Value::Array(a), b) => matches!(a.as_slice(), [item] if equal(item, b)),
        _ => a == b,
    }
}

fn contains(container: &Value, value: &Value) -> bool {
    match container {
        Value::Null => false,
        Value::Array(items) => items.iter().any(|item| equal(item, value)),
        item => equal(item, value),
    }
}

fn index(object: Value, index: &Value) -> Value {
    if matches!(
        index,
        Value::Null | Value::Bool(_) | Value::Integer(_) | Value::Float(_)
    ) {
        let number = to_int(index, 0);
        return match object {
            Value::Array(items) => usize::try_from(number)
                .ok()
                .and_then(|number| items.into_iter().nth(number))
                .unwrap_or(Value::Null),
            object if number == 0 => object,
            _ => Value::Null,
        };
    }
    match object {
        Value::Array(items) => items
            .into_iter()
            .find(|item| equal(item, index))
            .unwrap_or(Value::Null),
        _ => Value::Null,
    }
}

fn class(config: &Json) -> Option<&str> {
    config.get("$")?.as_str()
}

fn is_expression(class: Option<&str>) -> bool {
    matches!(class, Some("STU_919FD47C" | "STUConfigVarExpression"))
}

fn child<'a>(config: &'a Json, field: &str) -> &'a Json {
    config.get(field).unwrap_or(&Json::Null)
}

fn json_truthy(value: &Json) -> bool {
    match value {
        Json::Null => false,
        Json::Bool(value) => *value,
        Json::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Json::String(value) => !value.is_empty(),
        Json::Array(value) => !value.is_empty(),
        Json::Object(value) => !value.is_empty(),
    }
}

fn number(value: &Json) -> Result<f64, EvalError> {
    match value {
        Json::Null => Some(0.0),
        Json::Bool(value) => Some(f64::from(u8::from(*value))),
        Json::Number(value) => value.as_f64(),
        Json::String(value) => value.trim().parse().ok(),
        _ => None,
    }
    .ok_or(EvalError::MalformedConfig("expected a number"))
}

fn integer(value: &Json) -> Result<i64, EvalError> {
    match value {
        Json::Null => Some(0),
        Json::Bool(value) => Some(i64::from(*value)),
        Json::Number(value) => value
            .as_i64()
            .or_else(|| value.as_u64().map(|value| value as i64))
            .or_else(|| value.as_f64().map(|value| value as i64)),
        Json::String(value) => value.trim().parse().ok(),
        _ => None,
    }
    .ok_or(EvalError::MalformedConfig("expected an integer"))
}

fn identifier(config: &Json) -> Result<u32, EvalError> {
    let value = child(config, "m_identifier");
    if !json_truthy(value) {
        return Ok(0);
    }
    u32::try_from(integer(value)?)
        .map_err(|_| EvalError::MalformedConfig("variable identifier out of range"))
}

fn resource(value: &Json) -> Option<u64> {
    asset_guid(value).or_else(|| {
        let text = value.as_str()?;
        text.starts_with("0x").then(|| parse_guid(text)).flatten()
    })
}

fn resource_field(class: &str) -> Option<&'static str> {
    match class {
        "STUConfigVarIdentifier" => Some("m_identifier"),
        "STUConfigVarGameMessage" => Some("m_gameMessage"),
        "STUConfigVarEffect" => Some("m_effect"),
        "STUConfigVarLoadout" => Some("m_loadout"),
        "STUConfigVarHardPoint" => Some("m_hardPoint"),
        "STUConfigVarGameMode" => Some("m_gamemode"),
        "STU_BCD1C634" => Some("m_id"),
        "STU_8A491388" => Some("m_priority"),
        _ => None,
    }
}

struct Evaluator {
    limit: usize,
    steps: usize,
}

impl Evaluator {
    fn config(
        &mut self,
        config: &Json,
        context: &mut impl Context,
        depth: usize,
    ) -> Result<Value, EvalError> {
        if depth >= MAX_CONFIG_DEPTH {
            return Err(EvalError::NestingExceeded);
        }
        let fields = match config {
            Json::Null => return Ok(Value::Null),
            Json::Bool(value) => return Ok(Value::Bool(*value)),
            Json::Number(value) if value.is_i64() || value.is_u64() => {
                return Ok(Value::Integer(integer(config)?));
            }
            Json::Number(_) => return Ok(Value::Float(number(config)?)),
            Json::String(value) => return Ok(Value::String(value.clone())),
            Json::Array(_) => return Err(EvalError::UnsupportedConfig("untyped array".into())),
            Json::Object(fields) => fields,
        };
        let class = class(config).unwrap_or("?");
        let value = child(config, "m_value");
        let nested = depth + 1;
        let result = match class {
            "STUConfigVarDynamic" => context.variable(dynamic(config)?.expect("dynamic class")),
            "STUConfigVarBool" => Value::Bool(json_truthy(value)),
            "STUConfigVarInt" => Value::Integer(i64::from(integer(value)? as i32)),
            "STUConfigVarFloat" => Value::Float(rounded(number(value)?)),
            "STUConfigVarLogicalButton" => {
                Value::Integer(integer(child(config, "m_logicalButton"))?)
            }
            // The body bindings and expression constants use this explicit null operand.
            "STU_92CA44CC"
                if fields.get("m_value") == Some(&Json::Null)
                    && !json_truthy(child(config, "m_EE729DCB")) =>
            {
                Value::Null
            }
            "STU_58AA0A3B" => {
                // The authored weapon-manager defaults take the false branch on an unset value.
                // Populated input still lacks a verified predicate.
                if self.config(value, context, nested)? != Value::Null {
                    return Err(EvalError::UnsupportedConfig(class.into()));
                }
                Value::Bool(false)
            }
            "STUConfigVarAnimCategory"
                if fields.get("m_category") == Some(&Json::Null)
                    && child(config, "m_resourceKey").is_null() =>
            {
                Value::Null
            }
            "STUConfigVarVec3" => {
                let mut parts = [0.0; 3];
                for (part, (upper, lower)) in
                    parts.iter_mut().zip([("X", "x"), ("Y", "y"), ("Z", "z")])
                {
                    let source = value
                        .get(upper)
                        .or_else(|| value.get(lower))
                        .unwrap_or(&Json::Null);
                    *part = rounded(number(source)?);
                }
                Value::Vector(parts)
            }
            "STU_919FD47C" | "STUConfigVarExpression" => {
                return self.expression(config, context, nested);
            }
            "STU_B5A0CAF0" => context.owner(),
            "STU_FDBB0B67" => {
                let target = child(config, "m_entity");
                if !matches!(
                    self::class(target),
                    Some("STU_B5A0CAF0" | "STUConfigVarDynamic")
                ) {
                    return Err(EvalError::UnsupportedConfig(class.into()));
                }
                let Value::Entity(entity) = self.config(target, context, nested)? else {
                    return Err(EvalError::UnsupportedConfig(class.into()));
                };
                if entity == 0 || entity & 0x1fff_ffff == 0x1fff_ffff {
                    return Err(EvalError::UnsupportedConfig(class.into()));
                }
                let scale = context
                    .body_scale(entity)
                    .ok_or_else(|| EvalError::UnsupportedConfig(class.into()))?;
                Value::Float(scale)
            }
            "STU_3832D36C" => {
                let key = match dynamic(value)? {
                    Some(variable) => Some(variable.id),
                    None => u32::try_from(to_int(&self.config(value, context, nested)?, 0)).ok(),
                };
                key.map_or(Value::Null, |key| context.ruleset(key))
            }
            "STU_91EFD5B1" => {
                let value = self.config(value, context, nested)?;
                if value == Value::Null {
                    self.config(child(config, "m_default"), context, nested)?
                } else {
                    value
                }
            }
            "STU_3ACE35FB" => {
                let Some(variable) = dynamic(child(config, "m_E50CF556"))? else {
                    return Ok(Value::Bool(false));
                };
                let priority = stack_priority(child(config, "m_012B7AF7"))?;
                let top = context
                    .would_stack_to_top(variable, priority)
                    .ok_or_else(|| EvalError::UnsupportedConfig(class.into()))?;
                Value::Bool(top)
            }
            "STU_45EDF12D" if child(config, "m_key").is_null() => {
                let container = self.config(child(config, "m_814F8EC3"), context, nested)?;
                let value = self.config(value, context, nested)?;
                Value::Bool(contains(&container, &value))
            }
            "STU_7197C080" => {
                let base = to_float(&self.config(child(config, "m_01AFADB6"), context, nested)?);
                let least = to_float(&self.config(child(config, "m_EA70ACC2"), context, nested)?);
                let scalar = child(config, "m_69A20070");
                let scalar = if scalar.is_null() {
                    1.0
                } else {
                    to_float(&self.config(scalar, context, nested)?)
                };
                let product = rounded(base * scalar);
                Value::Float(if product > least { product } else { least })
            }
            _ => {
                if let Some(field) = resource_field(class) {
                    return Ok(Value::Asset(resource(child(config, field)).unwrap_or(0)));
                }
                if fields.contains_key("m_resourceKey") {
                    if let Some(guid) = fields
                        .iter()
                        .filter(|(key, _)| key.as_str() != "m_resourceKey")
                        .filter_map(|(_, value)| resource(value))
                        .find(|&guid| guid != 0)
                    {
                        return Ok(Value::Asset(guid));
                    }
                }
                if let Some(value) = context.native(config) {
                    return Ok(value);
                }
                match class {
                    "STU_CC623180" => Value::Bool(true),  // IsHUDEnabled
                    "STU_98E9109B" => Value::Bool(false), // CustomGameLogicEnabled
                    "STU_5E75FF05" => Value::Integer(0),  // ReticleType
                    "STU_C76C63AF" => Value::Float(0.0),  // ReticleNumericSetting
                    _ => return Err(EvalError::UnsupportedConfig(class.into())),
                }
            }
        };
        Ok(result)
    }

    fn expression(
        &mut self,
        config: &Json,
        context: &mut impl Context,
        depth: usize,
    ) -> Result<Value, EvalError> {
        let Expression {
            code,
            dynamics,
            floats,
            configs,
        } = Expression::read(config)?;
        let mut pc = 0;
        let mut stack = Vec::new();
        while pc < code.len() {
            if self.steps >= self.limit {
                return Err(EvalError::BudgetExceeded {
                    offset: pc,
                    limit: self.limit,
                });
            }
            self.steps += 1;
            let opcode = byte(&code[pc], pc)?;
            let width = operand_count(opcode);
            let next = pc + 1 + width;
            let operands = code
                .get(pc + 1..next)
                .ok_or_else(|| malformed(pc, "truncated opcode"))?;
            let mut args = [0; 2];
            for (index, (arg, value)) in args.iter_mut().zip(operands).enumerate() {
                *arg = byte(value, pc + 1 + index)?;
            }
            let arg = args[0];
            let value = match opcode {
                0 => break,
                1 => {
                    pc = jump(pc, arg, code.len())?;
                    continue;
                }
                2 | 3 => {
                    let top = stack
                        .last()
                        .ok_or_else(|| malformed(pc, "expression stack underflow"))?;
                    if truthy(top) != (opcode == 2) {
                        pc = jump(pc, arg, code.len())?;
                    } else {
                        stack.pop();
                        pc = next;
                    }
                    continue;
                }
                4 | 5 => {
                    let flag = truthy(&pop(&mut stack, pc)?);
                    pc = if flag == (opcode == 5) {
                        jump(pc, arg, code.len())?
                    } else {
                        next
                    };
                    continue;
                }
                6 => {
                    let value = table(floats, arg, pc)?;
                    if value.is_null() {
                        return Err(malformed(pc, "expected a float constant"));
                    }
                    Value::Float(rounded(number(value)?))
                }
                7 => Value::Integer(i64::from(identifier(table(dynamics, arg, pc)?)?)),
                8 | 9 => context.variable(stack_var(dynamics, arg, pc)?),
                10 => context.parent_variable(arg, stack_var(dynamics, args[1], pc)?),
                11 | 12 => {
                    let scope = if opcode == 11 {
                        Scope::Instance
                    } else {
                        Scope::Entity
                    };
                    lookup(&pop(&mut stack, pc)?, scope).map_or(Value::Null, |variable| {
                        context.parent_variable(arg, variable)
                    })
                }
                13 => context.parameter(identifier(table(dynamics, arg, pc)?)?),
                14 => {
                    let value = configs.get(usize::from(arg)).unwrap_or(&Json::Null);
                    self.config(value, context, depth)?
                }
                15 | 16 => {
                    let (object, key) = pair(&mut stack, pc)?;
                    entity_member(&object, &key, context)
                }
                17 => {
                    let (object, key) = pair(&mut stack, pc)?;
                    index(object, &key)
                }
                18 => {
                    let length = match pop(&mut stack, pc)? {
                        Value::Array(items) => items.len() as i64,
                        _ => 0,
                    };
                    Value::Integer(length)
                }
                20 => {
                    let start = stack
                        .len()
                        .checked_sub(usize::from(arg))
                        .ok_or_else(|| malformed(pc, "expression stack underflow"))?;
                    Value::Array(stack.split_off(start))
                }
                21 => context.parent_entity(arg),
                22 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    let mut items = into_array(a);
                    items.extend(into_array(b));
                    Value::Array(items)
                }
                23..=28 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    arithmetic(opcode, &a, &b, context)
                }
                29 => match pop(&mut stack, pc)? {
                    Value::Vector(parts) => Value::Vector(parts.map(|part| -part)),
                    value => Value::Float(rounded(-to_float(&value))),
                },
                30 => Value::Bool(!truthy(&pop(&mut stack, pc)?)),
                31 | 32 => {
                    let scope = if opcode == 31 {
                        Scope::Instance
                    } else {
                        Scope::Entity
                    };
                    lookup(&pop(&mut stack, pc)?, scope)
                        .map_or(Value::Null, |variable| context.variable(variable))
                }
                33..=36 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    let (a, b) = (to_float(&a), to_float(&b));
                    Value::Bool(match opcode {
                        33 => a - EPSILON <= b,
                        34 => a < b - EPSILON,
                        35 => a + EPSILON >= b,
                        36 => a > b + EPSILON,
                        _ => unreachable!(),
                    })
                }
                37 | 38 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    Value::Bool(equal(&a, &b) == (opcode == 37))
                }
                39 => {
                    let value = to_float(&pop(&mut stack, pc)?);
                    Value::Float(if value > 0.0 {
                        rounded(value.sqrt())
                    } else {
                        0.0
                    })
                }
                50 | 51 => {
                    let value = pop(&mut stack, pc)?;
                    Value::Float(
                        vector_position(&value, context)
                            .map_or_else(|| rounded(to_float(&value).abs()), magnitude),
                    )
                }
                52 => {
                    let [x, _, z] = vector(&pop(&mut stack, pc)?);
                    Value::Float(rounded(x.hypot(z)))
                }
                53 | 54 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    let a = vector_position(&a, context).unwrap_or([0.0; 3]);
                    let b = vector_position(&b, context).unwrap_or([0.0; 3]);
                    let delta = vector_op(a, b, |a, b| a - b);
                    Value::Float(if opcode == 53 {
                        magnitude(delta)
                    } else {
                        rounded(delta[0].hypot(delta[2]))
                    })
                }
                55 | 56 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    let [ax, ay, az] = vector(&a);
                    let [bx, by, bz] = vector(&b);
                    if opcode == 55 {
                        Value::Float(rounded(ax * bx + ay * by + az * bz))
                    } else {
                        Value::Vector([
                            rounded(ay * bz - az * by),
                            rounded(az * bx - ax * bz),
                            rounded(ax * by - ay * bx),
                        ])
                    }
                }
                57 => {
                    let parts = vector(&pop(&mut stack, pc)?);
                    let length = magnitude(parts);
                    Value::Vector(if length == 0.0 {
                        [0.0; 3]
                    } else {
                        parts.map(|part| rounded(part / length))
                    })
                }
                58 => {
                    let z = to_float(&pop(&mut stack, pc)?);
                    let (x, y) = pair(&mut stack, pc)?;
                    Value::Vector([to_float(&x), to_float(&y), z])
                }
                60..=62 => Value::Float(vector(&pop(&mut stack, pc)?)[usize::from(opcode - 60)]),
                63 | 64 => {
                    let (a, b) = pair(&mut stack, pc)?;
                    let (a, b) = (to_float(&a), to_float(&b));
                    Value::Float(if opcode == 63 {
                        minimum(a, b)
                    } else {
                        maximum(a, b)
                    })
                }
                65 => {
                    let hi = to_float(&pop(&mut stack, pc)?);
                    let (value, lo) = pair(&mut stack, pc)?;
                    let (value, lo) = (to_float(&value), to_float(&lo));
                    Value::Float(maximum(minimum(value, maximum(lo, hi)), minimum(lo, hi)))
                }
                66 => Value::Float(rounded(to_float(&pop(&mut stack, pc)?).floor())),
                67 => Value::Float(rounded(to_float(&pop(&mut stack, pc)?).ceil())),
                68 => Value::Integer(i64::from(round_half_away(to_float(&pop(&mut stack, pc)?)))),
                71 => {
                    let t = to_float(&pop(&mut stack, pc)?);
                    let (a, b) = pair(&mut stack, pc)?;
                    let (a, b) = (to_float(&a), to_float(&b));
                    Value::Float(rounded(a + (b - a) * t))
                }
                _ => return Err(EvalError::UnsupportedOpcode { opcode, offset: pc }),
            };
            stack.push(value);
            pc = next;
        }
        Ok(stack.pop().unwrap_or(Value::Null))
    }
}

struct Expression<'a> {
    code: &'a [Json],
    dynamics: &'a [Json],
    floats: &'a [Json],
    configs: &'a [Json],
}

impl<'a> Expression<'a> {
    fn read(config: &'a Json) -> Result<Self, EvalError> {
        let data = child(config, "m_expression");
        Ok(Self {
            code: array_field(data, "m_opcodes")?,
            dynamics: array_field(data, "m_dynamicVars")?,
            floats: array_field(data, "m_D99EF254")?,
            configs: array_field(config, "m_configVars")?,
        })
    }
}

fn array_field<'a>(config: &'a Json, key: &str) -> Result<&'a [Json], EvalError> {
    match child(config, key) {
        Json::Null => Ok(&[]),
        Json::Array(values) => Ok(values),
        _ => Err(EvalError::MalformedConfig("expected an expression table")),
    }
}

fn malformed(offset: usize, reason: &'static str) -> EvalError {
    EvalError::MalformedBytecode { offset, reason }
}

fn byte(value: &Json, offset: usize) -> Result<u8, EvalError> {
    value
        .as_u64()
        .and_then(|value| u8::try_from(value).ok())
        .ok_or_else(|| malformed(offset, "expected a byte"))
}

fn operand_count(opcode: u8) -> usize {
    match opcode {
        10 => 2,
        1..=9 | 11..=14 | 19..=21 => 1,
        _ => 0,
    }
}

fn table(values: &[Json], index: u8, offset: usize) -> Result<&Json, EvalError> {
    values
        .get(usize::from(index))
        .ok_or_else(|| malformed(offset, "expression table index out of range"))
}

fn stack_var(values: &[Json], index: u8, offset: usize) -> Result<Variable, EvalError> {
    dynamic(table(values, index, offset)?)?
        .ok_or_else(|| malformed(offset, "expected a dynamic variable"))
}

fn jump(pc: usize, offset: u8, size: usize) -> Result<usize, EvalError> {
    if offset == 0 {
        return Err(malformed(pc, "non-progressing jump"));
    }
    let target = pc + usize::from(offset);
    if target >= size {
        return Err(malformed(pc, "jump leaves the stream"));
    }
    Ok(target)
}

fn pop(stack: &mut Vec<Value>, pc: usize) -> Result<Value, EvalError> {
    stack
        .pop()
        .ok_or_else(|| malformed(pc, "expression stack underflow"))
}

fn pair(stack: &mut Vec<Value>, pc: usize) -> Result<(Value, Value), EvalError> {
    let b = pop(stack, pc)?;
    Ok((pop(stack, pc)?, b))
}

fn lookup(key: &Value, scope: Scope) -> Option<Variable> {
    Some(Variable {
        scope,
        id: u32::try_from(to_int(key, 0)).ok()?,
    })
}

fn entity_member(object: &Value, key: &Value, context: &mut impl Context) -> Value {
    let id = match object {
        Value::Entity(id) => *id,
        Value::Array(items) => match items.first() {
            Some(Value::Entity(id)) => *id,
            _ => return Value::Null,
        },
        _ => return Value::Null,
    };
    let key = match key {
        Value::Asset(guid) => Some((guid & 0xffff) as u32),
        key => u32::try_from(to_int(key, 0)).ok(),
    };
    if valid_entity(id) {
        key.map_or(Value::Null, |key| context.entity_member(id, key))
    } else {
        Value::Null
    }
}

fn into_array(value: Value) -> Vec<Value> {
    match value {
        Value::Null => Vec::new(),
        Value::Array(items) => items,
        value => vec![value],
    }
}

fn vector(value: &Value) -> [f64; 3] {
    match value {
        Value::Vector(parts) => *parts,
        _ => [0.0; 3],
    }
}

fn vector_position(value: &Value, context: &mut impl Context) -> Option<[f64; 3]> {
    match value {
        Value::Vector(parts) => Some(*parts),
        Value::Entity(id) => Some(context.position(*id)),
        _ => None,
    }
}

fn vector_op(a: [f64; 3], b: [f64; 3], op: impl Fn(f64, f64) -> f64) -> [f64; 3] {
    std::array::from_fn(|index| rounded(op(a[index], b[index])))
}

fn magnitude([x, y, z]: [f64; 3]) -> f64 {
    rounded((x * x + y * y + z * z).sqrt())
}

fn minimum(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

fn maximum(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

fn arithmetic(opcode: u8, a: &Value, b: &Value, context: &mut impl Context) -> Value {
    if matches!(opcode, 23 | 24)
        && matches!(a, Value::Vector(_) | Value::Entity(_))
        && matches!(b, Value::Vector(_) | Value::Entity(_))
    {
        let a = vector_position(a, context).expect("vector or entity");
        let b = vector_position(b, context).expect("vector or entity");
        return Value::Vector(vector_op(
            a,
            b,
            |a, b| if opcode == 23 { a + b } else { a - b },
        ));
    }
    if opcode == 25 {
        match (a, b) {
            (Value::Vector(a), Value::Vector(b)) => {
                return Value::Vector(vector_op(*a, *b, |a, b| a * b));
            }
            (Value::Vector(parts), scalar) | (scalar, Value::Vector(parts)) => {
                let scalar = to_float(scalar);
                return Value::Vector(parts.map(|part| rounded(part * scalar)));
            }
            _ => (),
        }
    }
    if let (26, Value::Vector(parts)) = (opcode, a) {
        if let Value::Vector(divisor) = b {
            return Value::Vector(vector_op(*parts, *divisor, |a, b| {
                if b == 0.0 { 0.0 } else { a / b }
            }));
        }
        let scalar = to_float(b);
        if scalar.abs() < 1.18e-36 {
            return Value::Vector([0.0; 3]);
        }
        let inverse = rounded(1.0 / scalar);
        return Value::Vector(parts.map(|part| rounded(part * inverse)));
    }
    let (x, y) = (to_float(a), to_float(b));
    let value = match opcode {
        23 => x + y,
        24 => x - y,
        25 => x * y,
        26 if y != 0.0 && !y.is_nan() => x / y,
        27 if y != 0.0 => x % y,
        28 if x > 0.0 => x.powf(y),
        _ => 0.0,
    };
    Value::Float(rounded(value))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::BTreeMap, path::PathBuf};

    use serde_json::json;

    use crate::script_graph::GraphStore;

    #[derive(Default)]
    struct Vars {
        instance: BTreeMap<u32, Value>,
        entity: BTreeMap<u32, Value>,
        reads: Vec<Variable>,
    }

    impl Context for Vars {
        fn variable(&mut self, variable: Variable) -> Value {
            self.reads.push(variable);
            let values = match variable.scope {
                Scope::Instance => &self.instance,
                Scope::Entity => &self.entity,
            };
            values.get(&variable.id).cloned().unwrap_or(Value::Null)
        }

        fn parent_variable(&mut self, levels: u8, variable: Variable) -> Value {
            match (levels, variable.scope, variable.id) {
                (1, Scope::Instance, 14152) => Value::Asset(0x02e0_0000_0000_0002),
                _ => Value::Null,
            }
        }

        fn entity_member(&mut self, entity: u32, id: u32) -> Value {
            match (entity, id) {
                (0xa000_0100, 7423) => Value::Integer(23),
                _ => Value::Null,
            }
        }

        fn ruleset(&mut self, id: u32) -> Value {
            match id {
                10265 => Value::Float(1.0),
                _ => Value::Null,
            }
        }

        fn native(&mut self, config: &Json) -> Option<Value> {
            match class(config)? {
                "STU_37F20D24" => Some(Value::Asset(0x0230_0000_0000_0018)),
                _ => None,
            }
        }
    }

    fn variable(id: u32, scope: Scope) -> Json {
        json!({
            "$": "STUConfigVarDynamic",
            "m_identifier": id,
            "m_60DB8F99": scope == Scope::Entity,
        })
    }

    fn expression(opcodes: Json, dynamics: &[Json], floats: &[f64], configs: &[Json]) -> Json {
        json!({
            "$": "STU_919FD47C",
            "m_configVars": configs,
            "m_expression": {
                "m_opcodes": opcodes,
                "m_dynamicVars": dynamics,
                "m_D99EF254": floats,
            },
        })
    }

    #[test]
    fn distinguishes_identifiers_arrays_entities_and_float_comparisons() {
        let id = Value::Asset(5);
        assert!(!equal(&id, &Value::Integer(5)));
        assert!(equal(&id, &Value::Bool(true)));
        assert!(equal(&Value::Integer(1), &Value::Float(1.000001)));
        assert!(!equal(&Value::Integer(1), &Value::Float(1.001)));
        assert!(equal(&Value::Float(0.0), &Value::Null));
        assert!(equal(&Value::Array(vec![id.clone()]), &id));
        assert!(!truthy(&Value::Array(vec![
            Value::Bool(false),
            Value::Bool(true)
        ])));
        assert!(!truthy(&Value::Entity(0xbfff_ffff)));
        assert!(truthy(&Value::Entity(0xa000_0101)));
        assert_eq!(to_int(&Value::Array(vec![Value::Float(-2.5)]), 0), -3);
        assert_eq!(to_int(&id, 9), 9);
        assert_eq!(round_half_away(4_294_967_296.0), 0);
    }

    #[test]
    fn short_circuit_preserves_the_deciding_operand_and_scope_reads() {
        let variables = [variable(1, Scope::Instance), variable(2, Scope::Entity)];
        let mut context = Vars {
            instance: BTreeMap::from([(1, Value::Float(0.0))]),
            entity: BTreeMap::from([(2, Value::Integer(7))]),
            ..Vars::default()
        };
        let and = expression(json!([8, 0, 2, 4, 8, 1, 0]), &variables, &[], &[]);
        assert_eq!(evaluate(&and, &mut context).unwrap(), Value::Float(0.0));
        assert_eq!(
            context.reads,
            [Variable {
                scope: Scope::Instance,
                id: 1
            }]
        );
        context.reads.clear();
        let or = expression(json!([8, 0, 3, 4, 8, 1, 0]), &variables, &[], &[]);
        assert_eq!(evaluate(&or, &mut context).unwrap(), Value::Integer(7));
        assert_eq!(
            context.reads,
            [
                Variable {
                    scope: Scope::Instance,
                    id: 1
                },
                Variable {
                    scope: Scope::Entity,
                    id: 2
                }
            ],
        );
        let lvalue_expression = expression(json!([9, 1, 0]), &variables, &[], &[]);
        assert_eq!(
            lvalue(&lvalue_expression).unwrap(),
            Some(Variable {
                scope: Scope::Entity,
                id: 2
            })
        );
    }

    #[test]
    fn executes_only_the_selected_path_and_bounds_nested_expressions() {
        let untaken = expression(json!([6, 0, 2, 255, 6, 1, 0]), &[], &[1.0, 7.0], &[]);
        assert_eq!(
            evaluate(&untaken, &mut EmptyContext).unwrap(),
            Value::Float(7.0)
        );
        for (opcodes, offset, reason) in [
            (json!([1, 0]), 0, "non-progressing jump"),
            (json!([1, 2]), 0, "jump leaves the stream"),
            (json!([10, 1]), 0, "truncated opcode"),
            (json!([23]), 0, "expression stack underflow"),
            (json!([1, -1]), 1, "expected a byte"),
        ] {
            let config = expression(opcodes, &[], &[], &[]);
            assert_eq!(
                evaluate(&config, &mut EmptyContext),
                Err(EvalError::MalformedBytecode { offset, reason }),
            );
        }
        let inner = expression(json!([6, 0, 0]), &[], &[3.0], &[]);
        let nested = expression(json!([14, 0, 0]), &[], &[], &[inner]);
        assert_eq!(
            evaluate_with_budget(&nested, &mut EmptyContext, 3),
            Err(EvalError::BudgetExceeded {
                offset: 2,
                limit: 3
            }),
        );
        assert_eq!(
            evaluate_with_budget(&nested, &mut EmptyContext, 4).unwrap(),
            Value::Float(3.0)
        );
    }

    #[test]
    fn reports_unsupported_operations_instead_of_returning_a_guessed_null() {
        let map = expression(json!([19, 0, 0]), &[], &[], &[]);
        assert_eq!(
            evaluate(&map, &mut EmptyContext),
            Err(EvalError::UnsupportedOpcode {
                opcode: 19,
                offset: 0
            }),
        );
        assert_eq!(
            evaluate(&json!({"$": "STU_BE0BD267"}), &mut EmptyContext),
            Err(EvalError::UnsupportedConfig("STU_BE0BD267".into())),
        );
    }

    #[test]
    #[ignore = "requires an authorized 104319 graph pack in ORPHEUS_RUNTIME_DIR"]
    fn authored_controller_hud_and_soldier_config_values_match_python() {
        let data = PathBuf::from(
            std::env::var_os("ORPHEUS_RUNTIME_DIR").expect("set ORPHEUS_RUNTIME_DIR"),
        )
        .join("data");
        let graphs = GraphStore::load(data.join("game_graphs_174.json.gz")).unwrap();
        let mut context = Vars {
            instance: BTreeMap::from([
                (28, Value::Integer(1)),
                (
                    375,
                    Value::Array(vec![Value::Bool(false), Value::Bool(true)]),
                ),
                (581, Value::Float(1.5)),
                (6884, Value::Float(rounded(0.511))),
                (6885, Value::Float(rounded(0.1))),
                (
                    8449,
                    Value::Array(vec![Value::Integer(1), Value::Integer(2)]),
                ),
                (11938, Value::Integer(7)),
                (17937, Value::Entity(0xa000_0100)),
                (25485, Value::Bool(true)),
            ]),
            entity: BTreeMap::from([
                (31, Value::Integer(1)),
                (9537, Value::Float(0.5)),
                (17153, Value::Bool(true)),
                (17154, Value::Asset(0x02e0_0000_0000_006e)),
                (27534, Value::Bool(true)),
            ]),
            ..Vars::default()
        };
        // Existing Python expr.evaluate against the same target graph fields
        // and scoped fixture variables. The timeout vector includes f32 bits.
        for (graph, rid, field, expected) in [
            (0x13c0, 6, "m_value", Value::Bool(false)),
            (0x13c0, 20, "m_condition", Value::Bool(true)),
            (0x13c1, 3, "m_condition", Value::Integer(7)),
            (0x0c90, 0, "m_16432044", Value::Asset(0x02e0_0000_0000_006e)),
            (0x288a, 10, "m_condition", Value::Bool(true)),
            (0x288b, 14, "m_hero", Value::Asset(0x02e0_0000_0000_0002)),
            (0x20e4, 3, "m_87276FE6", Value::Integer(23)),
            (0x0254, 8, "m_value", Value::Integer(30)),
        ] {
            let node = graphs.graph(graph).unwrap().by_rid(rid).unwrap();
            assert_eq!(
                evaluate(node.field(field).unwrap(), &mut context).unwrap(),
                expected,
                "{graph:04x} rid {rid} {field}"
            );
        }
        for (graph, state, field, expected) in [
            (
                0x0254,
                51,
                "m_timeout",
                Value::Float(f64::from(f32::from_bits(0x3f44_3958))),
            ),
            (0x0257, 12, "m_value", Value::Float(7.5)),
            (0x0258, 0, "m_05BE62DD", Value::Vector([0.0, 1.0, 0.0])),
        ] {
            let node = graphs.graph(graph).unwrap().state(state).unwrap();
            assert_eq!(
                evaluate(node.field(field).unwrap(), &mut context).unwrap(),
                expected,
                "{graph:04x} st {state} {field}"
            );
        }
    }
}
