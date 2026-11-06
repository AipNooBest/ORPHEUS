//! Shared statescript value, full-frame and chunk writers for build 104319.

use std::{collections::BTreeMap, error::Error, fmt};

use indexmap::IndexMap;

use crate::{
    bits::{BitError, BitWriter},
    script_graph::{Graph, ModeValue},
};

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Integer(i64),
    Float(f64),
    Asset(u64),
    Entity(u32),
    Vector([f64; 3]),
    Handle {
        kind: u8,
        instance: u32,
        state: u16,
        extra: u16,
    },
    String(String),
    Array(Vec<Value>),
}

impl From<ModeValue> for Value {
    fn from(value: ModeValue) -> Self {
        match value {
            ModeValue::Bool(value) => Self::Bool(value),
            ModeValue::Integer(value) => Self::Integer(value),
            ModeValue::Asset(value) => Self::Asset(value),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WireError {
    Bit(BitError),
    ZeroCommandFrame,
    InstanceId,
    GraphIndex(u32),
    SlotOutOfRange,
    InvalidVariable,
    InvalidBinding,
    InvalidValue,
    ChunkRange,
}

impl fmt::Display for WireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Bit(error) => error.fmt(f),
            Self::ZeroCommandFrame => f.write_str("an owner frame needs a nonzero command frame"),
            Self::InstanceId => f.write_str("script instance IDs must be distinct and positive"),
            Self::GraphIndex(index) => write!(f, "script graph {index} exceeds the wire index"),
            Self::SlotOutOfRange => f.write_str("script slot is outside the graph layout"),
            Self::InvalidVariable => f.write_str("zero terminates the script variable list"),
            Self::InvalidBinding => {
                f.write_str("script binding is outside its wire representation")
            }
            Self::InvalidValue => f.write_str("script value is outside its wire representation"),
            Self::ChunkRange => f.write_str("script chunk range or length exceeds the wire width"),
        }
    }
}

impl Error for WireError {}

impl From<BitError> for WireError {
    fn from(value: BitError) -> Self {
        Self::Bit(value)
    }
}

#[derive(Clone, Copy)]
pub enum ValueEncoding {
    Explicit,
    Compact,
}

impl ValueEncoding {
    fn compact(self) -> bool {
        matches!(self, Self::Compact)
    }
}

pub fn write_value(
    out: &mut BitWriter,
    value: &Value,
    encoding: ValueEncoding,
) -> Result<(), WireError> {
    let compact = encoding.compact();
    match value {
        Value::Bool(value) => {
            out.bits(0, 4)?;
            out.bit(*value);
        }
        Value::Integer(value) if (i64::from(i32::MIN)..=i64::from(u32::MAX)).contains(value) => {
            let (tag, width) = if compact && (0..16).contains(value) {
                (1, 4)
            } else if compact && i8::try_from(*value).is_ok() {
                (2, 8)
            } else if compact && i16::try_from(*value).is_ok() {
                (3, 16)
            } else {
                (4, 32)
            };
            out.bits(tag, 4)?;
            out.signed(*value, width)?;
        }
        Value::Float(value) if (*value as f32).is_finite() => {
            if compact && value.fract() == 0.0 && (0.0..16.0).contains(value) {
                out.bits(5, 4)?;
                out.bits(*value as u64, 4)?;
            } else if compact && value.fract() == 0.0 && (-128.0..128.0).contains(value) {
                out.bits(6, 4)?;
                out.signed(*value as i64, 8)?;
            } else {
                out.bits(8, 4)?;
                out.f32(*value as f32);
            }
        }
        Value::Asset(guid) => {
            let kind = guid >> 48;
            out.bits(9, 4)?;
            out.bit(true);
            out.bits(kind, 16)?;
            if kind != 0 {
                out.w_var(*guid as u32);
            }
        }
        Value::Entity(entity) => {
            out.bits(13, 4)?;
            let present = !compact || *entity != 0;
            out.bit(present);
            if present {
                out.bits(u64::from(*entity), 32)?;
            }
        }
        Value::Vector(parts)
            if compact && parts.iter().all(|value| (*value as f32).is_finite()) =>
        {
            out.bits(12, 4)?;
            for &part in parts {
                out.bit(part != 0.0);
            }
            for &part in parts {
                if part != 0.0 {
                    out.f32(part as f32);
                }
            }
        }
        Value::Handle {
            kind,
            instance,
            state,
            extra,
        } if compact => {
            out.bits(14, 4)?;
            out.bits(u64::from(kind & 7), 3)?;
            out.w_var(*instance);
            out.w_u16(*state);
            out.w_u16(*extra);
        }
        Value::Null if compact => {
            out.bits(15, 4)?;
            out.bits(3, 3)?;
        }
        Value::String(text) if compact && !text.contains('\0') => {
            out.bits(15, 4)?;
            out.bits(2, 3)?;
            out.raw(text.as_bytes());
            out.bits(0, 8)?;
        }
        Value::Array(items) if compact => {
            let length = u16::try_from(items.len()).map_err(|_| WireError::InvalidValue)?;
            out.bits(15, 4)?;
            out.bits(6, 3)?;
            out.w_u16(length);
            out.bit(false);
            if !items.is_empty() {
                out.bit(false);
                for item in items {
                    write_value(out, item, encoding)?;
                }
            }
        }
        _ => return Err(WireError::InvalidValue),
    }
    Ok(())
}

