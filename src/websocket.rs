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
use std::sync::Arc;
use tokio::sync::mpsc;

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
}

// ── WebSocket state (matches Starlette's WebSocketState) ───────────
// These values MUST match python/fastapi_rs/websockets.py::WebSocketState.

pub const STATE_CONNECTING: u8 = 0;
pub const STATE_CONNECTED: u8 = 1;
pub const STATE_DISCONNECTED: u8 = 2;
pub const STATE_RESPONSE: u8 = 3;

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
    /// On Close, raises RuntimeError with message "WS_CLOSED:<code>:<reason>"
    /// so the Python layer can extract the real close code.
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
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    format!("WS_CLOSED:{code}:{reason}")
                ));
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
    /// Zero-copy from axum's Bytes → PyBytes (one allocation for the PyBytes).
    /// On Close, raises RuntimeError with message "WS_CLOSED:<code>:<reason>".
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| rx.recv().map_err(|_| handle_close_err(&state)))?;
        let value: Py<PyAny> = match msg {
            WsMessage::Binary(b) => PyBytes::new(py, &b).into_any().unbind(),
            WsMessage::Text(s) => PyBytes::new(py, s.as_bytes()).into_any().unbind(),
            WsMessage::Close { code, reason } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    format!("WS_CLOSED:{code}:{reason}")
                ));
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
    fn accept(&self) -> PyResult<()> {
        self.state.store(STATE_CONNECTED, Ordering::Release);
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

pub async fn handle_ws_connection(
    socket: WebSocket,
    handler: Py<PyAny>,
    is_async: bool,
    scope_info: WsScopeInfo,
) {
    use futures_util::{SinkExt, StreamExt};

    let (mut ws_tx, mut ws_rx) = socket.split();

    // Outgoing writer: handles Send messages + Flush barrier signals
    let (tx_out, mut rx_out) = mpsc::unbounded_channel::<WriterCmd>();
    tokio::spawn(async move {
        while let Some(cmd) = rx_out.recv().await {
            match cmd {
                WriterCmd::Send(msg) => {
                    if ws_tx.send(msg).await.is_err() {
                        break;
                    }
                }
                WriterCmd::Flush(tx) => {
                    // All previous Send messages have been flushed (since we process
                    // them in order before hitting this Flush command). Fire the
                    // oneshot so the waiting close_and_wait() returns.
                    let _ = tx.send(());
                }
            }
        }
    });

    // Incoming: WS reader → crossbeam channel → Python handler
    let (cb_tx, cb_rx) = cb::unbounded::<WsMessage>();
    let state = Arc::new(AtomicU8::new(STATE_CONNECTING));
    let state_reader = state.clone();

    tokio::spawn(async move {
        while let Some(result) = ws_rx.next().await {
            let msg = match result {
                Ok(m) => m,
                Err(_) => break,
            };
            let ws_msg = match msg {
                Message::Text(t) => WsMessage::Text(t.to_string()),
                // Zero-copy: pass the Bytes through directly (axum 0.8 uses bytes::Bytes).
                Message::Binary(b) => WsMessage::Binary(b),
                Message::Close(frame) => {
                    let (code, reason) = frame
                        .map(|f| (f.code.into(), f.reason.to_string()))
                        .unwrap_or((1000u16, String::new()));
                    // Send the Close frame through the channel so receive_dict() can emit
                    // the disconnect event. Then drop the channel.
                    let _ = cb_tx.send(WsMessage::Close { code, reason });
                    state_reader.store(STATE_DISCONNECTED, Ordering::Release);
                    break;
                }
                // Ping/Pong are handled automatically by axum.
                _ => continue,
            };
            if cb_tx.send(ws_msg).is_err() {
                break;
            }
        }
    });

    let py_ws = PyWebSocket {
        tx: tx_out,
        rx: cb_rx,
        state,
        cached_dict: std::sync::OnceLock::new(),
        cached_text: std::sync::OnceLock::new(),
        cached_bytes: std::sync::OnceLock::new(),
        scope_info: Arc::new(scope_info),
    };

    let ws_obj = Python::attach(|py| {
        let ws_cell = Py::new(py, py_ws).expect("PyWebSocket");
        let ws_mod = py.import("fastapi_rs.websockets").expect("websockets");
        let ws_cls = ws_mod.getattr("WebSocket").expect("WebSocket");
        ws_cls.call1((ws_cell,)).expect("wrap").unbind()
    });

    let mut kwargs: HashMap<String, Py<PyAny>> = HashMap::new();
    kwargs.insert("websocket".to_string(), ws_obj);

    if is_async {
        let _ = handler_bridge::call_async_via_event_loop_pub(handler, kwargs).await;
    } else {
        let _ = handler_bridge::call_sync_handler(handler, kwargs).await;
    }
}
