//! Shared, cancellable TCP peers for the local Battle.net and lobby services.

use std::{
    io::{self, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const IO_POLL: Duration = Duration::from_millis(250);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);

pub(crate) struct PeerStream {
    socket: TcpStream,
    stopping: Arc<AtomicBool>,
    deadline: Option<Instant>,
}

impl PeerStream {
    pub(crate) fn new(socket: TcpStream, stopping: Arc<AtomicBool>) -> io::Result<Self> {
        socket.set_nonblocking(false)?;
        socket.set_read_timeout(Some(IO_POLL))?;
        socket.set_write_timeout(Some(IO_POLL))?;
        Ok(Self {
            socket,
            stopping,
            deadline: None,
        })
    }

    pub(crate) fn set_deadline(&mut self, deadline: Option<Instant>) {
        self.deadline = deadline;
    }

    pub(crate) fn deadline_expired(&self) -> bool {
        self.deadline.is_some_and(|end| Instant::now() >= end)
    }

    pub(crate) fn local_addr(&self) -> io::Result<SocketAddr> {
        self.socket.local_addr()
    }

    pub(crate) fn peer_addr(&self) -> io::Result<SocketAddr> {
        self.socket.peer_addr()
    }

    pub(crate) fn peek(&self, data: &mut [u8]) -> io::Result<usize> {
        self.transfer(self.deadline, |socket| socket.peek(data))
    }

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
        self.transfer(self.deadline, |mut socket| socket.read(buffer))
    }
}

impl Write for PeerStream {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        let deadline = self
            .deadline
            .unwrap_or_else(|| Instant::now() + WRITE_TIMEOUT);
        self.transfer(Some(deadline), |mut socket| socket.write(buffer))
    }

    fn flush(&mut self) -> io::Result<()> {
        self.socket.flush()
    }
}

pub(crate) fn retryable(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

#[derive(Default)]
pub(crate) struct PeerWorkers {
    stopping: Arc<AtomicBool>,
    workers: Vec<JoinHandle<()>>,
}

impl PeerWorkers {
    pub(crate) fn spawn(
        &mut self,
        name: &str,
        socket: TcpStream,
        handle: impl FnOnce(PeerStream) + Send + 'static,
    ) -> io::Result<()> {
        let stream = PeerStream::new(socket, Arc::clone(&self.stopping))?;
        let worker = thread::Builder::new()
            .name(name.into())
            .spawn(move || handle(stream))?;
        self.workers.push(worker);
        Ok(())
    }

    pub(crate) fn len(&self) -> usize {
        self.workers.len()
    }

    pub(crate) fn reap(&mut self) {
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

impl Drop for PeerWorkers {
    fn drop(&mut self) {
        // On Windows, closing a duplicated socket need not wake another
        // thread's blocking read. Peers check this flag between bounded I/O.
        self.stopping.store(true, Ordering::Release);
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}

pub(crate) fn accept(listener: &TcpListener) -> io::Result<Option<(TcpStream, SocketAddr)>> {
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