pub type Variables = IndexMap<u32, Value>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Binding {
    pub instance: u16,
    pub state: u16,
    pub state_bits: u8,
    pub priority: i8,
    pub above: bool,
}

pub(crate) type Bindings = BTreeMap<u32, Vec<Binding>>;

pub(crate) fn variable(
    out: &mut BitWriter,
    value: &Value,
    encoding: ValueEncoding,
) -> Result<(), WireError> {
    bound_variable(out, value, encoding, 0, &[])
}

pub(crate) fn variable_list(
    out: &mut BitWriter,
    variables: &Variables,
    encoding: ValueEncoding,
) -> Result<(), WireError> {
    bound_variable_list(out, variables, encoding, 0, &Bindings::new())
}

pub(crate) fn bound_variable(
    out: &mut BitWriter,
    value: &Value,
    encoding: ValueEncoding,
    own: u16,
    bindings: &[Binding],
) -> Result<(), WireError> {
    write_value(out, value, encoding)?;
    match bindings.len() {
        0 => out.bit(false),
        1 => out.bits(0b01, 2)?,
        2 => out.bits(0b011, 3)?,
        count => {
            let count = u16::try_from(count).map_err(|_| WireError::InvalidBinding)?;
            out.bits(0b111, 3)?;
            out.w_u16(count);
        }
    }
    for binding in bindings {
        if binding.instance == 0
            || binding.state_bits > u16::BITS as u8
            || u32::from(binding.state) >= 1 << binding.state_bits
        {
            return Err(WireError::InvalidBinding);
        }
        out.bit(false); // no path
        out.bit(binding.instance != own);
        if binding.instance != own {
            out.bit(false); // positive instance ID
            out.w_u16(binding.instance);
        }
        out.bits(u64::from(binding.state), usize::from(binding.state_bits))?;
        out.bit(false); // stack output slot zero
        let explicit = binding.priority != 0 || !binding.above;
        out.bit(explicit);
        if explicit {
            out.bit(true); // integer priority
            out.bit(binding.priority < 0);
            out.w_var(u32::from(binding.priority.unsigned_abs()));
            out.bit(binding.above);
        }
        out.bit(false); // no per-frame re-resolution
    }
    Ok(())
}

