//! Two-metre XZ columns shared by the ray and movement geometry indexes.

use std::collections::HashMap;

pub(crate) const CELL_WIDTH: f64 = 2.0;
const LARGE_COLUMNS: u64 = 4096;
const MAX_REFERENCES: usize = 1 << 24;

type Column = (i32, i32);

pub(crate) fn cell(coordinate: f64) -> Option<i32> {
    let value = (coordinate / CELL_WIDTH).floor();
    (f64::from(i32::MIN)..=f64::from(i32::MAX))
        .contains(&value)
        .then_some(value as i32)
}

#[derive(Clone, Copy)]
pub(crate) struct ColumnRange {
    first: Column,
    last: Column,
}

impl ColumnRange {
    pub(crate) fn new([low_x, low_z]: [f64; 2], [high_x, high_z]: [f64; 2]) -> Option<Self> {
        if low_x > high_x || low_z > high_z {
            return None;
        }
        Some(Self {
            first: (cell(low_x)?, cell(low_z)?),
            last: (cell(high_x)?, cell(high_z)?),
        })
    }

    fn contains(self, (x, z): Column) -> bool {
        (self.first.0..=self.last.0).contains(&x) && (self.first.1..=self.last.1).contains(&z)
    }

    fn count(self) -> u64 {
        let width = (i64::from(self.last.0) - i64::from(self.first.0) + 1) as u64;
        let depth = (i64::from(self.last.1) - i64::from(self.first.1) + 1) as u64;
        width.saturating_mul(depth)
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct GridLimit;

#[derive(Debug, Default)]
pub(crate) struct ColumnGrid {
    grid: HashMap<Column, Vec<u32>>,
    large: Vec<u32>,
    references: usize,
}

impl ColumnGrid {
    pub(crate) fn insert(&mut self, number: u32, range: ColumnRange) -> Result<(), GridLimit> {
        let count = range.count();
        if count > LARGE_COLUMNS {
            self.large.push(number);
            return Ok(());
        }
        let references = self.references + count as usize;
        if references > MAX_REFERENCES {
            return Err(GridLimit);
        }
        self.references = references;
        for x in range.first.0..=range.last.0 {
            for z in range.first.1..=range.last.1 {
                self.grid.entry((x, z)).or_default().push(number);
            }
        }
        Ok(())
    }

    fn regular_column(&self, column: Column) -> impl Iterator<Item = u32> + '_ {
        self.grid.get(&column).into_iter().flatten().copied()
    }

    pub(crate) fn column(&self, column: Column) -> impl Iterator<Item = u32> + '_ {
        self.regular_column(column)
            .chain(self.large.iter().copied())
    }

    /// Visit candidates in first-column order, with oversized geometry at the
    /// end of the first column. Regular IDs may recur in later columns.
    pub(crate) fn visit(&self, range: ColumnRange, mut consider: impl FnMut(u32)) {
        self.column(range.first).for_each(&mut consider);
        let count = range.count();
        if count > LARGE_COLUMNS && count > self.grid.len() as u64 {
            let mut occupied: Vec<_> = self
                .grid
                .keys()
                .copied()
                .filter(|&column| column != range.first && range.contains(column))
                .collect();
            occupied.sort_unstable();
            for column in occupied {
                self.regular_column(column).for_each(&mut consider);
            }
        } else {
            for x in range.first.0..=range.last.0 {
                for z in range.first.1..=range.last.1 {
                    if (x, z) != range.first {
                        self.regular_column((x, z)).for_each(&mut consider);
                    }
                }
            }
        }
    }
}
