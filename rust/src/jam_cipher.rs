//! PANAMA stream cipher in the JAM lobby protocol's word order.
//!
//! Ported from `ow174/jam/cipher.py`. The original MIT notice and upstream
//! attribution are retained in `LICENSE` and `CREDITS.md`.

const BLOCK_SIZE: usize = 32;

// Gamma inputs are ordered as (complement, OR, XOR) for each state word.
const GAMMA_INPUTS: [(usize, usize, usize); 17] = [
    (4, 8, 12),
    (5, 9, 13),
    (6, 10, 14),
    (7, 11, 15),
    (0, 4, 8),
    (1, 5, 9),
    (2, 6, 10),
    (3, 7, 11),
    (13, 0, 4),
    (14, 1, 5),
    (15, 2, 6),
    (16, 3, 7),
    (9, 13, 0),
    (10, 14, 1),
    (11, 15, 2),
    (12, 16, 3),
    (8, 12, 16),
];

// Pi destinations and rotation counts for the first 16 words. Word 16 stays.
const PI_OUTPUT: [(usize, u32); 16] = [
    (13, 15),
    (1, 4),
    (6, 2),
    (11, 9),
    (10, 23),
    (15, 27),
    (3, 8),
    (8, 3),
    (7, 24),
    (12, 1),
    (0, 10),
    (5, 28),
    (4, 6),
    (9, 21),
    (14, 13),
    (2, 14),
];

const NEAR_SLOTS: [usize; 8] = [0, 2, 4, 6, 1, 3, 5, 7];
const FAR_SLOTS: [usize; 8] = [5, 7, 0, 2, 4, 6, 1, 3];
const KEYSTREAM_WORDS: [usize; 8] = [14, 10, 6, 2, 15, 11, 7, 3];

fn panama_round(state: &[u32; 17], inject: &[u32; 8], stage: &[u32; 8]) -> [u32; 17] {
    let gamma = GAMMA_INPUTS.map(|(x, y, z)| (!state[x] | state[y]) ^ state[z]);
    let mut pi = [0; 17];
    for (word, (index, shift)) in gamma.into_iter().zip(PI_OUTPUT) {
        pi[index] = word.rotate_left(shift);
    }
    pi[16] = gamma[16];

    let mut theta = std::array::from_fn(|i| pi[i] ^ pi[(i + 1) % 17] ^ pi[(i + 13) % 17]);
    theta[16] ^= 1;
    for (group, pair) in (0..4).rev().enumerate() {
        let first = 4 * group;
        theta[first] ^= inject[2 * pair];
        theta[first + 1] ^= inject[2 * pair + 1];
        theta[first + 2] ^= stage[2 * pair];
        theta[first + 3] ^= stage[2 * pair + 1];
    }
    theta
}

/// One direction of a JAM connection's stream. Calls consume consecutive bytes.
pub struct Jam {
    state: [u32; 17],
    buffer: [[u32; 8]; 32],
    counter: u32,
    block: [u8; BLOCK_SIZE],
    position: usize,
}

impl Jam {
    /// Absorbs the 32-byte key twice and runs 32 blank steps.
    pub fn new(key: &[u8; 32]) -> Self {
        let mut cipher = Self {
            state: [0; 17],
            buffer: [[0; 8]; 32],
            counter: 1,
            block: [0; BLOCK_SIZE],
            position: BLOCK_SIZE,
        };
        let mut words = [0; 8];
        for (word, bytes) in words.iter_mut().zip(key.chunks_exact(4)) {
            *word = u32::from_le_bytes(bytes.try_into().expect("four-byte key word"));
        }
        for _ in 0..2 {
            cipher.push(&words);
        }
        for _ in 0..32 {
            cipher.pull();
        }
        cipher
    }

    fn stage_index(&self, offset: i32) -> usize {
        ((self.counter.wrapping_add_signed(offset) & 0x3e0) >> 5) as usize
    }

    fn feed_buffer(&mut self, words: [u32; 8]) {
        let near = self.stage_index(0x20);
        let far = self.stage_index(0x100);
        for ((word, near_slot), far_slot) in words.into_iter().zip(NEAR_SLOTS).zip(FAR_SLOTS) {
            let old = self.buffer[near][near_slot];
            self.buffer[near][near_slot] = old ^ word;
            self.buffer[far][far_slot] ^= old;
        }
    }

