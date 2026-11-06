//! Least-significant-bit-first fields used by the Overwatch 1.74 game link.
//!
//! Ported from `ow174/game/bits.py`. The selector bits are transmitted before the
//! value. See that module for the client reader locations and upstream research.

use std::fmt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BitError {
    Width(usize),
    ZeroSignedWidth,
    ValueDoesNotFit { value: u64, width: usize },
    EndOfStream { requested: usize, remaining: usize },
    InvalidRange { position: usize, limit: usize },
}

impl fmt::Display for BitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            Self::Width(width) => write!(f, "scalar width {width} exceeds 64 bits"),
            Self::ZeroSignedWidth => f.write_str("a signed field must have at least one bit"),
            Self::ValueDoesNotFit { value, width } => {
                write!(f, "{value} does not fit in {width} bits")
            }
            Self::EndOfStream {
                requested,
                remaining,
            } => write!(
                f,
                "the bit stream has {remaining} bits left, need {requested}"
            ),
            Self::InvalidRange { position, limit } => {
                write!(f, "invalid bit range from {position} to {limit}")
            }
        }
    }
}

impl std::error::Error for BitError {}

fn scalar_mask(width: usize) -> Result<u64, BitError> {
    match width {
        0..64 => Ok((1 << width) - 1),
        64 => Ok(u64::MAX),
        _ => Err(BitError::Width(width)),
    }
}

fn ensure_remaining(requested: usize, remaining: usize) -> Result<(), BitError> {
    if requested > remaining {
        return Err(BitError::EndOfStream {
            requested,
            remaining,
        });
    }
    Ok(())
}

const FRAME_WIDTHS: [usize; 3] = [18, 24, 32];
const ENTITY_WIDTHS: [usize; 3] = [8, 16, 32];
const SCRIPT_INDEX_WIDTHS: [usize; 3] = [4, 8, 16];
const SCRIPT_FRAME_WIDTHS: [usize; 3] = [16, 24, 32];
const GUID_WIDTHS: [usize; 3] = [12, 20, 32];
const ENTITY_INDEX_MASK: u32 = 0x1fff_ffff;

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct BitWriter {
    data: Vec<u8>,
    bit_len: usize,
}

impl BitWriter {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn bit_len(&self) -> usize {
        self.bit_len
    }

    pub fn as_bytes(&self) -> &[u8] {
        &self.data
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.data
    }

    pub fn bit(&mut self, flag: bool) {
        self.extend_bits(&[u8::from(flag)], 1);
    }

    pub fn bits(&mut self, value: u64, width: usize) -> Result<(), BitError> {
        let mask = scalar_mask(width)?;
        if value & !mask != 0 {
            return Err(BitError::ValueDoesNotFit { value, width });
        }
        self.extend_bits(&value.to_le_bytes(), width);
        Ok(())
    }

    /// Write the low bits of a two's-complement value, as the game expects.
    pub fn signed(&mut self, value: i64, width: usize) -> Result<(), BitError> {
        self.bits(value as u64 & scalar_mask(width)?, width)
    }

    pub fn var_a(&mut self, value: u32) {
        self.widths(value, FRAME_WIDTHS, true);
    }

    pub fn var_b(&mut self, value: u32) {
        self.widths(value, ENTITY_WIDTHS, false);
    }

    pub fn w_u16(&mut self, value: u16) {
        self.widths(u32::from(value), SCRIPT_INDEX_WIDTHS, false);
    }

    pub fn w_var(&mut self, value: u32) {
        self.var_b(value);
    }

    pub fn w_u32(&mut self, value: u32) {
        self.widths(value, SCRIPT_FRAME_WIDTHS, false);
    }

    pub fn guid_index(&mut self, guid: u64) {
        self.widths(guid as u32, GUID_WIDTHS, true);
    }

    pub fn entity_id(&mut self, entity: Option<u32>) {
        let entity = entity.unwrap_or(0);
        self.extend_bits(&[(entity >> 29) as u8 & 3], 2);
        self.var_b(entity & ENTITY_INDEX_MASK);
    }

    pub fn f32(&mut self, value: f32) {
        self.raw(&value.to_le_bytes());
    }

