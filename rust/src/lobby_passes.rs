//! Account pass pools used by the role queues and lobby progression.

use std::io;

use serde_json::Value as Json;

use crate::{
    accounts::{Profile, saved_integer},
    jam_codec::{
        Fields,
        Value::{Array, Signed, Struct, Unsigned},
    },
    lobby_transport::LobbyMessage,
};

pub const PASSES: u32 = 0xad1c_34ba;
pub const MAX_PASSES: i64 = 40;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u64)]
pub enum Pool {
    QuickPlay = 0x0d80_0000_0000_843c,
    Competitive = 0x0d80_0000_0000_843d,
}

impl Pool {
    pub fn from_guid(guid: u64) -> Option<Self> {
        [Self::QuickPlay, Self::Competitive]
            .into_iter()
            .find(|&pool| pool as u64 == guid)
    }

    fn profile_key(self) -> &'static str {
        match self {
            Self::QuickPlay => "quick_play",
            Self::Competitive => "competitive",
        }
    }

    pub fn count(self, profile: &Profile) -> io::Result<i64> {
        let key = self.profile_key();
        let Some(value) = profile.priority_passes.get(key) else {
            return Ok(0);
        };
        saved_integer(value).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("invalid saved pass count for {key}"),
            )
        })
    }

    pub fn change(self, profile: &mut Profile, amount: i64) -> io::Result<()> {
        let count = self
            .count(profile)?
            .saturating_add(amount)
            .clamp(0, MAX_PASSES);
        profile
            .priority_passes
            .insert(self.profile_key().into(), Json::from(count));
        Ok(())
    }
}

/// The message replaces the list, so it always includes both pools.
pub fn counts(profile: &Profile) -> io::Result<LobbyMessage> {
    let counts = [Pool::QuickPlay, Pool::Competitive]
        .into_iter()
        .map(|pool| {
            Ok(Struct(Fields::from([
                (0, Unsigned(pool as u64)),
                (8, Signed(pool.count(profile)?)),
            ])))
        })
        .collect::<io::Result<Vec<_>>>()?;
    Ok((
        PASSES,
        58501,
        Fields::from([(0x78, Struct(Fields::from([(0, Array(counts))])))]),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saved_passes_keep_pool_order_and_spending_clamps_to_limits() {
        let mut profile = Profile::default();
        profile
            .priority_passes
            .insert("quick_play".into(), Json::from(" 3 "));
        assert_eq!(
            Pool::from_guid(Pool::QuickPlay as u64),
            Some(Pool::QuickPlay)
        );
        assert_eq!(Pool::from_guid(0), None);
        assert_eq!(Pool::QuickPlay.count(&profile).unwrap(), 3);
        Pool::QuickPlay.change(&mut profile, -4).unwrap();
        Pool::Competitive.change(&mut profile, 46).unwrap();
        assert_eq!(Pool::QuickPlay.count(&profile).unwrap(), 0);
        assert_eq!(Pool::Competitive.count(&profile).unwrap(), 40);
        let (crc, id, fields) = counts(&profile).unwrap();
        assert_eq!((crc, id), (PASSES, 58501));
        let Struct(list) = &fields[&0x78] else {
            panic!("pass pool list missing")
        };
        let Array(pools) = &list[&0] else {
            panic!("pass pools missing")
        };
        assert_eq!(
            pools,
            &[
                Struct(Fields::from([
                    (0, Unsigned(Pool::QuickPlay as u64)),
                    (8, Signed(0))
                ])),
                Struct(Fields::from([
                    (0, Unsigned(Pool::Competitive as u64)),
                    (8, Signed(40))
                ])),
            ]
        );
    }
}
