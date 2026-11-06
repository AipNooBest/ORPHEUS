//! Project an initialized statescript body into the client baseline stream.
//!
//! The owner receives an H0 tree with all networked states off. An observer
//! receives the same tree and the initialized remote-synced states.

use std::{
    collections::{BTreeMap, BTreeSet},
    error::Error,
    fmt,
};

use crate::bits::BitWriter;
use crate::script_codecs::{Codec, CodecTable, PayloadError, PayloadMode, StatePayload};
use crate::script_graph::{Graph, GraphStore, Node, asset_guid};
use crate::script_init::{Snapshot, StateKind};
use crate::script_stream::{BodyStream, ScriptChunk, StreamError};
use crate::script_wire::{self, Instance, Layout, Value, Variables, WireError};

const OWNER_HEALTH: u32 = 253;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum View {
    Owner,
    Observer,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectedSnapshot {
    entity: u32,
    view: View,
    variables: Variables,
    instances: BTreeMap<u16, ProjectedInstance>,
}

impl ProjectedSnapshot {
    pub fn entity(&self) -> u32 {
        self.entity
    }

    pub fn view(&self) -> View {
        self.view
    }

    pub fn instance_count(&self) -> usize {
        self.instances.len()
    }
}

#[derive(Clone, Debug, PartialEq)]
struct ProjectedInstance {
    graph: u32,
    parent: Option<(u16, u16)>,
    variables: Variables,
    states: BTreeMap<u16, ProjectedState>,
}

#[derive(Clone, Debug, PartialEq)]
struct ProjectedState {
    active: bool,
    payload: StatePayload,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StateSite {
    pub instance: u16,
    pub graph: u32,
    pub state: u16,
}

#[derive(Debug)]
pub enum SnapshotError {
    Incomplete {
        entity: u32,
        diagnostics: usize,
        first: String,
    },
    MissingGraph {
        instance: u16,
        graph: u32,
    },
    MissingState(StateSite),
    MissingStateIndex {
        instance: u16,
        graph: u32,
        node: u16,
    },
    UnsupportedState {
        site: StateSite,
        class: String,
        kind: StateKind,
    },
    Payload {
        site: StateSite,
        source: PayloadError,
    },
    Wire(WireError),
    Stream(StreamError),
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Incomplete {
                entity,
                diagnostics,
                first,
            } => {
                write!(
                    f,
                    "body {entity:08X} has {diagnostics} initialization diagnostics, first {first}"
                )
            }
            Self::MissingGraph { instance, graph } => {
                write!(f, "instance {instance} has no graph {graph:04X}")
            }
            Self::MissingState(site) => write!(f, "missing statescript state {site:?}"),
            Self::MissingStateIndex {
                instance,
                graph,
                node,
            } => {
                write!(
                    f,
                    "instance {instance} graph {graph:04X} remote node {node} has no state index"
                )
            }
            Self::UnsupportedState { site, class, kind } => {
                write!(f, "unsupported active state {class} ({kind:?}) at {site:?}")
            }
            Self::Payload { site, source } => write!(f, "statescript state {site:?}: {source}"),
            Self::Wire(error) => error.fmt(f),
            Self::Stream(error) => error.fmt(f),
        }
    }
}

impl Error for SnapshotError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Payload { source, .. } => Some(source),
            Self::Wire(error) => Some(error),
            Self::Stream(error) => Some(error),
            _ => None,
        }
    }
}

impl From<WireError> for SnapshotError {
    fn from(error: WireError) -> Self {
        Self::Wire(error)
    }
}

impl From<StreamError> for SnapshotError {
    fn from(error: StreamError) -> Self {
        Self::Stream(error)
    }
}

pub struct SnapshotEncoder<'a> {
    graphs: &'a GraphStore,
    codecs: &'a CodecTable,
}

impl<'a> SnapshotEncoder<'a> {
    pub fn new(graphs: &'a GraphStore, codecs: &'a CodecTable) -> Self {
        Self { graphs, codecs }
    }

