//! Battle.net's stall TCP socket and the relay's local WebSocket endpoint.
//!
//! Ports the listener roles from `ow174/bnet/service.py` and the connection
//! handling from `ow174/bnet/rpc_server.py`. RPC content lives in `bnet_rpc`.

use std::io::{self, Read, Write};
use std::net::{SocketAddr, SocketAddrV4, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::http::{HeaderValue, StatusCode, header::SEC_WEBSOCKET_PROTOCOL};
use tungstenite::protocol::WebSocketConfig;
use tungstenite::{Error, Message, WebSocket, accept_hdr_with_config};

use crate::bnet_protocol::Protocol;
use crate::bnet_rpc::{Clock, Player, RpcSession};

pub const STALL_PORT: u16 = 1119;
pub const RPC_PORT: u16 = 21119;
pub const SUBPROTOCOL: &str = "v1.rpc.battle.net";
const MAX_RPC_SIZE: usize = 8 << 20;
const ACCEPT_POLL: Duration = Duration::from_millis(10);
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const IO_POLL: Duration = Duration::from_millis(250);
const PING_INTERVAL: Duration = Duration::from_secs(20);

/// Background listeners for the original client dial and the relay's RPC pipe.
pub struct BnetService {
    stall: SocketAddr,
    rpc: SocketAddr,
    stop: mpsc::Sender<()>,
    worker: Option<JoinHandle<io::Result<()>>>,
}

impl BnetService {
    /// Both ports must be available before any connection is accepted. The player
    /// lookup runs in the peer's worker and should return promptly.
    pub fn start(
        stall: SocketAddrV4,
        rpc: SocketAddrV4,
        lobby: SocketAddrV4,
        protocol: Arc<Protocol>,
        player: impl Fn(SocketAddr) -> Player + Send + Sync + 'static,
    ) -> io::Result<Self> {
        let stall_listener = TcpListener::bind(stall)?;
        let rpc_listener = TcpListener::bind(rpc)?;
        stall_listener.set_nonblocking(true)?;
        rpc_listener.set_nonblocking(true)?;
        let stall = stall_listener.local_addr()?;
        let rpc = rpc_listener.local_addr()?;
        let endpoint = Arc::new(RpcEndpoint {
            protocol,
            lobby,
            player: Box::new(player),
            started: Instant::now(),
            epoch: (wall_millis() / 1000) as u32,
        });
        let (stop, shutdown) = mpsc::channel();
        let worker = thread::Builder::new()
            .name("bnet".into())
            .spawn(move || serve(stall_listener, rpc_listener, endpoint, shutdown))?;
        Ok(Self {
            stall,
            rpc,
            stop,
            worker: Some(worker),
        })
    }

    pub fn stall_addr(&self) -> SocketAddr {
        self.stall
    }

    pub fn rpc_addr(&self) -> SocketAddr {
        self.rpc
    }

    /// Closes both listeners and all accepted sockets, including incomplete handshakes.
    pub fn stop(&mut self) -> io::Result<()> {
        if let Some(worker) = self.worker.take() {
            let _ = self.stop.send(());
            worker
                .join()
                .map_err(|_| io::Error::other("Battle.net listener thread panicked"))??;
        }
        Ok(())
    }
}

impl Drop for BnetService {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

struct RpcEndpoint {
    protocol: Arc<Protocol>,
    lobby: SocketAddrV4,
    player: Box<dyn Fn(SocketAddr) -> Player + Send + Sync>,
    started: Instant,
    epoch: u32,
}

impl RpcEndpoint {
    fn clock(&self) -> Clock {
        Clock {
            server_epoch: self
                .epoch
                .wrapping_add(self.started.elapsed().as_secs() as u32),
            wall_millis: wall_millis(),
        }
    }
}

fn wall_millis() -> u64 {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
}

struct PeerStream {
    socket: TcpStream,
    stopping: Arc<AtomicBool>,
    handshake_deadline: Option<Instant>,
}

impl PeerStream {
    fn transfer<T>(
        &self,
        deadline: Option<Instant>,
        mut operation: impl FnMut(&TcpStream) -> io::Result<T>,
    ) -> io::Result<T> {
        loop {
            if self.stopping.load(Ordering::Acquire) {
                return Err(io::ErrorKind::ConnectionAborted.into());
            }
            if deadline.is_some_and(|end| Instant::now() >= end) {
                return Err(io::ErrorKind::TimedOut.into());
            }
            match operation(&self.socket) {
                Err(error) if deadline.is_some() && retryable(&error) => continue,
                result => return result,
            }
        }
    }
}

impl Read for PeerStream {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.transfer(self.handshake_deadline, |mut socket| socket.read(buffer))
    }
}

impl Write for PeerStream {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        let deadline = self
            .handshake_deadline
            .unwrap_or_else(|| Instant::now() + HANDSHAKE_TIMEOUT);
        self.transfer(Some(deadline), |mut socket| socket.write(buffer))
    }

    fn flush(&mut self) -> io::Result<()> {
        self.socket.flush()
    }
}

