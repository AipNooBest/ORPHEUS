//! Build 104319 projectile confirmation, launch and release records.
//!
//! Ported from ow174/game/projectiles.py. The caller owns entity lifetime,
//! acknowledgement gates and frame selection.

use std::io;

use crate::{
    bits::{BitError, BitWriter},
    static_ray::Vec3,
    world::{Create, EntityUpdate, Operation},
};

const HIDDEN_POSITION: [f32; 3] = [0.0, -1000.0, 0.0];
const NO_BASELINE: u32 = u32::MAX;

pub fn confirmation(entity: u32, definition: u64) -> EntityUpdate {
    let mut create = Create::new(definition, Vec::new());
    create.position = HIDDEN_POSITION;
    EntityUpdate {
        entity,
        operation: Some(Operation::Create(Box::new(create))),
        ..EntityUpdate::default()
    }
}

/// Release an acknowledged predicted projectile at its client impact point.
/// The count-only state uses a blank baseline and a frame ahead of its flight.
pub fn release(frame: u32, impacts: u32) -> Result<BitWriter, BitError> {
    let mut out = BitWriter::new();
    out.bit(false); // no shot events
    out.bit(true); // one projectile state
    out.var_b(NO_BASELINE);
    out.var_b(frame.wrapping_add(1));
    out.bits(0, 2)?; // alive, no optional field at +260
    for position in HIDDEN_POSITION {
        out.f32(position);
    }
    out.bits(0, 5)?; // unchanged velocity, +88 and three lists
    out.bit(true); // impact count
    if impacts < 16 {
        out.bit(true);
        out.bits(u64::from(impacts), 4)?;
    } else {
        out.bit(false);
        out.bit(impacts < 256);
        out.bits(u64::from(impacts), if impacts < 256 { 8 } else { 32 })?;
    }
    out.bits(0, 6)?; // the remaining fields match the blank state
    Ok(out)
}

/// Channel-2 launch from a body whose viewer has the corresponding volley.
#[derive(Clone, Copy, Debug)]
pub struct ShotRecord<'a> {
    pub frame: u32,
    pub instance: u16,
    pub state: u16,
    pub activation: u16,
    pub ordinal: u32,
    pub volleys: u16,
    pub origin: [f32; 3],
    pub direction: Vec3,
    pub entities: &'a [u32],
}

