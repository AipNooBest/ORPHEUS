//! Triangle candidate queries in the retail movement engine's local frame.
//!
//! Ports the mesh query and its separating-axis triangle/box test from
//! `ow174/game/contacts.py`. The grid is shared with the f64 scene query,
//! while the exact test, scaled vertices and triangle metadata are f32.

use std::collections::HashSet;
use std::error::Error;
use std::fmt;
use std::sync::{Arc, OnceLock};

use crate::collision_cache::{Mesh, Triangle};
use crate::collision_grid::{ColumnGrid, ColumnRange};
use crate::contact_math::{Vec3, add, cross, dot, maxps, minps, mul, normalize, scale, sub};
use crate::contact_triangle::{ContactTriangle, TOUCH};

const QUERY_SLACK: f64 = 1e-3;

#[derive(Clone, Copy)]
struct BoxEdge {
    axis: Vec3,
    span_half: Vec3,
    abs_zxy: Vec3,
    abs_yzx: Vec3,
}

struct QueryBox {
    center: Vec3,
    half: Vec3,
}

impl QueryBox {
    fn new(low: [f64; 3], high: [f64; 3]) -> Self {
        Self {
            center: std::array::from_fn(|k| ((low[k] + high[k]) as f32) * 0.5),
            half: std::array::from_fn(|k| ((high[k] - low[k]) as f32) * 0.5),
        }
    }
}

fn sse_abs(vector: Vec3) -> Vec3 {
    vector.map(|value| maxps(0.0 - value, value))
}

/// Cached f32 constants for the engine's triangle/box test.
pub struct TriangleBox {
    corners: [Vec3; 3],
    normal: Vec3,
    abs_normal: Vec3,
    edges: [BoxEdge; 3],
}

impl TriangleBox {
    pub fn new(corners: [Vec3; 3]) -> Self {
        let [a, b, c] = corners;
        let e0 = sub(b, a);
        let e1 = sub(c, b);
        let e2 = sub(a, c);
        let normal = cross(e0, e1);
        let edges = [(e0, e2), (e1, e0), (e2, e1)].map(|(edge, span)| {
            let axis = normalize(edge);
            let [x, y, z] = sse_abs(axis);
            BoxEdge {
                axis,
                span_half: scale(sse_abs(cross(axis, span)), 0.5),
                abs_zxy: [z, x, y],
                abs_yzx: [y, z, x],
            }
        });
        Self {
            corners,
            normal,
            abs_normal: sse_abs(normal),
            edges,
        }
    }

    pub fn intersects(&self, low: Vec3, high: Vec3) -> bool {
        self.test(&QueryBox::new(low.map(f64::from), high.map(f64::from)))
    }

    fn test(&self, query: &QueryBox) -> bool {
        let [a, b, c] = self.corners.map(|point| sub(point, query.center));
        let half = query.half;
        for k in 0..3 {
            let top = maxps(maxps(a[k], b[k]), c[k]);
            let bottom = minps(minps(a[k], b[k]), c[k]);
            if maxps(bottom - half[k], (0.0 - half[k]) - top) > 0.0 {
                return false;
            }
        }
        let distance = dot(self.normal, a);
        if maxps(0.0 - distance, distance) - dot(self.abs_normal, half) > 0.0 {
            return false;
        }
        let [x, y, z] = half;
        let hy = [y, z, x];
        let hz = [z, x, y];
        for (edge, pair) in self.edges.iter().zip([add(c, a), add(b, a), add(c, b)]) {
            let center_axes = sse_abs(cross(edge.axis, pair));
            for k in 0..3 {
                let radius = edge.abs_zxy[k] * hy[k] + edge.abs_yzx[k] * hz[k];
                let gap = ((center_axes[k] * 0.5) - edge.span_half[k]) - radius;
                if gap > TOUCH {
                    return false;
                }
            }
        }
        true
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ContactBounds {
    pub low: Vec3,
    pub high: Vec3,
}

impl ContactBounds {
    fn triangle([a, b, c]: [Vec3; 3]) -> Self {
        Self {
            low: std::array::from_fn(|k| minimum(minimum(a[k], b[k]), c[k])),
            high: std::array::from_fn(|k| maximum(maximum(a[k], b[k]), c[k])),
        }
    }

    fn merged(self, other: Self) -> Self {
        Self {
            low: std::array::from_fn(|k| minimum(self.low[k], other.low[k])),
            high: std::array::from_fn(|k| maximum(self.high[k], other.high[k])),
        }
    }

    fn columns(self) -> Option<ColumnRange> {
        ColumnRange::new(
            [f64::from(self.low[0]), f64::from(self.low[2])],
            [f64::from(self.high[0]), f64::from(self.high[2])],
        )
    }

    fn overlaps(self, low: [f64; 3], high: [f64; 3]) -> bool {
        (0..3).all(|k| f64::from(self.low[k]) <= high[k] && f64::from(self.high[k]) >= low[k])
    }
}

// Python min and max retain the first bound on a signed-zero tie.
fn minimum(a: f32, b: f32) -> f32 {
    if b < a { b } else { a }
}

fn maximum(a: f32, b: f32) -> f32 {
    if b > a { b } else { a }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ContactMeshError {
    InvalidScale,
    InvalidGeometry,
    IndexLimit,
}

impl fmt::Display for ContactMeshError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidScale => "contact mesh scale and its inverse must be finite and nonzero",
            Self::InvalidGeometry => "contact mesh has invalid vertices, indices or bounds",
            Self::IndexLimit => "contact mesh exceeds the collision grid limit",
        })
    }
}

