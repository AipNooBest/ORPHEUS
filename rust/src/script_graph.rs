//! State graph metadata for retail 1.74 and its owner/remote wire tables.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{self, BufReader, Read},
    path::Path,
};

use flate2::bufread::MultiGzDecoder;
use serde::Deserialize;
use serde_json::{Map, Value};

use crate::accounts::parse_guid;

const MAX_GRAPH_BYTES: u64 = 128 * 1024 * 1024;
const GRAPH_TYPE: u64 = 0x0580;

#[derive(Debug, Deserialize)]
pub struct Header {
    #[serde(rename = "m_nodesBitCount")]
    pub nodes_bits: u8,
    #[serde(rename = "m_statesBitCount")]
    pub states_bits: u8,
    #[serde(rename = "m_remoteSyncNodesBitCount")]
    pub remote_bits: u8,
    #[serde(rename = "m_syncVarsBitCount")]
    pub sync_vars_bits: u8,
}

#[derive(Debug, Deserialize)]
pub struct Node {
    pub pos: u16,
    pub class: String,
    pub uid: u32,
    pub rid: u16,
    pub remote: i32,
    pub state: Option<u16>,
    #[serde(deserialize_with = "node_flag")]
    pub client_only: bool,
    #[serde(deserialize_with = "node_flag")]
    pub server_only: bool,
    pub fields: Value,
}

impl Node {
    pub fn is_networked(&self) -> bool {
        !self.client_only && !self.server_only
    }

    pub fn is_state(&self) -> bool {
        self.fields.get("m_2BBEEAB8").is_some_and(truthy)
    }

    pub fn is_action(&self) -> bool {
        self.fields.get("m_ADEB6E05").is_some_and(truthy)
    }

    pub fn field(&self, path: &str) -> Option<&Value> {
        field_at(&self.fields, path)
    }

    /// The plug's targets in data order, including unresolved targets.
    pub fn links(&self, path: &str) -> impl Iterator<Item = (Option<u16>, &str)> {
        self.field(path)
            .filter(|field| field.get("$").is_some())
            .and_then(|field| field.get("links")?.as_array())
            .into_iter()
            .flatten()
            .filter_map(|link| {
                let pair = link.as_array()?;
                let position = match pair.first()? {
                    Value::Null => None,
                    value => Some(u16::try_from(value.as_u64()?).ok()?),
                };
                Some((position, pair.get(1)?.as_str()?))
            })
    }