    pub fn raw(&mut self, data: &[u8]) {
        self.extend_bits(data, data.len() * 8);
    }

    /// Copy a bit span from little-endian bytes, excluding high padding bits.
    pub fn raw_bits(&mut self, data: &[u8], width: usize) -> Result<(), BitError> {
        ensure_remaining(width, data.len().saturating_mul(8))?;
        self.extend_bits(data, width);
        Ok(())
    }

    pub fn align(&mut self) {
        self.bit_len = self.data.len() * 8;
    }

    pub fn append(&mut self, other: &Self) {
        self.extend_bits(&other.data, other.bit_len);
    }

    fn widths(&mut self, value: u32, [small, middle, large]: [usize; 3], short_flag: bool) {
        let width = if value < 1 << small {
            self.bit(short_flag);
            small
        } else if value < 1 << middle {
            self.bit(!short_flag);
            self.bit(short_flag);
            middle
        } else {
            self.bit(!short_flag);
            self.bit(!short_flag);
            large
        };
        self.extend_bits(&value.to_le_bytes(), width);
    }

    fn extend_bits(&mut self, source: &[u8], width: usize) {
        let bytes = width.div_ceil(8);
        let start = self.bit_len / 8;
        let shift = self.bit_len % 8;
        self.bit_len += width;

        if shift == 0 && width % 8 == 0 {
            self.data.extend_from_slice(&source[..bytes]);
            return;
        }

        self.data.resize(self.bit_len.div_ceil(8), 0);
        for (offset, &byte) in source[..bytes].iter().enumerate() {
            let byte = if offset + 1 == bytes && width % 8 != 0 {
                byte & (u8::MAX >> (8 - width % 8))
            } else {
                byte
            };
            let index = start + offset;
            self.data[index] |= byte << shift;
            if shift != 0 && index + 1 < self.data.len() {
                self.data[index + 1] |= byte >> (8 - shift);
            }
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct BitReader<'a> {
    data: &'a [u8],
    position: usize,
    limit: usize,
}

impl<'a> BitReader<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            position: 0,
            limit: data.len() * 8,
        }
    }

    /// Read only the specified bit range, without treating byte padding as data.
    pub fn with_range(data: &'a [u8], position: usize, limit: usize) -> Result<Self, BitError> {
        if position > limit || limit > data.len().saturating_mul(8) {
            return Err(BitError::InvalidRange { position, limit });
        }
        Ok(Self {
            data,
            position,
            limit,
        })
    }

    pub fn position(&self) -> usize {
        self.position
    }

    pub fn left(&self) -> usize {
        self.limit - self.position
    }

    pub fn bit(&mut self) -> Result<bool, BitError> {
        Ok(self.bits(1)? != 0)
    }

    pub fn bits(&mut self, width: usize) -> Result<u64, BitError> {
        let mask = scalar_mask(width)?;
        ensure_remaining(width, self.left())?;
        let mut value = [0; 8];
        for (offset, byte) in value[..width.div_ceil(8)].iter_mut().enumerate() {
            *byte = self.byte_at(self.position + offset * 8);
        }
        self.position += width;
        Ok(u64::from_le_bytes(value) & mask)
    }

    pub fn signed(&mut self, width: usize) -> Result<i64, BitError> {
        if width == 0 {
            return Err(BitError::ZeroSignedWidth);
        }
        let value = self.bits(width)?;
        let shift = 64 - width;
        Ok(((value << shift) as i64) >> shift)
    }

    pub fn var_a(&mut self) -> Result<u32, BitError> {
        self.widths(FRAME_WIDTHS, true)
    }

    pub fn var_b(&mut self) -> Result<u32, BitError> {
        self.widths(ENTITY_WIDTHS, false)
    }

    pub fn w_u16(&mut self) -> Result<u16, BitError> {
        Ok(self.widths(SCRIPT_INDEX_WIDTHS, false)? as u16)
    }

    pub fn w_var(&mut self) -> Result<u32, BitError> {
        self.var_b()
    }

    pub fn w_u32(&mut self) -> Result<u32, BitError> {
        self.widths(SCRIPT_FRAME_WIDTHS, false)
    }

    pub fn guid_index(&mut self) -> Result<u32, BitError> {
        self.widths(GUID_WIDTHS, true)
    }

    pub fn entity_id(&mut self) -> Result<Option<u32>, BitError> {
        let kind = self.bits(2)? as u32;
        let value = self.var_b()?;
        Ok((value & ENTITY_INDEX_MASK != 0).then_some(0x8000_0000 | kind << 29 | value))
    }

    pub fn f32(&mut self) -> Result<f32, BitError> {
        Ok(f32::from_bits(self.bits(32)? as u32))
    }

    pub fn align(&mut self) {
        self.position = self.position.div_ceil(8).saturating_mul(8).min(self.limit);
    }

    /// Borrow the complete remaining bytes at an aligned position.
    pub fn aligned_bytes(&self) -> Option<&'a [u8]> {
        if self.position % 8 == 0 {
            Some(&self.data[self.position / 8..self.limit / 8])
        } else {
            None
        }
    }

    pub fn advance(&mut self, width: usize) -> Result<(), BitError> {
        ensure_remaining(width, self.left())?;
        self.position += width;
        Ok(())
    }

    pub fn raw_bits(&mut self, width: usize) -> Result<Vec<u8>, BitError> {
        ensure_remaining(width, self.left())?;
        let data = self.copy_bits(width);
        self.position += width;
        Ok(data)
    }

    /// Return all remaining bits moved down to bit zero without consuming them.
    pub fn rest(&self) -> Vec<u8> {
        self.copy_bits(self.left())
    }

    fn widths(
        &mut self,
        [small, middle, large]: [usize; 3],
        short_flag: bool,
    ) -> Result<u32, BitError> {
        let width = if self.bit()? == short_flag {
            small
        } else if self.bit()? == short_flag {
            middle
        } else {
            large
        };
        Ok(self.bits(width)? as u32)
    }

    fn byte_at(&self, position: usize) -> u8 {
        let index = position / 8;
        let shift = position % 8;
        let low = self.data[index] >> shift;
        if shift == 0 {
            low
        } else {
            low | (self.data.get(index + 1).copied().unwrap_or(0) << (8 - shift))
        }
    }

    fn copy_bits(&self, width: usize) -> Vec<u8> {
        let mut data: Vec<_> = (0..width.div_ceil(8))
            .map(|offset| self.byte_at(self.position + offset * 8))
            .collect();
        if width % 8 != 0 {
            *data.last_mut().expect("nonempty bit span") &= u8::MAX >> (8 - width % 8);
        }
        data
    }
}

