//! Login identity selection shared by the lobby and its message builders.

use std::io;

use crate::bnet_session_key::name_in_key;
use crate::jam_codec::{Fields, Value, id16};

pub const OUT_CONNECT: u32 = 0x3c7e_3468;
pub const LOGIN_MESSAGE: u32 = 21800;
pub const INCOMPATIBLE_CLIENT: u64 = 0x0de0_0000_0001_03bf;
pub const SIGNED_IN_ELSEWHERE: u64 = 0x0de0_0000_0001_3c84;
const ID_HIGH_TAG: u64 = 1 << 56;
const PARTY_ENTITY_TYPE: u64 = 0x1d << 40;

#[derive(Debug, PartialEq, Eq)]
pub enum Login {
    Named { name: String, tournament: bool },
    LocalDefault,
    IncompatibleRemote,
}

pub fn identify(fields: &Fields, local: bool) -> io::Result<Login> {
    if let Some(Value::String(name)) = fields.get(&0x78) {
        let name = name.trim();
        if !name.is_empty() {
            return Ok(Login::Named {
                name: name.into(),
                tournament: true,
            });
        }
    }

    if let Some(Value::Struct(bnet)) = fields.get(&0xb8) {
        let name = match bnet.get(&0x20) {
            None => None,
            Some(Value::Blob(key)) => name_in_key(key),
            Some(Value::Array(key)) => {
                let key = key
                    .iter()
                    .map(|byte| match byte {
                        Value::Unsigned(value) => u8::try_from(*value).map_err(|_| invalid_key()),
                        _ => Err(invalid_key()),
                    })
                    .collect::<io::Result<Vec<_>>>()?;
                name_in_key(&key)
            }
            Some(_) => return Err(invalid_key()),
        };
        if let Some(name) = name {
            return Ok(Login::Named {
                name,
                tournament: false,
            });
        }
    }

    Ok(if local {
        Login::LocalDefault
    } else {
        Login::IncompatibleRemote
    })
}

fn invalid_key() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid account-name session key",
    )
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Identity {
    pub account_lo: u32,
    pub session: [u64; 2],
    pub party_id: [u64; 2],
    pub party_entity: [u64; 2],
}

impl Identity {
    pub fn create(account_lo: u32, connection_seq: u32) -> io::Result<Self> {
        let mut random = [0; 24];
        getrandom::fill(&mut random).map_err(|error| io::Error::other(error.to_string()))?;
        Ok(Self::from_entropy(account_lo, connection_seq, random))
    }

    fn from_entropy(account_lo: u32, connection_seq: u32, random: [u8; 24]) -> Self {
        let connection = u64::from(connection_seq);
        Self {
            account_lo,
            session: [connection, 1],
            party_id: [
                u64::from_le_bytes(random[..8].try_into().unwrap()),
                u64::from_le_bytes(random[8..16].try_into().unwrap()),
            ],
            party_entity: [
                connection | PARTY_ENTITY_TYPE | ID_HIGH_TAG,
                u64::from_le_bytes(random[16..].try_into().unwrap()),
            ],
        }
    }

    pub fn for_account(account_lo: u32) -> Self {
        Self {
            account_lo,
            session: [u64::from(account_lo), 1],
            party_id: [0; 2],
            party_entity: [0; 2],
        }
    }

    pub fn account(self) -> Fields {
        id16(u64::from(self.account_lo), ID_HIGH_TAG)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bnet_session_key::session_key;

    #[test]
    fn login_uses_tournament_name_or_retail_session_key() {
        let key = session_key("rust").unwrap();
        let key = key
            .into_iter()
            .map(|byte| Value::Unsigned(u64::from(byte)))
            .collect();
        let mut fields = Fields::from([(
            0xb8,
            Value::Struct(Fields::from([(0x20, Value::Array(key))])),
        )]);
        assert_eq!(
            identify(&fields, false).unwrap(),
            Login::Named {
                name: "rust".into(),
                tournament: false
            }
        );
        fields.insert(0x78, Value::String("  Tournament ".into()));
        assert_eq!(
            identify(&fields, false).unwrap(),
            Login::Named {
                name: "Tournament".into(),
                tournament: true
            }
        );
        assert_eq!(identify(&Fields::new(), true).unwrap(), Login::LocalDefault);
        assert_eq!(
            identify(&Fields::new(), false).unwrap(),
            Login::IncompatibleRemote
        );
    }

    #[test]
    fn identity_matches_wire_tags_and_little_endian_random_halves() {
        let identity = Identity::from_entropy(0x1234_5678, 42, std::array::from_fn(|i| i as u8));
        assert_eq!(identity.session, [42, 1]);
        assert_eq!(
            identity.party_id,
            [0x0706_0504_0302_0100, 0x0f0e_0d0c_0b0a_0908]
        );
        assert_eq!(
            identity.party_entity,
            [0x0100_1d00_0000_002a, 0x1716_1514_1312_1110]
        );
        assert_eq!(identity.account()[&8], Value::Unsigned(1 << 56));
        assert_eq!(Identity::for_account(42).session, [42, 1]);
    }
}