    pub fn plugs(&self) -> BTreeMap<String, &Value> {
        let mut found = BTreeMap::new();
        collect_plugs(&self.fields, "", &mut found);
        found
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
pub struct SyncVar(pub Option<u32>, pub u8, pub u8);

impl SyncVar {
    pub fn has_presence_bit(self) -> bool {
        self.0.is_some() && self.1 == 0
    }
}

#[derive(Debug, Default)]
struct WireSlots {
    by_list_index: Vec<Option<usize>>,
    positions: Vec<u16>,
}

impl WireSlots {
    fn build(order: &[Option<u16>], graph: &Graph, require_state: bool) -> Self {
        let mut positions = Vec::new();
        let mut first_slot = vec![None; graph.by_pos.len()];
        let by_list_index = order
            .iter()
            .map(|&position| {
                let node = graph.node(position?)?;
                if !node.is_networked() || (require_state && !node.is_state()) {
                    return None;
                }
                let slot = *first_slot[usize::from(node.pos)].get_or_insert(positions.len());
                positions.push(node.pos);
                Some(slot)
            })
            .collect();
        Self {
            by_list_index,
            positions,
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct Graph {
    pub index: u32,
    pub header: Header,
    pub fields: Value,
    pub items: u16,
    pub containers: BTreeMap<u16, String>,
    pub nodes: Vec<Node>,
    #[serde(rename = "unknown_nodes")]
    pub unknown_rids: Vec<u16>,
    pub states: Vec<Option<u16>>,
    pub entries: Vec<Option<u16>>,
    #[serde(rename = "remote")]
    pub remote_nodes: Vec<Option<u16>>,
    pub sync_vars: Vec<SyncVar>,
    #[serde(skip)]
    by_pos: Vec<Option<usize>>,
    #[serde(skip)]
    by_rid: Vec<Option<usize>>,
    #[serde(skip)]
    owner: WireSlots,
    #[serde(skip)]
    remote: WireSlots,
    #[serde(skip)]
    presence: Vec<usize>,
}

impl Graph {
    fn compile(&mut self) -> io::Result<()> {
        self.by_pos = vec![None; usize::from(self.items)];
        if let Some(max) = self.nodes.iter().map(|node| node.rid).max() {
            self.by_rid = vec![None; usize::from(max) + 1];
        }
        for (index, node) in self.nodes.iter().enumerate() {
            let position = self
                .by_pos
                .get_mut(usize::from(node.pos))
                .ok_or_else(|| invalid("graph node outside the item table"))?;
            *position = Some(index);
            self.by_rid[usize::from(node.rid)] = Some(index);
        }
        self.owner = WireSlots::build(&self.states, self, false);
        self.remote = WireSlots::build(&self.remote_nodes, self, true);
        self.presence = self
            .sync_vars
            .iter()
            .enumerate()
            .filter_map(|(index, var)| var.has_presence_bit().then_some(index))
            .collect();
        Ok(())
    }

    pub fn node(&self, pos: u16) -> Option<&Node> {
        self.nodes.get((*self.by_pos.get(usize::from(pos))?)?)
    }

    pub fn by_rid(&self, rid: u16) -> Option<&Node> {
        self.nodes.get((*self.by_rid.get(usize::from(rid))?)?)
    }

    pub fn state(&self, index: usize) -> Option<&Node> {
        self.node((*self.states.get(index)?)?)
    }

    pub fn remote_node(&self, index: usize) -> Option<&Node> {
        self.node((*self.remote_nodes.get(index)?)?)
    }

    pub fn node_ref(&self, value: &Value) -> Option<&Node> {
        self.node(u16::try_from(value.get("node")?.as_u64()?).ok()?)
    }

    pub fn owner_positions(&self) -> &[u16] {
        &self.owner.positions
    }

    pub fn remote_positions(&self) -> &[u16] {
        &self.remote.positions
    }

    pub fn owner_slot(&self, index: usize) -> Option<usize> {
        *self.owner.by_list_index.get(index)?
    }

    pub fn remote_slot(&self, index: usize) -> Option<usize> {
        *self.remote.by_list_index.get(index)?
    }

    pub fn presence_vars(&self) -> impl Iterator<Item = (usize, SyncVar)> + '_ {
        self.presence
            .iter()
            .map(|&index| (index, self.sync_vars[index]))
    }

    pub fn presence_bit(&self, var: u32) -> Option<usize> {
        self.presence_vars()
            .position(|(_, entry)| entry.0 == Some(var))
    }

    pub fn graph_refs(&self) -> BTreeSet<u64> {
        let mut found = BTreeSet::new();
        collect_graphs(&self.fields, &mut found);
        for node in &self.nodes {
            collect_graphs(&node.fields, &mut found);
        }
        found
    }
}

#[derive(Debug, Deserialize)]
pub struct GraphBinding {
    pub graph: Option<u32>,
    #[serde(rename = "m_1EB5A024")]
    pub sync: Option<Vec<Value>>,
    #[serde(rename = "m_75408640")]
    pub flags: i64,
}

#[derive(Debug, Deserialize)]
pub struct Body {
    pub hero: Option<String>,
    pub name: String,
    pub graphs: Vec<GraphBinding>,
    pub manager: Option<u32>,
    pub weapons: Vec<Option<u32>>,
    #[serde(default)]
    pub ai_weapon_buttons: Map<String, Value>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModeValue {
    Bool(bool),
    Integer(i64),
    Asset(u64),
}

#[derive(Deserialize)]
struct Bundle {
    version: u32,
    graphs: Vec<Graph>,
    bodies: BTreeMap<String, Body>,
    #[serde(default)]
    entities: BTreeMap<String, Body>,
    #[serde(default)]
    mode_variables: BTreeMap<String, BTreeMap<u32, Value>>,
    missing: Vec<u32>,
}

#[derive(Debug)]
pub struct GraphStore {
    graphs: BTreeMap<u32, Graph>,
    bodies: BTreeMap<u64, Body>,
    entities: BTreeMap<u64, Body>,
    mode_variables: BTreeMap<u64, BTreeMap<u32, ModeValue>>,
    missing: Vec<u32>,
}

impl GraphStore {
    pub fn load(path: impl AsRef<Path>) -> io::Result<Self> {
        let input = BufReader::new(File::open(path)?);
        Self::from_gzip(input)
    }

    pub fn from_gzip(input: impl io::BufRead) -> io::Result<Self> {
        let mut data = Vec::new();
        MultiGzDecoder::new(input)
            .take(MAX_GRAPH_BYTES + 1)
            .read_to_end(&mut data)?;
        if data.len() as u64 > MAX_GRAPH_BYTES {
            return Err(invalid("state graph bundle exceeds the size limit"));
        }
        Self::from_json(&data)
    }

    pub fn from_json(data: &[u8]) -> io::Result<Self> {
        let bundle: Bundle = serde_json::from_slice(data).map_err(invalid)?;
        if bundle.version != 1 {
            return Err(invalid("unsupported state graph bundle version"));
        }
        let mut graphs = BTreeMap::new();
        for mut graph in bundle.graphs {
            graph.compile()?;
            graphs.insert(graph.index, graph);
        }
        Ok(Self {
            graphs,
            bodies: keyed_by_guid(bundle.bodies)?,
            entities: keyed_by_guid(bundle.entities)?,
            mode_variables: keyed_by_guid(bundle.mode_variables)?
                .into_iter()
                .map(|(guid, values)| {
                    let values = values
                        .into_iter()
                        .map(|(var, value)| Ok((var, mode_value(&value)?)))
                        .collect::<io::Result<_>>()?;
                    Ok((guid, values))
                })
                .collect::<io::Result<_>>()?,
            missing: bundle.missing,
        })
    }

    pub fn graph(&self, index: u32) -> Option<&Graph> {
        self.graphs.get(&index)
    }

    pub fn graph_indexes(&self) -> impl Iterator<Item = u32> + '_ {
        self.graphs.keys().copied()
    }

    pub fn bodies(&self) -> &BTreeMap<u64, Body> {
        &self.bodies
    }

    pub fn entity_body(&self, guid: u64) -> Option<&Body> {
        self.entities.get(&guid)
    }

    pub fn mode_variables(&self, mode: u64) -> Option<&BTreeMap<u32, ModeValue>> {
        self.mode_variables.get(&mode)
    }

    pub fn missing(&self) -> &[u32] {
        &self.missing
    }
}

pub fn asset_guid(value: &Value) -> Option<u64> {
    let text = value.as_str()?;
    let hex = text.strip_prefix("0x")?;
    if hex.len() == 16
        && hex
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'A'..=b'F'))
    {
        parse_guid(text)
    } else {
        None
    }
}

pub fn field_at<'a>(fields: &'a Value, path: &str) -> Option<&'a Value> {
    let mut value = fields;
    for part in path.split('.') {
        let (name, mut indexes) = part.split_at(part.find('[').unwrap_or(part.len()));
        if name.is_empty() || !name.chars().all(|ch| ch == '_' || ch.is_alphanumeric()) {
            return None;
        }
        value = value.get(name)?;
        while !indexes.is_empty() {
            let (index, tail) = indexes.strip_prefix('[')?.split_once(']')?;
            if index.is_empty() || !index.bytes().all(|ch| ch.is_ascii_digit()) {
                return None;
            }
            value = value.get(index.parse::<usize>().ok()?)?;
            indexes = tail;
        }
    }
    Some(value)
}