#[cfg(test)]
mod tests {
    use super::{BitError, BitReader, BitWriter};

    #[test]
    fn selector_boundaries_match_the_python_bit_stream() {
        // Emitted by ow174.game.bits.BitWriter for these fields, with no padding between them.
        let expected = [
            0xff, 0xff, 0x2f, 0x00, 0x00, 0x81, 0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x04, 0xf8,
            0x0f, 0x20, 0xa0, 0xff, 0xff, 0x01, 0x00, 0x02, 0x00, 0x7c, 0x10, 0xfd, 0x0f, 0x10,
            0xe0, 0xff, 0x3f, 0x00, 0x80, 0x80, 0xfe, 0xff, 0xff, 0x07, 0x00, 0x00, 0x08, 0xf8,
            0xff, 0x02, 0x40, 0x80, 0xff, 0xff, 0x0f, 0x00, 0x00, 0x04, 0x00,
        ];
        let frames = [0x3ffff, 0x40000, 0xffffff, 0x1000000];
        let entities = [255, 256, 65535, 65536];
        let indexes = [15, 16, 255, 256];
        let script_frames = [65535, 65536, 16777215, 16777216];
        let guids = [
            0x0250_0000_0000_0fff,
            0x4000_0000_0000_1000,
            0x4000_0000_000f_ffff,
            0x4000_0000_0010_0000,
        ];

        let mut writer = BitWriter::new();
        writer.bit(true);
        for value in frames {
            writer.var_a(value);
        }
        for value in entities {
            writer.var_b(value);
        }
        for value in indexes {
            writer.w_u16(value);
        }
        for value in script_frames {
            writer.w_u32(value);
        }
        for value in guids {
            writer.guid_index(value);
        }
        assert_eq!(writer.bit_len(), 422);
        assert_eq!(writer.as_bytes(), expected);

        let mut reader = BitReader::with_range(&expected, 0, writer.bit_len()).unwrap();
        assert!(reader.bit().unwrap());
        for value in frames {
            assert_eq!(reader.var_a().unwrap(), value);
        }
        for value in entities {
            assert_eq!(reader.w_var().unwrap(), value);
        }
        for value in indexes {
            assert_eq!(reader.w_u16().unwrap(), value);
        }
        for value in script_frames {
            assert_eq!(reader.w_u32().unwrap(), value);
        }
        for value in guids {
            assert_eq!(reader.guid_index().unwrap(), value as u32);
        }
        assert_eq!(reader.left(), 0);
    }

