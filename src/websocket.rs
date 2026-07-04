//! WebSocket bridge — Rust message loop, Python handler invocation.
//!
//! Architecture (two execution modes per connection):
//!   - Axum WebSocket → Rust tokio task reads messages, converts to typed enum
//!   - Typed messages flow via crossbeam channel to Python
//!   - State tracked via atomic u8 (matches Starlette's WebSocketState enum)
//!   - Binary preserved as `Bytes` (no UTF-8 coercion)
//!
//! THREAD mode (legacy, `FASTAPI_TURBO_WS_LOOP=0`): every connection owns a
//! dedicated `spawn_blocking` thread for its lifetime; receives block on the
//! crossbeam channel with the GIL released (`RecvAwaitable` never suspends —
//! the whole handler runs inside one `coro.send(None)`).
//!
//! LOOP mode (`FASTAPI_TURBO_WS_LOOP=1`): the handler coroutine runs as an
//! asyncio TASK on the shared `_async_worker` loop — many sockets per loop,
//! no per-connection thread, no 512-blocking-thread cap. Receive/accept/
//! close-flush become true loop-native awaitables: the reader task resolves
//! an armed asyncio Future via one `call_soon_threadsafe` per wake (coalesced
//! — pushes with no armed waiter are GIL-free), and the batch-drain buffer
//! serves already-queued frames without any loop machinery. Outbound switches
//! to a bounded channel: `try_send` on the hot path, a capacity Future on
//! `Full` (the `LoopChunkPush` precedent from streaming.rs) so a slow client
//! backpressures the handler instead of buffering unboundedly.
//!
//! DIRECT SEND (both modes, `FASTAPI_TURBO_WS_DIRECT=0` to disable):
//! outbound data frames are written to the socket inline by the calling
//! thread via one non-blocking poll of the shared write half — no channel
//! push, no select-task wake. The round-4 audit measured that wake ("hop2")
//! at 5.6µs p50 / 17.3µs p99 per message — the entire server-side latency
//! tail. Contended/not-ready/ordered-behind-queued-commands cases fall back
//! to the legacy channel; an atomic in-flight count keeps ordering strict.
//!
//! WRITE COALESCING (both modes, `FASTAPI_TURBO_WS_COALESCE=0` to disable):
//! when the handler already has more inbound frames pending (batch buffer /
//! channel non-empty), direct sends `start_send` into the sink's write
//! buffer without the per-frame flush syscall; ONE flush fires at the batch
//! boundary (see `ws_coalesce_enabled` docs). N pipelined echoes leave as
//! one syscall + one TCP segment, which keeps the peer's replies batched —
//! the defense against the per-frame-lockstep slow basin the WS fleet bench
//! exposed (bimodal 390k/128k msgs/s at 48 conns x pipeline 16).

use axum::extract::ws::Message;
use bytes::Bytes;
use crossbeam_channel as cb;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use tokio::sync::mpsc;

/// Write half of the connection, shared between the select task (queued
/// commands: Close, flush, fallback data frames) and direct senders
/// (`send_text`/`send_bytes` fast path). `None` before the upgrade completes
/// and after teardown.
type WsSink = Arc<
    tokio::sync::Mutex<
        Option<futures_util::stream::SplitSink<axum::extract::ws::WebSocket, Message>>,
    >,
>;

/// Cached `fastapi_turbo.exceptions.WebSocketDisconnect` class — used by the
/// receive awaitables to raise the correct typed exception without the
/// Python-side `try/except` translation layer.
static WS_DISCONNECT_CLS: OnceLock<Py<PyAny>> = OnceLock::new();

fn ws_disconnect_class(py: Python<'_>) -> Option<&'static Py<PyAny>> {
    if let Some(c) = WS_DISCONNECT_CLS.get() {
        return Some(c);
    }
    let exc = py.import("fastapi_turbo.exceptions").ok()?;
    let cls: Py<PyAny> = exc.getattr("WebSocketDisconnect").ok()?.unbind();
    let _ = WS_DISCONNECT_CLS.set(cls);
    WS_DISCONNECT_CLS.get()
}

fn disconnect_err(py: Python<'_>, code: u16, reason: &str) -> PyErr {
    match ws_disconnect_class(py) {
        Some(cls) => {
            let kwargs = pyo3::types::PyDict::new(py);
            let _ = kwargs.set_item("code", code);
            let _ = kwargs.set_item("reason", reason);
            match cls.bind(py).call((), Some(&kwargs)) {
                Ok(exc) => PyErr::from_value(exc),
                Err(_) => {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("WS_CLOSED:{code}:{reason}"))
                }
            }
        }
        None => pyo3::exceptions::PyRuntimeError::new_err(format!("WS_CLOSED:{code}:{reason}")),
    }
}

use crate::handler_bridge;

// ── Loop-residency mode (FASTAPI_TURBO_WS_LOOP) ────────────────────

/// Compile-time default for WS loop-residency when the env var is unset.
/// Opt-in (`FASTAPI_TURBO_WS_LOOP=1`) until the full battery + A/B gate a
/// default flip; `FASTAPI_TURBO_WS_LOOP=0` stays the kill switch either way.
const WS_LOOP_DEFAULT: bool = false;

/// Process-wide flag, read once.
fn ws_loop_enabled() -> bool {
    static FLAG: OnceLock<bool> = OnceLock::new();
    *FLAG.get_or_init(|| match std::env::var("FASTAPI_TURBO_WS_LOOP") {
        Ok(v) => matches!(v.trim(), "1" | "true" | "True" | "TRUE" | "yes" | "on"),
        Err(_) => WS_LOOP_DEFAULT,
    })
}

/// Direct-send fast path (`FASTAPI_TURBO_WS_DIRECT`, default ON): outbound
/// data frames are written to the socket inline by the calling thread when
/// the sink is free and immediately writable — one single-poll attempt with
/// a noop waker, so it can NEVER block (safe from the shared worker loop
/// too). Everything else (contention, kernel-buffer Pending, queued
/// commands ahead of us) falls back to the legacy channel + select task.
/// This removes the mpsc-push → select-task-wake hop ("hop2": 5.6µs p50,
/// 17.3µs p99 — the entire server-side latency tail in the round-4 audit).
fn ws_direct_enabled() -> bool {
    static FLAG: OnceLock<bool> = OnceLock::new();
    *FLAG.get_or_init(|| match std::env::var("FASTAPI_TURBO_WS_DIRECT") {
        Ok(v) => !matches!(v.trim(), "0" | "false" | "False" | "FALSE" | "no" | "off"),
        Err(_) => true,
    })
}

/// Write-coalescing on the direct-send path (`FASTAPI_TURBO_WS_COALESCE`,
/// default ON): when MORE inbound frames are already queued for this handler
/// (batch-drain buffer or channel non-empty), an outbound data frame is
/// `start_send`-committed to the sink's write buffer WITHOUT the per-frame
/// flush syscall. The flush happens once at the batch boundary — the send
/// that finds no more inbound frames pending, the receive that is about to
/// suspend/park, a 32KB byte cap, or a 1ms debounce net (a handler that
/// feeds replies then awaits something foreign can't strand them).
///
/// Under pipelining this turns N echo round trips into ONE write syscall and
/// ONE TCP segment. The segment matters more than the syscall: the WS fleet
/// bench showed a bimodal 390k/128k msgs/s regime (48 conns x pipeline 16)
/// whose slow basin is per-frame lockstep — 16 separate echo segments make
/// the client reply one-at-a-time, which keeps every hop (kevent, crossbeam
/// unpark, GIL attach) paying per-frame wake latency. One batched segment
/// keeps the client's replies batched too, defending the fast basin.
fn ws_coalesce_enabled() -> bool {
    static FLAG: OnceLock<bool> = OnceLock::new();
    *FLAG.get_or_init(|| match std::env::var("FASTAPI_TURBO_WS_COALESCE") {
        Ok(v) => !matches!(v.trim(), "0" | "false" | "False" | "FALSE" | "no" | "off"),
        Err(_) => true,
    })
}

/// Unflushed-bytes cap for write coalescing — a batch of large frames flushes
/// early instead of accumulating unboundedly in the sink's write buffer.
const WS_COALESCE_MAX_BYTES: usize = 32 * 1024;

