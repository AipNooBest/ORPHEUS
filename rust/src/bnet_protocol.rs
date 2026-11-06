//! Battle.net RPC framing for a WebSocket binary message.
//!
//! Each frame has a big-endian u16 header length, a protobuf Header, and
//! the protobuf body. The descriptor set is an external runtime input.

use std::{error::Error, fmt, fs, io, path::Path};

use prost::Message;
use prost_reflect::{
    Cardinality, DescriptorError, DescriptorPool, DynamicMessage, FieldDescriptor, Kind,
    MessageDescriptor, ReflectMessage, Value,
};

const HEADER_NAME: &str = "bgs.protocol.Header";
pub const CONNECT_REQUEST: &str = "bgs.protocol.connection.v1.ConnectRequest";
pub const CONNECT_RESPONSE: &str = "bgs.protocol.connection.v1.ConnectResponse";
pub const LOGON_REQUEST: &str = "bgs.protocol.authentication.v1.LogonRequest";
pub const LOGON_RESULT: &str = "bgs.protocol.authentication.v1.LogonResult";
pub const PROCESS_TASK_REQUEST: &str = "bgs.protocol.game_utilities.v2.client.ProcessTaskRequest";
pub const PROCESS_TASK_RESPONSE: &str = "bgs.protocol.game_utilities.v2.client.ProcessTaskResponse";

#[derive(Debug)]
pub enum ProtocolError {
    Io(io::Error),
    Descriptor(DescriptorError),
    Protobuf(prost::DecodeError),
    UnknownMessage(String),
    MissingRequiredField(String),
    InvalidHeaderSchema,
    WrongHeader,
    TruncatedHeader,
    HeaderTooLong(usize),
    BodyTooLong(usize),
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "could not read Battle.net descriptors: {error}"),
            Self::Descriptor(error) => write!(f, "invalid Battle.net descriptors: {error}"),
            Self::Protobuf(error) => write!(f, "invalid Battle.net protobuf: {error}"),
            Self::UnknownMessage(name) => write!(f, "no Battle.net message named {name}"),
            Self::MissingRequiredField(name) => write!(f, "protobuf field {name} is required"),
            Self::InvalidHeaderSchema => f.write_str("Battle.net Header lacks a u32 size field"),
            Self::WrongHeader => {
                f.write_str("frame header was not built from this Battle.net schema")
            }
            Self::TruncatedHeader => f.write_str("truncated Battle.net frame header"),
            Self::HeaderTooLong(size) => write!(f, "Battle.net frame header is {size} bytes"),
            Self::BodyTooLong(size) => write!(f, "Battle.net frame body is {size} bytes"),
        }
    }
}

impl Error for ProtocolError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Descriptor(error) => Some(error),
            Self::Protobuf(error) => Some(error),
            _ => None,
        }
    }
}

pub struct Protocol {
    pool: DescriptorPool,
    header: MessageDescriptor,
    size_field: FieldDescriptor,
}

impl Protocol {
    pub fn load(path: impl AsRef<Path>) -> Result<Self, ProtocolError> {
        let data = fs::read(path).map_err(ProtocolError::Io)?;
        Self::from_bytes(&data)
    }

    pub fn from_bytes(data: &[u8]) -> Result<Self, ProtocolError> {
        // Load the entire set together so files can resolve imports regardless of set order.
        let pool = DescriptorPool::decode(data).map_err(ProtocolError::Descriptor)?;
        let header = pool
            .get_message_by_name(HEADER_NAME)
            .ok_or_else(|| ProtocolError::UnknownMessage(HEADER_NAME.into()))?;
        let size_field = header
            .get_field_by_name("size")
            .filter(|field| matches!(field.kind(), Kind::Uint32) && !field.is_list())
            .ok_or(ProtocolError::InvalidHeaderSchema)?;
        Ok(Self {
            pool,
            header,
            size_field,
        })
    }

    pub fn new_message(&self, name: &str) -> Result<DynamicMessage, ProtocolError> {
        Ok(DynamicMessage::new(self.message_descriptor(name)?))
    }

