//! Prepared static collision geometry for retail build 104319.
//!
//! Both movement and expanded ray sidecars use the version-5 format in
//! `ow174/game/collision.py`. This module decodes the geometry and metadata.
//! Construction of contact worlds and spatial queries happens after loading.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::Path;

use lzma_rust2::XzReader;
use serde::{Deserialize, Deserializer, de};
use serde_json::Value;

pub const MAGIC: &[u8; 8] = b"OW174COL";
pub const VERSION: u32 = 5;
pub const RAY_GEOMETRY: &str = "expanded-static-rays";
pub const RAY_PROP_SCOPE: &str = "indexed-static-models";
pub const RAY_ROOT_PROP_SCOPE: &str = "indexed-and-authenticated-roots";
pub const ALL_CATEGORIES: u16 = 0xffff;

const MAX_HEADER_BYTES: usize = 1 << 20;
const MAX_COMPRESSED_BYTES: u64 = 128 << 20;
const MAX_GEOMETRY_BYTES: usize = 128 << 20;
const XZ_MEMORY_KIB: u32 = 64 * 1024;
const MAX_TOP_LEVEL_RECORDS: usize = 1 << 16;
const MAX_MODEL_SHAPES: usize = 1 << 16;

#[derive(Debug)]
pub enum CacheError {
    Io(io::Error),
    Magic,
    Version(u32),
    HeaderLength(u32),
    Header(serde_json::Error),
    CountLimit,
    Compression(io::Error),
    GeometryLimit,
    TruncatedGeometry,
    InvalidGeometry(&'static str),
    RayUsedForMovement,
    RayIdentity(u64),
    RayPropIdentity(u64),
}

impl fmt::Display for CacheError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => fmt::Display::fmt(error, f),
            Self::Magic => f.write_str("not a collision cache file"),
            Self::Version(version) => write!(f, "cache version {version}, expected {VERSION}"),
            Self::HeaderLength(length) => write!(f, "invalid collision header length {length}"),
            Self::Header(error) => write!(f, "invalid collision header: {error}"),
            Self::CountLimit => f.write_str("collision cache has too many records"),
            Self::Compression(error) => write!(f, "invalid XZ collision payload: {error}"),
            Self::GeometryLimit => f.write_str("collision payload exceeds the geometry limit"),
            Self::TruncatedGeometry => f.write_str("truncated collision geometry"),
            Self::InvalidGeometry(reason) => write!(f, "invalid collision geometry: {reason}"),
            Self::RayUsedForMovement => {
                f.write_str("a ray geometry cache cannot be loaded as a movement world")
            }
            Self::RayIdentity(map) => {
                write!(f, "expanded ray geometry does not match map {map:#x}")
            }
            Self::RayPropIdentity(map) => {
                write!(
                    f,
                    "expanded ray prop provenance does not match map {map:#x}"
                )
            }
        }
    }
}

impl Error for CacheError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) | Self::Compression(error) => Some(error),
            Self::Header(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for CacheError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct CacheHeader {
    #[serde(default)]
    pub name: String,
    /// Ordered pairs of vertex and triangle counts for each core mesh.
    pub meshes: Vec<[usize; 2]>,
    pub hulls: usize,
    pub models: usize,
    pub placements: usize,
    #[serde(deserialize_with = "hex_identifiers")]
    pub identifiers: Vec<u64>,
    /// The input catalog's provenance fields, retained without reinterpretation.
    #[serde(flatten)]
    pub source: BTreeMap<String, Value>,
}

fn hex_identifiers<'de, D: Deserializer<'de>>(input: D) -> Result<Vec<u64>, D::Error> {
    Vec::<String>::deserialize(input)?
        .into_iter()
        .map(|id| {
            let hex = id
                .strip_prefix("0x")
                .or_else(|| id.strip_prefix("0X"))
                .unwrap_or(&id);
            u64::from_str_radix(hex, 16).map_err(de::Error::custom)
        })
        .collect()
}

/// Identity from the external runtime's collision and prop catalogs.
#[derive(Clone, Copy, Debug)]
pub struct RayCacheIdentity<'a> {
    pub map_guid: u64,
    pub collision_ckey: &'a str,
    pub placement_ckey: Option<&'a str>,
    /// If specified, require the exact authenticated prop-record catalog digest.
    pub prop_records_sha256: Option<&'a str>,
}