impl Error for ContactMeshError {}

struct Record {
    bounds: ContactBounds,
    axes: OnceLock<Box<TriangleBox>>,
    triangle: OnceLock<Box<ContactTriangle>>,
}

/// Immutable mesh with lazily cached contact triangles and SAT constants.
pub struct ContactMesh {
    raw: Arc<Mesh>,
    scaled: Option<Vec<Vec3>>,
    scale: Vec3,
    inverse_scale: Vec3,
    bounds: Option<ContactBounds>,
    grid: ColumnGrid,
    records: Vec<Record>,
    pub mask: u16,
}

impl ContactMesh {
    pub fn new(
        mesh: impl Into<Arc<Mesh>>,
        mesh_scale: Vec3,
        mask: u16,
    ) -> Result<Self, ContactMeshError> {
        let inverse_scale = mesh_scale.map(|value| 1.0 / value);
        if !mesh_scale
            .into_iter()
            .chain(inverse_scale)
            .all(|value| value.is_finite() && value != 0.0)
        {
            return Err(ContactMeshError::InvalidScale);
        }
        let raw = mesh.into();
        let scaled = (mesh_scale != [1.0; 3]).then(|| {
            raw.vertices
                .iter()
                .map(|&point| mul(point, mesh_scale))
                .collect::<Vec<_>>()
        });
        let points = scaled.as_deref().unwrap_or(&raw.vertices);
        if !points.iter().flatten().all(|value| value.is_finite()) {
            return Err(ContactMeshError::InvalidGeometry);
        }

        let mut grid = ColumnGrid::default();
        let mut records = Vec::with_capacity(raw.triangles.len());
        let mut bounds = None::<ContactBounds>;
        for (number, record) in raw.triangles.iter().enumerate() {
            let id = u32::try_from(number).map_err(|_| ContactMeshError::IndexLimit)?;
            let points_for_triangle =
                corners(points, record).ok_or(ContactMeshError::InvalidGeometry)?;
            if record
                .wings
                .into_iter()
                .any(|wing| wing != -1 && points.get(wing as usize).is_none())
            {
                return Err(ContactMeshError::InvalidGeometry);
            }
            let own = ContactBounds::triangle(points_for_triangle);
            let range = own.columns().ok_or(ContactMeshError::InvalidGeometry)?;
            grid.insert(id, range)
                .map_err(|_| ContactMeshError::IndexLimit)?;
            bounds = Some(bounds.map_or(own, |old| old.merged(own)));
            records.push(Record {
                bounds: own,
                axes: OnceLock::new(),
                triangle: OnceLock::new(),
            });
        }
        Ok(Self {
            raw,
            scaled,
            scale: mesh_scale,
            inverse_scale,
            bounds,
            grid,
            records,
            mask,
        })
    }

    pub fn bounds(&self) -> Option<ContactBounds> {
        self.bounds
    }

    pub fn scale(&self) -> Vec3 {
        self.scale
    }

    pub(crate) fn inverse_scale(&self) -> Vec3 {
        self.inverse_scale
    }

    pub fn raw_corners(&self, number: u32) -> Option<[Vec3; 3]> {
        corners(&self.raw.vertices, self.raw.triangles.get(number as usize)?)
    }

    pub fn triangle(&self, number: u32) -> Option<&ContactTriangle> {
        let record = self.records.get(number as usize)?;
        Some(record.triangle.get_or_init(|| {
            Box::new(
                ContactTriangle::from_mesh(&self.raw, number as usize, self.scale)
                    .expect("mesh construction checked triangle and wing indices"),
            )
        }))
    }

    /// Source triangle numbers in increasing order, filtered by the face's mask.
    pub fn query(&self, low: Vec3, high: Vec3, category: u16) -> Vec<u32> {
        self.query_precise(low.map(f64::from), high.map(f64::from), category)
    }

