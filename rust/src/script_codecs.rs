//! Data-driven statescript payload layouts and verified runtime projections.
//!
//! Layouts and H0/H1 projections follow ow174/game/script/codecs.py and
//! ow174/game/statescript.py. The prepared runtime supplies the client table.

use std::{
    collections::{BTreeMap, HashMap},
    error::Error,
    fmt, fs, io,
    path::Path,
};

use indexmap::IndexMap;
use serde::Deserialize;

use crate::bits::{BitError, BitWriter};
use crate::script_wire::{Value, ValueEncoding, WireError, write_value};

pub type StatePayload = BTreeMap<String, Value>;
pub type LayoutFields<'a> = BTreeMap<&'a str, FieldValue<'a>>;

#[derive(Clone, Debug, PartialEq)]
pub enum FieldValue<'a> {
    Scalar(Value),
    Unsigned(u64),
    Rows(Vec<LayoutFields<'a>>),
}

impl From<Value> for FieldValue<'_> {
    fn from(value: Value) -> Self {
        Self::Scalar(value)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LayoutMode {
    pub owner: bool,
    pub full: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PayloadMode {
    Remote {
        full: bool,
    },
    Owner {
        full: bool,
        frame_time: i64,
        frame_ms: u32,
    },
}

impl PayloadMode {
    fn layout(self) -> LayoutMode {
        match self {
            Self::Remote { full } => LayoutMode { owner: false, full },
            Self::Owner { full, .. } => LayoutMode { owner: true, full },
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum PayloadError {
    Bit(BitError),
    Wire(WireError),
    Unverified(String),
    Unsupported(String),
    Field(String),
    Rows {
        field: String,
        expected: usize,
        actual: usize,
    },
    TimeRange,
}

impl fmt::Display for PayloadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Bit(error) => error.fmt(f),
            Self::Wire(error) => error.fmt(f),
            Self::Unverified(class) => write!(f, "no verified payload layout for {class}"),
            Self::Unsupported(class) => write!(f, "no runtime payload projection for {class}"),
            Self::Field(name) => write!(f, "invalid state payload field {name:?}"),
            Self::Rows {
                field,
                expected,
                actual,
            } => {
                write!(f, "{field:?} needs {expected} records, received {actual}")
            }
            Self::TimeRange => f.write_str("state payload time is outside the supported range"),
        }
    }
}

impl Error for PayloadError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Bit(error) => Some(error),
            Self::Wire(error) => Some(error),
            _ => None,
        }
    }
}

impl From<BitError> for PayloadError {
    fn from(error: BitError) -> Self {
        Self::Bit(error)
    }
}

impl From<WireError> for PayloadError {
    fn from(error: WireError) -> Self {
        Self::Wire(error)
    }
}

#[derive(Debug)]
pub struct CodecTable {
    entries: Vec<Codec>,
    index: HashMap<String, usize>,
}

impl CodecTable {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        Self::from_json(&fs::read_to_string(path)?)
    }

    pub fn from_json(text: &str) -> io::Result<Self> {
        let raw: RawTable = serde_json::from_str(text)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let mut entries = Vec::with_capacity(raw.classes.len());
        let mut index = HashMap::with_capacity(raw.classes.len() * 3);
        for (hash, row) in raw.classes {
            let ordinal = entries.len();
            for alias in [hash.clone(), row.name.clone(), format!("STU_{hash}")] {
                index.insert(alias, ordinal);
            }
            let family = row.codec.as_deref().and_then(Family::parse);
            entries.push(Codec {
                hash,
                name: row.name,
                codec: row.codec,
                layout: row.layout,
                stec: row.stec,
                status: row.status,
                family,
            });
        }
        Ok(Self { entries, index })
    }

    pub fn codec(&self, class: &str) -> Option<&Codec> {
        self.entries.get(*self.index.get(class)?)
    }

    pub fn classes(&self) -> impl Iterator<Item = &Codec> {
        self.entries.iter()
    }

    /// An unprojected state can only be sent off if its class has no StEC
    /// payload on the off path.
    pub fn sendable(&self, class: &str, active: bool) -> bool {
        self.codec(class).map_or(!active, |codec| {
            codec.projected() || (!active && !codec.stec())
        })
    }
}

