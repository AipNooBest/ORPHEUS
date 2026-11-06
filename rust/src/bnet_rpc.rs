//! Local Battle.net service handlers for login and lobby referral.

use std::borrow::Cow;
use std::collections::HashMap;
use std::error::Error;
use std::io;
use std::net::SocketAddrV4;

use prost_reflect::{DynamicMessage, Value};

use crate::bnet_protocol as protocol;
use crate::bnet_protocol::Protocol;
use crate::bnet_session_key::session_key;

const RESPONSE_SERVICE_ID: u32 = 254;
const REFERRAL_CID: u64 = 379_775_058;
const ACCOUNT_HIGH: u64 = 0x0100_0000_0000_0000;
const GAME_ACCOUNT_HIGH: u64 = 0x0200_0001_0050_726f;

type RpcResult<T> = Result<T, Box<dyn Error + Send + Sync>>;

pub struct Player {
    pub account: u64,
    pub game_account: u64,
    pub battle_tag: String,
    pub name: String,
}

#[derive(Clone, Copy)]
pub struct Clock {
    pub server_epoch: u32,
    pub wall_millis: u64,
}

pub struct RpcSession {
    token: u32,
    listener_ids: HashMap<u32, u32>,
}

impl Default for RpcSession {
    fn default() -> Self {
        Self::new()
    }
}

impl RpcSession {
    pub fn new() -> Self {
        Self {
            token: 0x4000_0000,
            listener_ids: HashMap::new(),
        }
    }

    /// Produce the response frames in send order for one request.
    pub fn dispatch(
        &mut self,
        protocol: &Protocol,
        data: &[u8],
        lobby: SocketAddrV4,
        clock: Clock,
        player: impl FnOnce() -> Player,
    ) -> RpcResult<Vec<Vec<u8>>> {
        let (header, body) = protocol.decode_frame(data)?;
        let service = u32_field(&header, "service_hash")?;
        let method = u32_field(&header, "method_id")?;
        let token = u32_field(&header, "token")?;
        let body = match (service, method) {
            (protocol::CONNECTION_HASH, protocol::CONNECT) => {
                self.connect(protocol, body, clock)?
            }
            (protocol::CONNECTION_HASH, protocol::KEEP_ALIVE | protocol::ECHO) => Vec::new(),
            (protocol::AUTH_SERVER_HASH, protocol::LOGON) => {
                protocol.decode_message(protocol::LOGON_REQUEST, body)?;
                let response = respond(protocol, token, &[])?;
                let result = logon_result(protocol, player())?;
                let notification = self.notify(
                    protocol,
                    protocol::AUTH_CLIENT_HASH,
                    protocol::ON_LOGON_COMPLETE,
                    &result,
                )?;
                return Ok(vec![response, notification]);
            }
            (protocol::GAME_UTILITIES_HASH, protocol::PROCESS_TASK) => {
                protocol.decode_message(protocol::PROCESS_TASK_REQUEST, body)?;
                referral(protocol, lobby)?
            }
            (
                protocol::AUTH_SERVER_HASH | protocol::GAME_UTILITIES_HASH | protocol::ACCOUNT_HASH,
                _,
            ) => Vec::new(),
            _ => return Ok(Vec::new()),
        };
        Ok(vec![respond(protocol, token, &body)?])
    }

    fn connect(&mut self, protocol: &Protocol, data: &[u8], clock: Clock) -> RpcResult<Vec<u8>> {
        let request = protocol.decode_message(protocol::CONNECT_REQUEST, data)?;
        let bind_field = field(&request, "bind_request")?;
        let bind = bind_field
            .as_message()
            .ok_or_else(|| invalid_field("bind_request"))?;
        let imported_field = field(bind, "imported_service")?;
        let imported = imported_field
            .as_list()
            .ok_or_else(|| invalid_field("imported_service"))?;
        let exported_field = field(bind, "exported_service")?;
        let exported = exported_field
            .as_list()
            .ok_or_else(|| invalid_field("exported_service"))?;

        let mut response = protocol.new_message(protocol::CONNECT_RESPONSE)?;
        let server_id = message(
            protocol,
            "bgs.protocol.ProcessId",
            [
                ("label", Value::U32(0)),
                ("epoch", Value::U32(clock.server_epoch)),
            ],
        )?;
        response.try_set_field_by_name("server_id", Value::Message(server_id))?;
        if request.has_field_by_name("client_id") {
            response
                .try_set_field_by_name("client_id", field(&request, "client_id")?.into_owned())?;
        }
        if !imported.is_empty() {
            let ids = (1..=u32::try_from(imported.len())?)
                .map(Value::U32)
                .collect();
            let bind = message(
                protocol,
                "bgs.protocol.connection.v1.BindResponse",
                [("imported_service_id", Value::List(ids))],
            )?;
            response.try_set_field_by_name("bind_response", Value::Message(bind))?;
        }
        response.try_set_field_by_name("server_time", Value::U64(clock.wall_millis))?;
        response.try_set_field_by_name(
            "use_bindless_rpc",
            field(&request, "use_bindless_rpc")?.into_owned(),
        )?;
        let encoded = protocol::encode_message(&response)?;
        let listeners = exported
            .iter()
            .map(|value| {
                let service = value
                    .as_message()
                    .ok_or_else(|| invalid_field("exported_service"))?;
                Ok((u32_field(service, "hash")?, u32_field(service, "id")?))
            })
            .collect::<io::Result<Vec<_>>>()?;
        self.listener_ids.extend(listeners);
        Ok(encoded)
    }