pub fn chunk_guid(map_guid: u64) -> u64 {
    0x0dd0_0011_0000_0000 | (map_guid & u64::from(u32::MAX))
}

impl CacheHeader {
    pub fn placement_groups(&self, identifiers: &BTreeSet<u64>) -> BTreeSet<u16> {
        let mut groups = BTreeSet::from([0]);
        for (index, identifier) in self.identifiers.iter().enumerate() {
            if identifiers.contains(identifier) {
                if let Ok(group) = u16::try_from(index + 1) {
                    groups.insert(group);
                }
            }
        }
        groups
    }

    fn text(&self, key: &str) -> Option<&str> {
        self.source.get(key).and_then(Value::as_str)
    }

    pub fn check_movement(&self) -> Result<(), CacheError> {
        if self.text("geometry") == Some(RAY_GEOMETRY) {
            return Err(CacheError::RayUsedForMovement);
        }
        Ok(())
    }

    pub fn check_ray(&self, identity: RayCacheIdentity<'_>) -> Result<(), CacheError> {
        let map = identity.map_guid;
        let scope = self.text("prop_scope");
        if self.text("geometry") != Some(RAY_GEOMETRY)
            || self.source.get("category_mask").and_then(Value::as_u64)
                != Some(u64::from(ALL_CATEGORIES))
            || !matches!(scope, Some(RAY_PROP_SCOPE | RAY_ROOT_PROP_SCOPE))
            || self.text("map") != Some(&format!("0x{map:016X}"))
            || self.text("chunk") != Some(&format!("0x{:016X}", chunk_guid(map)))
            || self.text("ckey") != Some(identity.collision_ckey)
        {
            return Err(CacheError::RayIdentity(map));
        }
        if scope == Some(RAY_ROOT_PROP_SCOPE) {
            let digest = self.text("prop_records_sha256").filter(|digest| {
                digest.len() == 64
                    && digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            });
            if identity.placement_ckey.is_none()
                || self.text("prop_placement_ckey") != identity.placement_ckey
                || digest.is_none()
                || identity
                    .prop_records_sha256
                    .is_some_and(|required| digest != Some(required))
            {
                return Err(CacheError::RayPropIdentity(map));
            }
        } else if identity.prop_records_sha256.is_some() {
            return Err(CacheError::RayPropIdentity(map));
        }
        Ok(())
    }
}