fn collect_plugs<'a>(value: &'a Value, path: &str, out: &mut BTreeMap<String, &'a Value>) {
    match value {
        Value::Object(fields) if fields.contains_key("$") && fields.contains_key("links") => {
            out.insert(path.to_owned(), value);
        }
        Value::Object(fields) => {
            for (key, item) in fields {
                if key != "$" {
                    let name = if path.is_empty() {
                        key.clone()
                    } else {
                        format!("{path}.{key}")
                    };
                    collect_plugs(item, &name, out);
                }
            }
        }
        Value::Array(values) => {
            for (index, item) in values.iter().enumerate() {
                collect_plugs(item, &format!("{path}[{index}]"), out);
            }
        }
        _ => {}
    }
}

fn collect_graphs(value: &Value, out: &mut BTreeSet<u64>) {
    match value {
        Value::Object(fields) => {
            for field in fields.values() {
                collect_graphs(field, out);
            }
        }
        Value::Array(values) => {
            for item in values {
                collect_graphs(item, out);
            }
        }
        _ => {
            if let Some(guid) = asset_guid(value).filter(|guid| guid >> 48 == GRAPH_TYPE) {
                out.insert(guid & 0x0000_ffff_ffff_ffff);
            }
        }
    }
}

fn keyed_by_guid<T>(records: BTreeMap<String, T>) -> io::Result<BTreeMap<u64, T>> {
    records
        .into_iter()
        .map(|(key, value)| {
            let hex = key.strip_prefix("0x").unwrap_or(&key);
            let guid = u64::from_str_radix(hex, 16).map_err(invalid)?;
            Ok((guid, value))
        })
        .collect()
}