    fn notify(
        &mut self,
        protocol: &Protocol,
        hash: u32,
        method: u32,
        body: &[u8],
    ) -> RpcResult<Vec<u8>> {
        let mut header = protocol.new_header();
        let token = self.token.wrapping_add(1);
        header.try_set_field_by_name(
            "service_id",
            Value::U32(*self.listener_ids.get(&hash).unwrap_or(&0)),
        )?;
        header.try_set_field_by_name("service_hash", Value::U32(hash))?;
        header.try_set_field_by_name("method_id", Value::U32(method))?;
        header.try_set_field_by_name("token", Value::U32(token))?;
        let frame = protocol.encode_frame(&mut header, body)?;
        self.token = token;
        Ok(frame)
    }
}

fn invalid_field(name: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        format!("unexpected Battle.net field {name}"),
    )
}

fn field<'a>(message: &'a DynamicMessage, name: &str) -> io::Result<Cow<'a, Value>> {
    message
        .get_field_by_name(name)
        .ok_or_else(|| invalid_field(name))
}

fn u32_field(message: &DynamicMessage, name: &str) -> io::Result<u32> {
    field(message, name)?
        .as_u32()
        .ok_or_else(|| invalid_field(name))
}

fn message<const N: usize>(
    protocol: &Protocol,
    name: &str,
    fields: [(&str, Value); N],
) -> RpcResult<DynamicMessage> {
    let mut message = protocol.new_message(name)?;
    for (name, value) in fields {
        message.try_set_field_by_name(name, value)?;
    }
    Ok(message)
}

fn respond(protocol: &Protocol, token: u32, body: &[u8]) -> RpcResult<Vec<u8>> {
    let mut header = protocol.new_header();
    header.try_set_field_by_name("service_id", Value::U32(RESPONSE_SERVICE_ID))?;
    header.try_set_field_by_name("method_id", Value::U32(0))?;
    header.try_set_field_by_name("token", Value::U32(token))?;
    header.try_set_field_by_name("status", Value::U32(0))?;
    Ok(protocol.encode_frame(&mut header, body)?)
}

fn logon_result(protocol: &Protocol, player: Player) -> RpcResult<Vec<u8>> {
    let account = message(
        protocol,
        "bgs.protocol.EntityId",
        [
            ("high", Value::U64(ACCOUNT_HIGH)),
            ("low", Value::U64(player.account)),
        ],
    )?;
    let game_account = message(
        protocol,
        "bgs.protocol.EntityId",
        [
            ("high", Value::U64(GAME_ACCOUNT_HIGH)),
            ("low", Value::U64(player.game_account)),
        ],
    )?;
    let logon = message(
        protocol,
        protocol::LOGON_RESULT,
        [
            ("error_code", Value::U32(0)),
            ("account_id", Value::Message(account)),
            (
                "game_account_id",
                Value::List(vec![Value::Message(game_account)]),
            ),
            ("email", Value::String("player@localhost".into())),
            ("available_region", Value::List(vec![Value::U32(1)])),
            ("connected_region", Value::U32(1)),
            ("battle_tag", Value::String(player.battle_tag)),
            ("geoip_country", Value::String("US".into())),
            (
                "session_key",
                Value::Bytes(Vec::from(session_key(&player.name)?).into()),
            ),
            ("restricted_mode", Value::Bool(false)),
        ],
    )?;
    Ok(protocol::encode_message(&logon)?)
}