    pub fn project(
        &self,
        snapshot: &Snapshot,
        view: View,
    ) -> Result<ProjectedSnapshot, SnapshotError> {
        if !snapshot.initialization_complete() {
            return Err(SnapshotError::Incomplete {
                entity: snapshot.entity,
                diagnostics: snapshot.diagnostics.len(),
                first: format!("{:?}", snapshot.diagnostics.first()),
            });
        }
        let mut entity_exclusions = BTreeSet::from([OWNER_HEALTH]);
        let mut instances = BTreeMap::new();
        for (&id, instance) in &snapshot.instances {
            let graph = self.graph(id, instance.graph)?;
            let exclusions = server_only_variables(graph);
            let variables = replicated_variables(&instance.variables, &exclusions);
            entity_exclusions.extend(exclusions);
            let mut states = BTreeMap::new();
            for (&raw, state) in &instance.states {
                let site = StateSite {
                    instance: id,
                    graph: graph.index,
                    state: raw,
                };
                let node = graph
                    .state(usize::from(raw))
                    .ok_or(SnapshotError::MissingState(site))?;
                if !node.is_networked()
                    || (view == View::Observer
                        && (presenter(node) || !graph.remote_positions().contains(&node.pos)))
                {
                    continue;
                }
                let codec = self.codecs.codec(&node.class);
                if !state.active && !(state.activation != 0 && codec.is_some_and(Codec::stec)) {
                    continue;
                }
                let payload = codec
                    .filter(|codec| codec.projected())
                    .and_then(|codec| state_payload(state.kind, codec))
                    .ok_or_else(|| SnapshotError::UnsupportedState {
                        site,
                        class: node.class.clone(),
                        kind: state.kind,
                    })?;
                states.insert(
                    raw,
                    ProjectedState {
                        active: state.active,
                        payload,
                    },
                );
            }
            instances.insert(
                id,
                ProjectedInstance {
                    graph: instance.graph,
                    parent: instance.parent,
                    variables,
                    states,
                },
            );
        }
        Ok(ProjectedSnapshot {
            entity: snapshot.entity,
            view,
            variables: replicated_variables(&snapshot.variables, &entity_exclusions),
            instances,
        })
    }

    /// The owner's first tree and the observer's first frame share the H0
    /// descriptor, variable, and remote slot writer.
    pub fn full(&self, snapshot: &ProjectedSnapshot) -> Result<BitWriter, SnapshotError> {
        let mut instances = Vec::with_capacity(snapshot.instances.len());
        for (&id, instance) in &snapshot.instances {
            let graph = self.graph(id, instance.graph)?;
            let mut wire = Instance::new(id, Layout::try_from(graph)?, instance.parent);
            wire.variables = instance.variables.clone();
            for (slot, (_, var)) in graph.presence_vars().enumerate() {
                if let Some(value) = var.0.and_then(|var| wire.variables.swap_remove(&var)) {
                    wire.presence.insert(slot, value);
                }
            }
            if snapshot.view == View::Observer {
                self.remote_states(id, graph, instance, &mut wire)?;
            }
            instances.push(wire);
        }
        Ok(script_wire::remote_full(&instances, &snapshot.variables)?)
    }

    /// Prepare the baseline for this body's delivery stream. The body create
    /// acknowledgement still controls when the resulting chunk may be sent.
    pub fn queue_full(
        &self,
        stream: &mut BodyStream<ProjectedSnapshot>,
        snapshot: &ProjectedSnapshot,
    ) -> Result<ScriptChunk, SnapshotError> {
        let payload = self.full(snapshot)?;
        let mut baseline = snapshot.clone();
        if baseline.view == View::Owner {
            for instance in baseline.instances.values_mut() {
                instance.states.clear();
            }
        }
        Ok(stream.full(&payload, baseline)?)
    }

    fn graph(&self, instance: u16, graph: u32) -> Result<&Graph, SnapshotError> {
        self.graphs
            .graph(graph)
            .ok_or(SnapshotError::MissingGraph { instance, graph })
    }

