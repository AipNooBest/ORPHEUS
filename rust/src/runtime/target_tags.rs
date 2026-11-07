//! Import authored target tags from DataTool's local entity XML exports.

use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::{self, BufReader, Read},
    path::Path,
};

use flate2::{Compression, bufread::MultiGzDecoder, write::GzEncoder};
use roxmltree::{Document, Node};
use serde_json::Value;

use super::{RuntimeError, at};
use crate::{
    accounts::parse_guid,
    script_graph::{MAX_GRAPH_BYTES, TARGET_TAG_TYPE},
};

const DRAGONML: &str = "https://yretenai.com/dragonml/v1";
const ENTITY_TYPE: u64 = 0x400;
const RESOURCE_INDEX_MASK: u64 = (1 << 48) - 1;
const MAX_XML_BYTES: u64 = 16 * 1024 * 1024;

pub(super) fn enrich(input: &Path, xml_dir: &Path) -> Result<Vec<u8>, RuntimeError> {
    if !at(xml_dir, fs::metadata(xml_dir))?.is_dir() {
        return at(
            xml_dir,
            Err(invalid("expected a DataTool .003 XML directory")),
        );
    }

    let decoder = MultiGzDecoder::new(BufReader::new(at(input, File::open(input))?));
    let mut bytes = Vec::new();
    at(
        input,
        decoder.take(MAX_GRAPH_BYTES + 1).read_to_end(&mut bytes),
    )?;
    if bytes.len() as u64 > MAX_GRAPH_BYTES {
        return at(
            input,
            Err(invalid("state graph bundle exceeds the size limit")),
        );
    }
    let mut bundle: Value = at(input, serde_json::from_slice(&bytes).map_err(invalid))?;
    drop(bytes);
    if bundle.get("version").and_then(Value::as_u64) != Some(1) {
        return at(
            input,
            Err(invalid("unsupported state graph bundle version")),
        );
    }

    for group in ["bodies", "entities"] {
        let Some(records) = bundle.get_mut(group) else {
            if group == "entities" {
                continue;
            }
            return at(input, Err(invalid("state graph bundle has no bodies")));
        };
        let records = at(
            input,
            records
                .as_object_mut()
                .ok_or_else(|| invalid("expected entity records")),
        )?;
        for (key, record) in records {
            let index = at(
                input,
                parse_guid(key)
                    .filter(|guid| (guid >> 48) & 0xfff == ENTITY_TYPE)
                    .map(|guid| guid & RESOURCE_INDEX_MASK)
                    .ok_or_else(|| invalid(format!("invalid .003 entity GUID {key}"))),
            )?;
            let path = xml_dir.join(format!("{index:012X}.003.xml"));
            if let Some(tags) = at(&path, read_tags(&path))? {
                let record = at(
                    input,
                    record
                        .as_object_mut()
                        .ok_or_else(|| invalid("expected an entity record")),
                )?;
                record.insert("target_tags".into(), Value::Array(tags));
            }
        }
    }

    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    at(
        input,
        serde_json::to_writer(&mut encoder, &bundle).map_err(invalid),
    )?;
    at(input, encoder.finish())
}

fn read_tags(path: &Path) -> io::Result<Option<Vec<Value>>> {
    let input = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let mut xml = String::new();
    input.take(MAX_XML_BYTES + 1).read_to_string(&mut xml)?;
    if xml.len() as u64 > MAX_XML_BYTES {
        return Err(invalid("entity XML exceeds the size limit"));
    }
    let document = Document::parse(&xml).map_err(invalid)?;
    let tags = entity_tags(document.root_element())?;
    Ok(Some(
        tags.into_iter()
            .map(|tag| Value::String(format!("0x{tag:016X}")))
            .collect(),
    ))
}

fn entity_tags(root: Node<'_, '_>) -> io::Result<BTreeSet<u64>> {
    if !root.has_tag_name("STUEntityDefinition") {
        return Err(invalid("expected a STUEntityDefinition root"));
    }
    let mut tags = BTreeSet::new();
    let Some(map) = named_field(root, "m_componentMap")? else {
        return Ok(tags);
    };
    if !map.has_tag_name((DRAGONML, "map")) {
        return Err(invalid("expected a DragonML component map"));
    }
    let Some(entry) = only_child(map, |node| node.has_tag_name("STUTargetTagComponent"))? else {
        return Ok(tags);
    };
    let component = named_field(entry, "dragon:value")?
        .filter(|node| node.has_tag_name("STUTargetTagComponent"))
        .ok_or_else(|| invalid("expected a target tag component value"))?;
    let Some(array) = named_field(component, "m_targetTags")? else {
        return Ok(tags);
    };
    if !array.has_tag_name((DRAGONML, "array")) {
        return Err(invalid("expected a DragonML target tag array"));
    }
    for node in array.children().filter(Node::is_element) {
        let guid = node
            .attribute("GUID")
            .filter(|_| {
                node.has_tag_name((DRAGONML, "ref"))
                    && node.attribute("Target") == Some("STUTargetTag")
            })
            .ok_or_else(|| invalid("expected a target tag resource reference"))?;
        if guid == "000000000000.001" {
            continue;
        }
        let (index, kind) = guid
            .split_once('.')
            .ok_or_else(|| invalid("invalid target tag GUID"))?;
        if !(1..=12).contains(&index.len())
            || !index.bytes().all(|byte| byte.is_ascii_hexdigit())
            || !kind.eq_ignore_ascii_case("06C")
        {
            return Err(invalid(format!("invalid .06C target tag GUID {guid}")));
        }
        let index = u64::from_str_radix(index, 16).map_err(invalid)?;
        tags.insert((TARGET_TAG_TYPE << 48) | index);
    }
    Ok(tags)
}

fn named_field<'a, 'input: 'a>(
    parent: Node<'a, 'input>,
    name: &str,
) -> io::Result<Option<Node<'a, 'input>>> {
    let child = only_child(parent, |node| {
        node.attribute((DRAGONML, "name")) == Some(name)
    })?;
    if parent
        .attribute(name)
        .is_some_and(|value| value != "{null}" || child.is_some())
    {
        return Err(invalid(format!("invalid DragonML field {name}")));
    }
    Ok(child)
}

fn only_child<'a, 'input: 'a>(
    parent: Node<'a, 'input>,
    mut matches: impl FnMut(Node<'a, 'input>) -> bool,
) -> io::Result<Option<Node<'a, 'input>>> {
    let mut children = parent
        .children()
        .filter(|node| node.is_element() && matches(*node));
    let result = children.next();
    if children.next().is_some() {
        return Err(invalid("duplicate DragonML target tag field"));
    }
    Ok(result)
}

fn invalid(error: impl Into<Box<dyn std::error::Error + Send + Sync>>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}
