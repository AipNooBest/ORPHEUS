//! Overwatch 1.74's game UDP packet format and receive window.
//!
//! The header's first 12 bytes are the authentication tag. The remaining
//! 22 bytes are authenticated additional data. The payload uses AES-256-GCM
//! with an eight-byte direction salt and the little-endian packet sequence as
//! its nonce. Keys are specific to the connection and direction.

use aes_gcm::{
    AesGcm, Nonce, Tag,
    aead::{AeadInPlace, KeyInit, consts::U12},
    aes::Aes256,
};

pub const HEADER_SIZE: usize = 34;
pub const TAG_SIZE: usize = 12;
pub const MAX_DATAGRAM: usize = 0x4f0;
pub const MAGIC: u8 = 0xad;
pub const SYN: u8 = 1;
pub const ACK: u8 = 2;
pub const FIN: u8 = 4;
pub const KIND_CONTROL: u8 = 0;
pub const KIND_FRAME: u8 = 1;
pub const ACK_WINDOW: u32 = 64;

const AUTHENTICATED_HEADER_SIZE: usize = HEADER_SIZE - TAG_SIZE;
const SEQUENCE_HALF: u32 = 1 << 31;

type GameAesGcm = AesGcm<Aes256, U12, U12>;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Packet {
    pub conn: u32,
    pub kind: u8,
    pub seq: u32,
    pub ack: u32,
    pub ack_bits: u64,
    pub flags: u8,
    pub payload: Vec<u8>,
}

impl Packet {
    /// Header bytes after the authentication tag.
    pub fn header(&self) -> [u8; AUTHENTICATED_HEADER_SIZE] {
        let mut header = [0; AUTHENTICATED_HEADER_SIZE];
        let word = (self.conn << 4) | u32::from(self.kind & 0x0f);
        header[..4].copy_from_slice(&word.to_le_bytes());
        header[4..8].copy_from_slice(&self.seq.to_le_bytes());
        header[8..12].copy_from_slice(&self.ack.to_le_bytes());
        header[12..20].copy_from_slice(&self.ack_bits.to_le_bytes());
        header[20] = self.flags;
        header[21] = MAGIC;
        header
    }
}

/// Peek at the untrusted connection ID to select a cipher for authentication.
pub fn peek_connection(datagram: &[u8]) -> Option<u32> {
    if datagram.len() < HEADER_SIZE || datagram[HEADER_SIZE - 1] != MAGIC {
        return None;
    }
    let word = u32::from_le_bytes(datagram[TAG_SIZE..TAG_SIZE + 4].try_into().unwrap());
    Some(word >> 4)
}

pub struct LinkCipher {
    cipher_in: GameAesGcm,
    cipher_out: GameAesGcm,
    salt_in: [u8; 8],
    salt_out: [u8; 8],
}

impl LinkCipher {
    /// Construct a link with the default all-zero salts used by the server.
    pub fn new(key_in: [u8; 32], key_out: [u8; 32]) -> Self {
        Self::with_salts(key_in, key_out, [0; 8], [0; 8])
    }

    pub fn with_salts(
        key_in: [u8; 32],
        key_out: [u8; 32],
        salt_in: [u8; 8],
        salt_out: [u8; 8],
    ) -> Self {
        Self {
            cipher_in: GameAesGcm::new((&key_in).into()),
            cipher_out: GameAesGcm::new((&key_out).into()),
            salt_in,
            salt_out,
        }
    }

    /// Seal a server-to-client datagram.
    ///
    /// The sender must not reuse a sequence with the same key and salt.
    pub fn seal(&self, packet: &Packet) -> Result<Vec<u8>, aes_gcm::Error> {
        let header = packet.header();
        let nonce = make_nonce(self.salt_out, packet.seq);
        let mut datagram = Vec::with_capacity(HEADER_SIZE + packet.payload.len());
        datagram.extend_from_slice(&[0; TAG_SIZE]);
        datagram.extend_from_slice(&header);
        datagram.extend_from_slice(&packet.payload);
        let tag = self.cipher_out.encrypt_in_place_detached(
            Nonce::<U12>::from_slice(&nonce),
            &header,
            &mut datagram[HEADER_SIZE..],
        )?;
        datagram[..TAG_SIZE].copy_from_slice(&tag);
        Ok(datagram)
    }

