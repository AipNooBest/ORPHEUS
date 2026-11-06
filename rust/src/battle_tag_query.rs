//! Before a remote game starts, its launcher asks the lobby for the account's displayed BattleTag.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

const QUERY: &[u8] = b"OW174 BATTLETAG ";
const TIMEOUT: Duration = Duration::from_secs(5);
const MAX_LINE_BYTES: u64 = 256;

pub fn ask(address: impl ToSocketAddrs, name: &str) -> io::Result<String> {
    let mut addresses = address.to_socket_addrs()?.peekable();
    let mut stream = loop {
        let address = addresses.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "no lobby address resolved")
        })?;
        match TcpStream::connect_timeout(&address, TIMEOUT) {
            Ok(stream) => break stream,
            Err(error) if addresses.peek().is_none() => return Err(error),
            Err(_) => (),
        }
    };
    stream.set_read_timeout(Some(TIMEOUT))?;
    stream.set_write_timeout(Some(TIMEOUT))?;

    let mut query = Vec::with_capacity(QUERY.len() + name.len() + 1);
    query.extend_from_slice(QUERY);
    query.extend_from_slice(name.as_bytes());
    query.push(b'\n');
    stream.write_all(&query)?;
    // An older lobby closes the connection when it receives this query instead of a game hello.
    Ok(read_line(&mut stream).unwrap_or_default())
}

/// A game hello stays unread so the caller can run its lobby handshake.
pub fn answer(
    stream: &mut TcpStream,
    battle_tag_of: impl FnOnce(&str) -> String,
) -> io::Result<bool> {
    let mut prefix = [0; QUERY.len()];
    if stream.peek(&mut prefix)? != prefix.len() || prefix != QUERY {
        return Ok(false);
    }
    let query = read_line(stream)?;
    let name = query.strip_prefix("OW174 BATTLETAG").unwrap_or("").trim();
    let mut tag = battle_tag_of(name);
    tag.push('\n');
    stream.write_all(tag.as_bytes())?;
    Ok(true)
}

fn read_line(stream: &mut TcpStream) -> io::Result<String> {
    let mut bytes = Vec::new();
    BufReader::new(stream)
        .take(MAX_LINE_BYTES)
        .read_until(b'\n', &mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes).trim().to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv4Addr, TcpListener};
    use std::thread;

    #[test]
    fn gets_the_host_nickname_and_leaves_a_game_hello_unread() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            assert!(
                answer(&mut stream, |name| {
                    assert_eq!(name, "玩家");
                    "Nickname#1234".into()
                })
                .unwrap()
            );

            let (mut stream, _) = listener.accept().unwrap();
            assert!(
                !answer(&mut stream, |_| panic!(
                    "game connection treated as BattleTag query"
                ))
                .unwrap()
            );
            let mut hello = [0; 17];
            stream.read_exact(&mut hello).unwrap();
            assert_eq!(&hello, b"HELLO PRO CLIENT\0");
        });

        assert_eq!(ask(address, " 玩家 ").unwrap(), "Nickname#1234");
        let mut game = TcpStream::connect(address).unwrap();
        game.write_all(b"HELLO PRO CLIENT\0").unwrap();
        server.join().unwrap();
    }
}
