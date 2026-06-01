//! Multi-worker **fd-passing acceptor** (pure Rust) — the OS-portable, load-aware
//! alternative to `SO_REUSEPORT` for `app.run(workers=N)`.
//!
//! ## Topology
//! ```text
//!   clients ──▶ :PORT
//!   ┌─ Rust acceptor ─┐   accept → fd-pass (SCM_RIGHTS) to the LEAST-LOADED worker
//!   └────────┬────────┘
//!   ┌────────┼───────────────┐
//!   ▼        ▼               ▼
//! worker 1  worker 2  …   worker N   (each runs the full Axum router on the passed fd)
//! ```
//!
//! ONE acceptor process binds the TCP port; N worker processes connect over a
//! unix-domain socket. For each accepted TCP connection the acceptor passes the
//! socket's **file descriptor** (`SCM_RIGHTS`) to the worker with the fewest
//! outstanding connections; that worker `dup`s the fd and runs the **full
//! assembled Axum router** on it via hyper's `serve_connection_with_upgrades`,
//! so **HTTP/1, HTTP/2, WebSocket, and SSE/streaming are all handled per worker**
//! (transport-agnostic distribution — the acceptor never parses a byte).
//!
//! ### Why fd-passing instead of `SO_REUSEPORT`
//! `SO_REUSEPORT` distributes by a **blind kernel hash** of the 4-tuple — oblivious
//! to load, so long-lived WS/SSE connections pile unevenly — and on macOS it does
//! not load-balance at all. The acceptor tracks a per-worker `outstanding` counter
//! (incremented on dispatch, decremented when the worker reports a connection
//! finished) and always picks the **minimum** — load-aware routing on every OS.
//! For pure high-rate *short* HTTP, plain `SO_REUSEPORT` (`server::run_server`)
//! is simpler and scales the accept path across workers; this mode targets the
//! mixed HTTP+WS+SSE workload where load-awareness + portability matter.

use std::os::fd::{FromRawFd, IntoRawFd, RawFd};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use sendfd::{RecvWithFd, SendWithFd};
use tokio::net::{TcpListener, TcpStream, UnixListener, UnixStream};
use tokio::sync::{mpsc, Notify};

// ----------------------------- fd transport helpers -----------------------------

/// Send one raw fd (plus a 1-byte tag — `SCM_RIGHTS` requires at least one data
/// byte to accompany the control message) over `sock`, driving `sendfd`'s
/// `try_io` send with tokio write-readiness.
async fn send_fd(sock: &UnixStream, fd: RawFd) -> std::io::Result<()> {
    let byte = [1u8];
    loop {
        sock.writable().await?;
        match sock.send_with_fd(&byte, &[fd]) {
            Ok(_) => return Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => return Err(e),
        }
    }
}

/// Receive one raw fd over `sock`. Returns `Ok(None)` on a clean EOF (the
/// acceptor went away).
async fn recv_fd(sock: &UnixStream) -> std::io::Result<Option<RawFd>> {
    let mut byte = [0u8; 1];
    let mut fds = [0 as RawFd; 1];
    loop {
        sock.readable().await?;
        match sock.recv_with_fd(&mut byte, &mut fds) {
            Ok((0, 0)) => return Ok(None), // peer closed
            Ok((_, n_fds)) if n_fds >= 1 => return Ok(Some(fds[0])),
            Ok(_) => continue, // data byte but no fd — wait, don't fabricate
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => return Err(e),
        }
    }
}

/// Reconstruct a [`tokio::net::TcpStream`] from a received raw fd (owned by this
/// process after the `SCM_RIGHTS` dup). Sets non-blocking + registers with the
/// current reactor.
fn tcp_from_fd(fd: RawFd) -> std::io::Result<TcpStream> {
    // SAFETY: `fd` was just received over SCM_RIGHTS and is owned solely by this
    // process; no other socket wraps it.
    let std_stream = unsafe { std::net::TcpStream::from_raw_fd(fd) };
    std_stream.set_nonblocking(true)?;
    TcpStream::from_std(std_stream)
}

// --------------------------------- worker ---------------------------------