fn retryable(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

#[derive(Default)]
struct Peers {
    stopping: Arc<AtomicBool>,
    workers: Vec<JoinHandle<()>>,
}

impl Peers {
    fn spawn(
        &mut self,
        socket: TcpStream,
        handle: impl FnOnce(PeerStream) + Send + 'static,
    ) -> io::Result<()> {
        // The accepted stream must be blocking on every platform.
        socket.set_nonblocking(false)?;
        socket.set_read_timeout(Some(IO_POLL))?;
        socket.set_write_timeout(Some(IO_POLL))?;
        let stream = PeerStream {
            socket,
            stopping: Arc::clone(&self.stopping),
            handshake_deadline: None,
        };
        let worker = thread::Builder::new()
            .name("bnet-peer".into())
            .spawn(move || handle(stream))?;
        self.workers.push(worker);
        Ok(())
    }

    fn reap(&mut self) {
        let mut index = 0;
        while index < self.workers.len() {
            if self.workers[index].is_finished() {
                let _ = self.workers.swap_remove(index).join();
            } else {
                index += 1;
            }
        }
    }
}

impl Drop for Peers {
    fn drop(&mut self) {
        // Windows can leave a blocked read pending when another thread shuts
        // down a duplicated handle. Every worker instead checks this flag
        // between bounded socket operations.
        self.stopping.store(true, Ordering::Release);
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}

fn accept(listener: &TcpListener) -> io::Result<Option<(TcpStream, SocketAddr)>> {
    match listener.accept() {
        Ok(peer) => Ok(Some(peer)),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::WouldBlock
                    | io::ErrorKind::Interrupted
                    | io::ErrorKind::ConnectionAborted
                    | io::ErrorKind::ConnectionReset
            ) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn serve(
    stall: TcpListener,
    rpc: TcpListener,
    endpoint: Arc<RpcEndpoint>,
    shutdown: mpsc::Receiver<()>,
) -> io::Result<()> {
    let mut peers = Peers::default();
    while matches!(shutdown.try_recv(), Err(mpsc::TryRecvError::Empty)) {
        peers.reap();
        let stall_peer = accept(&stall)?;
        let rpc_peer = accept(&rpc)?;
        if stall_peer.is_none() && rpc_peer.is_none() {
            if !matches!(
                shutdown.recv_timeout(ACCEPT_POLL),
                Err(mpsc::RecvTimeoutError::Timeout)
            ) {
                break;
            }
            continue;
        }
        if let Some((socket, _)) = stall_peer {
            let _ = peers.spawn(socket, hold_open);
        }
        if let Some((socket, address)) = rpc_peer {
            let endpoint = Arc::clone(&endpoint);
            let _ = peers.spawn(socket, move |socket| serve_rpc(socket, address, &endpoint));
        }
    }
    Ok(())
}

fn hold_open(mut socket: PeerStream) {
    // The relay swaps out the real client dial. Consume its TLS bytes without replying.
    let mut buffer = [0; 4096];
    loop {
        match socket.read(&mut buffer) {
            Ok(0) => return,
            Ok(_) => (),
            Err(error) if retryable(&error) => (),
            Err(_) => return,
        }
    }
}

// The handshake callback must return tungstenite's response type.
#[allow(clippy::result_large_err)]
fn negotiate(request: &Request, mut response: Response) -> Result<Response, ErrorResponse> {
    let supported = request
        .headers()
        .get_all(SEC_WEBSOCKET_PROTOCOL)
        .iter()
        .any(|value| {
            value
                .to_str()
                .ok()
                .is_some_and(|offered| offered.split(',').any(|name| name.trim() == SUBPROTOCOL))
        });
    if !supported {
        let mut error = ErrorResponse::new(Some(format!(
            "Expected WebSocket subprotocol {SUBPROTOCOL}"
        )));
        *error.status_mut() = StatusCode::BAD_REQUEST;
        return Err(error);
    }
    response.headers_mut().insert(
        SEC_WEBSOCKET_PROTOCOL,
        HeaderValue::from_static(SUBPROTOCOL),
    );
    Ok(response)
}

fn rpc_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .max_message_size(Some(MAX_RPC_SIZE))
        .max_frame_size(Some(MAX_RPC_SIZE))
}

fn serve_rpc(mut socket: PeerStream, address: SocketAddr, endpoint: &RpcEndpoint) {
    socket.handshake_deadline = Some(Instant::now() + HANDSHAKE_TIMEOUT);
    let Ok(mut websocket) = accept_hdr_with_config(socket, negotiate, Some(rpc_config())) else {
        return;
    };
    websocket.get_mut().handshake_deadline = None;
    let _ = run_rpc(&mut websocket, address, endpoint);
}

fn run_rpc(
    websocket: &mut WebSocket<PeerStream>,
    address: SocketAddr,
    endpoint: &RpcEndpoint,
) -> Result<(), Error> {
    let mut session = RpcSession::new();
    let mut next_ping = Instant::now() + PING_INTERVAL;
    let mut ping_number = 0u32;
    let mut pending_ping = None;
    loop {
        let now = Instant::now();
        if now >= next_ping {
            if pending_ping.is_some() {
                websocket.close(None)?;
                return Ok(());
            }
            ping_number = ping_number.wrapping_add(1);
            let ping = ping_number.to_be_bytes();
            websocket.send(Message::Ping(ping.to_vec().into()))?;
            pending_ping = Some(ping);
            next_ping = now + PING_INTERVAL;
        }

        match websocket.read() {
            Ok(Message::Binary(request)) => {
                // Malformed RPC frames and unhandled requests must not close the pipe.
                if let Ok(responses) = session.dispatch(
                    &endpoint.protocol,
                    &request,
                    endpoint.lobby,
                    endpoint.clock(),
                    || (endpoint.player)(address),
                ) {
                    for response in responses {
                        websocket.send(Message::Binary(response.into()))?;
                    }
                }
            }
            Ok(Message::Pong(payload)) => {
                if pending_ping
                    .as_ref()
                    .is_some_and(|ping| payload.as_ref() == ping.as_slice())
                {
                    pending_ping = None;
                }
            }
            Ok(Message::Ping(_)) => websocket.flush()?,
            Ok(Message::Close(_)) => {
                let _ = websocket.flush();
                return Ok(());
            }
            Ok(_) => (),
            Err(Error::Io(error)) if retryable(&error) => {}
            Err(Error::ConnectionClosed | Error::AlreadyClosed) => return Ok(()),
            Err(error) => return Err(error),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bnet_protocol::ACCOUNT_HASH;
    use prost_reflect::Value;
    use std::io::Write;
    use std::net::Ipv4Addr;
    use tungstenite::{HandshakeError, client, client::IntoClientRequest};

    // Synthetic FileDescriptorSet. It contains only the six Header fields needed
    // to exercise the real AccountService empty response through a WebSocket.
    const DESCRIPTORS: &[u8] =
        b"\x0a\x99\x01\x0a\x0dfixture.proto\x12\x0cbgs.protocol\x22r\x0a\x06Header\
        \x12\x12\x0a\x0aservice_id\x18\x01\x20\x02\x28\x0d\
        \x12\x11\x0a\x09method_id\x18\x02\x20\x01\x28\x0d\
        \x12\x0d\x0a\x05token\x18\x03\x20\x02\x28\x0d\
        \x12\x0e\x0a\x06status\x18\x04\x20\x01\x28\x0d\
        \x12\x0c\x0a\x04size\x18\x05\x20\x01\x28\x0d\
        \x12\x14\x0a\x0cservice_hash\x18\x0b\x20\x01\x28\x07\x62\x06proto2";

    fn start(
        stall: SocketAddrV4,
        rpc: SocketAddrV4,
        protocol: Arc<Protocol>,
    ) -> io::Result<BnetService> {
        BnetService::start(
            stall,
            rpc,
            SocketAddrV4::new(Ipv4Addr::LOCALHOST, 3724),
            protocol,
            |_| panic!("the synthetic account ack does not resolve a player"),
        )
    }

    fn local() -> SocketAddrV4 {
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)
    }

    fn tcp(address: SocketAddr) -> TcpStream {
        let socket = TcpStream::connect_timeout(&address, Duration::from_secs(3)).unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        socket
            .set_write_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        socket
    }

    fn request(address: SocketAddr, offered: Option<&'static str>) -> Request {
        let mut request = format!("ws://{address}/").into_client_request().unwrap();
        if let Some(offered) = offered {
            request
                .headers_mut()
                .insert(SEC_WEBSOCKET_PROTOCOL, HeaderValue::from_static(offered));
        }
        request
    }

    #[test]
    fn negotiates_rpc_and_ignores_malformed_messages_before_a_valid_response() {
        let protocol = Arc::new(Protocol::from_bytes(DESCRIPTORS).unwrap());
        let mut service = start(local(), local(), Arc::clone(&protocol)).unwrap();
        let address = service.rpc_addr();
        let (mut websocket, response) =
            client(request(address, Some(SUBPROTOCOL)), tcp(address)).unwrap();
        assert_eq!(response.headers()[SEC_WEBSOCKET_PROTOCOL], SUBPROTOCOL);

        websocket.send(Message::Text("ignored".into())).unwrap();
        websocket.send(Message::Binary(vec![0xff].into())).unwrap();
        let mut header = protocol.new_header();
        for (name, value) in [
            ("service_id", 1),
            ("service_hash", ACCOUNT_HASH),
            ("token", 73),
        ] {
            header.set_field_by_name(name, Value::U32(value));
        }
        let frame = protocol.encode_frame(&mut header, b"").unwrap();
        websocket.send(Message::Binary(frame.into())).unwrap();
        let Message::Binary(reply) = websocket.read().unwrap() else {
            panic!("RPC should reply with a binary frame");
        };
        let (response, body) = protocol.decode_frame(&reply).unwrap();
        assert_eq!(
            response.get_field_by_name("service_id").unwrap().as_u32(),
            Some(254)
        );
        assert_eq!(
            response.get_field_by_name("token").unwrap().as_u32(),
            Some(73)
        );
        assert!(body.is_empty());

        let ping = vec![1, 9].into();
        websocket.send(Message::Ping(ping)).unwrap();
        assert_eq!(websocket.read().unwrap(), Message::Pong(vec![1, 9].into()));

        service.stop().unwrap();
        assert!(websocket.read().is_err());
        assert!(TcpStream::connect(address).is_err());
        service.stop().unwrap();
    }

    #[test]
    fn holds_the_stall_connection_and_stops_an_unfinished_handshake() {
        let protocol = Arc::new(Protocol::from_bytes(DESCRIPTORS).unwrap());
        let mut service = start(local(), local(), protocol).unwrap();
        let mut stalled = tcp(service.stall_addr());
        let mut handshaking = tcp(service.rpc_addr());
        handshaking.write_all(b"GET / HTTP/1.1\r\n").unwrap();
        stalled.write_all(b"client TLS bytes").unwrap();
        stalled
            .set_read_timeout(Some(Duration::from_millis(200)))
            .unwrap();
        let mut byte = [0];
        assert!(
            matches!(stalled.read(&mut byte), Err(error) if matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut))
        );

        let start = Instant::now();
        service.stop().unwrap();
        assert!(start.elapsed() < Duration::from_secs(3));
        for result in [stalled.read(&mut byte), handshaking.read(&mut byte)] {
            assert!(match result {
                Ok(count) => count == 0,
                Err(error) => !retryable(&error),
            });
        }
        assert!(TcpStream::connect(service.stall_addr()).is_err());
    }

    #[test]
    fn rejects_missing_or_incompatible_protocol_and_configures_eight_mib_limit() {
        let protocol = Arc::new(Protocol::from_bytes(DESCRIPTORS).unwrap());
        let service = start(local(), local(), protocol).unwrap();
        let address = service.rpc_addr();
        for offered in [None, Some("unsupported.protocol")] {
            assert!(matches!(client(request(address, offered), tcp(address)),
                Err(HandshakeError::Failure(Error::Http(response))) if response.status() == StatusCode::BAD_REQUEST));
        }
        let config = rpc_config();
        assert_eq!(config.max_message_size, Some(8 << 20));
        assert_eq!(config.max_frame_size, Some(8 << 20));
    }

    #[test]
    fn releases_the_first_listener_when_the_second_bind_fails() {
        let protocol = Arc::new(Protocol::from_bytes(DESCRIPTORS).unwrap());
        let taken = TcpListener::bind(local()).unwrap();
        let probe = TcpListener::bind(local()).unwrap();
        let (SocketAddr::V4(rpc), SocketAddr::V4(stall)) =
            (taken.local_addr().unwrap(), probe.local_addr().unwrap())
        else {
            unreachable!("fixtures bound to IPv4 loopback")
        };
        drop(probe);
        assert!(start(stall, rpc, protocol).is_err());
        assert!(TcpListener::bind(stall).is_ok());
    }
}