    /// Open a client-to-server datagram, returning None on format or authentication failure.
    pub fn open(&self, datagram: &[u8]) -> Option<Packet> {
        if !(HEADER_SIZE..=MAX_DATAGRAM).contains(&datagram.len())
            || datagram[HEADER_SIZE - 1] != MAGIC
        {
            return None;
        }
        let header = &datagram[TAG_SIZE..HEADER_SIZE];
        let seq = u32::from_le_bytes(header[4..8].try_into().unwrap());
        let nonce = make_nonce(self.salt_in, seq);
        let mut payload = datagram[HEADER_SIZE..].to_vec();
        self.cipher_in
            .decrypt_in_place_detached(
                Nonce::<U12>::from_slice(&nonce),
                header,
                &mut payload,
                Tag::<U12>::from_slice(&datagram[..TAG_SIZE]),
            )
            .ok()?;

        let word = u32::from_le_bytes(header[..4].try_into().unwrap());
        Some(Packet {
            conn: word >> 4,
            kind: (word & 0x0f) as u8,
            seq,
            ack: u32::from_le_bytes(header[8..12].try_into().unwrap()),
            ack_bits: u64::from_le_bytes(header[12..20].try_into().unwrap()),
            flags: header[20],
            payload,
        })
    }
}

fn make_nonce(salt: [u8; 8], seq: u32) -> [u8; 12] {
    let mut nonce = [0; 12];
    nonce[..8].copy_from_slice(&salt);
    nonce[8..].copy_from_slice(&seq.to_le_bytes());
    nonce
}

/// Incoming sequence and acknowledgement history for the next outgoing header.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReceiveWindow {
    pub expected: u32,
    pub bits: u64,
}

impl ReceiveWindow {
    pub fn is_new(&self, seq: u32) -> bool {
        seq.wrapping_sub(self.expected) < SEQUENCE_HALF
    }

    /// Advance to the new sequence, ignoring duplicates and older datagrams.
    pub fn accept(&mut self, seq: u32) -> bool {
        if !self.is_new(seq) {
            return false;
        }
        let shift = seq.wrapping_sub(self.expected) + 1;
        self.bits = self.bits.checked_shl(shift).unwrap_or(0) | 1;
        self.expected = seq.wrapping_add(1);
        true
    }
}