fn referral(protocol: &Protocol, lobby: SocketAddrV4) -> RpcResult<Vec<u8>> {
    let attributes = [
        (
            "response_type",
            "string_value",
            Value::String("ReferralInfo".into()),
        ),
        ("k0", "blob_value", Value::Bytes(vec![0; 64].into())),
        ("k1", "blob_value", Value::Bytes(vec![0; 64].into())),
        ("k2", "blob_value", Value::Bytes(vec![0; 64].into())),
        ("k3", "blob_value", Value::Bytes(vec![0; 64].into())),
        ("cid", "uint_value", Value::U64(REFERRAL_CID)),
        ("hostv4", "string_value", Value::String(lobby.to_string())),
    ];
    let result = attributes
        .into_iter()
        .map(|(name, kind, value)| {
            let value = message(protocol, "bgs.protocol.v2.Variant", [(kind, value)])?;
            Ok(Value::Message(message(
                protocol,
                "bgs.protocol.v2.Attribute",
                [
                    ("name", Value::String(name.into())),
                    ("value", Value::Message(value)),
                ],
            )?))
        })
        .collect::<RpcResult<Vec<_>>>()?;
    let response = message(
        protocol,
        protocol::PROCESS_TASK_RESPONSE,
        [("result", Value::List(result))],
    )?;
    Ok(protocol::encode_message(&response)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};

    fn request(protocol: &Protocol, hash: u32, token: u32, body: &[u8]) -> Vec<u8> {
        let mut header = protocol.new_header();
        for (name, value) in [
            ("service_id", 0),
            ("service_hash", hash),
            ("method_id", 1),
            ("token", token),
        ] {
            header
                .try_set_field_by_name(name, Value::U32(value))
                .unwrap();
        }
        protocol.encode_frame(&mut header, body).unwrap()
    }

    #[test]
    #[ignore = "requires the separately prepared Battle.net descriptor set"]
    fn connect_logon_and_referral_match_the_python_rpc_frames() {
        let path = std::env::var_os("ORPHEUS_BNET_DESCRIPTORS").unwrap();
        let protocol = Protocol::load(std::path::PathBuf::from(path)).unwrap();
        let clock = Clock {
            server_epoch: 42,
            wall_millis: 1_700_000_000_123,
        };
        let lobby = "127.0.0.1:3724".parse().unwrap();
        let client_id = message(
            &protocol,
            "bgs.protocol.ProcessId",
            [("label", Value::U32(3)), ("epoch", Value::U32(7))],
        )
        .unwrap();
        let listener = message(
            &protocol,
            "bgs.protocol.connection.v1.BoundService",
            [
                ("hash", Value::U32(protocol::AUTH_CLIENT_HASH)),
                ("id", Value::U32(9)),
            ],
        )
        .unwrap();
        let bind = message(
            &protocol,
            "bgs.protocol.connection.v1.BindRequest",
            [
                (
                    "imported_service",
                    Value::List(vec![Value::Message(listener.clone())]),
                ),
                (
                    "exported_service",
                    Value::List(vec![Value::Message(listener)]),
                ),
            ],
        )
        .unwrap();
        let connect = message(
            &protocol,
            protocol::CONNECT_REQUEST,
            [
                ("client_id", Value::Message(client_id)),
                ("bind_request", Value::Message(bind)),
                ("use_bindless_rpc", Value::Bool(true)),
            ],
        )
        .unwrap();
        let mut session = RpcSession::new();
        let mut frames = session
            .dispatch(
                &protocol,
                &request(
                    &protocol,
                    protocol::CONNECTION_HASH,
                    10,
                    &protocol::encode_message(&connect).unwrap(),
                ),
                lobby,
                clock,
                || panic!("connect must not select an account"),
            )
            .unwrap();
        frames.extend(
            session
                .dispatch(
                    &protocol,
                    &request(&protocol, protocol::AUTH_SERVER_HASH, 11, &[]),
                    lobby,
                    clock,
                    || Player {
                        account: 0x1234_5678,
                        game_account: 0x2345_6789,
                        battle_tag: "Test#1234".into(),
                        name: "Test".into(),
                    },
                )
                .unwrap(),
        );
        frames.extend(
            session
                .dispatch(
                    &protocol,
                    &request(&protocol, protocol::GAME_UTILITIES_HASH, 12, &[]),
                    lobby,
                    clock,
                    || panic!("referral must not select an account"),
                )
                .unwrap(),
        );
        let digests: Vec<_> = frames
            .iter()
            .map(|frame| format!("{:x}", Sha256::digest(frame)))
            .collect();
        assert_eq!(
            digests,
            [
                "90d0eac714a8288ad7c4a8f7890879f25bddd16647f886b3db7615273d66ac25",
                "f09791df7941d5e4a654f0b9431c39dff64e8341a7b732c2355752117d4e7ea9",
                "81d669b28d96772e9622e49f55c2b932d25442a4428364db069029b32c8dbc1a",
                "336b3382ba33ff9ad7f27d557e3fdc789b580b50df363ca4f1be220a09c600c0",
            ]
        );
    }
}
