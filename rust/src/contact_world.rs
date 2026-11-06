//! Ordered static proxies and scene sweeps for retail movement collision.
//!
//! Ports `engine_world`, `StaticWorld`, and `scene_sweep` from the Python
//! reference. The character body and its per-shape contact state live above
//! this immutable geometry.

use std::collections::{BTreeSet, HashMap, HashSet, hash_map::Entry};
use std::error::Error;
use std::fmt;
use std::sync::Arc;

use crate::collision_cache::{CacheError, CollisionCache, Hull, Mesh, Placement, Shape};
use crate::collision_grid::{ColumnGrid, ColumnRange};
use crate::contact_gjk::PointSetSize;
use crate::contact_gjk::sweep::{
    CastFrame, CastOptions, CastShape, MeshProbe, ShapeHit, mesh_sweep, shape_cast,
};
use crate::contact_math::{ContactFrame, Vec3, add, maxps, minps, mul, scale, sub};
use crate::contact_mesh::{ContactBounds, ContactMesh, ContactMeshError};
use crate::contact_round::{RoundObstacle, RoundShape};
use crate::contact_triangle::SKIN;

const STATIC_COLUMN_WIDTH: f64 = 4.0;
const HUGE_PROXY_COLUMNS: u64 = 1024;
pub(crate) const PROXY_MARGIN: f32 = 0.1;
const SWEEP_REACH: f64 = 0.05;

#[derive(Debug)]
pub enum ContactWorldError {
    Cache(CacheError),
    Mesh(ContactMeshError),
    InvalidGeometry,
    IndexLimit,
}

impl fmt::Display for ContactWorldError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Cache(error) => fmt::Display::fmt(error, f),
            Self::Mesh(error) => fmt::Display::fmt(error, f),
            Self::InvalidGeometry => f.write_str("invalid static contact geometry or frame"),
            Self::IndexLimit => {
                f.write_str("static contact proxies exceed the collision grid limit")
            }
        }
    }
}

impl Error for ContactWorldError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Cache(error) => Some(error),
            Self::Mesh(error) => Some(error),
            _ => None,
        }
    }
}

impl From<CacheError> for ContactWorldError {
    fn from(value: CacheError) -> Self {
        Self::Cache(value)
    }
}

impl From<ContactMeshError> for ContactWorldError {
    fn from(value: ContactMeshError) -> Self {
        Self::Mesh(value)
    }
}

#[derive(Clone)]
pub enum StaticGeometry {
    Mesh(Arc<ContactMesh>),
    Hull(Arc<Hull>),
    Round { shape: RoundShape, mask: u16 },
}

impl StaticGeometry {
    pub fn mask(&self) -> u16 {
        match self {
            Self::Mesh(mesh) => mesh.mask,
            Self::Hull(hull) => hull.mask,
            Self::Round { mask, .. } => *mask,
        }
    }
}

pub struct StaticShape {
    geometry: StaticGeometry,
    frame: Option<ContactFrame>,
    matrix: Option<CastFrame>,
    fat: Option<ContactBounds>,
}

impl StaticShape {
    pub fn new(
        geometry: StaticGeometry,
        frame: Option<ContactFrame>,
    ) -> Result<Self, ContactWorldError> {
        if frame.is_some_and(|frame| {
            !frame
                .translation
                .into_iter()
                .chain(frame.rotation)
                .all(f32::is_finite)
        }) {
            return Err(ContactWorldError::InvalidGeometry);
        }
        let matrix = frame.map(CastFrame::from);
        let fat = shape_bounds(&geometry, frame, matrix).map(|bounds| ContactBounds {
            low: sub(bounds.low, [PROXY_MARGIN; 3]),
            high: add(bounds.high, [PROXY_MARGIN; 3]),
        });
        if fat
            .is_some_and(|bounds| !valid_box(bounds.low.map(f64::from), bounds.high.map(f64::from)))
        {
            return Err(ContactWorldError::InvalidGeometry);
        }
        Ok(Self {
            geometry,
            frame,
            matrix,
            fat,
        })
    }

    pub fn geometry(&self) -> &StaticGeometry {
        &self.geometry
    }

    pub fn frame(&self) -> Option<ContactFrame> {
        self.frame
    }

    pub fn fat_bounds(&self) -> Option<ContactBounds> {
        self.fat
    }
}

