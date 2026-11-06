//! Shared queries over placed triangle geometry for retail build 104319.
//!
//! Ports the two-metre XZ grid and traversal of `ow174/game/collision.py`.
//! The index is immutable. Queries reuse the same bounds and planes without a
//! per-column expansion cache or a lock. Prop placement, analytic round props,
//! and character contacts are handled by their respective world layers.

use std::collections::HashSet;
use std::error::Error;
use std::fmt;

use crate::collision_grid::{CELL_WIDTH, ColumnGrid, ColumnRange, cell};
use crate::static_ray::{Vec3, inside_xz, triangle_distance};

const MAX_RAY_COLUMNS: usize = 1 << 16;
const PLANE_EPSILON: f64 = 1e-12;
const RAY_EPSILON: f64 = 1e-9;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SceneTriangle {
    pub vertices: [Vec3; 3],
    pub mask: u16,
    pub surface: u16,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct GroundHit {
    pub y: f64,
    pub normal: Vec3,
    pub plane_offset: f64,
    pub mask: u16,
    pub surface: u16,
}

impl GroundHit {
    pub fn height_at(self, x: f64, z: f64) -> f64 {
        let [nx, ny, nz] = self.normal;
        (self.plane_offset - nx * x - nz * z) / ny
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct RayHit {
    pub distance: f64,
    pub point: Vec3,
    pub normal: Vec3,
    pub mask: u16,
    pub surface: u16,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IndexError {
    InvalidTriangle,
    IndexLimit,
    RayTraversalLimit,
}

impl fmt::Display for IndexError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidTriangle => "triangle is nonfinite or outside the collision grid",
            Self::IndexLimit => "collision geometry exceeds the triangle index limit",
            Self::RayTraversalLimit => "ray exceeds the collision column traversal limit",
        })
    }
}

impl Error for IndexError {}

#[derive(Clone, Copy, Debug)]
struct Bounds {
    min: Vec3,
    max: Vec3,
}

impl Bounds {
    fn columns(self) -> Option<ColumnRange> {
        ColumnRange::new([self.min[0], self.min[2]], [self.max[0], self.max[2]])
    }

    fn overlaps(self, other: Self) -> bool {
        (0..3).all(|axis| self.min[axis] <= other.max[axis] && self.max[axis] >= other.min[axis])
    }
}

#[derive(Debug)]
struct IndexedTriangle {
    triangle: SceneTriangle,
    bounds: Bounds,
    normal: Vec3,
    plane_offset: f64,
}

impl IndexedTriangle {
    fn new(triangle: SceneTriangle) -> Result<Self, IndexError> {
        let [a, b, c] = triangle.vertices;
        if !triangle.vertices.into_iter().flatten().all(f64::is_finite) {
            return Err(IndexError::InvalidTriangle);
        }
        let bounds = Bounds {
            min: std::array::from_fn(|axis| a[axis].min(b[axis]).min(c[axis])),
            max: std::array::from_fn(|axis| a[axis].max(b[axis]).max(c[axis])),
        };
        let [ux, uy, uz] = std::array::from_fn(|axis| b[axis] - a[axis]);
        let [vx, vy, vz] = std::array::from_fn(|axis| c[axis] - a[axis]);
        let normal = [uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx];
        let magnitude = dot(normal, normal).sqrt();
        if !magnitude.is_finite() {
            return Err(IndexError::InvalidTriangle);
        }
        let (normal, plane_offset) = if magnitude < PLANE_EPSILON {
            ([0.0; 3], 0.0)
        } else {
            let normal = normal.map(|component| component / magnitude);
            (normal, dot(normal, a))
        };
        if !plane_offset.is_finite() {
            return Err(IndexError::InvalidTriangle);
        }
        Ok(Self {
            triangle,
            bounds,
            normal,
            plane_offset,
        })
    }
}

fn dot(a: Vec3, b: Vec3) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

#[derive(Debug, Default)]
pub struct TriangleIndex {
    triangles: Vec<IndexedTriangle>,
    grid: ColumnGrid,
    lowest: Option<f64>,
}