#[derive(Debug)]
pub struct Codec {
    pub hash: String,
    pub name: String,
    pub codec: Option<String>,
    layout: Option<Vec<Op>>,
    stec: bool,
    status: String,
    family: Option<Family>,
}

impl Codec {
    pub fn verified(&self) -> bool {
        self.status == "VERIFIED" && self.layout.is_some()
    }

    pub fn empty(&self) -> bool {
        self.layout.as_ref().is_some_and(Vec::is_empty)
    }

    pub fn stec(&self) -> bool {
        self.stec
    }

    pub fn projected(&self) -> bool {
        self.verified() && self.family.is_some()
    }

    pub fn payload(
        &self,
        payload: &StatePayload,
        mode: PayloadMode,
    ) -> Result<BitWriter, PayloadError> {
        self.verified_layout()?;
        let family = self
            .family
            .ok_or_else(|| PayloadError::Unsupported(self.name.clone()))?;
        self.encode_layout(&project(family, payload, mode)?, mode.layout())
    }

    /// Write fields already expressed in the verified layout's names. Missing
    /// fields use the reference writer's zero, null or absent-link defaults.
    pub fn encode_layout(
        &self,
        fields: &LayoutFields<'_>,
        mode: LayoutMode,
    ) -> Result<BitWriter, PayloadError> {
        self.encode_layout_with_link(fields, mode, |out, _| {
            out.bit(false);
            Ok(())
        })
    }

    /// Linked-state references depend on the frame's instance context.
    pub fn encode_layout_with_link<'a>(
        &self,
        fields: &LayoutFields<'a>,
        mode: LayoutMode,
        mut link: impl FnMut(&mut BitWriter, Option<&FieldValue<'a>>) -> Result<(), PayloadError>,
    ) -> Result<BitWriter, PayloadError> {
        let mut out = BitWriter::new();
        write_ops(&mut out, self.verified_layout()?, fields, mode, &mut link)?;
        Ok(out)
    }

    fn verified_layout(&self) -> Result<&[Op], PayloadError> {
        self.layout
            .as_deref()
            .filter(|_| self.status == "VERIFIED")
            .ok_or_else(|| PayloadError::Unverified(self.name.clone()))
    }
}

#[derive(Deserialize)]
struct RawTable {
    classes: IndexMap<String, RawClass>,
}

#[derive(Deserialize)]
struct RawClass {
    name: String,
    codec: Option<String>,
    layout: Option<Vec<Op>>,
    stec: bool,
    status: String,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Op {
    Bits {
        f: String,
        n: usize,
    },
    F32 {
        f: String,
    },
    #[serde(rename = "w_u16")]
    Wu16 {
        f: String,
    },
    VarB {
        f: String,
    },
    SignedVar {
        f: String,
    },
    EntityId {
        f: String,
    },
    Value {
        f: String,
    },
    Link {
        f: String,
    },
    If {
        c: String,
        #[serde(rename = "then")]
        yes: Vec<Op>,
        #[serde(rename = "else")]
        no: Vec<Op>,
    },
    Repeat {
        f: String,
        count: Count,
        body: Vec<Op>,
    },
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum Count {
    Fixed(usize),
    Field(String),
}

fn invalid_field(name: &str) -> PayloadError {
    PayloadError::Field(name.into())
}

fn value_integer(value: &Value) -> Option<i128> {
    match value {
        Value::Bool(flag) => Some(i128::from(*flag)),
        Value::Integer(number) => Some(i128::from(*number)),
        Value::Entity(entity) => Some(i128::from(*entity)),
        Value::Float(number) if (i128::MIN as f64..-(i128::MIN as f64)).contains(number) => {
            Some(*number as i128)
        }
        _ => None,
    }
}

fn layout_integer(fields: &LayoutFields<'_>, name: &str) -> Result<i128, PayloadError> {
    match fields.get(name) {
        None => Ok(0),
        Some(FieldValue::Unsigned(number)) => Ok(i128::from(*number)),
        Some(FieldValue::Scalar(value)) => value_integer(value).ok_or_else(|| invalid_field(name)),
        Some(FieldValue::Rows(_)) => Err(invalid_field(name)),
    }
}

fn value_float(value: &Value) -> Option<f64> {
    match value {
        Value::Float(value) => Some(*value),
        Value::Integer(value) => Some(*value as f64),
        Value::Bool(value) => Some(f64::from(u8::from(*value))),
        _ => None,
    }
}

fn layout_float(fields: &LayoutFields<'_>, name: &str) -> Result<f32, PayloadError> {
    let value = match fields.get(name) {
        None => 0.0,
        Some(FieldValue::Unsigned(number)) => *number as f64,
        Some(FieldValue::Scalar(value)) => value_float(value).ok_or_else(|| invalid_field(name))?,
        Some(FieldValue::Rows(_)) => return Err(invalid_field(name)),
    };
    let narrow = value as f32;
    if value.is_finite() && !narrow.is_finite() {
        return Err(invalid_field(name));
    }
    Ok(narrow)
}

fn value_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Integer(value) => *value != 0,
        Value::Float(value) => *value != 0.0,
        Value::Entity(value) => *value != 0,
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        _ => true,
    }
}

