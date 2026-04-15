use axum::body::Body;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;

/// Build an Axum streaming response from a Python `StreamingResponse` object.
///
/// The Python object must have:
///   - `status_code: int`
///   - `headers: dict`
///   - `body_iterator`: an async or sync iterator yielding str/bytes chunks
pub fn create_streaming_response(_py: Python<'_>, obj: &Bound<'_, PyAny>) -> Response {
    let status_code: u16 = obj
        .getattr("status_code")
        .and_then(|a| a.extract())
        .unwrap_or(200);
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    // Collect headers
    let mut headers = HeaderMap::new();
    if let Ok(hdr_attr) = obj.getattr("headers") {
        if let Ok(dict) = hdr_attr.downcast::<PyDict>() {
            for (k, v) in dict.iter() {
                if let (Ok(key), Ok(val)) = (k.extract::<String>(), v.extract::<String>()) {
                    if let (Ok(hname), Ok(hval)) =
                        (HeaderName::try_from(key), HeaderValue::from_str(&val))
                    {
                        headers.insert(hname, hval);
                    }
                }
            }
        }
    }

    // Grab the body_iterator as a Py<PyAny> to pass across threads.
    let iterator: Py<PyAny> = obj
        .getattr("body_iterator")
        .expect("StreamingResponse missing body_iterator")
        .unbind();

    // Create a channel-backed stream.
    let (tx, rx) = mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(32);

    // Spawn a blocking task that iterates the Python generator and pushes
    // chunks through the channel.
    tokio::task::spawn_blocking(move || {
        Python::with_gil(|py| {
            let iter_obj = iterator.bind(py);

            // Try to detect async iterator (__aiter__ + __anext__)
            let is_async = iter_obj.hasattr("__aiter__").unwrap_or(false)
                && iter_obj.hasattr("__anext__").unwrap_or(false);

            if is_async {
                // For async generators we need to drive them on an event loop.
                iterate_async_generator(py, &iterator, &tx);
            } else {
                // Sync iterator
                iterate_sync_generator(py, iter_obj, &tx);
            }
        });
    });

    let stream = ReceiverStream::new(rx);
    let body = Body::from_stream(stream);

    (status, headers, body).into_response()
}

/// Iterate a synchronous Python iterator, sending each chunk through `tx`.
fn iterate_sync_generator(
    _py: Python<'_>,
    iter_obj: &Bound<'_, PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) {
    // Get a Python iterator via calling __iter__
    let py_iter = match iter_obj.call_method0("__iter__") {
        Ok(it) => it,
        Err(e) => {
            eprintln!("fastapi-rs: failed to iterate streaming body: {e}");
            return;
        }
    };

    loop {
        match py_iter.call_method0("__next__") {
            Ok(val) => {
                let chunk = python_val_to_bytes(&val);
                if tx.blocking_send(Ok(chunk)).is_err() {
                    break; // Client disconnected
                }
            }
            Err(e) => {
                // StopIteration means we're done
                if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py_iter.py()) {
                    break;
                }
                eprintln!("fastapi-rs: streaming iterator error: {e}");
                break;
            }
        }
    }
}

/// Iterate an async Python generator by scheduling it on the persistent
/// asyncio event loop (from handler_bridge).
fn iterate_async_generator(
    py: Python<'_>,
    iterator: &Py<PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) {
    let asyncio = py.import("asyncio").expect("asyncio import failed");

    // Build a coroutine that collects all chunks from the async iterator.
    // We define a small Python helper inline.
    let builtins = py.import("builtins").expect("builtins import failed");
    let exec_fn = builtins.getattr("exec").expect("exec missing");

    let globals = PyDict::new(py);
    let locals = PyDict::new(py);
    let helper_code = "async def _drain_async_iter(aiter):\n    chunks = []\n    async for chunk in aiter:\n        chunks.append(chunk)\n    return chunks\n";
    exec_fn
        .call1((helper_code, &globals, &locals))
        .expect("Failed to define drain helper");
    let drain_fn = locals
        .get_item("_drain_async_iter")
        .expect("drain fn missing")
        .expect("drain fn is None");

    let coro = drain_fn
        .call1((iterator.bind(py),))
        .expect("Failed to create drain coroutine");

    // Use the persistent event loop from handler_bridge.
    let event_loop = crate::handler_bridge::get_event_loop_pub(py)
        .expect("Failed to get event loop");

    let future = asyncio
        .call_method1(
            "run_coroutine_threadsafe",
            (coro, event_loop.bind(py)),
        )
        .expect("run_coroutine_threadsafe failed");

    // Block waiting for the result (we're already in a spawn_blocking context)
    let result = future
        .call_method1("result", (30.0_f64,))  // 30s timeout
        .expect("async iteration timed out or failed");

    // result is a list of chunks
    if let Ok(list) = result.downcast::<pyo3::types::PyList>() {
        for item in list.iter() {
            let chunk = python_val_to_bytes(&item);
            if tx.blocking_send(Ok(chunk)).is_err() {
                break;
            }
        }
    }
}

/// Convert a Python str or bytes value to `bytes::Bytes`.
fn python_val_to_bytes(val: &Bound<'_, PyAny>) -> bytes::Bytes {
    if let Ok(s) = val.extract::<String>() {
        bytes::Bytes::from(s)
    } else if let Ok(b) = val.extract::<Vec<u8>>() {
        bytes::Bytes::from(b)
    } else {
        let s = val.str().map(|s| s.to_string()).unwrap_or_default();
        bytes::Bytes::from(s)
    }
}