impl TriangleIndex {
    pub fn new(triangles: impl IntoIterator<Item = SceneTriangle>) -> Result<Self, IndexError> {
        let mut index = Self::default();
        for triangle in triangles {
            let number =
                u32::try_from(index.triangles.len()).map_err(|_| IndexError::IndexLimit)?;
            let triangle = IndexedTriangle::new(triangle)?;
            let range = triangle
                .bounds
                .columns()
                .ok_or(IndexError::InvalidTriangle)?;
            let bottom = triangle.bounds.min[1];
            index.lowest = Some(index.lowest.map_or(bottom, |value| value.min(bottom)));
            index
                .grid
                .insert(number, range)
                .map_err(|_| IndexError::IndexLimit)?;
            index.triangles.push(triangle);
        }
        Ok(index)
    }

    pub fn lowest(&self) -> f64 {
        self.lowest.unwrap_or(0.0)
    }

    /// Return triangles whose bounds overlap the box, in first-column order.
    pub fn nearby(&self, min: Vec3, max: Vec3, category: u16) -> Vec<&SceneTriangle> {
        let bounds = Bounds { min, max };
        let mut found = Vec::new();
        if category == 0 || !(0..3).all(|axis| min[axis] <= max[axis]) {
            return found;
        }
        let Some(range) = bounds.columns() else {
            return found;
        };
        let mut seen = HashSet::new();
        self.grid.visit(range, |number| {
            let candidate = &self.triangles[number as usize];
            if candidate.triangle.mask & category != 0
                && candidate.bounds.overlaps(bounds)
                && seen.insert(number)
            {
                found.push(&candidate.triangle);
            }
        });
        found
    }

    /// Highest triangle surface within the inclusive vertical interval.
    pub fn ground(
        &self,
        x: f64,
        z: f64,
        top: f64,
        bottom: f64,
        category: u16,
    ) -> Option<GroundHit> {
        if category == 0 || top.is_nan() || bottom.is_nan() || bottom > top {
            return None;
        }
        let column = (cell(x)?, cell(z)?);
        let bounds = Bounds {
            min: [x, bottom, z],
            max: [x, top, z],
        };
        let mut best = f64::NEG_INFINITY;
        let mut hit = None;
        for number in self.grid.column(column) {
            let candidate = &self.triangles[number as usize];
            let [nx, ny, nz] = candidate.normal;
            if candidate.triangle.mask & category == 0
                || !candidate.bounds.overlaps(bounds)
                || ny.abs() < RAY_EPSILON
            {
                continue;
            }
            let d = candidate.plane_offset;
            let y = (d - nx * x - nz * z) / ny;
            if y > top || y < bottom || y <= best || !inside_xz(&candidate.triangle.vertices, x, z)
            {
                continue;
            }
            let side = if ny < 0.0 { -1.0 } else { 1.0 };
            best = y;
            hit = Some(GroundHit {
                y,
                normal: candidate.normal.map(|component| side * component),
                plane_offset: side * d,
                mask: candidate.triangle.mask,
                surface: candidate.triangle.surface,
            });
        }
        hit
    }

