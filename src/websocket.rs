//! WebSocket bridge — Rust message loop, Python handler invocation.
//!
//! Architecture:
//!   - Axum WebSocket → Rust tokio task reads messages, converts to typed enum
//!   - Typed messages flow via crossbeam channel to Python
//!   - Python receives via ChannelAwaitable (custom awaitable with GIL release)
//!   - State tracked via atomic u8 (matches Starlette's WebSocketState enum)
//!   - Binary preserved as `Bytes` (no UTF-8 coercion)

use axum::extract::ws::{Message, WebSocket};
use bytes::Bytes;
use crossbeam_channel as cb;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, OnceLock};
use tokio::sync::mpsc;

/// Cached `fastapi_rs.exceptions.WebSocketDisconnect` class — used by the
/// receive awaitables to raise the correct typed exception without the
/// Python-side `try/except` translation layer.
static WS_DISCONNECT_CLS: OnceLock<Py<PyAny>> = OnceLock::new();

fn ws_disconnect_class(py: Python<'_>) -> Option<&'static Py<PyAny>> {
    if let Some(c) = WS_DISCONNECT_CLS.get() {
        return Some(c);
    }
    let exc = py.import("fastapi_rs.exceptions").ok()?;
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
                Err(_) => pyo3::exceptions::PyRuntimeError::new_err(
                    format!("WS_CLOSED:{code}:{reason}")
                ),
            }
        }
        None => pyo3::exceptions::PyRuntimeError::new_err(
            format!("WS_CLOSED:{code}:{reason}")
        ),
    }
}

use crate::handler_bridge;

/// Metadata about the inbound WebSocket upgrade request — populated by the
/// Rust route handler before the upgrade, used to build the Python scope dict.
#[derive(Default)]
pub struct WsScopeInfo {
    pub path: String,
    pub raw_path: Vec<u8>,
    pub query_string: Vec<u8>,
    pub headers: Vec<(String, String)>,     // all headers, lowercased
    pub client: Option<(String, u16)>,
    pub scheme: String,                     // "ws" or "wss"
    pub host: String,
    pub path_params: Vec<(String, String)>, // matched route path params
    /// Client-offered subprotocols (from the ``Sec-WebSocket-Protocol``
    /// request header, split on commas and trimmed). Starlette surfaces
    /// this in ``scope["subprotocols"]`` so apps can call
    /// ``ws.accept(subprotocol=...)`` with one of the offered values.
    pub subprotocols: Vec<String>,
}

// ── WebSocket state (matches Starlette's WebSocketState) ───────────
// These values MUST match python/fastapi_rs/websockets.py::WebSocketState.

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

// ── Awaitables — custom Python awaitables backed by crossbeam ──────
//
// Three flavors, each tight in its hot path:
//   - ChannelAwaitable  : returns ASGI dict (for receive())
//   - TextAwaitable     : returns str directly (for receive_text())
//   - BytesAwaitable    : returns bytes directly (for receive_bytes())
//
// All three share the SAME underlying channel receiver — each await consumes
// one message. Cached per-PyWebSocket so Python's await doesn't allocate
// a new pyclass per call.

fn handle_close_err(state: &Arc<AtomicU8>) -> PyErr {
    state.store(STATE_DISCONNECTED, Ordering::Release);
    pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed")
}

#[pyclass]
pub struct ChannelAwaitable {
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
}

#[pymethods]
impl ChannelAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    /// Returns ASGI-style dict: {"type": "websocket.receive", "text"|"bytes": ...}
    /// or {"type": "websocket.disconnect", "code": ..., "reason": ...}.
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| rx.recv().map_err(|_| handle_close_err(&state)))?;

        let dict = PyDict::new(py);
        match msg {
            WsMessage::Text(t) => {
                dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket.receive"))?;
                dict.set_item(pyo3::intern!(py, "text"), t)?;
            }
            WsMessage::Binary(b) => {
                dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket.receive"))?;
                dict.set_item(pyo3::intern!(py, "bytes"), PyBytes::new(py, &b))?;
            }
            WsMessage::Close { code, reason } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket.disconnect"))?;
                dict.set_item(pyo3::intern!(py, "code"), code)?;
                dict.set_item(pyo3::intern!(py, "reason"), reason)?;
            }
        }
        Err(pyo3::exceptions::PyStopIteration::new_err(dict.unbind()))
    }
}