/// Flush deferred (coalesced) outbound bytes. Non-blocking single-poll
/// attempt first; a contended or partial flush is handed to a spawned driver
/// (the sink lock serializes it with every other writer). Callable from any
/// thread, with or without the GIL — touches no Python objects.
fn flush_deferred(
    rt: Option<&tokio::runtime::Handle>,
    sink: &WsSink,
    unflushed: &Arc<AtomicUsize>,
) {
    use futures_util::Sink;
    use std::pin::Pin;
    use std::task::{Context, Waker};
    if unflushed.load(Ordering::Acquire) == 0 {
        return;
    }
    unflushed.store(0, Ordering::Release);
    if let Ok(mut guard) = sink.try_lock() {
        let Some(s) = guard.as_mut() else {
            return; // torn down — bytes died with the connection
        };
        let mut cx = Context::from_waker(Waker::noop());
        if Pin::new(s).poll_flush(&mut cx).is_ready() {
            return; // fully written (or sink broken — next op raises)
        }
        // Partial write (kernel buffer full) — fall through to the driver.
        drop(guard);
    }
    // Lock contended (select task mid-write — its own send flushes
    // everything anyway) or flush Pending: drive it from a task.
    if let Some(rt) = rt {
        let sink = sink.clone();
        rt.spawn(async move {
            use futures_util::SinkExt;
            let mut g = sink.lock().await;
            if let Some(s) = g.as_mut() {
                let _ = s.flush().await;
            }
        });
    }
}

/// Outbound-queue capacity in loop mode. Deep enough that echo/burst
/// workloads never hit the capacity-future path; shallow enough that a
/// slow client backpressures the handler instead of buffering unboundedly
/// (the unbounded thread-mode channel is an OOM vector under a stalled peer).
const WS_OUT_CAP: usize = 256;

/// Cached `fastapi_turbo.responses._resolve_stream_future` — the
/// `call_soon_threadsafe` target that resolves a loop-mode future
/// (`done()`-guarded so a future cancelled by loop shutdown never raises
/// `InvalidStateError` into the loop's exception handler).
fn ws_future_resolver(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static RESOLVER: OnceLock<Py<PyAny>> = OnceLock::new();
    if let Some(r) = RESOLVER.get() {
        return Ok(r);
    }
    let f = py
        .import("fastapi_turbo.responses")?
        .getattr("_resolve_stream_future")?
        .unbind();
    let _ = RESOLVER.set(f);
    Ok(RESOLVER.get().expect("just set"))
}

/// Cached `fastapi_turbo.websockets._ws_task_done` — done-callback for
/// loop-resident handler tasks (retrieves the exception so asyncio never
/// logs "Task exception was never retrieved"; the wrapper already routed
/// real errors through the app's capture queues).
fn ws_task_done_fn(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static DONE: OnceLock<Py<PyAny>> = OnceLock::new();
    if let Some(f) = DONE.get() {
        return Ok(f);
    }
    let f = py
        .import("fastapi_turbo.websockets")?
        .getattr("_ws_task_done")?
        .unbind();
    let _ = DONE.set(f);
    Ok(DONE.get().expect("just set"))
}

/// Create a fresh asyncio Future on the shared worker loop. Loop-mode
/// callers run ON the loop thread (the handler task called them
/// synchronously), so `create_future()` here is loop-safe.
fn create_loop_future(py: Python<'_>) -> PyResult<Py<PyAny>> {
    let loop_obj = handler_bridge::worker_loop().ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err("async worker loop not initialized")
    })?;
    loop_obj.call_method0(py, "create_future")
}

/// Resolve a loop-mode future from a NON-loop thread (tokio reader/writer
/// tasks, capacity waiters): one GIL attach + one `call_soon_threadsafe`.
fn resolve_ws_future(fut: Py<PyAny>) {
    Python::attach(|py| {
        let Some(call_soon) = handler_bridge::worker_call_soon() else {
            return;
        };
        let Ok(resolver) = ws_future_resolver(py) else {
            return;
        };
        let _ = call_soon.call1(py, (resolver.bind(py), fut.bind(py), py.None()));
    });
}

/// Receive-wake slot shared between the loop-resident handler task (arms a
/// Future when the channel is empty) and the Rust reader task (fires it after
/// pushing a frame). Wake coalescing is the whole GIL story: a push with no
/// armed waiter (task busy, frames already queued) costs ZERO GIL work; under
/// pipelining one attach+wake drains a whole burst through the batch buffer.
struct LoopWake {
    armed: AtomicBool,
    fut: Mutex<Option<Py<PyAny>>>,
}

impl LoopWake {
    fn new() -> Self {
        LoopWake {
            armed: AtomicBool::new(false),
            fut: Mutex::new(None),
        }
    }

    /// Fire the wake if armed (reader task / teardown). Idempotent.
    fn fire(&self) {
        if self.armed.swap(false, Ordering::AcqRel) {
            if let Some(fut) = self.fut.lock().ok().and_then(|mut g| g.take()) {
                resolve_ws_future(fut);
            }
        }
    }
}

/// Resolves the loop-mode accept-ready future exactly once — explicitly from
/// the on_upgrade callback, or via Drop on EVERY other exit path (upgrade
/// never polled, handshake abort, 30s accept timeout racing a late accept).
/// Without the Drop arm a handler task suspended on `await accept()` could
/// be stranded forever on the shared loop.
struct ReadyGuard {
    slot: Arc<Mutex<Option<Py<PyAny>>>>,
}

impl ReadyGuard {
    fn fire(&self) {
        if let Some(fut) = self.slot.lock().ok().and_then(|mut g| g.take()) {
            resolve_ws_future(fut);
        }
    }
}

impl Drop for ReadyGuard {
    fn drop(&mut self) {
        self.fire();
    }
}

/// Loop-thread callback (enqueued via `call_soon_threadsafe` with a fresh
/// empty contextvars.Context): spawn the WS handler coroutine as a task on
/// the shared worker loop. With the loop's eager task factory the first step
/// runs synchronously here — the handler reaches `await accept()` (and fires
/// the accept oneshot) without an extra loop iteration.
#[pyclass]
struct WsLoopJob {
    coro: Option<Py<PyAny>>,
}

#[pymethods]
impl WsLoopJob {
    fn __call__(&mut self, py: Python<'_>) {
        let Some(coro) = self.coro.take() else {
            return;
        };
        let Some(loop_obj) = handler_bridge::worker_loop() else {
            let _ = coro.call_method0(py, "close");
            return;
        };
        match loop_obj.call_method1(py, "create_task", (coro.bind(py),)) {
            Ok(task) => {
                // Retrieve-exception done-callback: the `_ws_entry` wrapper
                // already surfaces real errors through the app's capture
                // queues; this only silences asyncio's "exception was never
                // retrieved" for anything that escapes it (parity with the
                // thread path's `let _ =`).
                if let Ok(done) = ws_task_done_fn(py) {
                    let _ = task.call_method1(py, "add_done_callback", (done.bind(py),));
                }
            }
            Err(_) => {
                let _ = coro.call_method0(py, "close");
            }
        }
    }
}

/// Metadata about the inbound WebSocket upgrade request — populated by the
/// Rust route handler before the upgrade, used to build the Python scope dict.
#[derive(Default)]
pub struct WsScopeInfo {
    pub path: String,
    pub raw_path: Vec<u8>,
    pub query_string: Vec<u8>,
    pub headers: Vec<(String, String)>, // all headers, lowercased
    pub client: Option<(String, u16)>,
    pub scheme: String, // "ws" or "wss"
    pub host: String,
    pub path_params: Vec<(String, String)>, // matched route path params
    /// Client-offered subprotocols (from the ``Sec-WebSocket-Protocol``
    /// request header, split on commas and trimmed). Starlette surfaces
    /// this in ``scope["subprotocols"]`` so apps can call
    /// ``ws.accept(subprotocol=...)`` with one of the offered values.
    pub subprotocols: Vec<String>,
}

// ── WebSocket state (matches Starlette's WebSocketState) ───────────
// These values MUST match python/fastapi_turbo/websockets.py::WebSocketState.

pub const STATE_CONNECTING: u8 = 0;
pub const STATE_CONNECTED: u8 = 1;
pub const STATE_DISCONNECTED: u8 = 2;
// Note: Starlette also defines STATE_RESPONSE = 3 for when a WS handler
// returns an HTTP response instead of upgrading. We don't yet emit that.

// ── Typed message enum ────────────────────────────────────────────

/// Messages flowing from the WS reader task to Python.
/// Binary is preserved via `Bytes` (reference-counted, zero-copy from axum).
pub enum WsMessage {
    Text(String),
    Binary(Bytes),
    Close { code: u16, reason: String },
}

// ── Awaitable — one custom Python awaitable backed by crossbeam ────
//
// A single `RecvAwaitable` with a `RecvKind` selecting the return shape:
//   - Dict  : ASGI dict (for receive())
//   - Text  : str directly (for receive_text())
//   - Bytes : bytes directly (for receive_bytes())
//
// All kinds share the SAME underlying channel receiver — each await consumes
// one message. Cached per-PyWebSocket (one instance per kind) so Python's
// await doesn't allocate a new pyclass per call.

fn handle_close_err(state: &Arc<AtomicU8>) -> PyErr {
    state.store(STATE_DISCONNECTED, Ordering::Release);
    pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed")
}