pub fn read_header(input: &mut impl Read) -> Result<CacheHeader, CacheError> {
    let mut prefix = [0; 16];
    if let Err(error) = input.read_exact(&mut prefix) {
        return Err(if error.kind() == io::ErrorKind::UnexpectedEof {
            CacheError::Magic
        } else {
            CacheError::Io(error)
        });
    }
    if &prefix[..8] != MAGIC {
        return Err(CacheError::Magic);
    }
    let version = u32::from_le_bytes(prefix[8..12].try_into().unwrap());
    if version != VERSION {
        return Err(CacheError::Version(version));
    }
    let length = u32::from_le_bytes(prefix[12..].try_into().unwrap());
    if length == 0 || length as usize > MAX_HEADER_BYTES {
        return Err(CacheError::HeaderLength(length));
    }
    let mut text = vec![0; length as usize];
    input.read_exact(&mut text)?;
    let header: CacheHeader = serde_json::from_slice(&text).map_err(CacheError::Header)?;
    if [
        header.meshes.len(),
        header.hulls,
        header.models,
        header.placements,
    ]
    .into_iter()
    .any(|count| count > MAX_TOP_LEVEL_RECORDS)
        || header.identifiers.len() > usize::from(u16::MAX)
    {
        return Err(CacheError::CountLimit);
    }
    Ok(header)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Triangle {
    pub corners: [i32; 3],
    /// Additional vertices for the adjacent faces. -1 means no wing.
    pub wings: [i32; 3],
    pub mask: u16,
    pub surface: u16,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Mesh {
    pub vertices: Vec<[f32; 3]>,
    pub triangles: Vec<Triangle>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HalfEdge {
    pub twin: i8,
    pub origin: u8,
    pub face: u8,
    pub next: u8,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Hull {
    pub mask: u16,
    pub centroid: [f32; 3],
    pub vertices: Vec<[f32; 3]>,
    pub planes: Vec<[f32; 4]>,
    pub faces: Vec<u8>,
    pub half_edges: Vec<HalfEdge>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Shape {
    Sphere {
        mask: u16,
        center: [f32; 3],
        radius: f32,
    },
    Capsule {
        mask: u16,
        a: [f32; 3],
        b: [f32; 3],
        radius: f32,
    },
    Hull(Hull),
    Mesh {
        mask: u16,
        mesh: Mesh,
    },
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Placement {
    pub model: u32,
    pub group: u16,
    pub translation: [f32; 3],
    pub scale: [f32; 3],
    pub rotation: [f32; 4],
}

#[derive(Clone, Debug, PartialEq)]
pub struct CollisionCache {
    pub header: CacheHeader,
    pub meshes: Vec<Mesh>,
    pub hulls: Vec<Hull>,
    pub models: Vec<Vec<Shape>>,
    pub placements: Vec<Placement>,
}

pub fn load_cache(path: impl AsRef<Path>) -> Result<CollisionCache, CacheError> {
    read_cache(BufReader::new(File::open(path)?))
}

pub fn read_cache(mut input: impl Read) -> Result<CollisionCache, CacheError> {
    let header = read_header(&mut input)?;
    decode(input, header)
}

pub fn read_movement_cache(mut input: impl Read) -> Result<CollisionCache, CacheError> {
    let header = read_header(&mut input)?;
    header.check_movement()?;
    decode(input, header)
}

pub fn read_ray_cache(
    mut input: impl Read,
    identity: RayCacheIdentity<'_>,
) -> Result<CollisionCache, CacheError> {
    let header = read_header(&mut input)?;
    header.check_ray(identity)?;
    decode(input, header)
}

fn decode(input: impl Read, header: CacheHeader) -> Result<CollisionCache, CacheError> {
    let mut payload = Vec::new();
    let mut compressed = input.take(MAX_COMPRESSED_BYTES + 1);
    let decoder = XzReader::new_mem_limit(&mut compressed, true, XZ_MEMORY_KIB);
    decoder
        .take((MAX_GEOMETRY_BYTES + 1) as u64)
        .read_to_end(&mut payload)
        .map_err(CacheError::Compression)?;
    if payload.len() > MAX_GEOMETRY_BYTES || compressed.limit() == 0 {
        return Err(CacheError::GeometryLimit);
    }
    geometry(header, &payload)
}

struct Geometry<'a>(&'a [u8]);

impl<'a> Geometry<'a> {
    fn require(&self, size: usize) -> Result<(), CacheError> {
        if size > self.0.len() {
            return Err(CacheError::TruncatedGeometry);
        }
        Ok(())
    }

    fn take(&mut self, size: usize) -> Result<&'a [u8], CacheError> {
        let (head, tail) = self
            .0
            .split_at_checked(size)
            .ok_or(CacheError::TruncatedGeometry)?;
        self.0 = tail;
        Ok(head)
    }

    fn bytes<const N: usize>(&mut self) -> Result<[u8; N], CacheError> {
        Ok(self.take(N)?.try_into().unwrap())
    }

    fn u8(&mut self) -> Result<u8, CacheError> {
        Ok(self.bytes::<1>()?[0])
    }

    fn u16(&mut self) -> Result<u16, CacheError> {
        Ok(u16::from_le_bytes(self.bytes()?))
    }

    fn u32(&mut self) -> Result<u32, CacheError> {
        Ok(u32::from_le_bytes(self.bytes()?))
    }

    fn index3(&mut self) -> Result<[i32; 3], CacheError> {
        Ok([
            i32::from_le_bytes(self.bytes()?),
            i32::from_le_bytes(self.bytes()?),
            i32::from_le_bytes(self.bytes()?),
        ])
    }

    fn f32(&mut self) -> Result<f32, CacheError> {
        Ok(f32::from_le_bytes(self.bytes()?))
    }

    fn point(&mut self) -> Result<[f32; 3], CacheError> {
        Ok([self.f32()?, self.f32()?, self.f32()?])
    }

    fn quad(&mut self) -> Result<[f32; 4], CacheError> {
        Ok([self.f32()?, self.f32()?, self.f32()?, self.f32()?])
    }

    fn mesh(&mut self, vertex_count: usize, triangle_count: usize) -> Result<Mesh, CacheError> {
        self.require(mesh_bytes(vertex_count, triangle_count))?;
        let vertices = (0..vertex_count)
            .map(|_| self.point())
            .collect::<Result<_, _>>()?;
        let mut triangles = Vec::with_capacity(triangle_count);
        for _ in 0..triangle_count {
            let corners = self.index3()?;
            let wings = self.index3()?;
            let valid = |index| usize::try_from(index).is_ok_and(|index| index < vertex_count);
            if !corners.into_iter().all(valid)
                || !wings.into_iter().all(|index| index == -1 || valid(index))
            {
                return Err(CacheError::InvalidGeometry(
                    "triangle vertex index out of range",
                ));
            }
            triangles.push(Triangle {
                corners,
                wings,
                mask: 0,
                surface: 0,
            });
        }
        for triangle in &mut triangles {
            triangle.mask = self.u16()?;
        }
        for triangle in &mut triangles {
            triangle.surface = self.u16()?;
        }
        Ok(Mesh {
            vertices,
            triangles,
        })
    }

    fn hull(&mut self) -> Result<Hull, CacheError> {
        let mask = self.u16()?;
        let vertex_count = usize::from(self.u16()?);
        let face_count = usize::from(self.u16()?);
        let edge_count = usize::from(self.u16()?);
        self.require(12 + 12 * vertex_count + 17 * face_count + 4 * edge_count)?;
        let centroid = self.point()?;
        let vertices = (0..vertex_count)
            .map(|_| self.point())
            .collect::<Result<_, _>>()?;
        let planes = (0..face_count)
            .map(|_| self.quad())
            .collect::<Result<_, _>>()?;
        let faces = self.take(face_count)?.to_vec();
        if faces.iter().any(|&edge| usize::from(edge) >= edge_count) {
            return Err(CacheError::InvalidGeometry(
                "hull face points outside its edges",
            ));
        }
        let mut half_edges = Vec::with_capacity(edge_count);
        for _ in 0..edge_count {
            let [twin, origin, face, next] = self.bytes()?;
            if usize::from(origin) >= vertex_count
                || usize::from(face) >= face_count
                || usize::from(next) >= edge_count
            {
                return Err(CacheError::InvalidGeometry("hull edge index out of range"));
            }
            half_edges.push(HalfEdge {
                twin: twin as i8,
                origin,
                face,
                next,
            });
        }
        Ok(Hull {
            mask,
            centroid,
            vertices,
            planes,
            faces,
            half_edges,
        })
    }

    fn shape(&mut self) -> Result<Shape, CacheError> {
        Ok(match self.u8()? {
            0 => Shape::Sphere {
                mask: self.u16()?,
                center: self.point()?,
                radius: self.f32()?,
            },
            1 => Shape::Capsule {
                mask: self.u16()?,
                a: self.point()?,
                b: self.point()?,
                radius: self.f32()?,
            },
            2 => Shape::Hull(self.hull()?),
            3 => {
                let mask = self.u16()?;
                let vertex_count = self.u32()? as usize;
                let triangle_count = self.u32()? as usize;
                Shape::Mesh {
                    mask,
                    mesh: self.mesh(vertex_count, triangle_count)?,
                }
            }
            _ => return Err(CacheError::InvalidGeometry("unknown model shape kind")),
        })
    }

    fn model(&mut self, remaining_shapes: &mut usize) -> Result<Vec<Shape>, CacheError> {
        let count = usize::from(self.u16()?);
        if count > *remaining_shapes {
            return Err(CacheError::CountLimit);
        }
        self.require(11 * count)?; // A mesh header is the smallest shape.
        let model = (0..count).map(|_| self.shape()).collect::<Result<_, _>>()?;
        *remaining_shapes -= count;
        Ok(model)
    }

    fn placement(
        &mut self,
        model_count: usize,
        identifier_count: usize,
    ) -> Result<Placement, CacheError> {
        let model = self.u32()?;
        let group = self.u16()?;
        if model as usize >= model_count || usize::from(group) > identifier_count {
            return Err(CacheError::InvalidGeometry(
                "placement model or group index out of range",
            ));
        }
        Ok(Placement {
            model,
            group,
            translation: self.point()?,
            scale: self.point()?,
            rotation: self.quad()?,
        })
    }
}

fn mesh_bytes(vertices: usize, triangles: usize) -> usize {
    vertices
        .saturating_mul(12)
        .saturating_add(triangles.saturating_mul(28))
}

fn geometry(header: CacheHeader, payload: &[u8]) -> Result<CollisionCache, CacheError> {
    let mut raw = Geometry(payload);
    let minimum = header.meshes.iter().fold(
        20 * header.hulls + 2 * header.models + 46 * header.placements,
        |sum, &[vertices, triangles]| sum.saturating_add(mesh_bytes(vertices, triangles)),
    );
    raw.require(minimum)?;
    let meshes = header
        .meshes
        .iter()
        .map(|&[v, t]| raw.mesh(v, t))
        .collect::<Result<_, _>>()?;
    let hulls = (0..header.hulls)
        .map(|_| raw.hull())
        .collect::<Result<_, _>>()?;
    let mut remaining_shapes = MAX_MODEL_SHAPES;
    let models = (0..header.models)
        .map(|_| raw.model(&mut remaining_shapes))
        .collect::<Result<_, _>>()?;
    let placements = (0..header.placements)
        .map(|_| raw.placement(header.models, header.identifiers.len()))
        .collect::<Result<_, _>>()?;
    if !raw.0.is_empty() {
        return Err(CacheError::InvalidGeometry(
            "trailing bytes after placements",
        ));
    }
    Ok(CollisionCache {
        header,
        meshes,
        hulls,
        models,
        placements,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn encode_header(version: u32, header: &Value) -> Vec<u8> {
        let text = serde_json::to_vec(header).unwrap();
        [
            MAGIC.as_slice(),
            &version.to_le_bytes(),
            &(text.len() as u32).to_le_bytes(),
            &text,
        ]
        .concat()
    }

    fn header() -> Value {
        json!({
            "name": "synthetic",
            "meshes": [[3, 1]],
            "hulls": 1,
            "models": 1,
            "placements": 2,
            "identifiers": ["0x0000000000001234"],
            "triangles": 1,
        })
    }

    fn xz_fixture() -> Vec<u8> {
        // Python ow174.game.collision.pack on one synthetic core mesh, one hull,
        // a model with all four shapes, and two placements in different groups.
        from_hex(concat!(
            "fd377a585a000004e6d6b4460200210116000000742fe5a3e001c500745d00006a1e7887fb7a057c8",
            "db04fc223979e5f12a7306ff1a8a3fea0fc89001c99e6daca58363febe5419c7ff8591742366c9c",
            "ca413e871c52b1a550b2af829b5e51daa7e32a60cd0dd556a402906e5c8c57be215ed97735b6b909",
            "e80ffca480e467bb34ec336694df03247c81e7ca35841bb0b88d00009046f7a631c73eda00019001",
            "c6030000ade433b2b1c467fb020000000004595a",
        ))
    }

    fn from_hex(hex: &str) -> Vec<u8> {
        hex.as_bytes()
            .chunks_exact(2)
            .map(|bytes| u8::from_str_radix(std::str::from_utf8(bytes).unwrap(), 16).unwrap())
            .collect()
    }

    #[test]
    fn reads_python_xz_geometry_without_changing_float_bits_or_order() {
        let blob = [encode_header(VERSION, &header()), xz_fixture()].concat();
        let cache = read_movement_cache(blob.as_slice()).unwrap();
        let mesh = &cache.meshes[0];
        assert_eq!(cache.header.name, "synthetic");
        assert_eq!(
            cache.header.placement_groups(&BTreeSet::new()),
            BTreeSet::from([0])
        );
        assert_eq!(
            cache.header.placement_groups(&BTreeSet::from([0x1234])),
            BTreeSet::from([0, 1])
        );
        assert_eq!(
            mesh.vertices[0].map(f32::to_bits),
            [0, 0x8000_0000, 0x3f80_0000]
        );
        assert_eq!(mesh.vertices[1..], [[2.0, 3.0, 4.0], [-5.0, 6.0, 7.0]]);
        assert_eq!(
            mesh.triangles,
            [Triangle {
                corners: [2, 1, 0],
                wings: [1, -1, -1],
                mask: 0x2008,
                surface: 0x1234,
            }]
        );
        let hull = &cache.hulls[0];
        assert_eq!(hull.mask, 0x10);
        assert_eq!(hull.centroid, [2.0, 3.0, 4.0]);
        assert_eq!(hull.planes, [[0.0, 1.0, 0.0, -2.0]]);
        assert_eq!(hull.faces, [0]);
        assert_eq!(
            hull.half_edges[0],
            HalfEdge {
                twin: -1,
                origin: 0,
                face: 0,
                next: 1
            }
        );
        assert_eq!(
            cache.models[0],
            [
                Shape::Sphere {
                    mask: 8,
                    center: [1.0, 2.0, 3.0],
                    radius: 0.5
                },
                Shape::Capsule {
                    mask: 16,
                    a: [1.0, 1.5, 2.0],
                    b: [3.0, 4.0, 5.0],
                    radius: 0.25
                },
                Shape::Hull(hull.clone()),
                Shape::Mesh {
                    mask: 0x18,
                    mesh: mesh.clone()
                },
            ]
        );
        assert_eq!(
            cache.placements[0],
            Placement {
                model: 0,
                group: 0,
                translation: [1.0, 2.0, 3.0],
                scale: [1.0; 3],
                rotation: [0.0, 0.0, 0.0, 1.0],
            }
        );
        assert_eq!(
            cache.placements[1],
            Placement {
                model: 0,
                group: 1,
                translation: [3.0, 2.0, 1.0],
                scale: [2.0; 3],
                rotation: [0.0, 1.0, 0.0, 0.0],
            }
        );
    }

    #[test]
    fn rejects_other_versions_oversize_headers_and_truncated_compression() {
        for version in [2, 3, 4, 6] {
            let blob = encode_header(version, &header());
            assert!(
                matches!(read_cache(blob.as_slice()), Err(CacheError::Version(found)) if found == version)
            );
        }
        let mut blob = encode_header(VERSION, &header());
        blob[12..16].copy_from_slice(&(MAX_HEADER_BYTES as u32 + 1).to_le_bytes());
        assert!(matches!(
            read_cache(blob.as_slice()),
            Err(CacheError::HeaderLength(_))
        ));
        let mut metadata = header();
        metadata["placements"] = json!(MAX_TOP_LEVEL_RECORDS + 1);
        let blob = encode_header(VERSION, &metadata);
        assert!(matches!(
            read_cache(blob.as_slice()),
            Err(CacheError::CountLimit)
        ));

        let mut blob = [encode_header(VERSION, &header()), xz_fixture()].concat();
        blob.truncate(blob.len() - 6);
        assert!(matches!(
            read_cache(blob.as_slice()),
            Err(CacheError::Compression(_))
        ));
    }

    #[test]
    fn rejects_xz_dictionaries_above_the_working_memory_limit() {
        // Synthetic XZ stream with the LZMA2 dictionary changed to 256 MiB
        // and the block header CRC recalculated.
        let xz = from_hex(concat!(
            "fd377a585a000004e6d6b44602002101200000000988a57601000468656c6c6f00000000b137b9d",
            "be5da1e9b00011d05b82d80af1fb6f37d010000000004595a",
        ));
        let blob = [encode_header(VERSION, &header()), xz].concat();
        assert!(matches!(
            read_cache(blob.as_slice()),
            Err(CacheError::Compression(error)) if error.kind() == io::ErrorKind::OutOfMemory
        ));
    }

    #[test]
    fn geometry_checks_references_and_lengths_before_allocating() {
        let mut raw = Geometry(&[0; 64]);
        assert!(matches!(
            raw.mesh(usize::MAX, 1),
            Err(CacheError::TruncatedGeometry)
        ));
        let mut raw = [0; 40];
        raw[12] = 2; // First corner of the mesh with just one vertex.
        assert!(matches!(
            Geometry(&raw).mesh(1, 1),
            Err(CacheError::InvalidGeometry(_))
        ));
        assert!(matches!(
            Geometry(&[9; 12]).shape(),
            Err(CacheError::InvalidGeometry(_))
        ));
        assert!(matches!(
            Geometry(&[0; 46]).placement(0, 0),
            Err(CacheError::InvalidGeometry(_))
        ));
    }

    #[test]
    fn only_expected_ray_provenance_can_load_a_sidecar() {
        const MAP: u64 = 0x0800_0000_0000_0688;
        let digest = "ab".repeat(32);
        let mut metadata = header();
        metadata["geometry"] = json!(RAY_GEOMETRY);
        metadata["category_mask"] = json!(ALL_CATEGORIES);
        metadata["prop_scope"] = json!(RAY_ROOT_PROP_SCOPE);
        metadata["map"] = json!("0x0800000000000688");
        metadata["chunk"] = json!("0x0DD0001100000688");
        metadata["ckey"] = json!("core key");
        metadata["prop_placement_ckey"] = json!("prop key");
        metadata["prop_records_sha256"] = json!(digest);
        let blob = [encode_header(VERSION, &metadata), xz_fixture()].concat();
        let required = RayCacheIdentity {
            map_guid: MAP,
            collision_ckey: "core key",
            placement_ckey: Some("prop key"),
            prop_records_sha256: Some(&digest),
        };
        assert!(matches!(
            read_movement_cache(blob.as_slice()),
            Err(CacheError::RayUsedForMovement)
        ));
        assert_eq!(
            read_ray_cache(blob.as_slice(), required)
                .unwrap()
                .placements
                .len(),
            2
        );
        assert!(matches!(
            read_ray_cache(
                blob.as_slice(),
                RayCacheIdentity {
                    map_guid: MAP + 1,
                    ..required
                }
            ),
            Err(CacheError::RayIdentity(_))
        ));
        assert!(matches!(
            read_ray_cache(
                blob.as_slice(),
                RayCacheIdentity {
                    placement_ckey: None,
                    ..required
                }
            ),
            Err(CacheError::RayPropIdentity(_))
        ));
        metadata["prop_records_sha256"] = json!(digest.to_uppercase());
        let blob = encode_header(VERSION, &metadata);
        assert!(matches!(
            read_ray_cache(blob.as_slice(), required),
            Err(CacheError::RayPropIdentity(_))
        ));
    }
}