fn mode_value(value: &Value) -> io::Result<ModeValue> {
    if let Some(value) = value.as_bool() {
        return Ok(ModeValue::Bool(value));
    }
    if let Some(value) = value.as_i64() {
        return Ok(ModeValue::Integer(value));
    }
    asset_guid(value)
        .map(ModeValue::Asset)
        .ok_or_else(|| invalid("unsupported mode variable"))
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn node_flag<'de, D: serde::Deserializer<'de>>(decoder: D) -> Result<bool, D::Error> {
    Value::deserialize(decoder).map(|value| truthy(&value))
}

fn invalid(error: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fixture() -> Value {
        let node = |pos, rid, client_only, fields: Value| {
            json!({
                "pos": pos, "uid": 0, "rid": rid, "remote": 0, "state": null,
                "class": "Node", "client_only": client_only, "server_only": 0, "fields": fields
            })
        };
        let body = json!({
            "hero": null, "name": "Fixture", "manager": null, "weapons": [null, 42],
            "graphs": [{"graph": 42, "m_1EB5A024": null, "m_75408640": 0}]
        });
        json!({
            "version": 1,
            "graphs": [{
                "index": 42,
                "header": {"m_nodesBitCount": 4, "m_statesBitCount": 3,
                    "m_remoteSyncNodesBitCount": 3, "m_syncVarsBitCount": 3},
                "fields": {"ref": "0x058000000000001A", "ignored": "0x058000000000001b"},
                "items": 5, "containers": {"3": "Opaque"}, "unknown_nodes": [8],
                "nodes": [
                    node(0, 3, false, json!({
                        "m_2BBEEAB8": true,
                        "entries": [{"trigger": {"$": "Plug", "links": [[2, "input"], [null, "unknown"]]} }],
                        "related": ["0x058000000000002B", "0x040000000000002A"]
                    })),
                    node(1, 5, true, json!({"m_2BBEEAB8": true})),
                    node(2, 6, false, json!({"m_ADEB6E05": true}))
                ],
                "states": [0, 1, null, 0],
                "entries": [0, 3, 2, null],
                "remote": [2, null, 1, 0],
                "sync_vars": [[null, 0, 0], [12, 1, 0], [3, 0, 1], [42, 0, 0]]
            }],
            "bodies": {"0x0400000000000001": body.clone()},
            "entities": {"0x0400000000000002": body},
            "mode_variables": {"0x0230000000000018": {
                "1": false, "2": 3, "3": "0x0D8000000000195F"
            }},
            "missing": [43]
        })
    }

    #[test]
    fn builds_wire_slots_and_keeps_data_order_through_holes() {
        let store = GraphStore::from_json(&serde_json::to_vec(&fixture()).unwrap()).unwrap();
        let graph = store.graph(42).unwrap();
        assert_eq!(graph.owner_positions(), [0, 0]);
        assert_eq!(graph.remote_positions(), [0]);
        assert_eq!(graph.owner_slot(0), Some(0));
        assert_eq!(graph.owner_slot(3), Some(0));
        assert_eq!(graph.owner_slot(1), None);
        assert_eq!(graph.remote_slot(0), None);
        assert_eq!(graph.remote_slot(3), Some(0));
        assert_eq!(graph.presence_bit(42), Some(1));
        assert_eq!(graph.presence_bit(12), None);
        assert_eq!(graph.graph_refs(), BTreeSet::from([26, 43]));
        assert_eq!(graph.by_rid(6).unwrap().pos, 2);
        assert_eq!(graph.node_ref(&json!({"node": 2})).unwrap().rid, 6);
        assert!(graph.node(3).is_none());

        let node = graph.node(0).unwrap();
        let plug_path = "entries[0].trigger";
        assert_eq!(node.plugs().keys().next().unwrap(), plug_path);
        assert_eq!(
            node.links(plug_path).collect::<Vec<_>>(),
            [(Some(2), "input"), (None, "unknown")]
        );
        for invalid in [
            "entries[-1].trigger",
            "entries[0]x",
            ".entries[0]",
            "entries[1]",
        ] {
            assert_eq!(node.field(invalid), None);
        }
        assert_eq!(store.missing(), [43]);
        assert_eq!(
            store.bodies()[&0x0400_0000_0000_0001].graphs[0].graph,
            Some(42)
        );
        assert_eq!(
            store.entity_body(0x0400_0000_0000_0002).unwrap().name,
            "Fixture"
        );
        assert_eq!(
            store.mode_variables(0x0230_0000_0000_0018).unwrap(),
            &BTreeMap::from([
                (1, ModeValue::Bool(false)),
                (2, ModeValue::Integer(3)),
                (3, ModeValue::Asset(0x0d80_0000_0000_195f)),
            ])
        );
    }

    #[test]
    fn reads_gzip_and_rejects_truncated_or_inconsistent_data() {
        use flate2::{Compression, write::GzEncoder};
        use std::io::Write;

        let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
        encoder
            .write_all(&serde_json::to_vec(&fixture()).unwrap())
            .unwrap();
        let bytes = encoder.finish().unwrap();
        assert!(
            GraphStore::from_gzip(bytes.as_slice())
                .unwrap()
                .graph(42)
                .is_some()
        );
        assert!(GraphStore::from_gzip(&bytes[..bytes.len() - 4]).is_err());
        let mut bad = fixture();
        bad["graphs"][0]["nodes"][0]["pos"] = json!(6);
        assert!(GraphStore::from_json(&serde_json::to_vec(&bad).unwrap()).is_err());
        bad["version"] = json!(2);
        assert!(GraphStore::from_json(&serde_json::to_vec(&bad).unwrap()).is_err());
    }

    #[test]
    #[ignore = "requires a prepared 104319 state graph bundle"]
    fn whole_retail_wire_index_matches_the_python_reference() {
        use sha2::{Digest, Sha256};
        let root = std::path::PathBuf::from(std::env::var_os("ORPHEUS_RUNTIME_DIR").unwrap());
        let store = GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap();
        let mut digest = Sha256::new();
        fn sequence(digest: &mut Sha256, items: impl IntoIterator<Item = u64>) {
            let items: Vec<_> = items.into_iter().collect();
            digest.update((items.len() as u64).to_le_bytes());
            for item in items {
                digest.update(item.to_le_bytes());
            }
        }
        for index in store.graph_indexes() {
            let graph = store.graph(index).unwrap();
            sequence(
                &mut digest,
                [
                    u64::from(index),
                    u64::from(graph.items),
                    u64::from(graph.header.nodes_bits),
                    u64::from(graph.header.states_bits),
                    u64::from(graph.header.remote_bits),
                    u64::from(graph.header.sync_vars_bits),
                    graph.nodes.len() as u64,
                ],
            );
            sequence(
                &mut digest,
                graph.owner_positions().iter().copied().map(u64::from),
            );
            sequence(
                &mut digest,
                graph.remote_positions().iter().copied().map(u64::from),
            );
            sequence(
                &mut digest,
                graph.presence_vars().flat_map(|(index, var)| {
                    [
                        index as u64,
                        u64::from(var.0.unwrap()),
                        u64::from(var.1),
                        u64::from(var.2),
                    ]
                }),
            );
            sequence(&mut digest, graph.graph_refs());
            sequence(
                &mut digest,
                graph.unknown_rids.iter().copied().map(u64::from),
            );
        }
        assert_eq!(store.bodies().len(), 32);
        let expected = match store.graphs.len() {
            373 => "f54fb85e91d33b669c73239ddc77fb0f8f7fa6e6d0f04b83532bc1f953a4ac7c",
            405 => {
                assert_eq!(store.entities.len(), 8);
                assert_eq!(store.mode_variables.len(), 25);
                "eab09343964a53d44a53baa8d990ff9c2a3eea8922d9eb1927136fb7b661fc94"
            }
            count => panic!("no Python reference vector for the {count}-graph bundle"),
        };
        assert_eq!(format!("{:x}", digest.finalize()), expected);
    }
}