#[pyclass]
pub struct TextAwaitable {
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
}

#[pymethods]
impl TextAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    /// Returns str directly — fast path for receive_text().
    /// On Close, raises a proper `WebSocketDisconnect` with code + reason
    /// so the Python handler can catch it with one `except` clause — no
    /// Python-side exception translation wrapper needed.
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| rx.recv().map_err(|_| handle_close_err(&state)))?;
        let value: Py<PyAny> = match msg {
            WsMessage::Text(t) => t.into_pyobject(py)?.into_any().unbind(),
            WsMessage::Binary(b) => {
                String::from_utf8(b.to_vec())
                    .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err(
                        "Received binary message when expecting text"
                    ))?
                    .into_pyobject(py)?.into_any().unbind()
            }
            WsMessage::Close { code, reason } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                return Err(disconnect_err(py, code, &reason));
            }
        };
        Err(pyo3::exceptions::PyStopIteration::new_err(value))
    }
}

#[pyclass]
pub struct BytesAwaitable {
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
}

#[pymethods]
impl BytesAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

    /// Returns bytes directly — fast path for receive_bytes().
    /// On Close, raises `WebSocketDisconnect` directly.
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| rx.recv().map_err(|_| handle_close_err(&state)))?;
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

// ── PyWebSocket: the Rust-side WS handle exposed to Python ─────────

/// Command sent to the WS writer task.
/// `Flush` causes the writer to signal the crossbeam sender — used by `close()` to truly await.
pub enum WriterCmd {
    Send(Message),
    Flush(cb::Sender<()>),
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
    tx: mpsc::UnboundedSender<WriterCmd>,
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
    // Three cached awaitables — one per return-type (dict / text / bytes).
    // Each is lazily created on first use. Safe because a single WS has one reader.
    cached_dict: std::sync::OnceLock<Py<ChannelAwaitable>>,
    cached_text: std::sync::OnceLock<Py<TextAwaitable>>,
    cached_bytes: std::sync::OnceLock<Py<BytesAwaitable>>,
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
    fn get_dict_awaitable(&self, py: Python<'_>) -> Py<ChannelAwaitable> {
        self.cached_dict
            .get_or_init(|| {
                Py::new(py, ChannelAwaitable {
                    rx: self.rx.clone(),
                    state: self.state.clone(),
                }).expect("create dict awaitable")
            })
            .clone_ref(py)
    }

    fn get_text_awaitable(&self, py: Python<'_>) -> Py<TextAwaitable> {
        self.cached_text
            .get_or_init(|| {
                Py::new(py, TextAwaitable {
                    rx: self.rx.clone(),
                    state: self.state.clone(),
                }).expect("create text awaitable")
            })
            .clone_ref(py)
    }

    fn get_bytes_awaitable(&self, py: Python<'_>) -> Py<BytesAwaitable> {
        self.cached_bytes
            .get_or_init(|| {
                Py::new(py, BytesAwaitable {
                    rx: self.rx.clone(),
                    state: self.state.clone(),
                }).expect("create bytes awaitable")
            })
            .clone_ref(py)
    }
}

#[pymethods]
impl PyWebSocket {
    /// Accept the WebSocket upgrade with optional subprotocol + custom response headers.
    ///
    /// Deferred-upgrade architecture: sends AcceptParams to the route handler
    /// via oneshot, then blocks (GIL released) on ready_rx until the handler has
    /// finished upgrading and wired up the reader/writer tasks.
    ///
    /// Safe to call twice — second call is a no-op.
    #[pyo3(signature = (subprotocol=None, headers=None))]
    fn accept(
        &self,
        py: Python<'_>,
        subprotocol: Option<String>,
        headers: Option<Vec<(String, String)>>,
    ) -> PyResult<()> {
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
        Ok(())
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

    fn send_text(&self, data: String) -> PyResult<()> {
        self.tx
            .send(WriterCmd::Send(Message::Text(data.into())))
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}")))
    }

