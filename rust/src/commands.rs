//! Full and compressed client input records for Overwatch 1.74.
//!
//! The input layout and command-epoch behavior come from `ow174/game/commands.py`.
//! Frames use uint32 serial arithmetic. Deltas extend the decoded aim angles
//! beyond the range of the signed 16-bit absolute values.

use std::fmt;

use crate::bits::{BitError, BitReader};

pub const MAX_COMMANDS: u8 = 32;
pub const FRAME_HALF: u32 = 1 << 31;
pub const SUBFRAME: u32 = 0x10000;

pub const FIRE: u32 = 0x01;
pub const SECONDARY: u32 = 0x02;
pub const JUMP: u32 = 0x40;
pub const CROUCH: u32 = 0x80;

const DIRECTIONS: [(i8, i8); 8] = [
    (0, 127),
    (127, 127),
    (127, 0),
    (127, -127),
    (0, -127),
    (-127, -127),
    (-127, 0),
    (-127, 127),
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InputError {
    RecordLength { declared: u32, remaining: usize },
    CommandCount(u8),
    Truncated,
    Bits(BitError),
}

impl fmt::Display for InputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
            Self::RecordLength {
                declared,
                remaining,
            } => write!(f, "the record says {declared} bits, {remaining} are left"),
            Self::CommandCount(count) => write!(f, "{count} commands"),
            Self::Truncated => f.write_str("the record runs past the datagram"),
            Self::Bits(error) => fmt::Display::fmt(&error, f),
        }
    }
}

impl std::error::Error for InputError {}

impl From<BitError> for InputError {
    fn from(value: BitError) -> Self {
        match value {
            BitError::EndOfStream { .. } => Self::Truncated,
            _ => Self::Bits(value),
        }
    }
}