/// Direction of a WS receive awaitable — selects how the next channel
/// message is surfaced to Python. One pyclass instead of three near-identical
/// ones; each `PyWebSocket` caches one instance per kind.
#[derive(Clone, Copy)]
enum RecvKind {
    /// ASGI dict: {"type": "websocket.receive"/"websocket.disconnect", ...} (receive()).
    Dict,
    /// str directly; raises WebSocketDisconnect on close (receive_text()).
    Text,
    /// bytes directly; raises WebSocketDisconnect on close (receive_bytes()).
    Bytes,
}

#[pyclass]
pub struct RecvAwaitable {
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
    kind: RecvKind,
    // Batch-drain buffer — shared across the 3 kind-instances of one
    // connection. One blocking recv per burst; frames already queued in the
    // channel are drained into this buffer and served on later awaits
    // without a GIL detach or cross-thread wake.
    buf: Arc<std::sync::Mutex<std::collections::VecDeque<WsMessage>>>,
    // Loop-residency: when true, an empty channel returns an asyncio Future
    // (armed via `wake`) instead of blocking the thread — the enclosing
    // handler TASK suspends on it natively.
    loop_mode: bool,
    wake: Arc<LoopWake>,
    // Write-coalescing flush hooks: a receive that is about to suspend
    // (thread park / loop future) first flushes any deferred echo bytes so
    // the peer is never left waiting on frames parked in the sink buffer.
    sink: WsSink,
    rt: Option<tokio::runtime::Handle>,
    unflushed: Arc<AtomicUsize>,
}

/// Max frames pulled per wake — bounds GIL-resident burst processing.
const BATCH_DRAIN_CAP: usize = 64;

/// Outcome of a loop-mode receive attempt.
enum LoopStep {
    /// A frame was available (batch buffer refilled as a side effect).
    Msg(WsMessage),
    /// Channel empty — the armed Future to yield to the task.
    Pending(Py<PyAny>),
}

impl RecvAwaitable {
    /// Non-blocking pull: first frame from the channel + drain the rest of
    /// the burst into the shared buffer (identical batching to thread mode).
    /// `Ok(None)` = empty, `Err(())` = channel closed (reader task gone).
    fn try_pull(&self) -> Result<Option<WsMessage>, ()> {
        match self.rx.try_recv() {
            Ok(first) => {
                let mut b = self.buf.lock().unwrap();
                while b.len() < BATCH_DRAIN_CAP {
                    match self.rx.try_recv() {
                        Ok(m) => b.push_back(m),
                        Err(_) => break,
                    }
                }
                Ok(Some(first))
            }
            Err(cb::TryRecvError::Empty) => Ok(None),
            Err(cb::TryRecvError::Disconnected) => Err(()),
        }
    }

    /// Loop-mode receive: try_recv fast path, else arm a Future for the
    /// reader task to resolve. The arm-then-double-check ordering makes a
    /// lost wakeup impossible: the reader pushes BEFORE checking `armed`,
    /// so a frame that lands after our final empty-check finds `armed=true`
    /// and fires the wake.
    fn loop_next(&self, py: Python<'_>) -> PyResult<LoopStep> {
        match self.try_pull() {
            Ok(Some(m)) => return Ok(LoopStep::Msg(m)),
            Ok(None) => {}
            Err(()) => return Err(handle_close_err(&self.state)),
        }
        // About to suspend the handler task — the peer only sends more once
        // it sees our replies, so any coalesced echo bytes must hit the wire
        // NOW (no-op unless the direct path deferred a flush).
        flush_deferred(self.rt.as_ref(), &self.sink, &self.unflushed);
        let fut = create_loop_future(py)?;
        // Yielding a raw Future from a custom awaitable requires the
        // `_asyncio_future_blocking = True` protocol marker (asyncio's
        // Future.__await__ sets it the same way before `yield self`).
        fut.bind(py)
            .setattr(pyo3::intern!(py, "_asyncio_future_blocking"), true)?;
        *self.wake.fut.lock().unwrap() = Some(fut.clone_ref(py));
        self.wake.armed.store(true, Ordering::Release);
        // Double-check AFTER arming (lost-wakeup race close-out).
        match self.try_pull() {
            Ok(Some(m)) => {
                // Disarm best-effort; if the reader already took the flag it
                // will resolve the (now-unawaited) future — harmless, the
                // resolver is done()-guarded and the slot is replaced on the
                // next arm.
                self.wake.armed.swap(false, Ordering::AcqRel);
                Ok(LoopStep::Msg(m))
            }
            Ok(None) => Ok(LoopStep::Pending(fut)),
            Err(()) => {
                self.wake.armed.swap(false, Ordering::AcqRel);
                Err(handle_close_err(&self.state))
            }
        }
    }
}

#[pymethods]
impl RecvAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        // Batch-drain fast path: serve from the shared buffer when a prior
        // wake already pulled queued frames — no detach, no blocking recv.
        let buffered = self.buf.lock().unwrap().pop_front();
        let msg = match buffered {
            Some(m) => m,
            None if self.loop_mode => match self.loop_next(py)? {
                LoopStep::Msg(m) => m,
                // Suspend the task on the armed Future; the wakeup re-enters
                // __next__ and the try_pull then finds the frame(s).
                LoopStep::Pending(fut) => return Ok(fut),
            },
            None => {
                // About to park this thread until the peer sends more — and
                // the peer may be waiting on OUR coalesced replies. Flush
                // deferred bytes first (no-op when nothing was deferred).
                flush_deferred(self.rt.as_ref(), &self.sink, &self.unflushed);
                let rx = self.rx.clone();
                let state = self.state.clone();
                let first = py.detach(|| rx.recv().map_err(|_| handle_close_err(&state)))?;
                // One wake, many frames: move whatever else is already queued
                // into the buffer (bounded); later awaits pop without blocking.
                // FIFO order is preserved; a Close is just a buffered message
                // (the reader task stops after forwarding Close, so nothing
                // ever follows it in the channel).
                let mut b = self.buf.lock().unwrap();
                while b.len() < BATCH_DRAIN_CAP {
                    match self.rx.try_recv() {
                        Ok(m) => b.push_back(m),
                        Err(_) => break,
                    }
                }
                first
            }
        };
        match self.kind {
            RecvKind::Dict => {
                let dict = PyDict::new(py);
                match msg {
                    WsMessage::Text(t) => {
                        dict.set_item(
                            pyo3::intern!(py, "type"),
                            pyo3::intern!(py, "websocket.receive"),
                        )?;
                        dict.set_item(pyo3::intern!(py, "text"), t)?;
                    }
                    WsMessage::Binary(b) => {
                        dict.set_item(
                            pyo3::intern!(py, "type"),
                            pyo3::intern!(py, "websocket.receive"),
                        )?;
                        dict.set_item(pyo3::intern!(py, "bytes"), PyBytes::new(py, &b))?;
                    }
                    WsMessage::Close { code, reason } => {
                        self.state.store(STATE_DISCONNECTED, Ordering::Release);
                        dict.set_item(
                            pyo3::intern!(py, "type"),
                            pyo3::intern!(py, "websocket.disconnect"),
                        )?;
                        dict.set_item(pyo3::intern!(py, "code"), code)?;
                        dict.set_item(pyo3::intern!(py, "reason"), reason)?;
                    }
                }
                Err(pyo3::exceptions::PyStopIteration::new_err(dict.unbind()))
            }
            RecvKind::Text => {
                let value: Py<PyAny> = match msg {
                    WsMessage::Text(t) => t.into_pyobject(py)?.into_any().unbind(),
                    WsMessage::Binary(b) => String::from_utf8(b.to_vec())
                        .map_err(|_| {
                            pyo3::exceptions::PyRuntimeError::new_err(
                                "Received binary message when expecting text",
                            )
                        })?
                        .into_pyobject(py)?
                        .into_any()
                        .unbind(),
                    WsMessage::Close { code, reason } => {
                        self.state.store(STATE_DISCONNECTED, Ordering::Release);
                        return Err(disconnect_err(py, code, &reason));
                    }
                };
                Err(pyo3::exceptions::PyStopIteration::new_err(value))
            }
            RecvKind::Bytes => {
                let value: Py<PyAny> = match msg {
                    WsMessage::Binary(b) => PyBytes::new(py, &b).into_any().unbind(),
                    WsMessage::Text(s) => PyBytes::new(py, s.as_bytes()).into_any().unbind(),
                    WsMessage::Close { code, reason } => {
                        self.state.store(STATE_DISCONNECTED, Ordering::Release);
                        return Err(disconnect_err(py, code, &reason));
                    }
                };
                Err(pyo3::exceptions::PyStopIteration::new_err(value))
            }
        }
    }
}

// ── PyWebSocket: the Rust-side WS handle exposed to Python ─────────

/// Flush acknowledgement channel — mode-specific. Thread mode signals a
/// crossbeam sender (blocking `CloseAwaitable` recv); loop mode resolves an
/// asyncio Future via `call_soon_threadsafe`.
pub enum FlushAck {
    Thread(cb::Sender<()>),
    Loop(Py<PyAny>),
}