fn layout_truthy(value: Option<&FieldValue<'_>>) -> bool {
    match value {
        None => false,
        Some(FieldValue::Scalar(value)) => value_truthy(value),
        Some(FieldValue::Unsigned(value)) => *value != 0,
        Some(FieldValue::Rows(value)) => !value.is_empty(),
    }
}

fn condition(
    test: &str,
    fields: &LayoutFields<'_>,
    mode: LayoutMode,
) -> Result<bool, PayloadError> {
    Ok(match test {
        "H" => mode.owner,
        "!H" => !mode.owner,
        "full" => mode.full,
        "H&full" => mode.owner && mode.full,
        _ => {
            if let Some(name) = test.strip_prefix("valid:") {
                let entity = layout_integer(fields, name)?;
                entity != 0 && entity & 0x1fff_ffff != 0x1fff_ffff
            } else if let Some(name) = test.strip_prefix('!') {
                !layout_truthy(fields.get(name))
            } else {
                layout_truthy(fields.get(test))
            }
        }
    })
}

type LinkWriter<'fields, 'call> =
    dyn FnMut(&mut BitWriter, Option<&FieldValue<'fields>>) -> Result<(), PayloadError> + 'call;

fn write_ops<'a>(
    out: &mut BitWriter,
    ops: &[Op],
    fields: &LayoutFields<'a>,
    mode: LayoutMode,
    link: &mut LinkWriter<'a, '_>,
) -> Result<(), PayloadError> {
    for op in ops {
        match op {
            Op::Bits { f, n } => {
                let mask = match *n {
                    0..=63 => (1_u64 << *n) - 1,
                    64 => u64::MAX,
                    _ => return Err(invalid_field(f)),
                };
                out.bits(layout_integer(fields, f)? as u64 & mask, *n)?;
            }
            Op::F32 { f } => out.f32(layout_float(fields, f)?),
            Op::Wu16 { f } => {
                out.w_u16(u16::try_from(layout_integer(fields, f)?).map_err(|_| invalid_field(f))?)
            }
            Op::VarB { f } => {
                out.var_b(u32::try_from(layout_integer(fields, f)?).map_err(|_| invalid_field(f))?)
            }
            Op::SignedVar { f } => {
                let value = layout_integer(fields, f)?;
                let absolute = u32::try_from(value.unsigned_abs()).map_err(|_| invalid_field(f))?;
                out.bit(value < 0);
                out.w_var(absolute);
            }
            Op::EntityId { f } => {
                let entity = match fields.get(f.as_str()) {
                    Some(FieldValue::Scalar(Value::Null)) => 0,
                    _ => u32::try_from(layout_integer(fields, f)?).map_err(|_| invalid_field(f))?,
                };
                out.entity_id((entity != 0).then_some(entity));
            }
            Op::Value { f } => {
                let number;
                let value = match fields.get(f.as_str()) {
                    None => &Value::Null,
                    Some(FieldValue::Scalar(value)) => value,
                    Some(FieldValue::Unsigned(value)) if *value <= u64::from(u32::MAX) => {
                        number = Value::Integer(*value as i64);
                        &number
                    }
                    _ => return Err(invalid_field(f)),
                };
                write_value(out, value, ValueEncoding::Compact)?;
            }
            Op::Link { f } => link(out, fields.get(f.as_str()))?,
            Op::If { c, yes, no } => {
                let branch = if condition(c, fields, mode)? { yes } else { no };
                write_ops(out, branch, fields, mode, link)?;
            }
            Op::Repeat { f, count, body } => {
                let count = match count {
                    Count::Fixed(count) => *count,
                    Count::Field(name) => usize::try_from(layout_integer(fields, name)?)
                        .map_err(|_| invalid_field(name))?,
                };
                let rows = match fields.get(f.as_str()) {
                    None | Some(FieldValue::Scalar(Value::Null)) => &[][..],
                    Some(FieldValue::Rows(rows)) => rows.as_slice(),
                    _ => return Err(invalid_field(f)),
                };
                if rows.len() < count {
                    return Err(PayloadError::Rows {
                        field: f.clone(),
                        expected: count,
                        actual: rows.len(),
                    });
                }
                for row in &rows[..count] {
                    write_ops(out, body, row, mode, link)?;
                }
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug)]
enum Family {
    None,
    Switch,
    DelayedSwitch,
    Stack,
    Anim,
    Chase,
    Button,
    Ability,
    Targets,
    Volley,
    Subscript,
    Message,
    Send,
    Link,
    Hits,
    Flags13,
    Frames,
    Counter2,
    Pulser,
}

impl Family {
    fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "none" => Self::None,
            "switch" => Self::Switch,
            "two_bits" => Self::DelayedSwitch,
            "stack" => Self::Stack,
            "anim" => Self::Anim,
            "chase" => Self::Chase,
            "button" => Self::Button,
            "ability" => Self::Ability,
            "targets" => Self::Targets,
            "volley" => Self::Volley,
            "subscript" => Self::Subscript,
            "message" => Self::Message,
            "send" => Self::Send,
            "link" => Self::Link,
            "hits" => Self::Hits,
            "flags13" => Self::Flags13,
            "frames" => Self::Frames,
            "counter2" => Self::Counter2,
            "pulser" => Self::Pulser,
            _ => return None,
        })
    }
}