    /// Mesh sweeps retain the unrounded box endpoints until the SAT sums.
    pub(crate) fn query_precise(&self, low: [f64; 3], high: [f64; 3], category: u16) -> Vec<u32> {
        if category == 0
            || !(0..3).all(|k| low[k].is_finite() && high[k].is_finite() && low[k] <= high[k])
        {
            return Vec::new();
        }
        let outer_low = low.map(|value| value - QUERY_SLACK);
        let outer_high = high.map(|value| value + QUERY_SLACK);
        let Some(range) =
            ColumnRange::new([outer_low[0], outer_low[2]], [outer_high[0], outer_high[2]])
        else {
            return Vec::new();
        };
        let query = QueryBox::new(low, high);
        let vertices = self.scaled.as_deref().unwrap_or(&self.raw.vertices);
        let mut seen = HashSet::new();
        let mut found = Vec::new();
        self.grid.visit(range, |number| {
            let slot = number as usize;
            let record = &self.records[slot];
            let triangle = &self.raw.triangles[slot];
            if triangle.mask & category == 0
                || !record.bounds.overlaps(outer_low, outer_high)
                || !seen.insert(number)
            {
                return;
            }
            let test = record.axes.get_or_init(|| {
                let points =
                    corners(vertices, triangle).expect("mesh construction checked corner indices");
                Box::new(TriangleBox::new(points))
            });
            if test.test(&query) {
                found.push(number);
            }
        });
        found.sort_unstable();
        found
    }
}

fn corners(points: &[Vec3], record: &Triangle) -> Option<[Vec3; 3]> {
    let [a, b, c] = record.corners.map(|number| points.get(number as usize));
    Some([*a?, *b?, *c?])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn box_test_keeps_reference_edge_slack_and_exact_box_axes() {
        let test = TriangleBox::new([[0.0; 3], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]);
        assert!(test.intersects([0.2, 0.2, -0.1], [0.4, 0.4, 0.1]));
        assert!(test.intersects([0.501, 0.501, -0.1], [0.6, 0.6, 0.1]));
        assert!(!test.intersects([0.51, 0.51, -0.1], [0.6, 0.6, 0.1]));
        assert!(!test.intersects([0.2, 0.2, 0.0001], [0.4, 0.4, 0.1]));
        assert!(!test.intersects([0.8, 0.8, -0.1], [1.0, 1.0, 0.1]));
    }

    fn record(corners: [i32; 3], mask: u16, surface: u16) -> Triangle {
        Triangle {
            corners,
            wings: [-1; 3],
            mask,
            surface,
        }
    }

    #[test]
    fn candidate_numbers_are_sorted_unique_and_keep_coincident_faces() {
        let raw = Mesh {
            vertices: vec![
                [-1024.0, 0.0, -1024.0],
                [0.0, 0.0, 1024.0],
                [1024.0, 0.0, -1024.0],
                [-4.0, 0.0, -4.0],
                [0.0, 0.0, 4.0],
                [4.0, 0.0, -4.0],
            ],
            triangles: vec![
                record([0, 1, 2], 0x10, 10),
                record([3, 4, 5], 0x08, 11),
                record([3, 4, 5], 0x08, 11),
            ],
        };
        let mesh = ContactMesh::new(raw, [1.0; 3], 0xffff).unwrap();
        let low = [-4.0, -1.0, -4.0];
        let high = [4.0, 1.0, 4.0];
        assert_eq!(mesh.query(low, high, 0x18), [0, 1, 2]);
        assert_eq!(mesh.query(low, high, 0x08), [1, 2]);
        assert_eq!(mesh.query([-2000.0; 3], [2000.0; 3], 0x18), [0, 1, 2]);
        assert_eq!(mesh.query([10.0, -1.0, 0.0], [11.0, 1.0, 1.0], 0x10), [0]);
        assert!(mesh.query([0.0, -1.0, 0.0], [0.0, 1.0, 0.0], 0).is_empty());
    }

    #[test]
    fn scaled_candidate_mesh_preserves_raw_corners_and_contact_metadata() {
        let raw = Arc::new(Mesh {
            vertices: vec![[0.0; 3], [0.0, 0.0, 2.0], [2.0, 0.0, 0.0]],
            triangles: vec![record([0, 1, 2], 0x08, 11)],
        });
        let mesh = ContactMesh::new(raw.clone(), [2.0, 3.0, 4.0], 0x80).unwrap();
        assert_eq!(mesh.mask, 0x80);
        assert_eq!(mesh.scale(), [2.0, 3.0, 4.0]);
        assert_eq!(
            mesh.bounds(),
            Some(ContactBounds {
                low: [0.0; 3],
                high: [4.0, 0.0, 8.0]
            })
        );
        assert_eq!(mesh.raw_corners(0).unwrap(), raw.vertices.as_slice());
        assert_eq!(mesh.query([0.1, -0.1, 3.0], [0.3, 0.1, 3.5], 0x08), [0]);
        let triangle = mesh.triangle(0).unwrap();
        assert_eq!(
            (triangle.index, triangle.mask, triangle.surface),
            (0, 0x08, 11)
        );
        assert_eq!(
            *triangle.corners(),
            [[0.0; 3], [0.0, 0.0, 8.0], [4.0, 0.0, 0.0]]
        );
        assert!(std::ptr::eq(triangle, mesh.triangle(0).unwrap()));
        assert!(mesh.raw_corners(1).is_none() && mesh.triangle(1).is_none());
        assert!(matches!(
            ContactMesh::new(raw, [0.0, 1.0, 1.0], 0),
            Err(ContactMeshError::InvalidScale)
        ));
    }
}