impl FlushAck {
    fn fire(self) {
        match self {
            FlushAck::Thread(tx) => {
                let _ = tx.send(());
            }
            FlushAck::Loop(fut) => resolve_ws_future(fut),
        }
    }
}

/// Command sent to the WS writer task.
/// `Flush` acks once the writer drained all prior Sends — used by `close()` to truly await.
pub enum WriterCmd {
    Send(Message),
    Flush(FlushAck),
}

/// Outbound command sender — unbounded in thread mode (exact legacy
/// behavior), bounded in loop mode (`try_send` + capacity Future on Full;
/// the shared loop must NEVER block).
#[derive(Clone)]
enum WsCmdTx {
    Unbounded(mpsc::UnboundedSender<WriterCmd>),
    Bounded(mpsc::Sender<WriterCmd>),
}

/// Receiver counterpart, consumed by the per-connection select task.
enum WsCmdRx {
    Unbounded(mpsc::UnboundedReceiver<WriterCmd>),
    Bounded(mpsc::Receiver<WriterCmd>),
}

impl WsCmdRx {
    async fn recv(&mut self) -> Option<WriterCmd> {
        match self {
            WsCmdRx::Unbounded(r) => r.recv().await,
            WsCmdRx::Bounded(r) => r.recv().await,
        }
    }

    fn close(&mut self) {
        match self {
            WsCmdRx::Unbounded(r) => r.close(),
            WsCmdRx::Bounded(r) => r.close(),
        }
    }

    fn drain_next(&mut self) -> Option<WriterCmd> {
        match self {
            WsCmdRx::Unbounded(r) => r.try_recv().ok(),
            WsCmdRx::Bounded(r) => r.try_recv().ok(),
        }
    }
}

/// Parameters Python passes to `ws.accept(subprotocol=..., headers=...)`.
/// Sent from Python → route handler via a tokio oneshot, used to build the
/// actual WebSocket upgrade response (picking subprotocol, adding headers).
pub struct AcceptParams {
    pub subprotocol: Option<String>,
    pub headers: Vec<(String, String)>,
}

/// What Python signals through the accept oneshot: either "upgrade the
/// socket with these parameters" or "reject the handshake before upgrade
/// with this HTTP status". Starlette rejects pre-accept WS connections
/// with 403; we allow the caller to pick a code so dependency-injected
/// auth middlewares can return 401/403/etc.
pub enum AcceptAction {
    Accept(AcceptParams),
    Reject { status: u16 },
}

#[pyclass]
pub struct PyWebSocket {
    tx: WsCmdTx,
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
    // Loop-residency (FASTAPI_TURBO_WS_LOOP): handler runs as a task on the
    // shared worker loop; receive/accept/close-flush are loop-native futures.
    loop_mode: bool,
    // Request runtime handle — loop mode spawns capacity waiters onto it,
    // the direct-send path spawns flush drivers (the calling thread may have
    // no tokio runtime context of its own).
    rt: Option<tokio::runtime::Handle>,
    // Shared write half for the direct-send fast path (None until upgrade).
    sink: WsSink,
    // Send commands enqueued to the channel but not yet written by the
    // select task. Direct sends are only allowed at 0 — otherwise a frame
    // could overtake one still sitting in the channel. Incremented BEFORE
    // every channel push, decremented by the select task AFTER the sink
    // write completes.
    queued: Arc<AtomicUsize>,
    // Write-coalescing state: bytes start_send-committed to the sink buffer
    // but not yet flushed, and the debounce-net arm flag (one 1ms flush
    // timer per deferred burst — the batch-boundary flush almost always
    // wins the race; the timer only matters for handlers that feed replies
    // and then await something foreign).
    unflushed: Arc<AtomicUsize>,
    flush_armed: Arc<AtomicBool>,
    // Receive-wake slot shared with the reader task.
    wake: Arc<LoopWake>,
    // Accept-ready future slot — resolved by the on_upgrade callback (or its
    // drop guard) once the socket tasks are wired.
    ready_fut: Arc<Mutex<Option<Py<PyAny>>>>,
    // Three cached awaitables — one per return-type (dict / text / bytes).
    // Each is lazily created on first use. Safe because a single WS has one reader.
    cached_dict: std::sync::OnceLock<Py<RecvAwaitable>>,
    cached_text: std::sync::OnceLock<Py<RecvAwaitable>>,
    cached_bytes: std::sync::OnceLock<Py<RecvAwaitable>>,
    // Batch-drain frame buffer handed to every receive awaitable — see
    // `RecvAwaitable::buf`. Shared so receive()/receive_text()/receive_bytes()
    // interleave over one FIFO stream.
    recv_buf: Arc<std::sync::Mutex<std::collections::VecDeque<WsMessage>>>,
    // Scope info populated by the Rust route handler from the inbound request.
    // Python reads via get_scope_dict() to build HTTPConnection-like properties.
    scope_info: Arc<WsScopeInfo>,
    // Pre-upgrade signaling (deferred-upgrade architecture):
    //   accept_tx is consumed on first accept() call — sends chosen subprotocol
    //   and custom headers to the route handler, which then performs the upgrade.
    //   ready_rx blocks until the route handler has finished the upgrade and
    //   connected the post-upgrade socket tasks — then send/receive can flow.
    accept_tx: Arc<std::sync::Mutex<Option<tokio::sync::oneshot::Sender<AcceptAction>>>>,
    ready_rx: cb::Receiver<()>,
}

impl PyWebSocket {
    fn make_awaitable(&self, py: Python<'_>, kind: RecvKind) -> Py<RecvAwaitable> {
        Py::new(
            py,
            RecvAwaitable {
                rx: self.rx.clone(),
                state: self.state.clone(),
                kind,
                buf: self.recv_buf.clone(),
                loop_mode: self.loop_mode,
                wake: self.wake.clone(),
                sink: self.sink.clone(),
                rt: self.rt.clone(),
                unflushed: self.unflushed.clone(),
            },
        )
        .expect("create recv awaitable")
    }

    fn get_dict_awaitable(&self, py: Python<'_>) -> Py<RecvAwaitable> {
        self.cached_dict
            .get_or_init(|| self.make_awaitable(py, RecvKind::Dict))
            .clone_ref(py)
    }

    fn get_text_awaitable(&self, py: Python<'_>) -> Py<RecvAwaitable> {
        self.cached_text
            .get_or_init(|| self.make_awaitable(py, RecvKind::Text))
            .clone_ref(py)
    }

    fn get_bytes_awaitable(&self, py: Python<'_>) -> Py<RecvAwaitable> {
        self.cached_bytes
            .get_or_init(|| self.make_awaitable(py, RecvKind::Bytes))
            .clone_ref(py)
    }

    /// Direct-send attempt: write the frame to the socket inline, WITHOUT
    /// blocking and WITHOUT the select-task wake. Returns `None` when the
    /// frame was handled (written, committed to the sink's internal buffer
    /// with a spawned flush driver, or sunk by a broken sink — the legacy
    /// path also drops frames silently once the writer dies; the NEXT
    /// operation surfaces the error). Returns `Some(msg)` when the caller
    /// must fall back to the channel (sink busy/not ready/not yet upgraded,
    /// or commands already queued ahead of us).
    /// `defer_flush`: commit the frame to the sink's write buffer WITHOUT
    /// the flush syscall — the caller has established that more inbound
    /// frames are already pending, so a batch-boundary flush is coming
    /// (send-with-nothing-pending, pre-suspend receive, byte cap, or the
    /// 1ms debounce net armed by `queue_send`).
    fn try_direct_send(&self, msg: Message, defer_flush: bool, msg_len: usize) -> Option<Message> {
        use futures_util::Sink;
        use std::pin::Pin;
        use std::task::{Context, Poll, Waker};
        // A flush driver must be spawnable before we may commit a frame.
        let Some(rt) = self.rt.as_ref() else {
            return Some(msg);
        };
        let Ok(mut guard) = self.sink.try_lock() else {
            return Some(msg); // select task / another sender is mid-write
        };
        // Re-check UNDER the lock: a command still in the channel must be
        // written first. (Enqueuers increment before pushing; the select
        // task decrements only after its sink write completed — and that
        // write holds this lock, so 0-under-lock == nothing can overtake.)
        if self.queued.load(Ordering::Acquire) != 0 {
            return Some(msg);
        }
        let Some(s) = guard.as_mut() else {
            return Some(msg); // pre-upgrade or torn down — channel decides
        };
        let mut cx = Context::from_waker(Waker::noop());
        let mut s = Pin::new(s);
        match s.as_mut().poll_ready(&mut cx) {
            Poll::Ready(Ok(())) => {}
            // Pending (prior buffered bytes / BiLock contention) or sink
            // error — let the select task deal with it, in order.
            _ => return Some(msg),
        }
        if s.as_mut().start_send(msg).is_err() {
            // Frame consumed by a broken sink — legacy parity is a silent
            // drop (the writer task breaking loses queued frames the same
            // way); the next send/receive raises.
            return None;
        }
        if defer_flush {
            // Batched: leave the frame in the sink buffer; account for it
            // so the boundary flushers know there is work.
            self.unflushed.fetch_add(msg_len + 8, Ordering::AcqRel);
            return None;
        }
        self.unflushed.store(0, Ordering::Release); // this flush covers any deferred bytes
        match s.as_mut().poll_flush(&mut cx) {
            Poll::Ready(_) => None, // written (or sink broken — see above)
            Poll::Pending => {
                // Frame committed to the sink buffer but the kernel buffer
                // is full: drive the flush from a task (locks the sink, so
                // it serializes with every other writer; later writes flush
                // prior bytes first — order preserved either way).
                drop(guard);
                let sink = self.sink.clone();
                rt.spawn(async move {
                    use futures_util::SinkExt;
                    let mut g = sink.lock().await;
                    if let Some(s) = g.as_mut() {
                        let _ = s.flush().await;
                    }
                });
                None
            }
        }
    }