    fn push(&mut self, words: &[u32; 8]) {
        self.feed_buffer(*words);
        let inject = [0, 4, 1, 5, 2, 6, 3, 7].map(|index| words[index]);
        self.state = panama_round(&self.state, &inject, &self.buffer[self.stage_index(-0x200)]);
        self.counter = self.counter.wrapping_add(0x20);
    }

    fn pull(&mut self) {
        self.feed_buffer([12, 8, 4, 0, 13, 9, 5, 1].map(|index| self.state[index]));
        let inject = self.stage_index(-0x80);
        let stage = self.stage_index(0x200);
        self.state = panama_round(&self.state, &self.buffer[inject], &self.buffer[stage]);
        self.counter = self.counter.wrapping_add(0x20);
    }

    fn refill_block(&mut self) {
        for (bytes, index) in self.block.chunks_exact_mut(4).zip(KEYSTREAM_WORDS) {
            bytes.copy_from_slice(&self.state[index].to_le_bytes());
        }
        self.pull();
        self.position = 0;
    }

    /// Encrypts or decrypts a copy of `data`.
    pub fn crypt(&mut self, data: &[u8]) -> Vec<u8> {
        let mut output = data.to_vec();
        self.crypt_in_place(&mut output);
        output
    }

    /// Encrypts or decrypts `data`, retaining any unused bytes of the current block.
    pub fn crypt_in_place(&mut self, data: &mut [u8]) {
        for byte in data {
            if self.position == BLOCK_SIZE {
                self.refill_block();
            }
            *byte ^= self.block[self.position];
            self.position += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::Jam;

    fn fixture() -> ([u8; 32], Vec<u8>) {
        (
            std::array::from_fn(|i| i as u8),
            (0..=255).cycle().take(1024).collect(),
        )
    }

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    #[test]
    fn matches_the_python_cipher_vector() {
        let (key, plain) = fixture();
        let output = Jam::new(&key).crypt(&plain);
        // Bytes from the fixture in tests/test_wire_vectors.py, including the
        // last two blocks after the buffer's stage index has wrapped.
        assert_eq!(
            hex(&output[..96]),
            concat!(
                "f07f5ff2ccd01a0a7d44acd6d239c2af0da1ff35275baf5dfa6e09411b79d8b9",
                "596c5893e82c5ec86bf71a5d4653cff8f93f27aec60ce236557c3598e41d467d",
                "22a8b8c0aa723fc25de66c8a4eeedb4668e483de907dd8a05a3fddf75d1efb0d",
            )
        );
        assert_eq!(
            hex(&output[960..]),
            concat!(
                "da9d74f80fdf4c239bc12c4a6fb4ca15d51e8c5a1d3310b2b97682f295bcdeb3",
                "06e984e5ff727d62f57e9ab84432397f34c5b3fff67af441ebf2e70f7cb8dd78",
            )
        );
    }

    #[test]
    fn zero_key_matches_the_python_keystream() {
        let mut output = [0; 64];
        Jam::new(&[0; 32]).crypt_in_place(&mut output);
        assert_eq!(
            hex(&output),
            concat!(
                "6e8de383dc3055fe535cc7aeaa91d3a53df4194affb88000ba512af99099c455",
                "054d5d608a06df8dd525537933be7fe6f42815c22942eb19dfa2ea2af0b75c59",
            )
        );
    }

    #[test]
    fn fragmented_calls_continue_the_stream_and_decrypt() {
        let (key, plain) = fixture();
        let whole = Jam::new(&key).crypt(&plain);
        let mut cipher = Jam::new(&key);
        let mut fragmented = Vec::new();
        for piece in plain.chunks(37) {
            assert!(cipher.crypt(&[]).is_empty());
            fragmented.extend(cipher.crypt(piece));
        }
        assert_eq!(fragmented, whole);

        let mut decipher = Jam::new(&key);
        for piece in fragmented.chunks_mut(31) {
            decipher.crypt_in_place(piece);
        }
        assert_eq!(fragmented, plain);
    }
}