    #[test]
    fn mixed_fields_and_padding_match_the_python_bit_stream() {
        let expected = [
            0x9d, 0x37, 0x8d, 0x84, 0, 0, 0, 0, 0, 0, 0, 0x0f, 0xbf, 0, 3,
        ];
        let mut writer = BitWriter::new();
        writer.bits(5, 3).unwrap();
        writer.signed(-13, 7).unwrap();
        writer.entity_id(Some(0xa002_1234));
        writer.entity_id(None);
        writer.f32(-0.0);
        writer.raw(&[0x87, 0x5f]);
        writer.align();
        let mut tail = BitWriter::new();
        tail.bits(3, 2).unwrap();
        writer.append(&tail);
        assert_eq!(writer.bit_len(), 114);
        assert_eq!(writer.into_bytes(), expected);

        let mut reader = BitReader::with_range(&expected, 0, 114).unwrap();
        assert_eq!(reader.bits(3).unwrap(), 5);
        assert_eq!(reader.signed(7).unwrap(), -13);
        assert_eq!(reader.entity_id().unwrap(), Some(0xa002_1234));
        assert_eq!(reader.entity_id().unwrap(), None);
        assert_eq!(reader.f32().unwrap().to_bits(), (-0.0f32).to_bits());
        assert_eq!(reader.raw_bits(16).unwrap(), [0x87, 0x5f]);
        reader.align();
        assert_eq!(reader.position(), 112);
        assert_eq!(reader.rest(), [3]);
        assert_eq!(reader.left(), 2);
    }

    #[test]
    fn arbitrary_spans_preserve_the_tail_at_an_unaligned_offset() {
        let source = [0xff; 17];
        let mut writer = BitWriter::new();
        writer.bits(1, 3).unwrap();
        writer.raw_bits(&source, 131).unwrap();
        writer.bits(2, 2).unwrap();
        let mut reader = BitReader::with_range(writer.as_bytes(), 3, writer.bit_len()).unwrap();
        let mut expected = source;
        expected[16] = 7;
        assert_eq!(reader.raw_bits(131).unwrap(), expected);
        assert_eq!(reader.bits(2).unwrap(), 2);
        assert_eq!(reader.left(), 0);
    }

    #[test]
    fn checks_scalar_width_and_range_without_consuming_partial_fields() {
        let mut writer = BitWriter::new();
        writer.bit(true);
        assert_eq!(
            writer.bits(8, 3),
            Err(BitError::ValueDoesNotFit { value: 8, width: 3 })
        );
        assert_eq!(writer.bits(0, 65), Err(BitError::Width(65)));
        assert_eq!(writer.bit_len(), 1);
        writer.signed(i64::MIN, 64).unwrap();

        let mut reader = BitReader::with_range(writer.as_bytes(), 1, 65).unwrap();
        assert_eq!(reader.signed(0), Err(BitError::ZeroSignedWidth));
        assert_eq!(reader.bits(65), Err(BitError::Width(65)));
        assert_eq!(reader.position(), 1);
        assert_eq!(reader.signed(64).unwrap(), i64::MIN);
        assert_eq!(
            reader.bit(),
            Err(BitError::EndOfStream {
                requested: 1,
                remaining: 0,
            })
        );
        assert_eq!(reader.position(), 65);
        assert!(BitReader::with_range(&[0], 8, 9).is_err());
    }
}