    /// Queue an outbound frame. Fast path (both modes): direct single-poll
    /// write on the calling thread — no channel, no select-task wake.
    /// Fallback thread mode: unbounded push (legacy).
    /// Fallback loop mode: `try_send`; on Full, hand back a capacity Future — a tokio
    /// waiter `reserve()`s a slot, delivers the pending command itself (order
    /// preserved: the handler task is suspended on the Future until then) and
    /// resolves it via `call_soon_threadsafe`.
    fn queue_send(
        &self,
        py: Python<'_>,
        msg: Message,
        msg_len: usize,
    ) -> PyResult<Option<Py<PyAny>>> {
        let msg = if ws_direct_enabled() && self.queued.load(Ordering::Acquire) == 0 {
            // Write coalescing: when the handler ALREADY has more inbound
            // frames waiting (batch buffer or channel), it will come
            // straight back here — defer the flush syscall to the batch
            // boundary. One TCP segment per burst instead of per frame
            // keeps the peer's replies batched too (fast-basin defense).
            let defer_flush = ws_coalesce_enabled()
                && self.unflushed.load(Ordering::Acquire) + msg_len <= WS_COALESCE_MAX_BYTES
                && (!self.recv_buf.lock().unwrap().is_empty() || !self.rx.is_empty());
            // NOTE: no `py.detach` here — every branch below is a single
            // non-blocking poll (noop waker), so the GIL is held for ~1-3µs
            // of syscall at most. Detaching cost more than it saved: each
            // detach invites a GIL steal mid-burst, which spreads a batch
            // of echoes across GIL handoffs and shatters the write batch.
            match self.try_direct_send(msg, defer_flush, msg_len) {
                None => {
                    if defer_flush
                        && self
                            .flush_armed
                            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                            .is_ok()
                    {
                        // Debounce net: one 1ms timer per deferred burst.
                        // Usually a no-op (boundary flush wins); guarantees
                        // a handler that stops echoing can't strand replies
                        // in the sink buffer for more than ~1ms.
                        if let Some(rt) = self.rt.as_ref() {
                            let sink = self.sink.clone();
                            let unflushed = self.unflushed.clone();
                            let armed = self.flush_armed.clone();
                            rt.spawn(async move {
                                tokio::time::sleep(std::time::Duration::from_millis(1)).await;
                                armed.store(false, Ordering::Release);
                                if unflushed.swap(0, Ordering::AcqRel) > 0 {
                                    use futures_util::SinkExt;
                                    let mut g = sink.lock().await;
                                    if let Some(s) = g.as_mut() {
                                        let _ = s.flush().await;
                                    }
                                }
                            });
                        }
                    }
                    return Ok(None);
                }
                Some(m) => m,
            }
        } else {
            msg
        };
        // Counted from here on: the frame is committed to the CHANNEL path
        // (even the loop-mode Full case — its waiter still delivers it).
        self.queued.fetch_add(1, Ordering::AcqRel);
        match &self.tx {
            WsCmdTx::Unbounded(tx) => {
                tx.send(WriterCmd::Send(msg)).map_err(|e| {
                    pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}"))
                })?;
                Ok(None)
            }
            WsCmdTx::Bounded(tx) => match tx.try_send(WriterCmd::Send(msg)) {
                Ok(()) => Ok(None),
                Err(mpsc::error::TrySendError::Closed(_)) => Err(
                    pyo3::exceptions::PyRuntimeError::new_err("WS send: channel closed"),
                ),
                Err(mpsc::error::TrySendError::Full(pending)) => {
                    let rt = self.rt.as_ref().ok_or_else(|| {
                        pyo3::exceptions::PyRuntimeError::new_err("WS send: no runtime handle")
                    })?;
                    let fut = create_loop_future(py)?;
                    let fut_for_waiter = fut.clone_ref(py);
                    let tx2 = tx.clone();
                    rt.spawn(async move {
                        // Err = receiver gone — frame dropped, next send raises.
                        if let Ok(permit) = tx2.reserve().await {
                            permit.send(pending);
                        }
                        resolve_ws_future(fut_for_waiter);
                    });
                    Ok(Some(fut))
                }
            },
        }
    }

    /// Best-effort ORDERED queue for control commands (`close()`
    /// fire-and-forget, Close→Flush pairs). Control frames must never be
    /// wedged behind a full data queue — on Full a single waiter delivers
    /// the remaining commands sequentially once capacity frees (order
    /// between them preserved). Flush acks whose command can never be
    /// delivered (writer gone) fire immediately so a loop-mode caller
    /// awaiting the flush future can't hang.
    fn queue_seq(&self, cmds: Vec<WriterCmd>) {
        // Count every Send BEFORE pushing so the direct path can never
        // overtake a queued Close. (Cmds that die with the channel leave
        // the counter high — the connection is gone, direct sends stay
        // disabled and surface the channel's error instead.)
        let n_sends = cmds
            .iter()
            .filter(|c| matches!(c, WriterCmd::Send(_)))
            .count();
        if n_sends > 0 {
            self.queued.fetch_add(n_sends, Ordering::AcqRel);
        }
        match &self.tx {
            WsCmdTx::Unbounded(tx) => {
                for c in cmds {
                    if let Err(mpsc::error::SendError(WriterCmd::Flush(ack))) = tx.send(c) {
                        ack.fire();
                    }
                }
            }
            WsCmdTx::Bounded(tx) => {
                let mut iter = cmds.into_iter();
                while let Some(c) = iter.next() {
                    match tx.try_send(c) {
                        Ok(()) => {}
                        Err(mpsc::error::TrySendError::Closed(cmd)) => {
                            // Writer gone — nothing later can be delivered either.
                            for cmd in std::iter::once(cmd).chain(iter) {
                                if let WriterCmd::Flush(ack) = cmd {
                                    ack.fire();
                                }
                            }
                            return;
                        }
                        Err(mpsc::error::TrySendError::Full(cmd)) => {
                            let rest: Vec<WriterCmd> = std::iter::once(cmd).chain(iter).collect();
                            let Some(rt) = self.rt.as_ref() else {
                                for cmd in rest {
                                    if let WriterCmd::Flush(ack) = cmd {
                                        ack.fire();
                                    }
                                }
                                return;
                            };
                            let tx2 = tx.clone();
                            rt.spawn(async move {
                                for c in rest {
                                    if let Err(mpsc::error::SendError(WriterCmd::Flush(ack))) =
                                        tx2.send(c).await
                                    {
                                        ack.fire();
                                    }
                                }
                            });
                            return;
                        }
                    }
                }
            }
        }
    }
}