/// Serve ONE fd-passed connection with the full Axum router. Drives hyper's
/// auto (HTTP/1 + HTTP/2) connection server WITH upgrades, so WebSocket
/// handshakes and SSE/streaming responses on this connection work exactly as
/// under `axum::serve`. Connection-level errors (client disconnects) are normal
/// and ignored.
async fn serve_one_connection(stream: TcpStream, router: axum::Router) {
    use hyper_util::rt::{TokioExecutor, TokioIo};
    use hyper_util::server::conn::auto::Builder;
    use hyper_util::service::TowerToHyperService;

    let _ = stream.set_nodelay(true);
    let io = TokioIo::new(stream);
    // axum::Router<()> implements tower::Service<Request<Incoming>>; wrap it as a
    // hyper service. The response body (axum::body::Body) is a valid hyper body.
    let svc = TowerToHyperService::new(router);
    let _ = Builder::new(TokioExecutor::new())
        .serve_connection_with_upgrades(io, svc)
        .await;
}

/// Run one **worker** process's event loop: connect to the acceptor's unix
/// socket, then loop receiving fds and serving each on a tokio task with the
/// shared router. A 1-byte completion notice is sent back to the acceptor when
/// each connection finishes so it can decrement this worker's `outstanding`.
///
/// `ready` flips to `true` once connected + looping (test/ops hook). `shutdown`
/// stops accepting new fds; in-flight connections drain naturally.
#[allow(dead_code)] // wired into PyO3 + the app.run(workers=N) launcher in Step 8
pub async fn run_worker(
    acceptor_sock: &Path,
    router: axum::Router,
    ready: Arc<AtomicBool>,
    shutdown: impl std::future::Future<Output = ()> + Send,
) -> std::io::Result<()> {
    let sock = Arc::new(UnixStream::connect(acceptor_sock).await?);
    ready.store(true, Ordering::SeqCst);

    // Completion notices ride a dedicated task so a finishing connection never
    // races the recv path for the socket's write-readiness.
    let (done_tx, mut done_rx) = mpsc::unbounded_channel::<()>();
    let writer_sock = sock.clone();
    let completer = tokio::spawn(async move {
        while done_rx.recv().await.is_some() {
            loop {
                if writer_sock.writable().await.is_err() {
                    return;
                }
                match writer_sock.try_write(&[0u8]) {
                    Ok(_) => break,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
                    Err(_) => return,
                }
            }
        }
    });

    tokio::pin!(shutdown);
    let inflight = Arc::new(AtomicUsize::new(0));
    let drained = Arc::new(Notify::new());

    loop {
        let fd = tokio::select! {
            biased;
            _ = &mut shutdown => break,
            r = recv_fd(&sock) => match r {
                Ok(Some(fd)) => fd,
                Ok(None) => break, // acceptor closed
                Err(_) => break,
            },
        };

        let stream = match tcp_from_fd(fd) {
            Ok(s) => s,
            Err(_) => {
                let _ = done_tx.send(()); // never served, but free the slot
                continue;
            }
        };

        let router = router.clone();
        let done_tx = done_tx.clone();
        let inflight = inflight.clone();
        let drained = drained.clone();
        inflight.fetch_add(1, Ordering::SeqCst);
        tokio::spawn(async move {
            serve_one_connection(stream, router).await;
            let _ = done_tx.send(()); // decrement the acceptor's outstanding
            if inflight.fetch_sub(1, Ordering::SeqCst) == 1 {
                drained.notify_waiters();
            }
        });
    }

    // Drain: let in-flight connections finish.
    if inflight.load(Ordering::SeqCst) > 0 {
        loop {
            let notified = drained.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if inflight.load(Ordering::SeqCst) == 0 {
                break;
            }
            notified.await;
        }
    }
    drop(done_tx);
    let _ = completer.await;
    Ok(())
}

// --------------------------------- acceptor ---------------------------------

/// One connected worker, as seen by the acceptor.
struct Worker {
    sock: Arc<UnixStream>,
    outstanding: Arc<AtomicUsize>,
}

/// Shared acceptor state: the live worker set.
struct Workers {
    list: std::sync::Mutex<Vec<Worker>>,
}

impl Workers {
    fn new() -> Self {
        Workers {
            list: std::sync::Mutex::new(Vec::new()),
        }
    }