struct Vm<'a>(&'a StatePayload);

impl Vm<'_> {
    fn flag(&self, name: &str) -> bool {
        self.0.get(name).is_some_and(value_truthy)
    }

    fn optional_int(&self, name: &str) -> Result<Option<i64>, PayloadError> {
        match self.0.get(name) {
            None | Some(Value::Null) => Ok(None),
            Some(value) => value_integer(value)
                .and_then(|number| i64::try_from(number).ok())
                .map(Some)
                .ok_or_else(|| invalid_field(name)),
        }
    }

    fn integer(&self, name: &str, default: i64) -> Result<i64, PayloadError> {
        Ok(self.optional_int(name)?.unwrap_or(default))
    }

    fn float(&self, name: &str, default: f64) -> Result<f64, PayloadError> {
        self.0.get(name).map_or(Ok(default), |value| {
            value_float(value).ok_or_else(|| invalid_field(name))
        })
    }
}

fn project(
    family: Family,
    payload: &StatePayload,
    mode: PayloadMode,
) -> Result<LayoutFields<'static>, PayloadError> {
    let vm = Vm(payload);
    let owner = mode.layout().owner;
    let mut fields = LayoutFields::new();
    match family {
        Family::None | Family::Send | Family::Link => {}
        Family::Switch => {
            fields.insert("current", Value::Bool(vm.flag("current")).into());
        }
        Family::DelayedSwitch => {
            fields.insert("a", Value::Bool(vm.flag("current")).into());
            fields.insert("b", Value::Bool(vm.flag("undecided")).into());
        }
        Family::Stack if owner => {
            fields.insert("top", Value::Bool(vm.flag("top")).into());
            fields.insert("under", Value::Bool(vm.flag("under")).into());
        }
        Family::Button | Family::Anim | Family::Counter2 => {
            fields.insert("counter", Value::Integer(vm.integer("counter", 0)?).into());
        }
        Family::Volley => project_volley(&mut fields, &vm, mode)?,
        Family::Chase => project_chase(&mut fields, &vm, mode)?,
        Family::Ability => project_ability(&mut fields, &vm, mode)?,
        Family::Targets if owner => project_targets(&mut fields, &vm)?,
        Family::Subscript if owner => {
            fields.insert("child", Value::Integer(vm.integer("child", 0)?).into());
        }
        Family::Message => {
            fields.insert("stacked", Value::Bool(vm.flag("stacked")).into());
        }
        Family::Hits => {
            fields.insert("flag", Value::Bool(vm.flag("flag")).into());
        }
        Family::Flags13 if owner => {
            let value = vm.integer("value", 0)?;
            fields.insert("low", Value::Integer(value & 63).into());
            fields.insert("more", Value::Bool(value >> 6 != 0).into());
            fields.insert("high", Value::Integer((value >> 6) & 127).into());
        }
        // The reference H0 flags13 projection leaves low/more/high unset.
        Family::Stack | Family::Targets | Family::Subscript | Family::Flags13 => {}
        Family::Frames => {
            fields.insert("target", Value::Integer(vm.integer("target", 0)?).into());
        }
        Family::Pulser => {
            fields.insert("fresh", Value::Bool(vm.flag("fresh")).into());
            fields.insert("count", Value::Integer(vm.integer("count", 0)?).into());
        }
    }
    Ok(fields)
}