    pub fn decode_message(&self, name: &str, data: &[u8]) -> Result<DynamicMessage, ProtocolError> {
        DynamicMessage::decode(self.message_descriptor(name)?, data)
            .map_err(ProtocolError::Protobuf)
    }

    pub fn new_header(&self) -> DynamicMessage {
        DynamicMessage::new(self.header.clone())
    }

    /// Encode one frame and update Header.size when it differs from the body length.
    pub fn encode_frame(
        &self,
        header: &mut DynamicMessage,
        body: &[u8],
    ) -> Result<Vec<u8>, ProtocolError> {
        if header.descriptor() != self.header {
            return Err(ProtocolError::WrongHeader);
        }
        let body_size =
            u32::try_from(body.len()).map_err(|_| ProtocolError::BodyTooLong(body.len()))?;
        // A new proto2 Header omits size when the body is empty. An explicitly
        // present zero stays present, like the existing server.
        let size = Value::U32(body_size);
        if header.get_field(&self.size_field).as_ref() != &size {
            header.set_field(&self.size_field, size);
        }
        validate_required(header)?;
        let header_size = header.encoded_len();
        let length =
            u16::try_from(header_size).map_err(|_| ProtocolError::HeaderTooLong(header_size))?;
        let mut frame = Vec::with_capacity(2 + header_size + body.len());
        frame.extend(length.to_be_bytes());
        frame.extend(header.encode_to_vec());
        frame.extend(body);
        Ok(frame)
    }

    /// Decode the header and borrow the body from this WebSocket message.
    pub fn decode_frame<'a>(
        &self,
        data: &'a [u8],
    ) -> Result<(DynamicMessage, &'a [u8]), ProtocolError> {
        let length = data.get(..2).ok_or(ProtocolError::TruncatedHeader)?;
        let length = usize::from(u16::from_be_bytes(length.try_into().unwrap()));
        let header = data
            .get(2..2 + length)
            .ok_or(ProtocolError::TruncatedHeader)?;
        let header =
            DynamicMessage::decode(self.header.clone(), header).map_err(ProtocolError::Protobuf)?;
        Ok((header, &data[2 + length..]))
    }

    fn message_descriptor(&self, name: &str) -> Result<MessageDescriptor, ProtocolError> {
        self.pool
            .get_message_by_name(name)
            .ok_or_else(|| ProtocolError::UnknownMessage(name.into()))
    }
}

/// Encode an initialized proto2 message.
pub fn encode_message(message: &DynamicMessage) -> Result<Vec<u8>, ProtocolError> {
    validate_required(message)?;
    Ok(message.encode_to_vec())
}

fn validate_required(message: &DynamicMessage) -> Result<(), ProtocolError> {
    for field in message.descriptor().fields() {
        if message.has_field(&field) {
            validate_value(message.get_field(&field).as_ref())?;
        } else if field.cardinality() == Cardinality::Required {
            return Err(ProtocolError::MissingRequiredField(
                field.full_name().into(),
            ));
        }
    }
    Ok(())
}

fn validate_value(value: &Value) -> Result<(), ProtocolError> {
    match value {
        Value::Message(message) => validate_required(message),
        Value::List(values) => values.iter().try_for_each(validate_value),
        Value::Map(values) => values.values().try_for_each(validate_value),
        _ => Ok(()),
    }
}

pub const fn fnv1a32(text: &str) -> u32 {
    let mut value = 0x811c_9dc5_u32;
    let bytes = text.as_bytes();
    let mut offset = 0;
    while offset < bytes.len() {
        value = (value ^ bytes[offset] as u32).wrapping_mul(0x0100_0193);
        offset += 1;
    }
    value
}

pub const CONNECTION_HASH: u32 = fnv1a32("bnet.protocol.connection.ConnectionService");
pub const AUTH_SERVER_HASH: u32 = fnv1a32("bnet.protocol.authentication.AuthenticationServer");
pub const ACCOUNT_HASH: u32 = fnv1a32("bnet.protocol.account.AccountService");
pub const GAME_UTILITIES_HASH: u32 =
    fnv1a32("bnet.protocol.game_utilities.v2.client.GameUtilities");
