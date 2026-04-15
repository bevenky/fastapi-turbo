use axum::extract::ws::{Message, WebSocket};
use crossbeam_channel as cb;
use pyo3::prelude::*;
use std::collections::HashMap;
use tokio::sync::mpsc;

use crate::handler_bridge;

// ── Custom awaitable: blocks on crossbeam channel, zero asyncio overhead ──

/// A Python awaitable that blocks directly on a Rust crossbeam channel.
/// When Python `await`s this, `__next__` releases the GIL and blocks on the channel.
/// No pipe, no asyncio scheduling, no Queue — just a direct channel recv.
#[pyclass]
pub struct ChannelAwaitable {
    rx: cb::Receiver<String>,
}

#[pymethods]
impl ChannelAwaitable {
    fn __await__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    /// Blocks on crossbeam channel with GIL released. Returns the message
    /// and raises StopIteration (which is how Python iterators signal completion
    /// to the await machinery).
    fn __next__(&self, py: Python<'_>) -> PyResult<PyObject> {
        let rx = self.rx.clone();
        let msg = py.allow_threads(|| {
            rx.recv().map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed"))
        })?;
        // Raise StopIteration with the message as the value — this is how
        // Python's await protocol returns the final result from an iterator.
        Err(pyo3::exceptions::PyStopIteration::new_err(msg))
    }
}

// ── PyWebSocket: the Rust-side WS handle exposed to Python ──

#[pyclass]
pub struct PyWebSocket {
    tx: mpsc::UnboundedSender<Message>,
    rx: cb::Receiver<String>,
}

#[pymethods]
impl PyWebSocket {
    fn accept(&self) -> PyResult<()> { Ok(()) }

    fn send_text(&self, data: String) -> PyResult<()> {
        self.tx.send(Message::Text(data.into()))
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}")))
    }

    fn send_bytes(&self, data: Vec<u8>) -> PyResult<()> {
        self.tx.send(Message::Binary(data.into()))
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("WS send: {e}")))
    }

    /// Blocking receive for sync handlers (direct crossbeam recv).
    fn receive_text(&self, py: Python<'_>) -> PyResult<String> {
        let rx = self.rx.clone();
        py.allow_threads(|| {
            rx.recv().map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("WebSocket closed"))
        })
    }

    /// Returns a ChannelAwaitable for async handlers — Python can `await` this
    /// and it blocks directly on the crossbeam channel (zero asyncio overhead).
    fn receive_text_async(&self) -> ChannelAwaitable {
        ChannelAwaitable { rx: self.rx.clone() }
    }

    fn receive_bytes(&self, py: Python<'_>) -> PyResult<Vec<u8>> {
        self.receive_text(py).map(|s| s.into_bytes())
    }

    #[pyo3(signature = (code=None))]
    fn close(&self, code: Option<u16>) -> PyResult<()> {
        let _ = self.tx.send(Message::Close(Some(axum::extract::ws::CloseFrame {
            code: code.unwrap_or(1000), reason: "".into(),
        })));
        Ok(())
    }
}

// ── Pure Rust WS echo — baseline measurement ──

pub async fn handle_ws_echo_rust(socket: WebSocket) {
    use futures_util::{SinkExt, StreamExt};
    let (mut tx, mut rx) = socket.split();
    while let Some(Ok(msg)) = rx.next().await {
        match msg {
            Message::Text(_) | Message::Binary(_) => {
                if tx.send(msg).await.is_err() { break; }
            }
            Message::Close(_) => break,
            _ => {}
        }
    }
}

// ── Python handler bridge ──

pub async fn handle_ws_connection(
    socket: WebSocket,
    handler: Py<PyAny>,
    is_async: bool,
) {
    use futures_util::{SinkExt, StreamExt};

    let (mut ws_tx, mut ws_rx) = socket.split();

    // Outgoing: Python → tokio channel → WS writer
    let (tx_out, mut rx_out) = mpsc::unbounded_channel::<Message>();
    tokio::spawn(async move {
        while let Some(msg) = rx_out.recv().await {
            if ws_tx.send(msg).await.is_err() { break; }
        }
    });

    // Incoming: WS reader → crossbeam channel → Python (sync or async via ChannelAwaitable)
    let (cb_tx, cb_rx) = cb::unbounded::<String>();
    tokio::spawn(async move {
        while let Some(Ok(msg)) = ws_rx.next().await {
            let text = match msg {
                Message::Text(t) => t.to_string(),
                Message::Binary(b) => String::from_utf8_lossy(&b).to_string(),
                Message::Close(_) => break,
                _ => continue,
            };
            if cb_tx.send(text).is_err() { break; }
        }
    });

    let py_ws = PyWebSocket { tx: tx_out, rx: cb_rx };

    let ws_obj = Python::with_gil(|py| {
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