fn project_volley(
    fields: &mut LayoutFields<'static>,
    vm: &Vm<'_>,
    mode: PayloadMode,
) -> Result<(), PayloadError> {
    if let PayloadMode::Owner { frame_time, .. } = mode {
        let start = vm.integer("start", frame_time)?;
        let elapsed = frame_time
            .checked_sub(start)
            .ok_or(PayloadError::TimeRange)?;
        let offset = vm.integer("offset", 0)?;
        fields.insert("t1", Value::Integer(elapsed).into());
        fields.insert("has_offset", Value::Bool(offset != 0).into());
        if offset != 0 {
            fields.insert("offset", Value::Integer(offset).into());
            fields.insert(
                "subindex",
                Value::Integer(vm.integer("subindex", 0)?).into(),
            );
        }
    }
    let volleys = vm.integer("volleys", 1)? & 0xff;
    fields.insert("has_volleys", Value::Bool(volleys != 1).into());
    fields.insert("volleys", Value::Integer(volleys).into());
    fields.insert("counter", Value::Integer(vm.integer("counter", 0)?).into());
    Ok(())
}

fn project_chase(
    fields: &mut LayoutFields<'static>,
    vm: &Vm<'_>,
    mode: PayloadMode,
) -> Result<(), PayloadError> {
    if let Some(Value::Vector([x, y, z])) = vm.0.get("cur") {
        fields.insert("vector", Value::Bool(true).into());
        for (name, part) in [("x", *x), ("y", *y), ("z", *z)] {
            fields.insert(name, Value::Float(part).into());
        }
    } else {
        fields.insert("x", Value::Float(vm.float("cur", 0.0)?).into());
    }
    fields.insert("reached", Value::Bool(vm.flag("reached")).into());
    if let Some(remaining) = vm.optional_int("remaining")? {
        fields.insert("has_remaining", Value::Bool(true).into());
        fields.insert("big", Value::Bool(remaining >= 1 << 16).into());
        fields.insert("remaining", Value::Integer(remaining.max(0)).into());
    }
    if let PayloadMode::Owner {
        frame_time,
        frame_ms,
        ..
    } = mode
    {
        if let Some(last) = vm.optional_int("last")? {
            let t = elapsed(frame_time, frame_ms, last)?;
            if t >= 0 {
                fields.insert("has_last", Value::Bool(true).into());
                fields.insert("t", Value::Integer(t).into());
            }
        }
    }
    Ok(())
}