#[pymethods]
impl PyWebSocket {
    /// Accept the WebSocket upgrade with optional subprotocol + custom response headers.
    ///
    /// Deferred-upgrade architecture: sends AcceptParams to the route handler
    /// via oneshot, then waits until the handler has finished upgrading and
    /// wired up the reader/writer tasks. Thread mode blocks (GIL released) on
    /// ready_rx; loop mode returns an asyncio Future for the caller to await
    /// (resolved by the on_upgrade callback / its drop guard) — the shared
    /// loop must never block.
    ///
    /// Safe to call twice — second call is a no-op (returns None).
    #[pyo3(signature = (subprotocol=None, headers=None))]
    fn accept(
        &self,
        py: Python<'_>,
        subprotocol: Option<String>,
        headers: Option<Vec<(String, String)>>,
    ) -> PyResult<Option<Py<PyAny>>> {
        // Take the accept_tx (one-shot — only fires on the first accept call)
        let accept_tx = {
            let mut guard = self.accept_tx.lock().unwrap();
            guard.take()
        };

        if let Some(tx) = accept_tx {
            let params = AcceptParams {
                subprotocol,
                headers: headers.unwrap_or_default(),
            };
            if self.loop_mode {
                // Stash the ready future BEFORE firing the oneshot — the
                // route handler only reaches on_upgrade after the recv, so
                // the resolver can never miss it.
                let fut = create_loop_future(py)?;
                *self.ready_fut.lock().unwrap() = Some(fut.clone_ref(py));
                if tx.send(AcceptAction::Accept(params)).is_err() {
                    // Route handler gone (timeout/abort) — nobody will
                    // resolve; complete now so the caller can't hang. We are
                    // ON the loop thread, so set_result directly is safe.
                    self.ready_fut.lock().unwrap().take();
                    let _ = fut.call_method1(py, "set_result", (py.None(),));
                }
                self.state.store(STATE_CONNECTED, Ordering::Release);
                return Ok(Some(fut));
            }
            // send() is infallible unless the receiver was dropped — treat as
            // already-accepted or route-handler-gone.
            let _ = tx.send(AcceptAction::Accept(params));

            // Block until the route handler signals the upgrade is complete.
            let rx = self.ready_rx.clone();
            py.detach(|| {
                // recv returns Err if the sender was dropped (connection aborted);
                // in that case we just fall through — subsequent send/receive
                // will fail, which is the right error surface.
                let _ = rx.recv();
            });
        }
        // Either we just accepted, or it was already accepted earlier.
        self.state.store(STATE_CONNECTED, Ordering::Release);
        Ok(None)
    }

    /// Reject the handshake before upgrade. Starlette normative path
    /// for pre-accept ``WebSocketException``: the HTTP upgrade response
    /// becomes a plain ``<status> Forbidden`` (status defaults to 403)
    /// and no WebSocket frame ever travels. Safe to call at most once
    /// and before ``accept()`` — later calls no-op because the oneshot
    /// has already fired.
    #[pyo3(signature = (status=403))]
    fn reject(&self, status: u16) -> PyResult<()> {
        let accept_tx = {
            let mut guard = self.accept_tx.lock().unwrap();
            guard.take()
        };
        if let Some(tx) = accept_tx {
            let _ = tx.send(AcceptAction::Reject { status });
        }
        // Mark the WS as disconnected so any lingering user code that
        // tries to send/receive gets an immediate error rather than
        // hanging on the channel.
        self.state.store(STATE_DISCONNECTED, Ordering::Release);
        Ok(())
    }

    /// Return the current application (server-side) state as a u8.
    /// Maps to Python's WebSocketState enum.
    fn get_application_state(&self) -> u8 {
        self.state.load(Ordering::Acquire)
    }

    /// Return the current client-side state as a u8.
    /// We track a single state for both — they diverge only during close-send
    /// in full ASGI spec, which we don't yet implement separately.
    fn get_client_state(&self) -> u8 {
        self.state.load(Ordering::Acquire)
    }

    /// Queue a text frame. Returns None when queued (caller awaits its
    /// pre-allocated immediate awaitable) or, in loop mode under
    /// backpressure, an asyncio Future the caller must await (resolves when
    /// the frame has been handed to the outbound queue).
    fn send_text(&self, py: Python<'_>, data: String) -> PyResult<Option<Py<PyAny>>> {
        let len = data.len();
        self.queue_send(py, Message::Text(data.into()), len)
    }

    /// Accept `bytes` directly (no Vec<u8> intermediate).
    fn send_bytes(&self, py: Python<'_>, data: &Bound<'_, PyBytes>) -> PyResult<Option<Py<PyAny>>> {
        let slice = data.as_bytes();
        let owned = Bytes::copy_from_slice(slice);
        let len = owned.len();
        self.queue_send(py, Message::Binary(owned), len)
    }

    /// Build the ASGI-style scope dict on demand. Called by Python properties
    /// like ws.headers, ws.url, ws.client, ws.query_params.
    fn get_scope_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket"))?;
        dict.set_item(pyo3::intern!(py, "scheme"), self.scope_info.scheme.as_str())?;
        dict.set_item(pyo3::intern!(py, "path"), self.scope_info.path.as_str())?;
        dict.set_item(
            pyo3::intern!(py, "raw_path"),
            PyBytes::new(py, &self.scope_info.raw_path),
        )?;
        dict.set_item(
            pyo3::intern!(py, "query_string"),
            PyBytes::new(py, &self.scope_info.query_string),
        )?;
        dict.set_item(pyo3::intern!(py, "http_version"), "1.1")?;

        // Headers as a list of (bytes, bytes) tuples — matches ASGI spec.
        let headers_list = PyList::empty(py);
        for (k, v) in &self.scope_info.headers {
            let tup = (
                PyBytes::new(py, k.as_bytes()),
                PyBytes::new(py, v.as_bytes()),
            );
            headers_list.append(tup)?;
        }
        dict.set_item(pyo3::intern!(py, "headers"), headers_list)?;

        // Client address
        if let Some((host, port)) = &self.scope_info.client {
            dict.set_item(pyo3::intern!(py, "client"), (host.as_str(), *port))?;
        } else {
            dict.set_item(pyo3::intern!(py, "client"), py.None())?;
        }
        dict.set_item(
            pyo3::intern!(py, "server"),
            (self.scope_info.host.as_str(), 0u16),
        )?;

        // Path params (matched from Axum routing)
        let params_dict = PyDict::new(py);
        for (k, v) in &self.scope_info.path_params {
            params_dict.set_item(k.as_str(), v.as_str())?;
        }
        dict.set_item(pyo3::intern!(py, "path_params"), params_dict)?;

        // ASGI-standard client-offered subprotocols — required by apps
        // that negotiate via ``scope["subprotocols"]`` before calling
        // ``ws.accept(subprotocol=...)``.
        let sp_list = pyo3::types::PyList::empty(py);
        for s in &self.scope_info.subprotocols {
            sp_list.append(s.as_str())?;
        }
        dict.set_item(pyo3::intern!(py, "subprotocols"), sp_list)?;

        // ASGI type marker — some third-party auth/CSRF middlewares
        // short-circuit on `scope["type"] == "websocket"`.
        dict.set_item(pyo3::intern!(py, "type"), "websocket")?;

        Ok(dict)
    }

    // ── Async receive — returns cached awaitable per return-type ───

    /// Returns the ASGI dict awaitable (for `await ws.receive()`).
    fn receive_async(&self, py: Python<'_>) -> Py<RecvAwaitable> {
        self.get_dict_awaitable(py)
    }

    /// Returns a TEXT-specific awaitable (for `await ws.receive_text()`).
    /// Fast path: no dict allocation, direct str return.
    fn receive_text_async(&self, py: Python<'_>) -> Py<RecvAwaitable> {
        self.get_text_awaitable(py)
    }

    /// Returns a BYTES-specific awaitable (for `await ws.receive_bytes()`).
    /// Fast path: no dict allocation, direct bytes return.
    fn receive_bytes_async(&self, py: Python<'_>) -> Py<RecvAwaitable> {
        self.get_bytes_awaitable(py)
    }

    /// Queue a Close frame with optional code + reason.
    /// Does NOT await flush — the frame may still be in the tokio mpsc when this returns.
    /// Use close_and_wait() for true "close + flushed" semantics.
    #[pyo3(signature = (code=None, reason=None))]
    fn close(&self, code: Option<u16>, reason: Option<String>) -> PyResult<()> {
        self.queue_seq(vec![WriterCmd::Send(Message::Close(Some(
            axum::extract::ws::CloseFrame {
                code: code.unwrap_or(1000),
                reason: reason.unwrap_or_default().into(),
            },
        )))]);
        self.state.store(STATE_DISCONNECTED, Ordering::Release);
        Ok(())
    }

    /// Returns a Python awaitable that resolves when the writer has flushed the
    /// Close frame to the underlying WS sink. The caller should already have
    /// called close() (or this method auto-sends one). Thread mode returns the
    /// blocking `CloseAwaitable`; loop mode an asyncio Future (both awaitable).
    #[pyo3(signature = (code=None, reason=None))]
    fn close_and_wait(
        &self,
        py: Python<'_>,
        code: Option<u16>,
        reason: Option<String>,
    ) -> PyResult<Py<PyAny>> {
        let close_cmd = WriterCmd::Send(Message::Close(Some(axum::extract::ws::CloseFrame {
            code: code.unwrap_or(1000),
            reason: reason.unwrap_or_default().into(),
        })));
        self.state.store(STATE_DISCONNECTED, Ordering::Release);
        if self.loop_mode {
            let fut = create_loop_future(py)?;
            // Close then Flush through the SAME ordered path — the writer
            // drains all prior Sends before acking the flush.
            self.queue_seq(vec![
                close_cmd,
                WriterCmd::Flush(FlushAck::Loop(fut.clone_ref(py))),
            ]);
            return Ok(fut);
        }
        let (tx, rx) = cb::bounded::<()>(1);
        self.queue_seq(vec![close_cmd, WriterCmd::Flush(FlushAck::Thread(tx))]);
        Ok(Py::new(py, CloseAwaitable { rx })?.into_any())
    }
}

