//! Retail 104319 lobby handshake. The signing input is supplied by the local runtime.

use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::jam_cipher::Jam;

const HELLO_CLIENT: &[u8; 17] = b"HELLO PRO CLIENT\0";
const HELLO_SERVER: &[u8; 17] = b"HELLO PRO SERVER\0";
const ZERO_KEY: [u8; 32] = [0; 32];
const READ_TIMEOUT: Duration = Duration::from_secs(30);
const DIFFICULTY: u8 = 0;
type HmacSha256 = Hmac<Sha256>;

pub struct Channel {
    /// Server to client, advanced past the encrypted state blob.
    pub tx: Jam,
    /// Client to server, ready for the first lobby frame.
    pub rx: Jam,
    pub seq: u32,
}

fn mac(key: &[u8], first: &[u8], second: &[u8]) -> HmacSha256 {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC supports arbitrary key lengths");
    mac.update(first);
    mac.update(second);
    mac
}

fn client_value(stream: &mut TcpStream) -> io::Result<[u8; 32]> {
    let mut message = [0; 40];
    stream.read_exact(&mut message)?;
    Ok(message[8..].try_into().unwrap())
}

/// Construct the address-bound state record from a separately provided signing key.
pub fn build_state_blob(seq: u32, host: Ipv4Addr, port: u16, signing_key: &[u8; 64]) -> [u8; 292] {
    let mut blob = [0; 292];
    for (i, byte) in blob[..256].iter_mut().enumerate() {
        *byte = i as u8;
    }
    blob[0] = 2;
    blob[1..5].copy_from_slice(&host.octets());
    blob[5..7].copy_from_slice(&port.to_le_bytes());
    let signature = mac(signing_key, &blob[..176], &blob[208..256])
        .finalize()
        .into_bytes();
    blob[176..208].copy_from_slice(&signature);

    // Peer ID, channel ID and final state flag.
    blob[256..260].copy_from_slice(&seq.to_le_bytes());
    blob[261..266].copy_from_slice(&[5, 0, 1, 0xef, 0xbe]);
    blob[272..276].copy_from_slice(&seq.to_le_bytes());
    blob[277..280].copy_from_slice(&[13, 0, 1]);
    blob[282..284].copy_from_slice(&port.to_le_bytes());
    blob[284..288].copy_from_slice(&host.octets());
    blob[288..292].copy_from_slice(&1u32.to_le_bytes());
    blob
}

/// Complete the lobby handshake on the accepted IPv4 connection.
pub fn server_handshake(
    stream: &mut TcpStream,
    conn_id: u32,
    signing_key: &[u8; 64],
) -> io::Result<Channel> {
    let SocketAddr::V4(address) = stream.local_addr()? else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "the lobby handshake requires IPv4",
        ));
    };
    stream.set_read_timeout(Some(READ_TIMEOUT))?;

    let mut hello = [0; HELLO_CLIENT.len()];
    stream.read_exact(&mut hello)?;
    if &hello != HELLO_CLIENT {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unexpected lobby hello",
        ));
    }
    stream.write_all(HELLO_SERVER)?;

    let client_nonce = client_value(stream)?;
    let mut challenge = [0; 65];
    getrandom::fill(&mut challenge).map_err(|error| io::Error::other(error.to_string()))?;
    challenge[32] = DIFFICULTY;
    stream.write_all(&challenge)?;
    let server_nonce = &challenge[33..];

    let client_proof = client_value(stream)?;
    mac(&ZERO_KEY, &client_nonce, server_nonce)
        .verify_slice(&client_proof)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "lobby proof does not match"))?;
    let server_proof: [u8; 32] = mac(&ZERO_KEY, server_nonce, &client_nonce)
        .finalize()
        .into_bytes()
        .into();

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(io::Error::other)?;
    let seq = now.as_millis() as u32 ^ (conn_id << 16);
    let mut tx = Jam::new(&client_proof);
    let mut blob = build_state_blob(seq, *address.ip(), address.port(), signing_key);
    tx.crypt_in_place(&mut blob);
    let mut response = [0; 324];
    response[..32].copy_from_slice(&server_proof);
    response[32..].copy_from_slice(&blob);
    stream.write_all(&response)?;
    Ok(Channel {
        tx,
        rx: Jam::new(&server_proof),
        seq,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    use sha2::Digest;

    use crate::jam_framing::{self, FrameReader};

    #[test]
    fn state_blob_matches_reference_with_a_synthetic_key() {
        let key = std::array::from_fn(|i| i as u8);
        let blob = build_state_blob(0x1234_5678, Ipv4Addr::new(192, 0, 2, 10), 12357, &key);
        assert_eq!(
            format!("{:x}", Sha256::digest(blob)),
            "6e501e5e6f00f3aa809489a10d98cb7616eb1b4f5a8e910d5d303678ba660198"
        );
    }

    #[test]
    fn lobby_handshake_exchanges_encrypted_frames_over_loopback() {
        let key = [0x42; 64];
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let channel = server_handshake(&mut stream, 4, &key).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut incoming = FrameReader::new(channel.rx);
            let mut bytes = [0; 5];
            stream.read_exact(&mut bytes).unwrap();
            assert_eq!(incoming.feed(&bytes), [jam_framing::PONG]);
            jam_framing::send_frame(&mut stream, &mut { channel.tx }, &jam_framing::CONNECTED)
                .unwrap();
            channel.seq
        });

        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        client.write_all(HELLO_CLIENT).unwrap();
        let mut hello = [0; HELLO_SERVER.len()];
        client.read_exact(&mut hello).unwrap();
        assert_eq!(&hello, HELLO_SERVER);

        let mut nonce = [0x83; 40];
        nonce[..8].fill(0);
        client.write_all(&nonce).unwrap();
        let mut challenge = [0; 65];
        client.read_exact(&mut challenge).unwrap();
        assert_eq!(challenge[32], DIFFICULTY);

        let client_nonce = &nonce[8..];
        let server_nonce = &challenge[33..];
        let proof: [u8; 32] = mac(&ZERO_KEY, client_nonce, server_nonce)
            .finalize()
            .into_bytes()
            .into();
        let mut message = [0; 40];
        message[8..].copy_from_slice(&proof);
        client.write_all(&message).unwrap();

        let mut response = [0; 324];
        client.read_exact(&mut response).unwrap();
        mac(&ZERO_KEY, server_nonce, client_nonce)
            .verify_slice(&response[..32])
            .unwrap();

        let mut rx = Jam::new(&proof);
        let mut tx = Jam::new(response[..32].try_into().unwrap());
        rx.crypt_in_place(&mut response[32..]);
        jam_framing::send_frame(&mut client, &mut tx, &jam_framing::PONG).unwrap();

        let mut reader = FrameReader::new(rx);
        let mut bytes = [0; 5];
        client.read_exact(&mut bytes).unwrap();
        assert_eq!(reader.feed(&bytes), [jam_framing::CONNECTED]);
        let seq = server.join().unwrap();
        assert_eq!(
            response[32..],
            build_state_blob(seq, Ipv4Addr::LOCALHOST, address.port(), &key)
        );
    }
}