fn project_ability(
    fields: &mut LayoutFields<'static>,
    vm: &Vm<'_>,
    mode: PayloadMode,
) -> Result<(), PayloadError> {
    fields.insert("flags", Value::Integer(vm.integer("flags", 0)?).into());
    let cur = vm.float("cur", 0.0)?;
    if cur > 0.0 || (mode.layout().owner && cur.is_nan()) {
        let rate = vm.float("rate", 1.0)?;
        fields.insert("has_cur", Value::Bool(true).into());
        fields.insert("cur", Value::Float(cur).into());
        fields.insert("has_rate", Value::Bool(rate != 1.0).into());
        fields.insert("rate", Value::Float(rate).into());
        if let PayloadMode::Owner {
            frame_time,
            frame_ms,
            ..
        } = mode
        {
            if let Some(last) = vm.optional_int("last")? {
                fields.insert(
                    "t",
                    Value::Integer(elapsed(frame_time, frame_ms, last)?.max(0)).into(),
                );
            }
        }
    }
    Ok(())
}

fn project_targets(fields: &mut LayoutFields<'static>, vm: &Vm<'_>) -> Result<(), PayloadError> {
    let targets = match vm.0.get("targets") {
        None => &[][..],
        Some(Value::Array(targets)) => targets.as_slice(),
        _ => return Err(invalid_field("targets")),
    };
    let mut rows = Vec::with_capacity(targets.len());
    for target in targets {
        let Value::Array(pair) = target else {
            return Err(invalid_field("targets"));
        };
        let [entity, age] = pair.as_slice() else {
            return Err(invalid_field("targets"));
        };
        rows.push(LayoutFields::from([
            ("entity", entity.clone().into()),
            ("age", age.clone().into()),
        ]));
    }
    let count = u16::try_from(rows.len()).map_err(|_| invalid_field("targets"))?;
    fields.insert("has_list", Value::Bool(true).into());
    fields.insert("n", Value::Integer(i64::from(count)).into());
    fields.insert("targets", FieldValue::Rows(rows));
    Ok(())
}