    /// First triangle hit along a unit direction, or an explicit error if the
    /// input would traverse more than 65,536 two-metre columns.
    pub fn cast_ray(
        &self,
        origin: Vec3,
        direction: Vec3,
        length: f64,
        category: u16,
    ) -> Result<Option<RayHit>, IndexError> {
        if category == 0
            || self.triangles.is_empty()
            || length <= 0.0
            || !length.is_finite()
            || direction == [0.0; 3]
            || !origin.into_iter().chain(direction).all(f64::is_finite)
        {
            return Ok(None);
        }
        let [ox, oy, oz] = origin;
        let [dx, dy, dz] = direction;
        let (Some(mut x), Some(mut z)) = (cell(ox), cell(oz)) else {
            return Ok(None);
        };
        let (step_x, mut next_x, delta_x) = ray_axis(ox, dx, x);
        let (step_z, mut next_z, delta_z) = ray_axis(oz, dz, z);
        let mut seen = HashSet::new();
        let mut best = f64::INFINITY;
        let mut hit = None;
        let mut start = 0.0;
        for visited in 1..=MAX_RAY_COLUMNS {
            let end = next_x.min(next_z).min(length);
            let start_y = oy + dy * start;
            let end_y = oy + dy * end;
            let low = start_y.min(end_y) - RAY_EPSILON;
            let high = start_y.max(end_y) + RAY_EPSILON;
            for number in self.grid.column((x, z)) {
                let candidate = &self.triangles[number as usize];
                if candidate.triangle.mask & category == 0
                    || candidate.bounds.max[1] < low
                    || candidate.bounds.min[1] > high
                    || !seen.insert(number)
                {
                    continue;
                }
                let Some(distance) =
                    triangle_distance(origin, direction, &candidate.triangle.vertices)
                else {
                    continue;
                };
                if distance < best && distance <= length {
                    best = distance;
                    hit = Some(candidate);
                }
            }
            if best <= end || next_x.min(next_z) > length {
                break;
            }
            // On an X/Z tie the reference visits the Z neighbor first.
            if next_x < next_z {
                let Some(next_cell) = x.checked_add(step_x) else {
                    break;
                };
                (start, next_x, x) = (next_x, next_x + delta_x, next_cell);
            } else {
                let Some(next_cell) = z.checked_add(step_z) else {
                    break;
                };
                (start, next_z, z) = (next_z, next_z + delta_z, next_cell);
            }
            if visited == MAX_RAY_COLUMNS {
                return Err(IndexError::RayTraversalLimit);
            }
        }
        Ok(hit.map(|candidate| {
            let normal = candidate.normal;
            let side = if dot(normal, direction) > 0.0 {
                -1.0
            } else {
                1.0
            };
            RayHit {
                distance: best,
                point: std::array::from_fn(|axis| origin[axis] + direction[axis] * best),
                normal: normal.map(|component| side * component),
                mask: candidate.triangle.mask,
                surface: candidate.triangle.surface,
            }
        }))
    }
}