    /// Register a freshly-connected worker + spawn its completion-notice reader
    /// (each byte decrements `outstanding`; EOF removes the worker).
    fn add(self: &Arc<Self>, sock: UnixStream) {
        let sock = Arc::new(sock);
        let outstanding = Arc::new(AtomicUsize::new(0));
        let reader_sock = sock.clone();
        let reader_outstanding = outstanding.clone();
        let workers = self.clone();
        let id_sock = Arc::as_ptr(&sock) as usize;
        tokio::spawn(async move {
            let mut buf = [0u8; 64];
            loop {
                if reader_sock.readable().await.is_err() {
                    break;
                }
                match reader_sock.try_read(&mut buf) {
                    Ok(0) => break, // worker disconnected
                    Ok(n) => {
                        for _ in 0..n {
                            if reader_outstanding.load(Ordering::SeqCst) > 0 {
                                reader_outstanding.fetch_sub(1, Ordering::SeqCst);
                            }
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
                    Err(_) => break,
                }
            }
            let mut list = workers.list.lock().unwrap_or_else(|p| p.into_inner());
            list.retain(|w| Arc::as_ptr(&w.sock) as usize != id_sock);
        });
        let mut list = self.list.lock().unwrap_or_else(|p| p.into_inner());
        list.push(Worker { sock, outstanding });
    }

    /// Pick the least-loaded worker, optimistically increment its `outstanding`,
    /// and return its socket. `None` if no workers are connected.
    fn pick(&self) -> Option<(Arc<UnixStream>, Arc<AtomicUsize>)> {
        let list = self.list.lock().unwrap_or_else(|p| p.into_inner());
        let chosen = list
            .iter()
            .min_by_key(|w| w.outstanding.load(Ordering::SeqCst))?;
        chosen.outstanding.fetch_add(1, Ordering::SeqCst);
        Some((chosen.sock.clone(), chosen.outstanding.clone()))
    }
}

/// Run the **acceptor** process's event loop: accept worker registrations on
/// `workers` (unix), accept TCP connections on `tcp`, and fd-pass each TCP
/// connection to the least-loaded worker (no byte is read — fully
/// transport-agnostic). `ready`/`grace`/`shutdown` mirror the server drain.
#[allow(dead_code)] // wired into PyO3 + the app.run(workers=N) launcher in Step 8
pub async fn run_acceptor(
    tcp: TcpListener,
    workers: UnixListener,
    ready: Arc<AtomicBool>,
    grace: Duration,
    shutdown: impl std::future::Future<Output = ()> + Send,
) -> std::io::Result<()> {
    let workers_state = Arc::new(Workers::new());
    tokio::pin!(shutdown);

    let inflight = Arc::new(AtomicUsize::new(0));
    let drained = Arc::new(Notify::new());

    loop {
        tokio::select! {
            biased;
            _ = &mut shutdown => break,
            accepted = workers.accept() => {
                if let Ok((sock, _addr)) = accepted {
                    workers_state.add(sock);
                }
            }
            accepted = tcp.accept() => {
                let (stream, _peer) = match accepted {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                let ws = workers_state.clone();
                let inflight = inflight.clone();
                let drained = drained.clone();
                inflight.fetch_add(1, Ordering::SeqCst);
                tokio::spawn(async move {
                    dispatch_one(stream, ws).await;
                    if inflight.fetch_sub(1, Ordering::SeqCst) == 1 {
                        drained.notify_waiters();
                    }
                });
            }
        }
    }

    // Drain in-flight dispatch tasks (the brief fd-pass), bounded by `grace`.
    ready.store(false, Ordering::Relaxed);
    let deadline = tokio::time::sleep(grace);
    tokio::pin!(deadline);
    loop {
        let notified = drained.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        if inflight.load(Ordering::SeqCst) == 0 {
            break;
        }
        tokio::select! {
            _ = &mut notified => {}
            _ = &mut deadline => break,
        }
    }
    Ok(())
}

/// Fd-pass one accepted TCP connection to the least-loaded worker. The acceptor
/// reads no bytes — the worker's Axum stack sees the connection from the first
/// byte, so every transport (HTTP/WS/SSE) works.
async fn dispatch_one(stream: TcpStream, workers: Arc<Workers>) {
    let (sock, outstanding) = match workers.pick() {
        Some(w) => w,
        None => return, // no workers — dropping the stream closes it
    };

    // Take ownership of the raw fd out of the tokio stream so neither the tokio
    // Drop nor the worker's dup double-closes it.
    let std_stream = match stream.into_std() {
        Ok(s) => s,
        Err(_) => {
            outstanding.fetch_sub(1, Ordering::SeqCst);
            return;
        }
    };
    let fd = std_stream.into_raw_fd();

    let pass = send_fd(&sock, fd).await;
    // The worker dup'd the fd across SCM_RIGHTS; close OUR copy exactly once.
    // SAFETY: we still own `fd` until this drop; the worker holds a separate dup.
    let _own = unsafe { std::net::TcpStream::from_raw_fd(fd) };
    drop(_own);

    if pass.is_err() {
        outstanding.fetch_sub(1, Ordering::SeqCst); // never landed; free the slot
    }
}

// --------------------------------- tests ---------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// fd round-trip: pass a live TCP socket's fd over a UnixStream pair and echo
    /// bytes through the reconstructed stream — proving the passed fd is the same
    /// live socket.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn fd_round_trip_over_unix_socket() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let server = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = server.local_addr().unwrap();
        let connect = tokio::spawn(async move { TcpStream::connect(addr).await.unwrap() });
        let (accepted, _peer) = server.accept().await.unwrap();
        let mut client = connect.await.unwrap();

        let (a, b) = UnixStream::pair().unwrap();
        let fd = accepted.into_std().unwrap().into_raw_fd();
        send_fd(&a, fd).await.unwrap();
        let _own = unsafe { std::net::TcpStream::from_raw_fd(fd) };
        drop(_own);

        let got_fd = recv_fd(&b).await.unwrap().expect("an fd");
        let mut server_end = tcp_from_fd(got_fd).unwrap();

        client.write_all(b"ping").await.unwrap();
        let mut buf = [0u8; 4];
        server_end.read_exact(&mut buf).await.unwrap();
        assert_eq!(&buf, b"ping");
        server_end.write_all(b"pong").await.unwrap();
        let mut back = [0u8; 4];
        client.read_exact(&mut back).await.unwrap();
        assert_eq!(&back, b"pong", "bytes did not round-trip through the passed fd");
    }

    /// End-to-end: acceptor + one worker serving a real Axum router. An HTTP GET
    /// to the acceptor's port is fd-passed to the worker and answered by the
    /// router — proving the full HTTP transport works over the fd-passing path.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn acceptor_fd_passes_http_to_worker() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let sock_path = std::env::temp_dir().join(format!("ftc-{}.sock", std::process::id()));
        let _ = std::fs::remove_file(&sock_path);
        let unix = UnixListener::bind(&sock_path).unwrap();
        let tcp = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let tcp_addr = tcp.local_addr().unwrap();

        // Acceptor.
        let acc_ready = Arc::new(AtomicBool::new(true));
        let (acc_tx, acc_rx) = tokio::sync::oneshot::channel::<()>();
        tokio::spawn(async move {
            let _ = run_acceptor(
                tcp,
                unix,
                acc_ready,
                Duration::from_secs(2),
                async { let _ = acc_rx.await; },
            )
            .await;
        });

        // Worker with a trivial router.
        let router = axum::Router::new()
            .route("/hi", axum::routing::get(|| async { "hello-via-fd-pass" }));
        let w_ready = Arc::new(AtomicBool::new(false));
        let (w_tx, w_rx) = tokio::sync::oneshot::channel::<()>();
        let path = sock_path.clone();
        tokio::spawn(async move {
            let _ = run_worker(&path, router, w_ready, async { let _ = w_rx.await; }).await;
        });
        tokio::time::sleep(Duration::from_millis(200)).await;

        // Raw HTTP/1.1 GET to the acceptor port.
        let mut conn = TcpStream::connect(tcp_addr).await.unwrap();
        conn.write_all(b"GET /hi HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut resp = Vec::new();
        conn.read_to_end(&mut resp).await.unwrap();
        let text = String::from_utf8_lossy(&resp);
        assert!(text.contains("200 OK"), "expected 200 via fd-pass, got: {text}");
        assert!(
            text.contains("hello-via-fd-pass"),
            "router body not served over the fd-passed connection: {text}"
        );

        let _ = w_tx.send(());
        let _ = acc_tx.send(());
        let _ = std::fs::remove_file(&sock_path);
    }
}