fn shape_bounds(
    geometry: &StaticGeometry,
    frame: Option<ContactFrame>,
    matrix: Option<CastFrame>,
) -> Option<ContactBounds> {
    let transform = matrix.unwrap_or_else(|| ContactFrame::IDENTITY.into());
    match geometry {
        StaticGeometry::Hull(hull) => {
            if hull.vertices.is_empty() {
                return None;
            }
            let mut low = [f32::MAX; 3];
            let mut high = [-f32::MAX; 3];
            for &point in &hull.vertices {
                let turned = transform.rotate_only(point);
                low = std::array::from_fn(|k| minps(low[k], turned[k]));
                high = std::array::from_fn(|k| maxps(high[k], turned[k]));
            }
            let translation = frame.map_or([0.0; 3], |frame| frame.translation);
            Some(ContactBounds {
                low: add(sub(low, [SKIN; 3]), translation),
                high: add(add([SKIN; 3], high), translation),
            })
        }
        StaticGeometry::Round {
            shape: RoundShape::Sphere(sphere),
            ..
        } => {
            let center = transform.transform(sphere.center);
            Some(ContactBounds {
                low: sub(center, [sphere.radius; 3]),
                high: add(center, [sphere.radius; 3]),
            })
        }
        StaticGeometry::Round {
            shape: RoundShape::Capsule(capsule),
            ..
        } => {
            let a = transform.transform(capsule.a);
            let b = transform.transform(capsule.b);
            Some(ContactBounds {
                low: std::array::from_fn(|k| minps(a[k], b[k]) - capsule.radius),
                high: std::array::from_fn(|k| maxps(a[k], b[k]) + capsule.radius),
            })
        }
        StaticGeometry::Mesh(mesh) => {
            let bounds = mesh.bounds()?;
            let low = sub(bounds.low, [SKIN; 3]);
            let high = add(bounds.high, [SKIN; 3]);
            let Some(frame) = frame else {
                return Some(ContactBounds { low, high });
            };
            Some(oriented_box(
                ContactBounds { low, high },
                frame,
                absolute_columns(transform),
            ))
        }
    }
}

pub(crate) fn absolute_columns(matrix: CastFrame) -> [Vec3; 3] {
    matrix
        .columns()
        .map(|column| column.map(|x| maxps(0.0 - x, x)))
}

pub(crate) fn oriented_box(
    bounds: ContactBounds,
    frame: ContactFrame,
    [c0, c1, c2]: [Vec3; 3],
) -> ContactBounds {
    let ContactBounds { low, high } = bounds;
    let center = frame.place(scale(add(low, high), 0.5));
    let [hx, hy, hz] = scale(sub(high, low), 0.5);
    let extent = add(add(scale(c1, hy), scale(c0, hx)), scale(c2, hz));
    ContactBounds {
        low: sub(center, extent),
        high: add(extent, center),
    }
}

fn valid_box(low: [f64; 3], high: [f64; 3]) -> bool {
    (0..3).all(|k| low[k].is_finite() && high[k].is_finite() && low[k] <= high[k])
}

fn columns(low: [f64; 3], high: [f64; 3]) -> Option<ColumnRange> {
    ColumnRange::for_width([low[0], low[2]], [high[0], high[2]], STATIC_COLUMN_WIDTH)
}