pub(crate) fn bound_variable_list(
    out: &mut BitWriter,
    variables: &Variables,
    encoding: ValueEncoding,
    own: u16,
    bindings: &Bindings,
) -> Result<(), WireError> {
    out.bit(!variables.is_empty());
    if !variables.is_empty() {
        let mut emit = |id, value: &Value| {
            if id == 0 {
                return Err(WireError::InvalidVariable);
            }
            out.w_var(id);
            let links = bindings.get(&id).map_or(&[][..], Vec::as_slice);
            bound_variable(out, value, encoding, own, links)
        };
        if encoding.compact() {
            let mut ids: Vec<_> = variables.keys().copied().collect();
            ids.sort_unstable();
            for id in ids {
                emit(id, &variables[&id])?;
            }
        } else {
            for (&id, value) in variables {
                emit(id, value)?;
            }
        }
        out.w_var(0);
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Layout {
    pub graph: u16,
    pub owner_slots: usize,
    pub remote_slots: usize,
    pub presence_slots: usize,
}

impl TryFrom<&Graph> for Layout {
    type Error = WireError;

    fn try_from(graph: &Graph) -> Result<Self, Self::Error> {
        Ok(Self {
            graph: u16::try_from(graph.index).map_err(|_| WireError::GraphIndex(graph.index))?,
            owner_slots: graph.owner_positions().len(),
            remote_slots: graph.remote_positions().len(),
            presence_slots: graph.presence_vars().count(),
        })
    }
}

pub struct Instance {
    pub id: u16,
    pub layout: Layout,
    /// Parent instance and raw state index.
    pub parent: Option<(u16, u16)>,
    pub presence: BTreeMap<usize, Value>,
    pub variables: Variables,
    /// Owner entries contain the payload after the active bit. Remote entries
    /// contain the complete state record, including its presence and active bits.
    pub states: BTreeMap<usize, BitWriter>,
}

impl Instance {
    pub fn new(id: u16, layout: Layout, parent: Option<(u16, u16)>) -> Self {
        Self {
            id,
            layout,
            parent,
            presence: BTreeMap::new(),
            variables: Variables::new(),
            states: BTreeMap::new(),
        }
    }
}

pub(crate) fn write_descriptor(
    out: &mut BitWriter,
    graph: u16,
    parent: Option<(u16, u16)>,
) -> Result<(), WireError> {
    out.bits(u64::from(graph), 16)?;
    out.bits(0, 2)?; // this entity, flag clear
    out.bit(parent.is_some());
    if let Some((parent, state)) = parent {
        out.w_var(u32::from(parent));
        out.w_var(u32::from(state));
    }
    out.bit(false); // no references
    Ok(())
}

pub fn owner_full(
    command_frame: u32,
    instances: &[Instance],
    variables: &Variables,
) -> Result<BitWriter, WireError> {
    if command_frame == 0 {
        return Err(WireError::ZeroCommandFrame);
    }
    full_frame(
        Some(command_frame),
        instances,
        variables,
        ValueEncoding::Explicit,
    )
}

pub fn remote_full(instances: &[Instance], variables: &Variables) -> Result<BitWriter, WireError> {
    full_frame(None, instances, variables, ValueEncoding::Compact)
}

pub fn variables_frame(variables: &Variables) -> Result<BitWriter, WireError> {
    full_frame(None, &[], variables, ValueEncoding::Explicit)
}

fn full_frame(
    command_frame: Option<u32>,
    instances: &[Instance],
    variables: &Variables,
    encoding: ValueEncoding,
) -> Result<BitWriter, WireError> {
    let owner = command_frame.is_some();
    let mut ordered: Vec<_> = instances.iter().collect();
    ordered.sort_unstable_by_key(|instance| instance.id);
    let mut out = BitWriter::new();
    out.bit(false);
    out.bit(owner);
    if let Some(command_frame) = command_frame {
        out.bit(false);
        out.w_var(command_frame);
    }
    let mut last = 0;
    for instance in &ordered {
        if instance.id <= last {
            return Err(WireError::InstanceId);
        }
        out.w_u16(instance.id - last);
        last = instance.id;
        write_descriptor(&mut out, instance.layout.graph, instance.parent)?;
    }
    out.w_u16(0);
    variable_list(&mut out, variables, encoding)?;
    for instance in ordered {
        let states = if owner {
            instance.layout.owner_slots
        } else {
            instance.layout.remote_slots
        };
        if instance
            .presence
            .last_key_value()
            .is_some_and(|(&slot, _)| slot >= instance.layout.presence_slots)
            || instance
                .states
                .last_key_value()
                .is_some_and(|(&slot, _)| slot >= states)
        {
            return Err(WireError::SlotOutOfRange);
        }
        for slot in 0..instance.layout.presence_slots {
            let value = instance.presence.get(&slot);
            out.bit(value.is_some());
            if let Some(value) = value {
                variable(&mut out, value, encoding)?;
            }
        }
        variable_list(&mut out, &instance.variables, encoding)?;
        for slot in 0..states {
            match instance.states.get(&slot) {
                Some(payload) => {
                    if owner {
                        out.bit(true);
                    } else if payload.bit_len() == 0 {
                        return Err(WireError::InvalidValue);
                    }
                    out.append(payload);
                }
                None => out.bit(false),
            }
        }
        if owner {
            out.bits(3, 2)?; // end instance events
        }
    }
    out.bits(0, 2)?; // no frame events
    Ok(out)
}

pub fn subscript(child: u16) -> BitWriter {
    let mut out = BitWriter::new();
    out.w_u16(child);
    out.bit(false);
    out
}

pub fn owner_ack(command_frame: u32) -> Result<BitWriter, WireError> {
    if command_frame == 0 {
        return Err(WireError::ZeroCommandFrame);
    }
    let mut out = BitWriter::new();
    out.bits(2, 3)?;
    out.w_var(command_frame);
    out.w_var(7); // body length through the "more" bit
    out.w_u16(0); // no instances
    out.bits(0, 5)?; // variables, instance events, more, frame events
    Ok(out)
}

pub fn chunk(payload: &BitWriter, first: u32, span: u32) -> Result<BitWriter, WireError> {
    first.checked_add(span).ok_or(WireError::ChunkRange)?;
    let width = u32::try_from(payload.bit_len()).map_err(|_| WireError::ChunkRange)?;
    let mut out = BitWriter::new();
    out.w_u32(first);
    out.w_var(span);
    out.bit(false);
    out.w_var(width);
    out.append(payload);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn check(bits: &BitWriter, count: usize, expected: &str) {
        let actual: String = bits
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!(bits.bit_len(), count);
        assert_eq!(actual, expected);
    }

    #[test]
    fn tagged_values_match_the_python_writer() {
        let values = [
            Value::Bool(true),
            Value::Integer(-23),
            Value::Integer(3),
            Value::Integer(132),
            Value::Integer(65_540),
            Value::Float(4.0),
            Value::Float(-3.0),
            Value::Float(1.5),
            Value::Asset(0x0280_0000_0000_001c),
            Value::Entity(0),
            Value::Entity(0xa000_0001),
            Value::Array(vec![
                Value::Null,
                Value::String("Rust".into()),
                Value::Bool(false),
            ]),
            Value::Handle {
                kind: 3,
                instance: 20,
                state: 10,
                extra: 4,
            },
            Value::Vector([0.0, -1.0, 2.0]),
        ];
        let mut bits = BitWriter::new();
        for value in &values {
            write_value(&mut bits, value, ValueEncoding::Compact).unwrap();
        }
        check(
            &bits,
            379,
            "50d263861080080002008aac1f0100807f32a0008ed603000040df86df4baa6e8e0e00f85050c4060000fc0500000002",
        );
    }

    #[test]
    fn full_owner_and_ack_match_python_with_an_active_child() {
        let variables = Variables::from([
            (12, Value::Bool(true)),
            (37, Value::Integer(-23)),
            (42, Value::Float(1.5)),
            (77, Value::Asset(0x0280_0000_0000_001c)),
            (105, Value::Entity(0xa000_0101)),
        ]);
        check(
            &variables_frame(&variables).unwrap(),
            213,
            "8018202594feffff8f0a020000ffd09401057090d6030200400100",
        );
        let mut parent = Instance::new(
            1,
            Layout {
                graph: 0x13c1,
                owner_slots: 2,
                remote_slots: 0,
                presence_slots: 2,
            },
            None,
        );
        parent.presence.insert(0, Value::Bool(false));
        parent.variables.insert(3, Value::Integer(8));
        parent.states.insert(0, subscript(2));
        parent.states.insert(1, BitWriter::new());
        let child = Instance::new(
            2,
            Layout {
                graph: 0x288a,
                owner_slots: 1,
                remote_slots: 0,
                presence_slots: 0,
            },
            Some((1, 43)),
        );
        check(
            &owner_full(1001, &[child, parent], &variables).unwrap(),
            378,
            "2a7d40044f805044a1002b400c90124affffff47050100807f68ca80023848eb010100a000043440080000000024ce00",
        );
        let ack = owner_ack(1001).unwrap();
        check(&ack, 40, "2a7dc00100");
        check(&chunk(&ack, 2, 7).unwrap(), 76, "04001c80a2d2071c0000");
    }

    #[test]
    fn rejects_out_of_range_inputs_without_emitting_a_complete_frame() {
        let variables = Variables::from([(0, Value::Bool(true))]);
        assert_eq!(variables_frame(&variables), Err(WireError::InvalidVariable));
        assert_eq!(owner_ack(0), Err(WireError::ZeroCommandFrame));
        assert_eq!(
            chunk(&BitWriter::new(), u32::MAX, 1),
            Err(WireError::ChunkRange)
        );
        let layout = Layout {
            graph: 1,
            owner_slots: 1,
            remote_slots: 1,
            presence_slots: 1,
        };
        let mut instance = Instance::new(1, layout, None);
        instance.presence.insert(1, Value::Bool(false));
        assert_eq!(
            owner_full(1, &[instance], &Variables::new()),
            Err(WireError::SlotOutOfRange)
        );
        let mut bits = BitWriter::new();
        assert_eq!(
            write_value(&mut bits, &Value::Integer(i64::MAX), ValueEncoding::Compact),
            Err(WireError::InvalidValue)
        );
    }
}
