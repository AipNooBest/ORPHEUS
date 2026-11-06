//! The local Battle.net emulator passes the UTF-8 account name to the lobby in the login key.

use std::io;

pub const KEY_SIZE: usize = 64;
const MARK: &[u8] = b"ow174:";
pub const MAX_NAME_BYTES: usize = KEY_SIZE - MARK.len();

pub fn session_key(name: &str) -> io::Result<[u8; KEY_SIZE]> {
    if name.len() > MAX_NAME_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "account name exceeds session key size",
        ));
    }
    let mut key = [0; KEY_SIZE];
    key[..MARK.len()].copy_from_slice(MARK);
    key[MARK.len()..MARK.len() + name.len()].copy_from_slice(name.as_bytes());
    Ok(key)
}

pub fn name_in_key(key: &[u8]) -> Option<String> {
    let name = key.strip_prefix(MARK)?;
    let end = name.iter().rposition(|&byte| byte != 0)? + 1;
    let name = &name[..end];
    let decoded = String::from_utf8_lossy(name);
    let name = decoded.trim();
    (!name.is_empty()).then(|| name.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_key_keeps_the_reference_marker_and_utf8_bytes() {
        let key = session_key("测试").unwrap();
        assert_eq!(&key[..12], b"ow174:\xe6\xb5\x8b\xe8\xaf\x95");
        assert_eq!(&key[12..], &[0; 52]);
        assert_eq!(name_in_key(&key).as_deref(), Some("测试"));
        assert_eq!(name_in_key(b"ow174: X \0\0").as_deref(), Some("X"));
        assert_eq!(name_in_key(b"ow174:\0\0"), None);
        assert_eq!(name_in_key(b"other"), None);
        assert!(session_key(&"x".repeat(MAX_NAME_BYTES)).is_ok());
        assert!(session_key(&"x".repeat(MAX_NAME_BYTES + 1)).is_err());
    }
}