    /// Accept `bytes` directly (no Vec<u8> intermediate).
    fn send_bytes(&self, data: &Bound<'_, PyBytes>) -> PyResult<()> {
        let slice = data.as_bytes();
        let owned = Bytes::copy_from_slice(slice);
        self.tx
            .send(WriterCmd::Send(Message::Binary(owned)))
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}")))
    }

    /// Build the ASGI-style scope dict on demand. Called by Python properties
    /// like ws.headers, ws.url, ws.client, ws.query_params.
    fn get_scope_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new(py);
        dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket"))?;
        dict.set_item(pyo3::intern!(py, "scheme"), self.scope_info.scheme.as_str())?;
        dict.set_item(pyo3::intern!(py, "path"), self.scope_info.path.as_str())?;
        dict.set_item(pyo3::intern!(py, "raw_path"), PyBytes::new(py, &self.scope_info.raw_path))?;
        dict.set_item(pyo3::intern!(py, "query_string"), PyBytes::new(py, &self.scope_info.query_string))?;
        dict.set_item(pyo3::intern!(py, "http_version"), "1.1")?;

        // Headers as a list of (bytes, bytes) tuples — matches ASGI spec.
        let headers_list = PyList::empty(py);
        for (k, v) in &self.scope_info.headers {
            let tup = (PyBytes::new(py, k.as_bytes()), PyBytes::new(py, v.as_bytes()));
            headers_list.append(tup)?;
        }
        dict.set_item(pyo3::intern!(py, "headers"), headers_list)?;

        // Client address
        if let Some((host, port)) = &self.scope_info.client {
            dict.set_item(pyo3::intern!(py, "client"), (host.as_str(), *port))?;
        } else {
            dict.set_item(pyo3::intern!(py, "client"), py.None())?;
        }
        dict.set_item(pyo3::intern!(py, "server"), (self.scope_info.host.as_str(), 0u16))?;

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

    // ── Sync receive (blocking with GIL released) ──────────────────

    /// Blocking receive for sync handlers. Returns only TEXT messages.
    /// If a binary message arrives, we decode as UTF-8 best-effort
    /// (matches the old behavior for backward compat).
    fn receive_text(&self, py: Python<'_>) -> PyResult<String> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| {
            rx.recv().map_err(|_| {
                state.store(STATE_DISCONNECTED, Ordering::Release);
                pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed")
            })
        })?;
        match msg {
            WsMessage::Text(s) => Ok(s),
            WsMessage::Binary(b) => {
                // Best-effort UTF-8 decode. Users expecting binary should call receive_bytes.
                String::from_utf8(b.to_vec()).map_err(|_| {
                    pyo3::exceptions::PyRuntimeError::new_err(
                        "Received binary message when expecting text",
                    )
                })
            }
            WsMessage::Close { .. } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                Err(pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed"))
            }
        }
    }

    /// Blocking receive for BYTES. Preserves binary without UTF-8 coercion.
    fn receive_bytes<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| {
            rx.recv().map_err(|_| {
                state.store(STATE_DISCONNECTED, Ordering::Release);
                pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed")
            })
        })?;
        match msg {
            WsMessage::Binary(b) => Ok(PyBytes::new(py, &b)),
            WsMessage::Text(s) => Ok(PyBytes::new(py, s.as_bytes())),
            WsMessage::Close { .. } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                Err(pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed"))
            }
        }
    }

    /// Blocking receive returning the raw ASGI dict (Starlette-compatible).
    fn receive_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| {
            rx.recv().map_err(|_| {
                state.store(STATE_DISCONNECTED, Ordering::Release);
                pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed")
            })
        })?;
        let dict = PyDict::new(py);
        match msg {
            WsMessage::Text(s) => {
                dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket.receive"))?;
                dict.set_item(pyo3::intern!(py, "text"), s)?;
            }
            WsMessage::Binary(b) => {
                dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket.receive"))?;
                dict.set_item(pyo3::intern!(py, "bytes"), PyBytes::new(py, &b))?;
            }
            WsMessage::Close { code, reason } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                dict.set_item(pyo3::intern!(py, "type"), pyo3::intern!(py, "websocket.disconnect"))?;
                dict.set_item(pyo3::intern!(py, "code"), code)?;
                dict.set_item(pyo3::intern!(py, "reason"), reason)?;
            }
        }
        Ok(dict)
    }

    // ── Async receive — returns cached awaitable per return-type ───

    /// Returns the ASGI dict awaitable (for `await ws.receive()`).
    fn receive_async(&self, py: Python<'_>) -> Py<ChannelAwaitable> {
        self.get_dict_awaitable(py)
    }

    /// Returns a TEXT-specific awaitable (for `await ws.receive_text()`).
    /// Fast path: no dict allocation, direct str return.
    fn receive_text_async(&self, py: Python<'_>) -> Py<TextAwaitable> {
        self.get_text_awaitable(py)
    }

    /// Returns a BYTES-specific awaitable (for `await ws.receive_bytes()`).
    /// Fast path: no dict allocation, direct bytes return.
    fn receive_bytes_async(&self, py: Python<'_>) -> Py<BytesAwaitable> {
        self.get_bytes_awaitable(py)
    }

    /// Queue a Close frame with optional code + reason.
    /// Does NOT await flush — the frame may still be in the tokio mpsc when this returns.
    /// Use close_and_wait() for true "close + flushed" semantics.
    #[pyo3(signature = (code=None, reason=None))]
    fn close(&self, code: Option<u16>, reason: Option<String>) -> PyResult<()> {
        let _ = self.tx.send(WriterCmd::Send(Message::Close(Some(
            axum::extract::ws::CloseFrame {
                code: code.unwrap_or(1000),
                reason: reason.unwrap_or_default().into(),
            },
        ))));
        self.state.store(STATE_DISCONNECTED, Ordering::Release);
        Ok(())
    }

    /// Returns a Python awaitable that resolves when the writer has flushed the
    /// Close frame to the underlying WS sink. The caller should already have
    /// called close() (or this method auto-sends one).
    #[pyo3(signature = (code=None, reason=None))]
    fn close_and_wait(&self, py: Python<'_>, code: Option<u16>, reason: Option<String>) -> PyResult<Py<CloseAwaitable>> {
        // Queue the close frame
        let _ = self.tx.send(WriterCmd::Send(Message::Close(Some(
            axum::extract::ws::CloseFrame {
                code: code.unwrap_or(1000),
                reason: reason.unwrap_or_default().into(),
            },
        ))));
        // Queue a flush signal — the writer drains all prior Sends before firing.
        let (tx, rx) = cb::bounded::<()>(1);
        let _ = self.tx.send(WriterCmd::Flush(tx));
        self.state.store(STATE_DISCONNECTED, Ordering::Release);

        Py::new(py, CloseAwaitable { rx })
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
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> { slf }

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

// ── Pure Rust WS echo — baseline measurement ──────────────────────

pub async fn handle_ws_echo_rust(socket: WebSocket) {
    use futures_util::{SinkExt, StreamExt};
    let (mut tx, mut rx) = socket.split();
    while let Some(Ok(msg)) = rx.next().await {
        match msg {
            Message::Text(_) | Message::Binary(_) => {
                if tx.send(msg).await.is_err() {
                    break;
                }
            }
            Message::Close(_) => break,
            _ => {}
        }
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

    // Pre-create all channels. They work immediately — messages start flowing
    // once the reader/writer tasks spawn in the on_upgrade callback.
    let (tx_out, rx_out) = mpsc::unbounded_channel::<WriterCmd>();
    let (cb_tx, cb_rx) = cb::unbounded::<WsMessage>();
    let (accept_tx, accept_rx) = tokio::sync::oneshot::channel::<AcceptAction>();
    let (ready_tx, ready_rx) = cb::bounded::<()>(1);
    let state = Arc::new(AtomicU8::new(STATE_CONNECTING));

    // Capture path params before scope_info is moved, for passing as kwargs to the Python handler.
    let ws_path_params: Vec<(String, String)> = scope_info.path_params.clone();

    let py_ws = PyWebSocket {
        tx: tx_out,
        rx: cb_rx,
        state: state.clone(),
        cached_dict: std::sync::OnceLock::new(),
        cached_text: std::sync::OnceLock::new(),
        cached_bytes: std::sync::OnceLock::new(),
        scope_info: Arc::new(scope_info),
        accept_tx: Arc::new(std::sync::Mutex::new(Some(accept_tx))),
        ready_rx,
    };

    // Spawn the Python handler in a background task. It will create the Python
    // WebSocket wrapper, call accept(), and interact with the socket.
    let ws_obj = Python::attach(|py| {
        let ws_cell = Py::new(py, py_ws).expect("PyWebSocket");
        let ws_mod = py.import("fastapi_rs.websockets").expect("websockets");
        let ws_cls = ws_mod.getattr("WebSocket").expect("WebSocket");
        ws_cls.call1((ws_cell,)).expect("wrap").unbind()
    });

    // Run the Python WS handler on a DEDICATED blocking thread via
    // `spawn_blocking`. This ensures the handler, its thread-local event
    // loop, and the WS I/O select task stay on a stable thread boundary
    // (the I/O task runs on the tokio worker, the handler runs in its own
    // thread). Previously we routed through the global event-loop thread
    // (`call_async_via_event_loop_pub`), which added a cross-thread wake
    // for every await — ~5-8 μs per message in tight echo loops.
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

    // Wait for the Python handler to call accept() OR reject(). Bound
    // with a timeout so a handler that never resolves doesn't hang a
    // client connection.
    let params = match tokio::time::timeout(
        std::time::Duration::from_secs(30),
        accept_rx,
    )
    .await
    {
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
    let mut response = upgrade.on_upgrade(move |socket| async move {
        use futures_util::{SinkExt, StreamExt};
        let (mut ws_tx, mut ws_rx) = socket.split();
        let state_r = state_clone;
        let mut rx_out = rx_out;

        // Signal Python that the WS is ready BEFORE entering the loop —
        // Python's accept() unblocks and may start sending immediately.
        let _ = ready_tx.send(());

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
                                if ws_tx.send(msg).await.is_err() { break; }
                            }
                            Some(WriterCmd::Flush(tx)) => {
                                let _ = tx.send(());
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
                                    .map(|f| (f.code.into(), f.reason.to_string()))
                                    .unwrap_or((1000u16, String::new()));
                                let _ = cb_tx.send(WsMessage::Close { code, reason });
                                state_r.store(STATE_DISCONNECTED, Ordering::Release);
                                break;
                            }
                            _ => continue, // Ping/Pong handled by axum
                        };
                        if cb_tx.send(ws_msg).is_err() { break; }
                    }
                }
            }
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

/// Backward-compat alias — old code path took a fully-upgraded WebSocket.
/// Routes now call handle_ws_upgrade() directly via router.rs.
#[allow(dead_code)]
pub async fn handle_ws_connection(
    _socket: WebSocket,
    _handler: Py<PyAny>,
    _is_async: bool,
    _scope_info: WsScopeInfo,
) {
    // Deprecated — router.rs now calls handle_ws_upgrade(ws_upgrade, ...) directly.
    unreachable!("handle_ws_connection is deprecated; use handle_ws_upgrade");
}