/// Interpret the peer's ACK. None means it has not reached this sequence.
pub fn delivery(seq: u32, ack: u32, ack_bits: u64) -> Option<bool> {
    let back = ack.wrapping_sub(1).wrapping_sub(seq);
    if back >= SEQUENCE_HALF {
        return None;
    }
    Some(back < ACK_WINDOW && (ack_bits >> back) & 1 != 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn link_pair() -> (LinkCipher, LinkCipher) {
        let client_key = std::array::from_fn(|index| index as u8);
        let server_key = std::array::from_fn(|index| 32 + index as u8);
        let client_salt = *b"clients!";
        let server_salt = *b"servers!";
        (
            LinkCipher::with_salts(client_key, server_key, client_salt, server_salt),
            LinkCipher::with_salts(server_key, client_key, server_salt, client_salt),
        )
    }

    #[test]
    fn matches_python_datagrams_in_both_directions() {
        // Produced by ow174.game.link.LinkCipher with these deterministic test keys.
        let incoming = [
            0x79, 0xfa, 0x10, 0xf0, 0x07, 0x01, 0x2f, 0x27, 0x8d, 0x7d, 0x29, 0x29, 0x71, 0x56,
            0x34, 0x12, 0x67, 0x45, 0x23, 0x01, 0xef, 0xcd, 0xab, 0x89, 0xe0, 0xac, 0x68, 0x24,
            0xdf, 0x9b, 0x57, 0x13, 0x02, 0xad, 0x58, 0x3e, 0xe4, 0x37, 0x46, 0x26, 0x4b, 0xdc,
            0x7e,
        ];
        let outgoing = [
            0x16, 0x5d, 0x56, 0x31, 0x50, 0x6c, 0x88, 0x2e, 0x96, 0xf8, 0x7e, 0xca, 0x70, 0x56,
            0x34, 0x12, 0xff, 0xff, 0xff, 0xff, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x03, 0xad,
        ];
        let conn = 0x0123_4567;
        let frame = Packet {
            conn,
            kind: KIND_FRAME,
            seq: 0x0123_4567,
            ack: 0x89ab_cdef,
            ack_bits: 0x1357_9bdf_2468_ace0,
            flags: ACK,
            payload: b"\0ORPHEUS\xff".to_vec(),
        };
        let syn_ack = Packet {
            conn,
            seq: u32::MAX,
            ack: 1,
            ack_bits: 1,
            flags: SYN | ACK,
            ..Packet::default()
        };
        let (server, client) = link_pair();
        assert_eq!(peek_connection(&incoming), Some(conn));
        assert_eq!(client.seal(&frame).unwrap(), incoming);
        assert_eq!(server.open(&incoming), Some(frame));
        assert_eq!(server.seal(&syn_ack).unwrap(), outgoing);
        assert_eq!(client.open(&outgoing), Some(syn_ack));
    }

    #[test]
    fn rejects_tampering_and_wrong_directions() {
        let (server, client) = link_pair();
        let packet = Packet {
            conn: 1,
            seq: 9,
            kind: KIND_FRAME,
            payload: b"frame".to_vec(),
            ..Packet::default()
        };
        let mut datagram = client.seal(&packet).unwrap();
        assert_eq!(client.open(&datagram), None);
        for offset in [0, TAG_SIZE, 16, 20, 32, HEADER_SIZE - 1, HEADER_SIZE] {
            datagram[offset] ^= 1;
            assert_eq!(server.open(&datagram), None);
            datagram[offset] ^= 1;
        }
        assert_eq!(server.open(&datagram), Some(packet));
    }

    #[test]
    fn validates_datagram_sizes_and_magic() {
        let key = [0; 32];
        let cipher = LinkCipher::new(key, key);
        let mut packet = Packet {
            payload: vec![0; MAX_DATAGRAM - HEADER_SIZE],
            ..Packet::default()
        };
        let valid = cipher.seal(&packet).unwrap();
        assert_eq!(cipher.open(&valid), Some(packet.clone()));
        assert_eq!(peek_connection(&valid[..HEADER_SIZE - 1]), None);
        assert_eq!(cipher.open(&valid[..HEADER_SIZE - 1]), None);

        packet.seq = 1;
        packet.payload.push(0);
        let oversized = cipher.seal(&packet).unwrap();
        assert_eq!(peek_connection(&oversized), Some(0));
        assert_eq!(cipher.open(&oversized), None);

        let mut bad_magic = valid;
        bad_magic[HEADER_SIZE - 1] = 0;
        assert_eq!(peek_connection(&bad_magic), None);
    }

    #[test]
    fn acknowledges_arrivals_but_drops_late_packets() {
        let mut window = ReceiveWindow::default();
        for seq in [0, 1, 3] {
            assert!(window.accept(seq));
        }
        assert!(!window.accept(2));
        assert!(!window.accept(3));
        assert_eq!(
            window,
            ReceiveWindow {
                expected: 4,
                bits: 0b1101
            }
        );
        assert_eq!(delivery(3, window.expected, window.bits), Some(true));
        assert_eq!(delivery(2, window.expected, window.bits), Some(false));
        assert_eq!(delivery(0, window.expected, window.bits), Some(true));
        assert_eq!(delivery(4, window.expected, window.bits), None);
    }

    #[test]
    fn acknowledgements_wrap_and_expire() {
        let mut window = ReceiveWindow {
            expected: u32::MAX - 1,
            bits: 0,
        };
        assert!(window.accept(u32::MAX - 1));
        assert!(window.accept(0));
        assert!(!window.accept(u32::MAX));
        assert!(!window.is_new(window.expected.wrapping_add(SEQUENCE_HALF)));
        assert_eq!(
            window,
            ReceiveWindow {
                expected: 1,
                bits: 0b101
            }
        );
        assert_eq!(
            delivery(u32::MAX - 1, window.expected, window.bits),
            Some(true)
        );
        assert_eq!(
            delivery(u32::MAX, window.expected, window.bits),
            Some(false)
        );
        assert!(window.accept(63));
        assert_eq!(window.bits, (1 << 63) | 1);
        assert_eq!(delivery(0, window.expected, window.bits), Some(true));
        assert!(window.accept(64));
        assert_eq!(window.bits, 0b11);
        assert_eq!(delivery(0, window.expected, window.bits), Some(false));
        assert!(window.accept(128));
        assert_eq!(window.bits, 1);
    }
}