// ── CloseAwaitable: await for close flush ──────────────────────────

/// Custom awaitable that blocks on a crossbeam channel (GIL released).
/// Returned by close_and_wait().
#[pyclass]
pub struct CloseAwaitable {
    rx: cb::Receiver<()>,
}

#[pymethods]
impl CloseAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rx = self.rx.clone();
        py.detach(|| {
            // Block until the writer signals flush (or errors out — in which case
            // we silently succeed; connection is already closed).
            let _ = rx.recv();
        });
        Err(pyo3::exceptions::PyStopIteration::new_err(py.None()))
    }
}

// ── Python handler bridge ─────────────────────────────────────────

/// Entry point for a WebSocket route. Uses deferred-upgrade architecture:
///   1. Build PyWebSocket with pre-created channels + accept oneshot
///   2. Spawn Python handler
///   3. Wait for Python to call accept(subprotocol=..., headers=...)
///   4. Upgrade the WS with the chosen subprotocol
///   5. In the upgrade callback, spawn reader/writer tasks wired to the
///      pre-created channels, then signal ready
///   6. Python's accept() returns, normal send/receive flows
///
/// Returns the axum Response (101 Switching Protocols + upgrade callback).
pub async fn handle_ws_upgrade(
    ws: axum::extract::WebSocketUpgrade,
    handler: Py<PyAny>,
    is_async: bool,
    scope_info: WsScopeInfo,
) -> axum::response::Response {
    use axum::response::IntoResponse;

    // Loop-residency decision, per connection: flag on + async handler +
    // worker loop reachable + runtime context for capacity waiters. Sync
    // handlers (is_async=false) keep the dedicated thread unconditionally.
    let loop_mode = is_async && ws_loop_enabled() && {
        handler_bridge::init_async_worker();
        handler_bridge::worker_call_soon().is_some()
            && tokio::runtime::Handle::try_current().is_ok()
    };

    // Pre-create all channels. They work immediately — messages start flowing
    // once the reader/writer tasks spawn in the on_upgrade callback.
    let (tx_out, rx_out) = if loop_mode {
        let (tx, rx) = mpsc::channel::<WriterCmd>(WS_OUT_CAP);
        (WsCmdTx::Bounded(tx), WsCmdRx::Bounded(rx))
    } else {
        let (tx, rx) = mpsc::unbounded_channel::<WriterCmd>();
        (WsCmdTx::Unbounded(tx), WsCmdRx::Unbounded(rx))
    };
    let (cb_tx, cb_rx) = cb::unbounded::<WsMessage>();
    let (accept_tx, accept_rx) = tokio::sync::oneshot::channel::<AcceptAction>();
    let (ready_tx, ready_rx) = cb::bounded::<()>(1);
    let state = Arc::new(AtomicU8::new(STATE_CONNECTING));
    let wake = Arc::new(LoopWake::new());
    let ready_fut: Arc<Mutex<Option<Py<PyAny>>>> = Arc::new(Mutex::new(None));
    let sink: WsSink = Arc::new(tokio::sync::Mutex::new(None));
    let queued = Arc::new(AtomicUsize::new(0));

    // Capture path params before scope_info is moved, for passing as kwargs to the Python handler.
    let ws_path_params: Vec<(String, String)> = scope_info.path_params.clone();

    let py_ws = PyWebSocket {
        tx: tx_out,
        rx: cb_rx,
        state: state.clone(),
        loop_mode,
        // Both modes: loop-mode capacity waiters + direct-send flush
        // drivers spawn onto the request runtime.
        rt: tokio::runtime::Handle::try_current().ok(),
        sink: sink.clone(),
        queued: queued.clone(),
        unflushed: Arc::new(AtomicUsize::new(0)),
        flush_armed: Arc::new(AtomicBool::new(false)),
        wake: wake.clone(),
        ready_fut: ready_fut.clone(),
        recv_buf: Arc::new(std::sync::Mutex::new(std::collections::VecDeque::new())),
        cached_dict: std::sync::OnceLock::new(),
        cached_text: std::sync::OnceLock::new(),
        cached_bytes: std::sync::OnceLock::new(),
        scope_info: Arc::new(scope_info),
        accept_tx: Arc::new(std::sync::Mutex::new(Some(accept_tx))),
        ready_rx,
    };

    // Loop-mode accept-ready resolution guard. Created BEFORE any return
    // path exists so that Drop fires it on every exit (reject / timeout /
    // upgrade-never-polled) — a handler task awaiting accept() can never be
    // stranded on the shared loop. No-op while the slot is empty.
    let ready_guard = ReadyGuard {
        slot: ready_fut.clone(),
    };

    // Spawn the Python handler in a background task. It will create the Python
    // WebSocket wrapper, call accept(), and interact with the socket.
    let ws_obj = Python::attach(|py| {
        let ws_cell = Py::new(py, py_ws).expect("PyWebSocket");
        let ws_mod = py.import("fastapi_turbo.websockets").expect("websockets");
        let ws_cls = ws_mod.getattr("WebSocket").expect("WebSocket");
        ws_cls.call1((ws_cell,)).expect("wrap").unbind()
    });

    if loop_mode {
        // LOOP mode: run the handler coroutine as a TASK on the shared
        // worker loop — many sockets per loop, no dedicated thread. The
        // coroutine object is built here (creation doesn't execute the
        // body); `WsLoopJob.__call__` does `create_task` on the loop
        // thread. A fresh empty contextvars.Context matches the dedicated
        // thread's ambient context (the TestClient contextvars replay
        // happens INSIDE `_ws_entry`, unchanged). If the enqueue fails
        // (loop torn down mid-flight), dropping `ws_obj` drops the accept
        // oneshot and the 500 "handler did not accept" path below fires.
        let enq: PyResult<()> = Python::attach(|py| {
            let coro = if ws_path_params.is_empty() {
                handler.call1(py, (ws_obj.bind(py),))?
            } else {
                let kwargs = PyDict::new(py);
                for (k, v) in &ws_path_params {
                    kwargs.set_item(k.as_str(), v.as_str())?;
                }
                handler.call(py, (ws_obj.bind(py),), Some(&kwargs))?
            };
            let call_soon = handler_bridge::worker_call_soon().ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("worker loop not initialized")
            })?;
            let job = Py::new(py, WsLoopJob { coro: Some(coro) })?;
            let ctx = py.import("contextvars")?.getattr("Context")?.call0()?;
            let kwargs = PyDict::new(py);
            kwargs.set_item(pyo3::intern!(py, "context"), ctx)?;
            call_soon.call(py, (job.bind(py),), Some(&kwargs))?;
            Ok(())
        });
        // Drop our ws ref NOW: the enqueued coroutine holds its own. On an
        // enqueue failure this is the LAST ref — dropping it drops the accept
        // oneshot, so the 500 "handler did not accept" path below fires
        // immediately instead of waiting out the 30s timeout.
        drop(ws_obj);
        if let Err(e) = enq {
            eprintln!("fastapi-turbo: WS loop-mode enqueue failed: {e}");
        }
    } else {
        // THREAD mode (legacy): run the Python WS handler on a DEDICATED
        // blocking thread via `spawn_blocking`. This ensures the handler,
        // its thread-local event loop, and the WS I/O select task stay on
        // a stable thread boundary (the I/O task runs on the tokio worker,
        // the handler runs in its own thread).
        //
        // The WS object is passed POSITIONALLY so user-defined parameter
        // names work regardless of what they call it (`ws`, `websocket`,
        // `conn`, ...). vLLM uses `websocket`; SGLang would use whatever.
        // Path params (e.g., /ws/{room_id}) are passed as keyword arguments
        // so handlers can declare them as additional parameters.
        tokio::task::spawn_blocking(move || {
            Python::attach(|py| {
                if ws_path_params.is_empty() {
                    // No path params — call with just the WS object positionally
                    if is_async {
                        let _ = handler_bridge::call_async_on_local_loop_positional(
                            py, &handler, ws_obj,
                        );
                    } else {
                        let _ = handler.call1(py, (ws_obj.bind(py),));
                    }
                } else {
                    // Has path params — pass them as kwargs
                    let kwargs = PyDict::new(py);
                    for (k, v) in &ws_path_params {
                        let _ = kwargs.set_item(k.as_str(), v.as_str());
                    }
                    if is_async {
                        let _ = handler_bridge::call_async_on_local_loop_positional_with_kwargs(
                            py, &handler, ws_obj, &kwargs,
                        );
                    } else {
                        let _ = handler.call(py, (ws_obj.bind(py),), Some(&kwargs));
                    }
                }
            });
        });
    }

    // Wait for the Python handler to call accept() OR reject(). Bound
    // with a timeout so a handler that never resolves doesn't hang a
    // client connection.
    let params = match tokio::time::timeout(std::time::Duration::from_secs(30), accept_rx).await {
        Ok(Ok(AcceptAction::Accept(p))) => p,
        Ok(Ok(AcceptAction::Reject { status })) => {
            // Starlette semantics: pre-accept ``WebSocketException``
            // aborts the handshake with an HTTP status body; no WS
            // frame is sent, client sees a normal HTTP error response.
            let sc = axum::http::StatusCode::from_u16(status)
                .unwrap_or(axum::http::StatusCode::FORBIDDEN);
            return (sc, sc.canonical_reason().unwrap_or("Forbidden")).into_response();
        }
        Ok(Err(_)) | Err(_) => {
            // oneshot dropped (handler exited without accept) OR timeout.
            return (
                axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                "WebSocket handler did not accept",
            )
                .into_response();
        }
    };

    // Apply subprotocol negotiation (axum picks it up via the Sec-WebSocket-Protocol
    // response header when building the 101 response).
    let mut upgrade = ws;
    if let Some(ref proto) = params.subprotocol {
        upgrade = upgrade.protocols([proto.clone()]);
    }
    let extra_headers = params.headers.clone();

    // Now perform the upgrade. on_upgrade returns a Response (101 Switching Protocols);
    // the closure runs AFTER hyper completes the TCP upgrade.
    let state_clone = state.clone();
    let wake_r = wake.clone();
    let mut response = upgrade.on_upgrade(move |socket| async move {
        use futures_util::{SinkExt, StreamExt};
        let (ws_tx, mut ws_rx) = socket.split();
        let state_r = state_clone;
        let mut rx_out = rx_out;
        let ready_guard = ready_guard;
        let sink_w = sink;
        let queued_w = queued;

        // Publish the write half FIRST: once accept() returns, sends may
        // take the direct path, which needs the sink in its slot.
        *sink_w.lock().await = Some(ws_tx);

        // Signal Python that the WS is ready BEFORE entering the loop —
        // Python's accept() unblocks and may start sending immediately.
        // Thread mode: crossbeam signal. Loop mode: resolve the accept
        // future (the guard also fires on Drop, so an upgrade that dies
        // before this point can never strand the handler task).
        let _ = ready_tx.send(());
        ready_guard.fire();

        // SINGLE task handles both read and write via tokio::select!.
        // Avoids the inter-task wake-up penalty that cost ~5-8 μs per
        // message round-trip when reader and writer were separate tasks.
        tokio::spawn(async move {
            loop {
                tokio::select! {
                    biased; // Favour writes (queued by Python) over reads to
                            // minimise perceived echo-loop latency.
                    maybe_cmd = rx_out.recv() => {
                        match maybe_cmd {
                            Some(WriterCmd::Send(msg)) => {
                                // Detect a server-initiated close so we
                                // can do a graceful WS handshake termination:
                                // send the Close frame, then DRAIN the
                                // read side until the client echoes Close
                                // (or a short timeout fires) before
                                // closing the sink. RFC 6455 §5.5.1 +
                                // §7.1.4 — the side that sends the
                                // first Close should drain pending
                                // incoming frames so the peer's TCP
                                // close lands cleanly. Without the drain,
                                // closing the sink immediately makes the
                                // client's reader see a TCP RST after
                                // the Close, which surfaces as
                                // ``ConnectionResetError`` in test debug
                                // logs even on orderly shutdowns
                                // (R29 saw this; R30 fixes the noise).
                                let is_close = matches!(msg, Message::Close(_));
                                let sent_ok = {
                                    let mut g = sink_w.lock().await;
                                    match g.as_mut() {
                                        Some(s) => s.send(msg).await.is_ok(),
                                        None => false,
                                    }
                                };
                                // Decrement AFTER the write completed (and
                                // after the lock dropped) — the direct path
                                // treats 0-under-lock as "nothing ahead".
                                queued_w.fetch_sub(1, Ordering::AcqRel);
                                if !sent_ok { break; }
                                if is_close {
                                    state_r.store(STATE_DISCONNECTED, Ordering::Release);
                                    // Drain incoming frames until the
                                    // client's close echo arrives or a
                                    // 200ms guard fires. The client side
                                    // (``websockets`` library) sees the
                                    // server's Close and replies in the
                                    // same trip, so 200ms is comfortably
                                    // above the client's reply latency
                                    // without making test teardowns
                                    // sluggish.
                                    let drain_deadline = tokio::time::Instant::now()
                                        + std::time::Duration::from_millis(200);
                                    loop {
                                        let remaining = drain_deadline
                                            .saturating_duration_since(tokio::time::Instant::now());
                                        if remaining.is_zero() {
                                            break;
                                        }
                                        match tokio::time::timeout(remaining, ws_rx.next()).await {
                                            Err(_) => break, // timeout
                                            Ok(None) => break, // peer dropped
                                            Ok(Some(Err(_))) => break, // read error
                                            Ok(Some(Ok(Message::Close(_)))) => break, // got ack
                                            Ok(Some(Ok(_))) => continue, // drop other frames
                                        }
                                    }
                                    if let Some(s) = sink_w.lock().await.as_mut() {
                                        let _ = s.close().await;
                                    }
                                    break;
                                }
                            }
                            Some(WriterCmd::Flush(ack)) => {
                                ack.fire();
                            }
                            None => break,  // channel closed
                        }
                    }
                    maybe_result = ws_rx.next() => {
                        let result = match maybe_result {
                            Some(r) => r,
                            None => break,
                        };
                        let msg = match result {
                            Ok(m) => m,
                            Err(_) => break,
                        };
                        let ws_msg = match msg {
                            Message::Text(t) => WsMessage::Text(t.to_string()),
                            Message::Binary(b) => WsMessage::Binary(b),
                            Message::Close(frame) => {
                                let (code, reason) = frame
                                    .map(|f| (f.code, f.reason.to_string()))
                                    .unwrap_or((1000u16, String::new()));
                                let echo_reason = reason.clone();
                                let _ = cb_tx.send(WsMessage::Close { code, reason });
                                // Loop mode: wake a handler task parked on
                                // receive so it observes the disconnect
                                // while we echo the close below.
                                wake_r.fire();
                                state_r.store(STATE_DISCONNECTED, Ordering::Release);
                                // Client initiated close — echo a Close
                                // frame back so the client's
                                // ``websockets`` library treats this as
                                // ``ConnectionClosedOK`` instead of an
                                // abnormal shutdown, then close the sink.
                                {
                                    let mut g = sink_w.lock().await;
                                    if let Some(s) = g.as_mut() {
                                        let _ = s
                                            .send(Message::Close(Some(
                                                axum::extract::ws::CloseFrame {
                                                    code,
                                                    reason: echo_reason.into(),
                                                },
                                            )))
                                            .await;
                                        let _ = s.close().await;
                                    }
                                }
                                break;
                            }
                            _ => continue, // Ping/Pong handled by axum
                        };
                        if cb_tx.send(ws_msg).is_err() { break; }
                        // Loop mode: one attach + call_soon_threadsafe per
                        // ARMED wake only — a busy handler (or one still
                        // draining the batch buffer) leaves `armed` false
                        // and this is a single atomic swap, GIL-free.
                        wake_r.fire();
                    }
                }
            }
            // Connection teardown — order matters for loop mode:
            //   1. Drop the inbound sender so a woken receiver's try_recv
            //      observes Disconnected (thread mode: recv Err), exactly
            //      like the legacy channel-drop semantics.
            //   2. Fire any armed receive-wake so a handler task parked on
            //      a receive future can't be stranded.
            //   3. Ack any still-queued loop-mode flushes so close_and_wait
            //      callers can't hang (thread mode resolved this by the
            //      crossbeam sender drop unblocking CloseAwaitable).
            //   4. Take the write half OUT of the shared slot — PyWebSocket
            //      may outlive the connection (held by Python), and the TCP
            //      socket must close NOW, not at Python GC time. Direct
            //      sends see the empty slot and fall back to the closed
            //      channel, which raises exactly like the legacy path.
            drop(cb_tx);
            wake_r.fire();
            rx_out.close();
            while let Some(cmd) = rx_out.drain_next() {
                if let WriterCmd::Flush(ack) = cmd {
                    ack.fire();
                }
            }
            sink_w.lock().await.take();
        });
    });

    // Inject custom headers from `accept(headers=...)` into the 101 response.
    // This lets WS handlers set headers like Set-Cookie during the handshake,
    // matching Starlette's accept(headers=...) API.
    {
        let hdrs = response.headers_mut();
        for (name, value) in extra_headers {
            if let (Ok(hn), Ok(hv)) = (
                axum::http::HeaderName::from_bytes(name.as_bytes()),
                axum::http::HeaderValue::from_str(&value),
            ) {
                if hn.as_str().eq_ignore_ascii_case("set-cookie") {
                    hdrs.append(hn, hv);
                } else {
                    hdrs.insert(hn, hv);
                }
            }
        }
    }

    response
}