pub const AUTH_CLIENT_HASH: u32 = fnv1a32("bnet.protocol.authentication.AuthenticationClient");
pub const CHALLENGE_NOTIFY_HASH: u32 = fnv1a32("bnet.protocol.challenge.ChallengeNotify");
pub const ACCOUNT_NOTIFY_HASH: u32 = fnv1a32("bnet.protocol.account.AccountNotify");

pub fn service_name(hash: u32) -> Option<&'static str> {
    match hash {
        CONNECTION_HASH => Some("ConnectionService"),
        AUTH_SERVER_HASH => Some("AuthenticationServer"),
        ACCOUNT_HASH => Some("AccountService"),
        GAME_UTILITIES_HASH => Some("GameUtilities.v2"),
        AUTH_CLIENT_HASH => Some("AuthenticationClient"),
        CHALLENGE_NOTIFY_HASH => Some("ChallengeNotify"),
        ACCOUNT_NOTIFY_HASH => Some("AccountNotify"),
        _ => None,
    }
}

// ConnectionService
pub const CONNECT: u32 = 1;
pub const BIND: u32 = 2;
pub const ECHO: u32 = 3;
pub const FORCE_DISCONNECT: u32 = 4;
pub const KEEP_ALIVE: u32 = 5;
pub const ENCRYPT: u32 = 6;
pub const REQUEST_DISCONNECT: u32 = 7;
// AuthenticationServer
pub const LOGON: u32 = 1;
pub const VERIFY_WEB_CREDENTIALS: u32 = 7;
pub const GENERATE_WEB_CREDENTIALS: u32 = 8;
// GameUtilities
pub const PROCESS_TASK: u32 = 1;
pub const GET_ALL_VALUES_FOR_ATTRIBUTE: u32 = 2;
// ChallengeNotify
pub const ON_EXTERNAL_CHALLENGE: u32 = 3;
// AuthenticationClient
pub const ON_SERVER_STATE_CHANGE: u32 = 4;
pub const ON_LOGON_COMPLETE: u32 = 5;

#[cfg(test)]
mod tests {
    use super::*;

    // A synthetic proto2 FileDescriptorSet with header.proto before its base.proto import.
    // Header: required uint32 service_id=1, optional uint32 method_id=2,
    // required uint32 token=3, optional uint32 size=5, repeated Detail error=7,
    // optional fixed32 service_hash=11. Detail: required uint32 code=1.
    const DESCRIPTORS: &[u8] =
        b"\x0a\xb2\x01\x0a\x0cheader.proto\x12\x0cbgs.protocol\x1a\x0abase.proto\
        \x22\x87\x01\x0a\x06Header\x12\x12\x0a\x0aservice_id\x18\x01\x20\x02\x28\x0d\
        \x12\x11\x0a\x09method_id\x18\x02\x20\x01\x28\x0d\x12\x0d\x0a\x05token\x18\x03\
        \x20\x02\x28\x0d\x12\x0c\x0a\x04size\x18\x05\x20\x01\x28\x0d\
        \x12\x14\x0a\x0cservice_hash\x18\x0b\x20\x01\x28\x07\x12\x23\x0a\x05error\
        \x18\x07\x20\x03\x28\x0b\x32\x14.bgs.protocol.Detail\x0a\x32\x0a\x0abase.proto\
        \x12\x0cbgs.protocol\x22\x16\x0a\x06Detail\x12\x0c\x0a\x04code\x18\x01\x20\x02\x28\x0d";

    fn header(protocol: &Protocol) -> DynamicMessage {
        let mut header = protocol.new_header();
        header.set_field_by_name("service_id", Value::U32(0));
        header.set_field_by_name("method_id", Value::U32(CONNECT));
        header.set_field_by_name("token", Value::U32(0x1234));
        header.set_field_by_name("service_hash", Value::U32(CONNECTION_HASH));
        header
    }

    #[test]
    fn matches_python_battle_net_frame() {
        let protocol = Protocol::from_bytes(DESCRIPTORS).unwrap();
        let mut header = header(&protocol);
        let expected = [
            0x00, 0x0e, 0x08, 0x00, 0x10, 0x01, 0x18, 0xb4, 0x24, 0x28, 0x04, 0x5d, 0x91, 0x69,
            0x44, 0x65, 0x00, b'f', b'o', b'o',
        ];
        assert_eq!(
            protocol.encode_frame(&mut header, b"\0foo").unwrap(),
            expected
        );
        let (decoded, body) = protocol.decode_frame(&expected).unwrap();
        assert_eq!(decoded, header);
        assert_eq!(body, b"\0foo");
    }