fn ray_axis(origin: f64, direction: f64, cell: i32) -> (i32, f64, f64) {
    let step = if direction > 0.0 { 1 } else { -1 };
    if direction.abs() <= RAY_EPSILON {
        return (step, f64::INFINITY, f64::INFINITY);
    }
    let boundary = (f64::from(cell) + f64::from(i32::from(direction > 0.0))) * CELL_WIDTH;
    (
        step,
        (boundary - origin) / direction,
        CELL_WIDTH / direction.abs(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEAM0: u16 = 0x08;
    const TEAM1: u16 = 0x10;
    const MOVER: u16 = TEAM0 | TEAM1;

    fn floor(y: f64, half_width: f64, mask: u16, surface: u16) -> SceneTriangle {
        SceneTriangle {
            vertices: [
                [-half_width, y, -half_width],
                [half_width, y, -half_width],
                [0.0, y, half_width],
            ],
            mask,
            surface,
        }
    }

    fn wall(z: f64, mask: u16, surface: u16) -> SceneTriangle {
        SceneTriangle {
            vertices: [[-1.0, -1.0, z], [1.0, -1.0, z], [0.0, 1.0, z]],
            mask,
            surface,
        }
    }

    #[test]
    fn ground_selects_highest_matching_plane_and_keeps_first_tie() {
        let slope = SceneTriangle {
            vertices: [[0.0, 1.0, 0.0], [2.0, 2.0, 0.0], [0.0, 1.0, 2.0]],
            mask: TEAM0,
            surface: 2,
        };
        let index = TriangleIndex::new([
            floor(-1.0, 5.0, MOVER, 1),
            slope,
            SceneTriangle {
                surface: 3,
                ..slope
            },
            floor(4.0, 5.0, TEAM1, 4),
        ])
        .unwrap();
        assert_eq!(index.lowest(), -1.0);
        let slope = index.ground(1.0, 0.5, 4.0, -1.0, TEAM0).unwrap();
        assert_eq!(slope.surface, 2);
        assert!((slope.y - 1.5).abs() < 1e-15);
        assert!(slope.normal[1] > 0.0);
        assert!((slope.height_at(2.0, 0.0) - 2.0).abs() < 1e-15);
        let upper = index.ground(1.0, 0.5, 4.0, 4.0, TEAM1).unwrap();
        assert_eq!((upper.surface, upper.y), (4, 4.0));
        assert!(index.ground(1.5, 1.5, 2.0, 0.0, TEAM0).is_none());
    }

    #[test]
    fn nearby_returns_unique_bounds_matches_in_python_column_order() {
        let index = TriangleIndex::new([
            floor(0.0, 1000.0, MOVER, 1),
            floor(0.0, 5.0, TEAM0, 2),
            floor(1.0, 5.0, TEAM1, 3),
            floor(5.0, 5.0, TEAM0, 4),
        ])
        .unwrap();
        let surfaces = |min, max, mask| {
            index
                .nearby(min, max, mask)
                .into_iter()
                .map(|triangle| triangle.surface)
                .collect::<Vec<_>>()
        };
        assert_eq!(
            surfaces([-4.0, 0.0, -4.0], [4.0, 1.0, 4.0], MOVER),
            [2, 3, 1]
        );
        assert_eq!(
            surfaces([-1000.0, 0.0, -1000.0], [4.0, 1.0, 4.0], TEAM0),
            [1, 2]
        );
    }

    #[test]
    fn ray_preserves_masks_winding_ties_and_inclusive_grid_boundary() {
        let index = TriangleIndex::new([
            wall(-4.0, MOVER, 1),
            wall(2.0, TEAM0, 2),
            wall(2.0, TEAM0, 3),
            wall(6.0, TEAM1, 4),
        ])
        .unwrap();
        let origin = [0.0; 3];
        let forward = [0.0, 0.0, 1.0];
        let back = [0.0, 0.0, -1.0];
        let cast = |origin, direction, length, mask| {
            index.cast_ray(origin, direction, length, mask).unwrap()
        };
        let team0 = cast(origin, forward, 2.0, TEAM0).unwrap();
        assert_eq!(
            (team0.distance, team0.point, team0.normal, team0.surface),
            (2.0, [0.0, 0.0, 2.0], back, 2)
        );
        let team1 = cast(origin, forward, 10.0, TEAM1).unwrap();
        assert_eq!((team1.distance, team1.mask, team1.surface), (6.0, TEAM1, 4));
        let reverse = cast([0.0, 0.0, -3.0], back, 1.0, TEAM1).unwrap();
        assert_eq!(
            (reverse.distance, reverse.normal, reverse.surface),
            (1.0, forward, 1)
        );
        assert!(cast(origin, forward, 1.999, TEAM0).is_none());
    }

    #[test]
    fn oversized_triangles_follow_regular_triangles_and_can_hit_in_an_empty_cell() {
        let index =
            TriangleIndex::new([floor(0.0, 1000.0, MOVER, 1), floor(0.0, 5.0, MOVER, 2)]).unwrap();
        let down = [0.0, -1.0, 0.0];
        assert_eq!(
            index
                .cast_ray([0.0, 1.0, 0.0], down, 1.0, MOVER)
                .unwrap()
                .unwrap()
                .surface,
            2
        );
        assert_eq!(
            index
                .cast_ray([100.0, 1.0, 0.0], down, 1.0, MOVER)
                .unwrap()
                .unwrap()
                .surface,
            1
        );
        assert_eq!(
            index.ground(100.0, 0.0, 0.0, 0.0, MOVER).unwrap().surface,
            1
        );
    }

    #[test]
    fn invalid_inputs_and_extreme_traversal_are_bounded() {
        let mut invalid = wall(0.0, MOVER, 0);
        invalid.vertices[0][0] = f64::NAN;
        assert!(matches!(
            TriangleIndex::new([invalid]),
            Err(IndexError::InvalidTriangle)
        ));
        invalid.vertices[0][0] = 2.0 * f64::from(i32::MAX) + 2.0;
        assert!(matches!(
            TriangleIndex::new([invalid]),
            Err(IndexError::InvalidTriangle)
        ));
        let index = TriangleIndex::new([wall(0.0, MOVER, 0)]).unwrap();
        assert_eq!(TriangleIndex::default().lowest(), 0.0);
        assert_eq!(
            index.cast_ray([f64::NAN, 0.0, 0.0], [0.0, 0.0, 1.0], 1.0, MOVER),
            Ok(None)
        );
        assert_eq!(
            index.cast_ray([0.0; 3], [0.0, 0.0, 1.0], f64::INFINITY, MOVER),
            Ok(None)
        );
        assert_eq!(
            index.cast_ray([0.0; 3], [0.0, 0.0, 1.0], 1e6, MOVER),
            Err(IndexError::RayTraversalLimit)
        );
    }
}