pub fn shot_events(shots: &[ShotRecord<'_>]) -> io::Result<BitWriter> {
    let mut shots: Vec<_> = shots.iter().collect();
    shots.sort_by_key(|shot| shot.frame);
    let mut out = BitWriter::new();
    out.bit(true);
    let mut last = None;
    for shot in shots {
        out.bit(true);
        if let Some(previous) = last {
            let step = shot.frame - previous;
            out.bit(step >= 16);
            if step < 16 {
                out.bits(u64::from(step), 4).map_err(io::Error::other)?;
            } else {
                out.bit(step >= 256);
                out.bits(u64::from(step), if step < 256 { 8 } else { 32 })
                    .map_err(io::Error::other)?;
            }
        } else {
            out.var_b(shot.frame);
        }
        write_shot(&mut out, shot)?;
        last = Some(shot.frame);
    }
    out.bit(false); // no more events
    out.bit(false); // no projectile state
    Ok(out)
}

fn write_shot(out: &mut BitWriter, shot: &ShotRecord<'_>) -> io::Result<()> {
    let count = u16::try_from(shot.entities.len())
        .map_err(|_| invalid("too many projectile IDs for a shot record"))?;
    if !shot.origin.into_iter().all(f32::is_finite)
        || !shot.direction.into_iter().all(f64::is_finite)
        || shot.direction == [0.0; 3]
    {
        return Err(invalid("invalid projectile launch transform"));
    }
    escaped(out, shot.ordinal, 4, 8).map_err(io::Error::other)?;
    out.bit(false); // no spread
    out.bit(false); // volley form
    out.w_u16(shot.instance);
    out.w_u16(shot.state);
    out.bits(u64::from(shot.activation & 63), 6)
        .map_err(io::Error::other)?;
    out.bit(shot.volleys == 1);
    if shot.volleys != 1 {
        out.w_u16(shot.volleys);
    }
    out.w_u16(0);
    for position in shot.origin {
        out.f32(position);
    }
    for angle in direction16(shot.direction) {
        out.bits(u64::from(angle), 16).map_err(io::Error::other)?;
    }
    let Some(&first) = shot.entities.first() else {
        out.bit(false);
        return Ok(());
    };
    out.bit(true);
    out.w_u16(count);
    // Channel 2 uses an escape value in the 16-bit tier rather than
    // the regular entity selector used by the rest of the world frame.
    out.bits(u64::from((first >> 29) & 3), 2)
        .map_err(io::Error::other)?;
    escaped(out, first & 0x1fff_ffff, 8, 16).map_err(io::Error::other)?;
    for ids in shot.entities.windows(2) {
        let step = i64::from(ids[1]) - i64::from(ids[0]);
        out.bit(step == 1);
        if step != 1 {
            out.bit(step < 0);
            escaped(out, step.unsigned_abs() as u32, 4, 8).map_err(io::Error::other)?;
        }
    }
    Ok(())
}

fn escaped(out: &mut BitWriter, value: u32, small: usize, middle: usize) -> Result<(), BitError> {
    let fits_small = value < 1 << small;
    out.bit(!fits_small);
    if fits_small {
        return out.bits(u64::from(value), small);
    }
    let escape = (1 << middle) - 1;
    out.bits(u64::from(value.min(escape)), middle)?;
    if value >= escape {
        out.bits(u64::from(value), 32)?;
    }
    Ok(())
}

fn direction16([x, y, z]: Vec3) -> [u16; 2] {
    let units = |angle: f64, scale: f64| {
        let value = (angle * (scale / std::f64::consts::PI))
            .round()
            .clamp(-32768.0, 32767.0);
        (value as i16 as u16) ^ 0x8000
    };
    let yaw = if x == 0.0 && z == 0.0 {
        0.0
    } else {
        x.atan2(z)
    };
    [
        units(yaw, 32768.0),
        units(y.clamp(-1.0, 1.0).asin(), 65536.0),
    ]
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write;

    fn check(payload: &BitWriter, bits: usize, expected: &str) {
        let hex = payload
            .as_bytes()
            .iter()
            .fold(String::new(), |mut out, value| {
                write!(out, "{value:02x}").unwrap();
                out
            });
        assert_eq!((payload.bit_len(), hex.as_str()), (bits, expected));
    }

    #[test]
    fn confirmation_and_release_match_the_python_wire_bytes() {
        let update = confirmation(0xa090_0400, 0x0400_0000_0000_072c);
        assert_eq!(update.entity, 0xa090_0400);
        let Some(Operation::Create(definition)) = update.operation else {
            panic!("projectile create")
        };
        check(
            &crate::world::create(0, &definition).unwrap(),
            350,
            "b21c030000000000801e31000000000000000000000000000000000000e00f0000e00f0000e00f0000e00f00",
        );
        for (count, bits, bytes) in [
            (1, 169, "feffffff9f0e0c0000000000007ac400000000e00000"),
            (15, 169, "feffffff9f0e0c0000000000007ac400000000e00700"),
            (16, 174, "feffffff9f0e0c0000000000007ac400000000a01000"),
            (255, 174, "feffffff9f0e0c0000000000007ac400000000a0ff00"),
            (
                256,
                198,
                "feffffff9f0e0c0000000000007ac400000000200001000000",
            ),
        ] {
            check(&release(12345, count).unwrap(), bits, bytes);
        }
    }

    #[test]
    fn observer_launches_match_python_at_frame_counter_and_entity_escapes() {
        let yaw = std::f64::consts::FRAC_PI_4;
        let pitch = std::f64::consts::FRAC_PI_8;
        let shots = [
            ShotRecord {
                frame: 1000,
                instance: 11,
                state: 10,
                activation: 65,
                ordinal: 0,
                volleys: 1,
                origin: [1.25, 2.5, -3.75],
                direction: [0.0, 0.0, 1.0],
                entities: &[0xa00c_0000, 0xa00c_0001, 0xa00b_0000],
            },
            ShotRecord {
                frame: 1005,
                instance: 256,
                state: 19,
                activation: 3,
                ordinal: 255,
                volleys: 3,
                origin: [-1.0, 10.0, 0.0],
                direction: [0.0, 1.0, 0.0],
                entities: &[0xa000_ffff],
            },
            ShotRecord {
                frame: 1030,
                instance: 1,
                state: 1,
                activation: 2,
                ordinal: 16,
                volleys: 2,
                origin: [0.0; 3],
                direction: [
                    yaw.sin() * pitch.cos(),
                    -pitch.sin(),
                    yaw.cos() * pitch.cos(),
                ],
                entities: &[],
            },
            ShotRecord {
                frame: 2000,
                instance: 1,
                state: 4,
                activation: 0,
                ordinal: 1,
                volleys: 1,
                origin: [0.0; 3],
                direction: [1.0, 0.0, 0.0],
                entities: &[0xa000_0004],
            },
        ];
        let payload = shot_events(&[shots[2], shots[0], shots[3], shots[1]]).unwrap();
        check(
            &payload,
            946,
            concat!(
                "873e00b034080000407f000040800000e080010001009bfeff03003000f47f00400040f5ff1f00008001029a1818000000f80b000012040000000000f8ff5ff4ffffff1f",
                "0060192110424000000000000000000000000000002800982b0f000008040202000000000000000000000000006000c0220400"
            ),
        );
    }
}