/// The forward distance between two uint32 frames.
pub fn frame_distance(newer: u32, older: u32) -> u32 {
    newer.wrapping_sub(older)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Click {
    pub subframe: u8,
    pub yaw: i16,
    pub pitch: i16,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Command {
    pub frame: u32,
    pub forward: i8,
    pub right: i8,
    pub yaw: i32,
    pub pitch: i32,
    pub buttons: u32,
    pub action: u8,
    pub extra: u16,
    pub click: Option<Click>,
}

impl Command {
    pub fn subframe(&self) -> u8 {
        self.click.map_or(u8::MAX, |click| click.subframe)
    }

    /// Seconds before the frame at which the client drew the bodies it aimed at.
    /// Quanta are in microseconds, truncated to whole milliseconds on the wire path.
    pub fn view_delay(&self, quanta: u32) -> f64 {
        let lead = (1.0 - f64::from(self.subframe()) / 255.0) * f64::from(quanta / 1000);
        let lead = (lead + 0.5) as u32;
        f64::from(self.extra.wrapping_add(lead as u16)) * 0.001
    }

    pub fn aim(&self) -> (i32, i32) {
        self.click.map_or((self.yaw, self.pitch), |click| {
            (i32::from(click.yaw), i32::from(click.pitch))
        })
    }
}

/// Decode a single input record, oldest command first.
pub fn read_commands(reader: &mut BitReader<'_>) -> Result<Vec<Command>, InputError> {
    if reader.left() == 0 || !reader.bit()? {
        return Ok(Vec::new());
    }
    let declared = reader.bits(32)? as u32;
    if declared as usize > reader.left() {
        return Err(InputError::RecordLength {
            declared,
            remaining: reader.left(),
        });
    }
    reader.advance(24)?; // Latency and key flags.
    let count = reader.bits(8)? as u8;
    if !(1..=MAX_COMMANDS).contains(&count) {
        return Err(InputError::CommandCount(count));
    }

    let mut commands = Vec::with_capacity(usize::from(count));
    let mut command = first_command(reader)?;
    commands.push(command);
    for _ in 1..count {
        command = next_command(reader, command)?;
        commands.push(command);
    }
    Ok(commands)
}

fn first_command(reader: &mut BitReader<'_>) -> Result<Command, BitError> {
    let frame = reader.bits(32)? as u32;
    let (forward, right) = throttles(reader, 0, 0)?;
    let yaw = reader.signed(16)? as i32;
    let pitch = reader.signed(16)? as i32;
    let buttons = reader.bits(24)? as u32;
    let action = reader.bits(8)? as u8;
    let click = subframe(reader, buttons)?;
    let extra = reader.bits(16)? as u16;
    Ok(Command {
        frame,
        forward,
        right,
        yaw,
        pitch,
        buttons,
        action,
        extra,
        click,
    })
}

fn next_command(reader: &mut BitReader<'_>, mut command: Command) -> Result<Command, BitError> {
    let advance = if reader.bit()? {
        if reader.bit()? {
            reader.bits(2)?
        } else if reader.bit()? {
            reader.bits(6)?
        } else {
            reader.bits(32)?
        }
    } else {
        1
    };
    command.frame = command.frame.wrapping_add(advance as u32);
    if !reader.bit()? {
        return Ok(command);
    }
    (command.forward, command.right) = throttles(reader, command.forward, command.right)?;
    if reader.bit()? {
        command.pitch += signed_delta(reader, 8, 10)?;
    }
    if reader.bit()? {
        command.yaw += signed_delta(reader, 8, 10)?;
    }
    if reader.bit()? {
        command.extra = command
            .extra
            .wrapping_add(signed_delta(reader, 5, 8)? as u16);
    }
    if reader.bit()? {
        command.buttons = reader.bits(24)? as u32;
    }
    if reader.bit()? {
        command.action = reader.bits(8)? as u8;
    }
    command.click = subframe(reader, command.buttons)?;
    Ok(command)
}

fn signed_delta(reader: &mut BitReader<'_>, small: usize, middle: usize) -> Result<i32, BitError> {
    let width = if reader.bit()? {
        small
    } else if reader.bit()? {
        middle
    } else {
        16
    };
    Ok(reader.signed(width)? as i32)
}

fn throttles(
    reader: &mut BitReader<'_>,
    mut forward: i8,
    mut right: i8,
) -> Result<(i8, i8), BitError> {
    if !reader.bit()? {
        return Ok((forward, right));
    }
    if reader.bit()? {
        return if reader.bit()? {
            Ok(DIRECTIONS[reader.bits(3)? as usize])
        } else {
            Ok((0, 0))
        };
    }
    if reader.bit()? {
        right = reader.signed(8)? as i8;
    }
    if reader.bit()? {
        forward = reader.signed(8)? as i8;
    }
    Ok((forward, right))
}

fn subframe(reader: &mut BitReader<'_>, buttons: u32) -> Result<Option<Click>, BitError> {
    if buttons & SUBFRAME == 0 {
        return Ok(None);
    }
    Ok(Some(Click {
        subframe: reader.bits(8)? as u8,
        yaw: reader.signed(16)? as i16,
        pitch: reader.signed(16)? as i16,
    }))
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct CommandQueue {
    last_frame: Option<u32>,
    epoch: u64,
    wraps: u64,
}

impl CommandQueue {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn last_frame(&self) -> Option<u32> {
        self.last_frame
    }

    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    pub fn wraps(&self) -> u64 {
        self.wraps
    }

    /// Reset at an explicit lifecycle boundary, after the caller has retired the
    /// old body's simulation, timers and owner-frame stream.
    pub fn reset(&mut self, last_frame: Option<u32>) {
        self.last_frame = last_frame;
        self.epoch += 1;
        self.wraps = 0;
    }

    pub fn consume(&mut self, commands: &[Command]) -> Vec<Command> {
        self.consume_window(commands, None, None).0
    }

    /// Admit the fresh prefix that fits the current body epoch. Return the first
    /// frame that needs a new body or a separate handoff.
    pub fn before_limit(
        &mut self,
        commands: &[Command],
        limit: u32,
        anchor: Option<u32>,
    ) -> (Vec<Command>, Option<u32>) {
        self.consume_window(commands, Some(limit), anchor)
    }

    fn consume_window(
        &mut self,
        commands: &[Command],
        limit: Option<u32>,
        anchor: Option<u32>,
    ) -> (Vec<Command>, Option<u32>) {
        let mut fresh = Vec::new();
        let mut previous = self.last_frame.or(anchor);
        for &command in commands {
            if let Some(previous) = previous {
                let distance = frame_distance(command.frame, previous);
                if distance == 0 || distance >= FRAME_HALF {
                    continue;
                }
            }
            let wrapped = previous.is_some_and(|frame| command.frame < frame);
            if limit.is_some_and(|limit| wrapped || command.frame > limit) {
                return (fresh, Some(command.frame));
            }
            if wrapped {
                self.wraps += 1;
            }
            fresh.push(command);
            previous = Some(command.frame);
            self.last_frame = previous;
        }
        (fresh, None)
    }
}

#[cfg(test)]
mod tests {
    use super::{Click, Command, CommandQueue, FRAME_HALF, InputError, SUBFRAME, read_commands};
    use crate::bits::{BitReader, BitWriter};

    #[test]
    fn matches_the_existing_forward_and_turn_fixture() {
        // tests/test_game_commands.py::record(5000)
        let bytes = [
            0x55, 0x01, 0, 0, 0x46, 0, 0, 0x04, 0x10, 0x27, 0, 0, 0x2e, 0, 0, 0x06, 0x7f, 0, 0, 0,
            0x80, 0x11, 0, 0x29, 0x4b, 0,
        ];
        let mut reader = BitReader::new(&bytes);
        let first = Command {
            frame: 5000,
            forward: 127,
            pitch: -500,
            extra: 35,
            ..Command::default()
        };
        let second = Command {
            frame: 5001,
            yaw: 300,
            ..first
        };
        assert_eq!(read_commands(&mut reader).unwrap(), [first, second]);
        assert_eq!(reader.position(), 203);
    }

    #[test]
    fn matches_a_python_record_with_wrap_deltas_and_clicks() {
        // Generated by the Python BitWriter and decoded by Python read_commands.
        let bytes = [
            0x29, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08, 0xfc, 0xff, 0xff, 0xff, 0x0b, 0x38,
            0xe9, 0xff, 0x0f, 0x00, 0x30, 0x00, 0x20, 0x00, 0x02, 0x08, 0x60, 0x10, 0xa0, 0xaf,
            0xff, 0xff, 0xfb, 0xbf, 0x2c, 0x5d, 0xf8, 0xcf, 0xff, 0x4f, 0x00, 0x68, 0x28, 0x00,
            0x00, 0x01, 0x00, 0x17, 0x00, 0x40, 0xf9, 0xff, 0x2b, 0x00, 0x00, 0x50, 0x15,
        ];
        let mut reader = BitReader::new(&bytes);
        let commands = read_commands(&mut reader).unwrap();
        let first = Command {
            frame: u32::MAX - 1,
            forward: 73,
            right: -128,
            yaw: 32767,
            pitch: -32768,
            buttons: SUBFRAME | 1,
            action: 16,
            extra: 65533,
            click: Some(Click {
                subframe: 64,
                yaw: -32000,
                pitch: 32000,
            }),
        };
        let second = Command {
            frame: 1,
            yaw: 33067,
            pitch: -32769,
            extra: 2,
            click: Some(Click {
                subframe: 255,
                yaw: -7,
                pitch: 9,
            }),
            ..first
        };
        let third = Command {
            frame: 34,
            ..second
        };
        let fourth = Command {
            frame: 65570,
            yaw: 33067,
            pitch: -65537,
            buttons: 2,
            action: 0xaa,
            extra: 32769,
            ..Command::default()
        };
        assert_eq!(commands, [first, second, third, fourth]);
        assert_eq!(commands[0].aim(), (-32000, 32000));
        assert_eq!(commands[0].view_delay(16100), 9.0 * 0.001);
        assert_eq!(commands[1].aim(), (-7, 9));
        assert_eq!(commands[1].view_delay(16100), 2.0 * 0.001);
        assert_eq!(commands[3].aim(), (33067, -65537));
        assert_eq!(reader.position(), 437);
        assert_eq!(reader.left(), 3);
    }

    #[test]
    fn handles_absent_records_and_rejects_invalid_counts_and_truncation() {
        assert!(read_commands(&mut BitReader::new(&[])).unwrap().is_empty());
        let mut absent = BitReader::new(&[0]);
        assert!(read_commands(&mut absent).unwrap().is_empty());
        assert_eq!(absent.position(), 1);

        let mut writer = BitWriter::new();
        writer.bit(true);
        writer.bits(32, 32).unwrap();
        assert_eq!(
            read_commands(&mut BitReader::new(writer.as_bytes())),
            Err(InputError::RecordLength {
                declared: 32,
                remaining: 7,
            })
        );

        writer.bits(0, 24).unwrap();
        for (count, expected) in [
            (0, InputError::CommandCount(0)),
            (33, InputError::CommandCount(33)),
            (1, InputError::Truncated),
        ] {
            let mut record = writer.clone();
            record.bits(count, 8).unwrap();
            assert_eq!(
                read_commands(&mut BitReader::new(record.as_bytes())),
                Err(expected)
            );
        }
    }

    fn at_frames(frames: &[u32]) -> Vec<Command> {
        frames
            .iter()
            .map(|&frame| Command {
                frame,
                ..Command::default()
            })
            .collect()
    }

    #[test]
    fn queue_admits_only_forward_commands_and_resets_only_explicitly() {
        let mut queue = CommandQueue::new();
        assert_eq!(
            queue.consume(&at_frames(&[10, 10, 11])),
            at_frames(&[10, 11])
        );
        assert_eq!(
            queue.consume(&at_frames(&[9, 11, 11 + FRAME_HALF, 12])),
            at_frames(&[12])
        );
        assert!(queue.consume(&at_frames(&[1])).is_empty());
        assert_eq!(queue.last_frame(), Some(12));
        assert_eq!(queue.epoch(), 0);
        queue.reset(None);
        assert_eq!(queue.epoch(), 1);
        assert_eq!(queue.consume(&at_frames(&[1])), at_frames(&[1]));
    }

    #[test]
    fn queue_limits_use_the_body_anchor_and_leave_wraps_for_later() {
        let mut queue = CommandQueue::new();
        let (fresh, boundary) = queue.before_limit(&at_frames(&[100, 101, 102]), 101, Some(100));
        assert_eq!((fresh, boundary), (at_frames(&[101]), Some(102)));
        assert_eq!(queue.last_frame(), Some(101));

        queue.reset(Some(u32::MAX - 1));
        let (fresh, boundary) = queue.before_limit(&at_frames(&[u32::MAX, 0, 1]), u32::MAX, None);
        assert_eq!((fresh, boundary), (at_frames(&[u32::MAX]), Some(0)));
        assert_eq!(queue.last_frame(), Some(u32::MAX));
        assert_eq!(queue.wraps(), 0);
        assert_eq!(queue.consume(&at_frames(&[0, 1])), at_frames(&[0, 1]));
        assert_eq!(queue.wraps(), 1);
        assert!(queue.consume(&at_frames(&[u32::MAX])).is_empty());
    }
}