    fn remote_states(
        &self,
        id: u16,
        graph: &Graph,
        instance: &ProjectedInstance,
        wire: &mut Instance,
    ) -> Result<(), SnapshotError> {
        for (slot, &position) in graph.remote_positions().iter().enumerate() {
            let node = graph
                .node(position)
                .expect("compiled remote positions reference nodes");
            let raw = node.state.ok_or(SnapshotError::MissingStateIndex {
                instance: id,
                graph: graph.index,
                node: position,
            })?;
            let Some(state) = instance.states.get(&raw) else {
                continue;
            };
            let site = StateSite {
                instance: id,
                graph: graph.index,
                state: raw,
            };
            let codec = self
                .codecs
                .codec(&node.class)
                .ok_or_else(|| SnapshotError::Payload {
                    site,
                    source: PayloadError::Unverified(node.class.clone()),
                })?;
            let mut bits = BitWriter::new();
            bits.bit(true); // X: present in this full frame
            if codec.stec() {
                bits.bit(true); // Y: this StEC entry has a payload
            }
            bits.bit(state.active);
            let payload = codec
                .payload(&state.payload, PayloadMode::Remote { full: true })
                .map_err(|source| SnapshotError::Payload { site, source })?;
            bits.append(&payload);
            wire.states.insert(slot, bits);
        }
        Ok(())
    }
}

fn server_only_variables(graph: &Graph) -> BTreeSet<u32> {
    graph
        .fields
        .get("m_9CEC6985")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(asset_guid)
        .map(|guid| u32::from(guid as u16))
        .collect()
}

fn replicated_variables(values: &Variables, excluded: &BTreeSet<u32>) -> Variables {
    values
        .iter()
        .filter(|(id, value)| !excluded.contains(*id) && !matches!(*value, Value::Null))
        .map(|(&id, value)| (id, value.clone()))
        .collect()
}

fn presenter(node: &Node) -> bool {
    node.class == "STUStatescriptStateUXPresenter" || node.fields.get("m_45216F79").is_some()
}