fn elapsed(frame_time: i64, frame_ms: u32, last: i64) -> Result<i64, PayloadError> {
    frame_time
        .checked_add(i64::from(frame_ms))
        .and_then(|next| next.checked_sub(last))
        .ok_or(PayloadError::TimeRange)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const FIXTURE: &str = r#"{"classes":{
        "ABCDEF00":{
            "name":"FixtureState","codec":"custom","stec":false,"status":"VERIFIED","layout":[
                {"op":"bits","f":"tag","n":64},
                {"op":"if","c":"H","then":[{"op":"signed_var","f":"dt"}],"else":[{"op":"w_u16","f":"short"}]},
                {"op":"f32","f":"scale"},
                {"op":"if","c":"H&full","then":[{"op":"value","f":"data"}],"else":[]},
                {"op":"if","c":"full","then":[{"op":"link","f":"bind"}],"else":[]},
                {"op":"w_u16","f":"n"},
                {"op":"repeat","count":"n","f":"rows","body":[
                    {"op":"entity_id","f":"entity"},
                    {"op":"if","c":"valid:entity","then":[{"op":"var_b","f":"extra"}],"else":[]},
                    {"op":"bits","f":"flag","n":1}]},
                {"op":"repeat","count":2,"f":"flags","body":[{"op":"bits","f":"set","n":1}]},
                {"op":"if","c":"!a","then":[{"op":"bits","f":"b","n":1}],"else":[]}
            ]
        },
        "ABCDEF01":{"name":"NoLayout","codec":"unknown","stec":true,"status":"UNVERIFIED","layout":null},
        "ABCDEF02":{"name":"Empty","codec":"none","stec":false,"status":"VERIFIED","layout":[]}
    }}"#;

    fn fields() -> LayoutFields<'static> {
        LayoutFields::from([
            ("tag", FieldValue::Unsigned(0xfedc_ba98_7654_3210)),
            ("dt", Value::Integer(-70000).into()),
            ("short", Value::Integer(300).into()),
            ("scale", Value::Float(1.25).into()),
            (
                "data",
                Value::Array(vec![
                    Value::Bool(true),
                    Value::Integer(-23),
                    Value::String("x".into()),
                ])
                .into(),
            ),
            ("bind", Value::Integer(9).into()),
            ("n", Value::Integer(2).into()),
            (
                "rows",
                FieldValue::Rows(vec![
                    LayoutFields::from([
                        ("entity", Value::Entity(0xa000_0101).into()),
                        ("extra", Value::Integer(70000).into()),
                        ("flag", Value::Bool(true).into()),
                    ]),
                    LayoutFields::from([("entity", Value::Entity(0xbfff_ffff).into())]),
                ]),
            ),
            (
                "flags",
                FieldValue::Rows(vec![
                    LayoutFields::from([("set", Value::Bool(true).into())]),
                    LayoutFields::new(),
                ]),
            ),
            ("b", Value::Bool(true).into()),
        ])
    }

    fn check(out: &BitWriter, length: usize, hex: &str) {
        let actual: String = out
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!((out.bit_len(), actual.as_str()), (length, hex));
    }

    #[test]
    fn resolves_all_aliases_and_rejects_unverified_or_unprojected_active_states() {
        let table = CodecTable::from_json(FIXTURE).unwrap();
        let codec = table.codec("FixtureState").unwrap();
        assert!(std::ptr::eq(table.codec("ABCDEF00").unwrap(), codec));
        assert!(std::ptr::eq(table.codec("STU_ABCDEF00").unwrap(), codec));
        assert!(codec.verified());
        assert!(!codec.projected());
        assert!(!table.sendable("FixtureState", true));
        assert!(table.sendable("FixtureState", false));
        assert!(!table.sendable("NoLayout", false));
        assert!(table.sendable("missing", false));
        assert!(table.codec("Empty").unwrap().empty());
        assert!(table.sendable("Empty", true));
        let mode = PayloadMode::Remote { full: true };
        assert_eq!(
            codec.payload(&StatePayload::new(), mode),
            Err(PayloadError::Unsupported("FixtureState".into()))
        );
        assert_eq!(
            table
                .codec("NoLayout")
                .unwrap()
                .payload(&StatePayload::new(), mode),
            Err(PayloadError::Unverified("NoLayout".into()))
        );
    }

    #[test]
    fn generic_layout_matches_python_for_modes_links_tagged_values_and_repeats() {
        let table = CodecTable::from_json(FIXTURE).unwrap();
        let codec = table.codec("FixtureState").unwrap();
        let fields = fields();
        let owner = codec
            .encode_layout_with_link(
                &fields,
                LayoutMode {
                    owner: true,
                    full: true,
                },
                |out, link| {
                    assert_eq!(link, Some(&FieldValue::Scalar(Value::Integer(9))));
                    out.bit(true);
                    out.w_u16(9);
                    Ok(())
                },
            )
            .unwrap();
        check(
            &owner,
            291,
            "1032547698badcfe878b0800000000fd791ba0a4bff0004a520101c3450400ecffffff0f05",
        );
        let remote = codec.encode_layout(&fields, LayoutMode::default()).unwrap();
        check(
            &remote,
            214,
            "1032547698badcfeb304000080fe900a08182e220060ffffff7f28",
        );
    }

    #[test]
    fn malformed_layout_fields_report_sizes_and_types() {
        let table = CodecTable::from_json(FIXTURE).unwrap();
        let codec = table.codec("FixtureState").unwrap();
        let mut fields = fields();
        fields.insert("n", Value::Integer(3).into());
        assert_eq!(
            codec.encode_layout(&fields, LayoutMode::default()),
            Err(PayloadError::Rows {
                field: "rows".into(),
                expected: 3,
                actual: 2
            })
        );
        fields.insert("short", Value::Integer(-1).into());
        assert_eq!(
            codec.encode_layout(&fields, LayoutMode::default()),
            Err(PayloadError::Field("short".into()))
        );
    }

    fn sample_value(value: &serde_json::Value) -> Value {
        use serde_json::Value as Json;
        match value {
            Json::Null => Value::Null,
            Json::Bool(value) => Value::Bool(*value),
            Json::Number(number) => number
                .as_i64()
                .map_or_else(|| Value::Float(number.as_f64().unwrap()), Value::Integer),
            Json::Array(items) => Value::Array(items.iter().map(sample_value).collect()),
            Json::Object(object) => {
                let parts = object["vector"].as_array().unwrap();
                Value::Vector(std::array::from_fn(|axis| parts[axis].as_f64().unwrap()))
            }
            Json::String(string) => Value::String(string.clone()),
        }
    }

    fn samples() -> BTreeMap<String, Vec<StatePayload>> {
        // tests/test_statescript_codecs.py::SAMPLES with explicit vectors and
        // all three delayed switch payloads from the generic layout writer.
        let corpus = json!({
            "none":[{}],
            "switch":[{"current":true},{"current":false}],
            "two_bits":[
                {"current":false,"undecided":false},
                {"current":false,"undecided":true},
                {"current":true,"undecided":false}
            ],
            "stack":[{"top":true,"under":false},{"top":false,"under":true}],
            "button":[{"counter":0},{"counter":13},{"counter":21}],
            "volley":[
                {"start":15840,"counter":5},
                {"start":16003,"offset":-7,"subindex":2,"volleys":3,"counter":70}
            ],
            "anim":[{"counter":6},{"counter":9}],
            "chase":[
                {"cur":1.5,"reached":false,"remaining":null,"last":null},
                {"cur":{"vector":[1.0,-2.0,0.5]},"reached":true,"remaining":70000,"last":16005},
                {"cur":0.25,"remaining":300,"last":15960}
            ],
            "ability":[
                {"flags":5,"cur":0.0},
                {"flags":2,"cur":3.5,"rate":1.0,"last":15960},
                {"flags":7,"cur":1.25,"rate":0.5,"last":16016}
            ],
            "targets":[{},{"targets":[
                [0xa000_0101_u32,0],[0xa000_0102_u32,255],[0xa000_0103_u32,256],
                [0xa000_0104_u32,65535],[0xa000_0105_u32,65536]
            ]}],
            "subscript":[{"child":13},{"child":300}],
            "message":[{"stacked":true},{"stacked":false}],
            "send":[{}],
            "link":[{}],
            "hits":[{"flag":true},{"flag":false}],
            "flags13":[{"value":0},{"value":63},{"value":64},{"value":8191}],
            "frames":[{"target":9199}],
            "counter2":[{"counter":0},{"counter":3},{"counter":6}],
            "pulser":[{"fresh":false,"count":0},{"fresh":true,"count":300}]
        });
        corpus
            .as_object()
            .unwrap()
            .iter()
            .map(|(family, cases)| {
                let cases = cases
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|case| {
                        case.as_object()
                            .unwrap()
                            .iter()
                            .map(|(key, value)| (key.clone(), sample_value(value)))
                            .collect()
                    })
                    .collect();
                (family.clone(), cases)
            })
            .collect()
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 state codec table"]
    fn actual_payloads_match_python_in_owner_and_both_observer_modes() {
        use sha2::{Digest, Sha256};
        use std::path::PathBuf;

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let table = CodecTable::load(root.join("data/statescript_codecs_174.json")).unwrap();
        assert_eq!(table.classes().count(), 598);
        assert_eq!(
            table.classes().filter(|codec| codec.verified()).count(),
            595
        );
        assert_eq!(table.classes().filter(|codec| codec.stec()).count(), 4);
        assert!(!table.sendable("STU_2F07848F", false));
        let mut projected: Vec<_> = table.classes().filter(|codec| codec.projected()).collect();
        projected.sort_by(|left, right| left.hash.cmp(&right.hash));
        assert_eq!(projected.len(), 580);
        let samples = samples();
        assert_eq!(samples.len(), 19);
        let modes = [
            PayloadMode::Owner {
                full: false,
                frame_time: 16000,
                frame_ms: 16,
            },
            PayloadMode::Remote { full: true },
            PayloadMode::Remote { full: false },
        ];
        let mut digest = Sha256::new();
        let mut count = 0;
        for codec in projected {
            let cases = &samples[codec.codec.as_deref().unwrap()];
            for (mode, style) in modes.into_iter().enumerate() {
                for (index, payload) in cases.iter().enumerate() {
                    let out = codec.payload(payload, style).unwrap();
                    digest.update(codec.hash.as_bytes());
                    digest.update([u8::try_from(mode).unwrap()]);
                    digest.update(u32::try_from(index).unwrap().to_le_bytes());
                    digest.update(u32::try_from(out.bit_len()).unwrap().to_le_bytes());
                    digest.update(out.as_bytes());
                    count += 1;
                }
            }
        }
        assert_eq!(count, 1833);
        assert_eq!(
            format!("{:x}", digest.finalize()),
            "6f316769d720476a184200555630aa4ed0b33dd9418daf524cd3019412c14955"
        );
    }
}
