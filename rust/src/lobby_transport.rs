//! Encrypted lobby connection and its negotiated group indexes.

use std::collections::{HashMap, VecDeque};
use std::io::{self, Read};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use crate::jam_cipher::Jam;
use crate::jam_framing::{self, FrameReader};
use crate::jam_handshake::Channel;

const POLL: Duration = Duration::from_secs(2);
const KEEPALIVE: Duration = Duration::from_secs(10);

#[derive(Debug, PartialEq, Eq)]
pub enum Event {
    Announced,
    Message { crc: u32, offset: u8, body: Vec<u8> },
}

pub struct LobbyTransport {
    stream: TcpStream,
    reader: FrameReader,
    tx: Jam,
    pending: VecDeque<Vec<u8>>,
    groups: Vec<u32>,
    wire_of: HashMap<u32, u8>,
    last_keepalive: Instant,
    pub sequence: u32,
}

impl LobbyTransport {
    pub fn new(stream: TcpStream, channel: Channel) -> io::Result<Self> {
        stream.set_read_timeout(Some(POLL))?;
        Ok(Self {
            stream,
            reader: FrameReader::new(channel.rx),
            tx: channel.tx,
            pending: VecDeque::new(),
            groups: Vec::new(),
            wire_of: HashMap::new(),
            last_keepalive: Instant::now(),
            sequence: channel.seq,
        })
    }

    pub fn groups(&self) -> &[u32] {
        &self.groups
    }

    pub fn wire_for(&self, crc: u32) -> Option<u8> {
        self.wire_of.get(&crc).copied()
    }

    pub fn send(&mut self, payload: &[u8]) -> io::Result<()> {
        jam_framing::send_frame(&mut self.stream, &mut self.tx, payload)
    }

    /// Handle control traffic in frame order and return the next lobby event.
    pub fn receive(&mut self) -> io::Result<Option<Event>> {
        let mut data = [0; 65_536];
        loop {
            while let Some(frame) = self.pending.pop_front() {
                if let Some(event) = self.handle_frame(frame)? {
                    return Ok(Some(event));
                }
            }

            match self.stream.read(&mut data) {
                Ok(0) => return Ok(None),
                Ok(count) => self.pending.extend(self.reader.feed(&data[..count])),
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                    ) =>
                {
                    if self.last_keepalive.elapsed() > KEEPALIVE {
                        self.send(&jam_framing::PONG)?;
                        self.last_keepalive = Instant::now();
                    }
                }
                Err(error) => return Err(error),
            }
        }
    }

    fn handle_frame(&mut self, frame: Vec<u8>) -> io::Result<Option<Event>> {
        let [wire, offset, ..] = frame.as_slice() else {
            return Ok(None);
        };
        if *wire != jam_framing::CONTROL_WIRE {
            let Some(&crc) = self.groups.get(usize::from(*wire) - 1) else {
                return Ok(None);
            };
            return Ok(Some(Event::Message {
                crc,
                offset: *offset,
                body: frame[2..].to_vec(),
            }));
        }

        match *offset {
            jam_framing::ANNOUNCE => {
                let groups = jam_framing::parse_announcement(&frame)?;
                // Zero is reserved for controls. A later duplicate CRC wins outbound mapping.
                self.wire_of = groups
                    .iter()
                    .take(usize::from(u8::MAX))
                    .enumerate()
                    .map(|(index, &crc)| (crc, (index + 1) as u8))
                    .collect();
                self.groups = groups;
                self.send(&jam_framing::CONNECTED)?;
                Ok(Some(Event::Announced))
            }
            jam_framing::PING => {
                self.last_keepalive = Instant::now();
                self.send(&jam_framing::PONG)?;
                Ok(None)
            }
            _ => Ok(None),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv4Addr, TcpListener};
    use std::thread;

    #[test]
    fn negotiates_groups_before_dispatching_and_handles_reannouncement() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let channel = Channel {
                rx: Jam::new(&[1; 32]),
                tx: Jam::new(&[2; 32]),
                seq: 42,
            };
            let mut transport = LobbyTransport::new(stream, channel).unwrap();

            assert_eq!(transport.receive().unwrap(), Some(Event::Announced));
            assert_eq!(transport.groups(), [1, 2, 1]);
            assert_eq!(transport.wire_for(1), Some(3));
            assert_eq!(
                transport.receive().unwrap(),
                Some(Event::Message {
                    crc: 2,
                    offset: 7,
                    body: b"abc".to_vec(),
                })
            );
            assert_eq!(transport.receive().unwrap(), Some(Event::Announced));
            assert_eq!(transport.groups(), [4]);
            assert_eq!(transport.wire_for(2), None);
            assert_eq!(transport.wire_for(4), Some(1));
            assert_eq!(transport.receive().unwrap(), None);
        });

        let mut client = TcpStream::connect(address).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut tx = Jam::new(&[1; 32]);
        let mut outgoing = Vec::new();
        for frame in [
            b"\x02\x06ignored".as_slice(),
            b"\x00\x00\x03\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x01\x00\x00\x00",
            &jam_framing::PONG,
            b"\x02\x07abc",
            b"\x00\x00\x01\x00\x00\x00\x04\x00\x00\x00",
        ] {
            jam_framing::send_frame(&mut outgoing, &mut tx, frame).unwrap();
        }
        use std::io::Write;
        client.write_all(&outgoing).unwrap();
        let mut replies = [0; 15];
        client.read_exact(&mut replies).unwrap();
        let mut reader = FrameReader::new(Jam::new(&[2; 32]));
        assert_eq!(
            reader.feed(&replies),
            [
                jam_framing::CONNECTED,
                jam_framing::PONG,
                jam_framing::CONNECTED,
            ]
        );

        drop(client);
        server.join().unwrap();
    }
}