fn state_payload(kind: StateKind, codec: &Codec) -> Option<StatePayload> {
    Some(match (kind, codec.codec.as_deref()) {
        (StateKind::Plain, Some("none")) if codec.empty() => StatePayload::new(),
        (StateKind::BooleanSwitch(current), Some("switch")) => {
            StatePayload::from([("current".into(), Value::Bool(current))])
        }
        (StateKind::SubScript { child }, Some("subscript")) => StatePayload::from([(
            "child".into(),
            Value::Integer(i64::from(child.unwrap_or_default())),
        )]),
        (StateKind::MessageIdle, Some("message")) => StatePayload::new(),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::script_init::{
        Diagnostic, EmptyWorld, Issue, MAX_INIT_WORK, Root, Site, initialize,
    };
    use serde_json::{Value as Json, json};

    const ENTITY: u32 = 0xa000_0101;
    const CODECS: &str = r#"{"classes":{
        "12340001":{"name":"STUStatescriptState","codec":"none","stec":false,"status":"VERIFIED","layout":[]},
        "12340002":{"name":"STUStatescriptStateBooleanSwitch","codec":"switch","stec":false,"status":"VERIFIED",
            "layout":[{"op":"bits","f":"current","n":1}]},
        "12340003":{"name":"STU_37D754C8","codec":"subscript","stec":false,"status":"VERIFIED",
            "layout":[{"op":"if","c":"H","then":[{"op":"w_u16","f":"child"},{"op":"bits","f":"flag","n":1}],"else":[]}]},
        "12340004":{"name":"STUStatescriptStateGameMessageEntry","codec":"message","stec":false,"status":"VERIFIED",
            "layout":[{"op":"if","c":"H","then":[{"op":"bits","f":"has_sender","n":1}],"else":[]},
                      {"op":"bits","f":"stacked","n":1}]}
    }}"#;

    fn node(position: u16, class: &str, state: Option<u16>, mut fields: Json) -> Json {
        fields["m_2BBEEAB8"] = json!(state.is_some());
        fields["m_ADEB6E05"] = json!(false);
        json!({
            "pos":position, "class":class, "uid":position, "rid":position,
            "remote":-1, "state":state, "client_only":false, "server_only":false,
            "fields":fields,
        })
    }

    fn graph(
        index: u32,
        nodes: Vec<Json>,
        states: Json,
        entries: Json,
        remote: Json,
        sync: Json,
    ) -> Json {
        json!({
            "index":index, "items":nodes.len(), "nodes":nodes, "states":states,
            "entries":entries, "remote":remote, "sync_vars":sync, "containers":{},
            "unknown_nodes":[], "fields":{"m_9CEC6985":["0x0D800000000001A4"]},
            "header":{
                "m_nodesBitCount":4, "m_statesBitCount":4,
                "m_remoteSyncNodesBitCount":4, "m_syncVarsBitCount":4
            }
        })
    }

    fn fixture() -> (GraphStore, Snapshot) {
        let root = graph(
            42,
            vec![
                node(
                    0,
                    "STUStatescriptEntry",
                    None,
                    json!({
                        "m_outPlug":{"$":"Plug","links":[[1,"m_beginPlug"],[2,"m_beginPlug"],
                            [3,"m_beginPlug"],[4,"m_beginPlug"]],"m_FF3DAF1E":[]}
                    }),
                ),
                node(
                    1,
                    "STUStatescriptStateBooleanSwitch",
                    Some(0),
                    json!({
                        "m_condition":{"$":"STUConfigVarBool","m_value":true}
                    }),
                ),
                node(
                    2,
                    "STU_37D754C8",
                    Some(1),
                    json!({
                        "m_36F7B64C":{"m_36F7B64C":{"m_C974E369":{"m_graph":"0x058000000000002B"}}}
                    }),
                ),
                node(
                    3,
                    "STUStatescriptState",
                    Some(2),
                    json!({"m_45216F79":null}),
                ),
                node(4, "STUStatescriptStateGameMessageEntry", Some(3), json!({})),
            ],
            json!([1, 2, 3, 4]),
            json!([0]),
            json!([null, 3, 1, 2, 4, 1]),
            json!([[100, 0, 0], [100, 0, 0], [200, 1, 0], [null, 0, 0]]),
        );
        let child = graph(43, vec![], json!([]), json!([]), json!([]), json!([]));
        let graphs = GraphStore::from_json(
            &serde_json::to_vec(&json!({
                "version":1, "graphs":[root,child], "bodies":{}, "missing":[]
            }))
            .unwrap(),
        )
        .unwrap();
        let entity = Variables::from([
            (7, Value::Asset(0x0580_0000_0000_0042)),
            (55, Value::Integer(-12)),
            (253, Value::Float(200.0)),
            (420, Value::Integer(666)),
        ]);
        let mut root = Root::new(1, 42);
        root.variables.extend([
            (100, Value::Integer(17)),
            (200, Value::Bool(true)),
            (300, Value::Float(3.5)),
            (400, Value::Null),
            (420, Value::Integer(2)),
        ]);
        let snapshot = initialize(
            &graphs,
            ENTITY,
            entity,
            vec![root],
            &mut EmptyWorld,
            MAX_INIT_WORK,
        )
        .unwrap();
        assert!(
            snapshot.initialization_complete(),
            "{:?}",
            snapshot.diagnostics
        );
        (graphs, snapshot)
    }

    fn check(bits: &BitWriter, length: usize, hex: &str) {
        let actual: String = bits
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!((bits.bit_len(), actual.as_str()), (length, hex));
    }

    #[test]
    fn baseline_matches_python_for_both_views_and_enters_the_acknowledged_stream() {
        let (graphs, snapshot) = fixture();
        let codecs = CodecTable::from_json(CODECS).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        let owner = encoder.project(&snapshot, View::Owner).unwrap();
        let observer = encoder.project(&snapshot, View::Observer).unwrap();
        assert_eq!(owner.entity(), ENTITY);
        assert_eq!(owner.instance_count(), 2);
        assert_eq!(owner.instances[&2].parent, Some((1, 1)));
        assert_eq!(owner.variables.keys().copied().collect::<Vec<_>>(), [7, 55]);
        assert_eq!(
            owner.instances[&1]
                .variables
                .keys()
                .copied()
                .collect::<Vec<_>>(),
            [100, 200, 300]
        );
        assert!(owner.instances[&1].states.contains_key(&2));
        assert!(!observer.instances[&1].states.contains_key(&2));

        check(
            &encoder.full(&owner).unwrap(),
            250,
            "081500102b00142000e820031610e246e8012811840cc5128000006040000000",
        );
        check(
            &encoder.full(&observer).unwrap(),
            257,
            "081500102b00142000e820031610e246e8012811840cc512800000604000f83b00",
        );
        let mut stream = BodyStream::new(ENTITY);
        let chunk = encoder.queue_full(&mut stream, &owner).unwrap();
        assert_eq!(chunk.update.entity, ENTITY);
        assert!(chunk.resend);
        assert!(!stream.ready_to_send(chunk.last));
        assert!(stream.base().is_none());
        check(
            chunk.update.chunk.as_ref().unwrap(),
            286,
            "000004a08f500100b102400102800e326001216e841e801241c8502c0108000006040000",
        );
        stream.create_arrived();
        assert!(stream.ready_to_send(chunk.last));
        assert!(stream.arrived(chunk.last).advanced);
        let (_, base) = stream.base().unwrap();
        assert_eq!(base.view(), View::Owner);
        assert!(base.instances.values().all(|item| item.states.is_empty()));
        assert!(!owner.instances[&1].states.is_empty());
    }

    #[test]
    fn unsupported_active_states_and_initialization_diagnostics_are_explicit() {
        let (graphs, mut snapshot) = fixture();
        let mut raw: Json = serde_json::from_str(CODECS).unwrap();
        raw["classes"].as_object_mut().unwrap().remove("12340001");
        let codecs = CodecTable::from_json(&raw.to_string()).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        assert!(matches!(
            encoder.project(&snapshot, View::Owner),
            Err(SnapshotError::UnsupportedState {
                site: StateSite {
                    instance: 1,
                    graph: 42,
                    state: 2
                },
                ..
            })
        ));
        assert!(encoder.project(&snapshot, View::Observer).is_ok());
        snapshot.diagnostics.push(Diagnostic {
            site: Site {
                entity: ENTITY,
                instance: 1,
                graph: 42,
                node: None,
            },
            issue: Issue::UnsupportedAction("fixture".into()),
        });
        assert!(matches!(
            encoder.project(&snapshot, View::Observer),
            Err(SnapshotError::Incomplete { entity: ENTITY, diagnostics: 1, first })
                if first.contains("fixture")
        ));
    }

    #[test]
    #[ignore = "requires the separately prepared 104319 graph and codec tables"]
    fn actual_shared_ui_graph_baselines_match_python() {
        use sha2::{Digest, Sha256};
        use std::path::{Path, PathBuf};

        let root = std::env::var_os("ORPHEUS_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).to_owned());
        let graphs = GraphStore::load(root.join("data/game_graphs_174.json.gz")).unwrap();
        let codecs = CodecTable::load(root.join("data/statescript_codecs_174.json")).unwrap();
        let encoder = SnapshotEncoder::new(&graphs, &codecs);
        for (graph, expected_owner, expected_observer) in [
            (
                0x20e4,
                "8b5be8f21a99210445094b65c6ba62621a489da48170c97ef9062a99127ec0f6",
                "7e3658bf64c37a631561b21b33fff4f68119e038896d153ee84d7f4cb6e29649",
            ),
            (
                0x0cb6,
                "6de347e8a7097efa974f5fc61cce965c69998e849c11c11a3c96165dd8408f48",
                "e62bc520a0acf2f9064de7de9b053ba46c3861c4b4c481f606e357560eca6eea",
            ),
        ] {
            let snapshot = initialize(
                &graphs,
                ENTITY,
                Variables::new(),
                vec![Root::new(1, graph)],
                &mut EmptyWorld,
                MAX_INIT_WORK,
            )
            .unwrap();
            assert!(
                snapshot.initialization_complete(),
                "{:?}",
                snapshot.diagnostics
            );
            for (view, expected_bits, expected) in [
                (View::Owner, 37, expected_owner),
                (View::Observer, 38, expected_observer),
            ] {
                let projection = encoder.project(&snapshot, view).unwrap();
                let frame = encoder.full(&projection).unwrap();
                assert_eq!(frame.bit_len(), expected_bits);
                assert_eq!(format!("{:x}", Sha256::digest(frame.as_bytes())), expected);
            }
        }
    }
}