    #[test]
    fn preserves_absent_and_explicit_zero_sizes() {
        let protocol = Protocol::from_bytes(DESCRIPTORS).unwrap();
        let mut header = header(&protocol);
        let unset = protocol.encode_frame(&mut header, b"").unwrap();
        assert_eq!(&unset[..2], &[0, 12]);
        assert!(!header.has_field_by_name("size"));
        header.set_field_by_name("size", Value::U32(42));
        let set = protocol.encode_frame(&mut header, b"").unwrap();
        assert_eq!(&set[..2], &[0, 14]);
        assert_eq!(&set[9..11], &[0x28, 0]);
        assert!(header.has_field_by_name("size"));
        assert_eq!(protocol.encode_frame(&mut header, b"").unwrap(), set);
    }

    #[test]
    fn rejects_truncated_headers_and_incomplete_messages() {
        let protocol = Protocol::from_bytes(DESCRIPTORS).unwrap();
        for data in [&[][..], &[0][..], &[0, 6, 0x08, 0x00][..]] {
            assert!(matches!(
                protocol.decode_frame(data),
                Err(ProtocolError::TruncatedHeader)
            ));
        }
        assert!(matches!(
            protocol.decode_frame(&[0, 1, 0xff]),
            Err(ProtocolError::Protobuf(_))
        ));
        assert!(matches!(
            protocol.encode_frame(&mut protocol.new_header(), b""),
            Err(ProtocolError::MissingRequiredField(_))
        ));

        let incomplete = protocol.new_message("bgs.protocol.Detail").unwrap();
        let mut header = header(&protocol);
        header.set_field_by_name("error", Value::List(vec![Value::Message(incomplete)]));
        assert!(matches!(
            encode_message(&header),
            Err(ProtocolError::MissingRequiredField(name)) if name == "bgs.protocol.Detail.code"
        ));
        assert!(matches!(
            protocol.new_message("bgs.protocol.Unknown"),
            Err(ProtocolError::UnknownMessage(_))
        ));
    }

    #[test]
    fn service_hashes_match_the_protocol() {
        assert_eq!(CONNECTION_HASH, 0x6544_6991);
        assert_eq!(AUTH_SERVER_HASH, 0x0dec_fc01);
        assert_eq!(ACCOUNT_HASH, 0x62da_0891);
        assert_eq!(GAME_UTILITIES_HASH, 0x5dbb_51c2);
        assert_eq!(AUTH_CLIENT_HASH, 0x7124_0e35);
        assert_eq!(CHALLENGE_NOTIFY_HASH, 0xbbda_171f);
        assert_eq!(ACCOUNT_NOTIFY_HASH, 0x54df_da17);
        assert_eq!(service_name(GAME_UTILITIES_HASH), Some("GameUtilities.v2"));
        assert_eq!(service_name(0), None);
    }

    #[test]
    #[ignore = "requires the separately prepared Battle.net descriptor set"]
    fn loads_external_descriptors() {
        let path = std::env::var_os("ORPHEUS_BNET_DESCRIPTORS")
            .expect("set ORPHEUS_BNET_DESCRIPTORS to the prepared descriptor set");
        let protocol = Protocol::load(path).unwrap();
        for name in [
            CONNECT_REQUEST,
            CONNECT_RESPONSE,
            LOGON_REQUEST,
            LOGON_RESULT,
            PROCESS_TASK_REQUEST,
            PROCESS_TASK_RESPONSE,
        ] {
            assert!(protocol.new_message(name).is_ok(), "{name}");
        }
        let mut header = header(&protocol);
        let frame = protocol.encode_frame(&mut header, b"body").unwrap();
        let (decoded, body) = protocol.decode_frame(&frame).unwrap();
        assert_eq!(decoded, header);
        assert_eq!(body, b"body");
    }
}