pub(crate) fn overlaps(low: [f64; 3], high: [f64; 3], bounds: ContactBounds) -> bool {
    (0..3).all(|k| {
        !(high[k] - f64::from(bounds.low[k])).is_sign_negative()
            && !(f64::from(bounds.high[k]) - low[k]).is_sign_negative()
    })
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct WorldHit {
    pub hit: ShapeHit,
    pub shape: u32,
    pub triangle: Option<u32>,
}

pub struct ContactWorld {
    shapes: Vec<StaticShape>,
    grid: ColumnGrid,
}

impl ContactWorld {
    /// Keep the supplied proxy order. It also determines equal-fraction ties.
    pub fn new(shapes: Vec<StaticShape>) -> Result<Self, ContactWorldError> {
        let mut grid = ColumnGrid::with_large_limit(HUGE_PROXY_COLUMNS);
        for (index, shape) in shapes.iter().enumerate() {
            let id = u32::try_from(index).map_err(|_| ContactWorldError::IndexLimit)?;
            let Some(bounds) = shape.fat else {
                continue;
            };
            let range = columns(bounds.low.map(f64::from), bounds.high.map(f64::from))
                .ok_or(ContactWorldError::InvalidGeometry)?;
            grid.insert(id, range)
                .map_err(|_| ContactWorldError::IndexLimit)?;
        }
        Ok(Self { shapes, grid })
    }

    /// Consume the movement cache and retain only the selected placement groups.
    pub fn from_movement_cache(
        cache: CollisionCache,
        identifiers: &BTreeSet<u64>,
    ) -> Result<Self, ContactWorldError> {
        cache.header.check_movement()?;
        let groups = cache.header.placement_groups(identifiers);
        let mut shapes = Vec::new();
        for hull in cache.hulls {
            shapes.push(StaticShape::new(
                StaticGeometry::Hull(Arc::new(hull)),
                None,
            )?);
        }
        for mesh in cache.meshes {
            let mesh = Arc::new(ContactMesh::new(mesh, [1.0; 3], u16::MAX)?);
            shapes.push(StaticShape::new(StaticGeometry::Mesh(mesh), None)?);
        }
        let mut scaled = HashMap::new();
        let mut raw_meshes = HashMap::<(u32, usize), Arc<Mesh>>::new();
        for placement in cache.placements {
            if !groups.contains(&placement.group) {
                continue;
            }
            let model = cache
                .models
                .get(placement.model as usize)
                .ok_or(ContactWorldError::InvalidGeometry)?;
            let frame = ContactFrame {
                translation: placement.translation,
                rotation: placement.rotation,
            };
            for (slot, shape) in model.iter().enumerate() {
                let key = (placement.model, slot, placement.scale.map(f32::to_bits));
                let geometry = match scaled.entry(key) {
                    Entry::Occupied(found) => found.into_mut(),
                    Entry::Vacant(slot_entry) => {
                        slot_entry.insert(placed_geometry(shape, placement, slot, &mut raw_meshes)?)
                    }
                };
                shapes.push(StaticShape::new(geometry.clone(), Some(frame))?);
            }
        }
        Self::new(shapes)
    }

    pub fn shapes(&self) -> &[StaticShape] {
        &self.shapes
    }

    /// Find shapes whose fat bounds overlap the box, in proxy order.
    pub fn query(&self, bounds: ContactBounds) -> Vec<u32> {
        self.query_precise(bounds.low.map(f64::from), bounds.high.map(f64::from))
    }

    fn query_precise(&self, low: [f64; 3], high: [f64; 3]) -> Vec<u32> {
        if !valid_box(low, high) {
            return Vec::new();
        }
        let Some(range) = columns(low, high) else {
            return Vec::new();
        };
        let mut found = Vec::new();
        let mut seen = HashSet::new();
        self.grid.visit(range.expanded(1), |id| {
            let shape = &self.shapes[id as usize];
            if shape.fat.is_some_and(|bounds| overlaps(low, high, bounds)) && seen.insert(id) {
                found.push(id);
            }
        });
        found.sort_unstable();
        found
    }

    /// Sweep the probe through all applicable static shapes, in proxy order.
    pub fn scene_sweep(&self, probe: MeshProbe<'_>) -> Result<Option<WorldHit>, PointSetSize> {
        if probe.points.is_empty() || probe.points.len() > usize::from(u8::MAX) {
            return Err(PointSetSize);
        }
        let translation = sub(probe.end, probe.start);
        let extent = probe
            .points
            .iter()
            .flatten()
            .map(|lane| lane.abs())
            .fold(0.0, f32::max);
        let reach = (f64::from(extent) + f64::from(probe.radius)) + SWEEP_REACH;
        let low = std::array::from_fn(|k| f64::from(probe.start[k].min(probe.end[k])) - reach);
        let high = std::array::from_fn(|k| f64::from(probe.start[k].max(probe.end[k])) + reach);
        let b = CastShape {
            points: probe.points,
            radius: maxps(SKIN, probe.radius),
            frame: Some(
                ContactFrame {
                    translation: probe.start,
                    ..ContactFrame::IDENTITY
                }
                .into(),
            ),
        };
        let mut nearest = None;
        let mut max_fraction = 1.0;
        for id in self.query_precise(low, high) {
            let shape = &self.shapes[id as usize];
            if shape.geometry.mask() & probe.category == 0 {
                continue;
            }
            let options = CastOptions {
                translation,
                max_fraction,
                hit_touching: false,
                shrink: false,
            };
            let cast = |points, radius| {
                shape_cast(
                    CastShape {
                        points,
                        radius,
                        frame: shape.matrix,
                    },
                    b,
                    options,
                )
            };
            let result = match &shape.geometry {
                StaticGeometry::Mesh(mesh) => mesh_sweep(
                    mesh,
                    shape.frame.unwrap_or(ContactFrame::IDENTITY),
                    probe,
                    max_fraction,
                )?
                .map(|hit| (hit.hit, Some(hit.triangle))),
                StaticGeometry::Hull(hull) => cast(&hull.vertices, SKIN)?.map(|hit| (hit, None)),
                StaticGeometry::Round {
                    shape: RoundShape::Capsule(capsule),
                    ..
                } => cast(&[capsule.a, capsule.b], capsule.radius)?.map(|hit| (hit, None)),
                StaticGeometry::Round {
                    shape: RoundShape::Sphere(sphere),
                    ..
                } => cast(&[sphere.center], sphere.radius)?.map(|hit| (hit, None)),
            };
            if let Some((hit, triangle)) = result {
                max_fraction = hit.fraction;
                nearest = Some(WorldHit {
                    hit,
                    shape: id,
                    triangle,
                });
            }
        }
        Ok(nearest)
    }
}

fn placed_geometry(
    shape: &Shape,
    placement: Placement,
    slot: usize,
    raw_meshes: &mut HashMap<(u32, usize), Arc<Mesh>>,
) -> Result<StaticGeometry, ContactWorldError> {
    let scale = placement.scale;
    match shape {
        Shape::Mesh { mask, mesh } => {
            let raw = raw_meshes
                .entry((placement.model, slot))
                .or_insert_with(|| Arc::new(mesh.clone()));
            Ok(StaticGeometry::Mesh(Arc::new(ContactMesh::new(
                Arc::clone(raw),
                scale,
                *mask,
            )?)))
        }
        Shape::Hull(hull) => {
            let mut hull = hull.clone();
            if scale != [1.0; 3] {
                hull.centroid = mul(hull.centroid, scale);
                for vertex in &mut hull.vertices {
                    *vertex = mul(*vertex, scale);
                }
                for plane in &mut hull.planes {
                    plane[3] *= scale[0];
                }
            }
            Ok(StaticGeometry::Hull(Arc::new(hull)))
        }
        _ => {
            let obstacle =
                RoundObstacle::from_placement(shape, placement).expect("only round shapes remain");
            Ok(StaticGeometry::Round {
                shape: obstacle.shape,
                mask: obstacle.mask,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::collision_cache::Triangle;
    use crate::contact_round::{Capsule, Sphere};
    use serde_json::json;

    const DISC: [Vec3; 5] = [
        [0.4, 0.015, 0.0],
        [0.0, 0.015, 0.4],
        [-0.4, 0.015, 0.0],
        [0.0, 0.015, -0.4],
        [0.0; 3],
    ];

    fn cube() -> Hull {
        Hull {
            mask: 1,
            centroid: [-2.0, 0.6, 0.0],
            vertices: vec![
                [-2.3, 0.4, -0.3],
                [-2.3, 0.4, 0.3],
                [-2.3, 0.8, -0.3],
                [-2.3, 0.8, 0.3],
                [-1.7, 0.4, -0.3],
                [-1.7, 0.4, 0.3],
                [-1.7, 0.8, -0.3],
                [-1.7, 0.8, 0.3],
            ],
            planes: vec![],
            faces: vec![],
            half_edges: vec![],
        }
    }

    fn sphere(center: Vec3, radius: f32, mask: u16) -> StaticGeometry {
        StaticGeometry::Round {
            shape: RoundShape::Sphere(Sphere { center, radius }),
            mask,
        }
    }

    #[test]
    fn scene_cast_dispatches_round_and_hull_shapes_and_preserves_first_tie() {
        let geometry = [
            sphere([0.0, 1.0, 0.0], 0.4, 1),
            sphere([0.0, 1.0, 0.0], 0.4, 1),
            StaticGeometry::Round {
                shape: RoundShape::Capsule(Capsule {
                    a: [2.0, 0.5, 0.0],
                    b: [2.0, 1.0, 0.0],
                    radius: 0.3,
                }),
                mask: 2,
            },
            StaticGeometry::Hull(Arc::new(cube())),
        ];
        let world = ContactWorld::new(
            geometry
                .into_iter()
                .map(|geometry| StaticShape::new(geometry, None).unwrap())
                .collect(),
        )
        .unwrap();
        let sweep = |x, category| {
            world
                .scene_sweep(MeshProbe {
                    points: &DISC,
                    radius: 0.015,
                    start: [x, 2.0, 0.0],
                    end: [x, -1.0, 0.0],
                    category,
                })
                .unwrap()
        };
        for (x, category, shape, expected) in [
            (
                0.0,
                1,
                0,
                [0x3e4b_17e5, 0, 0x3fb3_3333, 0, 0, 0x3f80_0000, 0],
            ),
            (
                2.0,
                2,
                2,
                [0x3e6d_3a06, 0x4000_0000, 0x3fa6_6666, 0, 0, 0x3f80_0000, 0],
            ),
            (
                -2.0,
                1,
                3,
                [
                    0x3eca_3d71,
                    0xc000_0000,
                    0x3f4f_5c28,
                    0,
                    0,
                    0x3f7f_ffff,
                    0x8000_0000,
                ],
            ),
        ] {
            let result = sweep(x, category).unwrap();
            assert_eq!((result.shape, result.triangle), (shape, None));
            assert_eq!(result.hit.fraction.to_bits(), expected[0]);
            assert_eq!(result.hit.point.map(f32::to_bits), expected[1..4]);
            assert_eq!(result.hit.normal.map(f32::to_bits), expected[4..7]);
        }
        assert!(sweep(0.0, 2).is_none());
    }

    #[test]
    fn broadphase_keeps_oversized_proxies_and_signed_zero_overlap_rule() {
        let world = ContactWorld::new(vec![
            StaticShape::new(sphere([0.0; 3], 150.0, 1), None).unwrap(),
            StaticShape::new(sphere([100.0, 0.0, 0.0], 0.2, 1), None).unwrap(),
        ])
        .unwrap();
        let q = ContactBounds {
            low: [99.0, -1.0, -1.0],
            high: [101.0, 1.0, 1.0],
        };
        assert_eq!(world.query(q), [0, 1]);
        assert_eq!(
            world.query(ContactBounds {
                low: [-180.0; 3],
                high: [180.0; 3]
            }),
            [0, 1]
        );
        assert!(
            world
                .query(ContactBounds {
                    low: [200.0; 3],
                    high: [201.0; 3]
                })
                .is_empty()
        );
        let zero = ContactBounds {
            low: [-1.0; 3],
            high: [-0.0, 1.0, 1.0],
        };
        assert!(!overlaps([0.0; 3], [1.0; 3], zero));
        assert!(overlaps([-0.0, 0.0, 0.0], [1.0; 3], zero));
    }

    #[test]
    fn movement_cache_keeps_shape_order_groups_and_model_scale_reuse() {
        let mesh = Mesh {
            vertices: vec![[0.0; 3], [0.0, 0.0, 2.0], [2.0, 0.0, 0.0]],
            triangles: vec![Triangle {
                corners: [0, 1, 2],
                wings: [-1; 3],
                mask: 1,
                surface: 0,
            }],
        };
        let placement = |group, x| Placement {
            model: 0,
            group,
            translation: [x, 0.0, 0.0],
            scale: [1.25, 0.75, 1.5],
            rotation: ContactFrame::IDENTITY.rotation,
        };
        let cache = CollisionCache {
            header: serde_json::from_value(json!({
                "meshes": [[3, 1]], "hulls": 1, "models": 1, "placements": 2,
                "identifiers": ["AB"],
            }))
            .unwrap(),
            meshes: vec![mesh.clone()],
            hulls: vec![cube()],
            models: vec![vec![Shape::Mesh { mesh, mask: 1 }]],
            placements: vec![placement(0, 5.0), placement(1, 10.0)],
        };
        let core = ContactWorld::from_movement_cache(cache.clone(), &BTreeSet::new()).unwrap();
        let mode = ContactWorld::from_movement_cache(cache, &BTreeSet::from([0xab])).unwrap();
        assert_eq!((core.shapes().len(), mode.shapes().len()), (3, 4));
        let [hull, map, prop_a, prop_b] = mode.shapes() else {
            panic!("expected all four shapes");
        };
        assert!(matches!(hull.geometry(), StaticGeometry::Hull(_)));
        assert!(matches!(map.geometry(), StaticGeometry::Mesh(_)));
        assert!(hull.frame().is_none() && map.frame().is_none());
        let (StaticGeometry::Mesh(a), StaticGeometry::Mesh(b)) =
            (prop_a.geometry(), prop_b.geometry())
        else {
            panic!("expected both mesh props");
        };
        assert!(Arc::ptr_eq(a, b));
        assert_eq!(prop_b.frame().unwrap().translation, [10.0, 0.0, 0.0]);
    }
}
