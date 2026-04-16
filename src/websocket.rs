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
use pyo3::types::{PyBytes, PyDict};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use tokio::sync::mpsc;

use crate::handler_bridge;

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
    /// On Close, raises RuntimeError (matches old behavior; Python layer converts to WebSocketDisconnect).
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
            WsMessage::Close { .. } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                return Err(pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed"));
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
    fn __next__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let rx = self.rx.clone();
        let state = self.state.clone();
        let msg = py.detach(|| rx.recv().map_err(|_| handle_close_err(&state)))?;
        let value: Py<PyAny> = match msg {
            WsMessage::Binary(b) => PyBytes::new(py, &b).into_any().unbind(),
            WsMessage::Text(s) => PyBytes::new(py, s.as_bytes()).into_any().unbind(),
            WsMessage::Close { .. } => {
                self.state.store(STATE_DISCONNECTED, Ordering::Release);
                return Err(pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed"));
            }
        };
        Err(pyo3::exceptions::PyStopIteration::new_err(value))
    }
}

// ── PyWebSocket: the Rust-side WS handle exposed to Python ─────────

#[pyclass]
pub struct PyWebSocket {
    tx: mpsc::UnboundedSender<Message>,
    rx: cb::Receiver<WsMessage>,
    state: Arc<AtomicU8>,
    // Three cached awaitables — one per return-type (dict / text / bytes).
    // Each is lazily created on first use. Safe because a single WS has one reader.
    cached_dict: std::sync::OnceLock<Py<ChannelAwaitable>>,
    cached_text: std::sync::OnceLock<Py<TextAwaitable>>,
    cached_bytes: std::sync::OnceLock<Py<BytesAwaitable>>,
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
            .send(Message::Text(data.into()))
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}")))
    }

    /// Accept `bytes` directly (no Vec<u8> intermediate).
    fn send_bytes(&self, data: &Bound<'_, PyBytes>) -> PyResult<()> {
        let slice = data.as_bytes();
        let owned = Bytes::copy_from_slice(slice);
        self.tx
            .send(Message::Binary(owned))
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}")))
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

    #[pyo3(signature = (code=None))]
    fn close(&self, code: Option<u16>) -> PyResult<()> {
        let _ = self.tx.send(Message::Close(Some(axum::extract::ws::CloseFrame {
            code: code.unwrap_or(1000),
            reason: "".into(),
        })));
        self.state.store(STATE_DISCONNECTED, Ordering::Release);
        Ok(())
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

pub async fn handle_ws_connection(socket: WebSocket, handler: Py<PyAny>, is_async: bool) {
    use futures_util::{SinkExt, StreamExt};

    let (mut ws_tx, mut ws_rx) = socket.split();

    // Outgoing: Python → tokio channel → WS writer
    let (tx_out, mut rx_out) = mpsc::unbounded_channel::<Message>();
    tokio::spawn(async move {
        while let Some(msg) = rx_out.recv().await {
            if ws_tx.send(msg).await.is_err() {
                break;
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
