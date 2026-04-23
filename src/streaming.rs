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
///
/// TTFB hot path — vLLM and SGLang return a new StreamingResponse on every
/// `chat/completions` request. We keep GIL-bound work to the bare minimum:
/// three attribute reads via interned strings, a short-circuit for the
/// typical "just content-type" header set, and detection of async-vs-sync
/// iteration done up front so the off-thread streaming task doesn't spend
/// its startup budget re-probing the iterator.
pub fn create_streaming_response(py: Python<'_>, obj: &Bound<'_, PyAny>) -> Response {
    // Interned names are a pointer-equality lookup on the type's tp_dict —
    // skips PyUnicode_FromString and hash/compare on every call.
    let status_code: u16 = obj
        .getattr(pyo3::intern!(py, "status_code"))
        .and_then(|a| a.extract())
        .unwrap_or(200);
    let status = StatusCode::from_u16(status_code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);

    // Collect headers. For SSE the common case is 1-2 entries (content-type
    // plus maybe cache-control) so avoid preallocating a large HeaderMap.
    let mut headers = HeaderMap::with_capacity(4);
    if let Ok(hdr_attr) = obj.getattr(pyo3::intern!(py, "headers")) {
        // Support both plain dict and MutableHeaders (which has .items())
        if let Ok(dict) = hdr_attr.downcast::<PyDict>() {
            for (k, v) in dict.iter() {
                let (Ok(key), Ok(val)) = (k.extract::<String>(), v.extract::<String>()) else {
                    continue;
                };
                if let (Ok(hname), Ok(hval)) =
                    (HeaderName::try_from(&*key), HeaderValue::from_str(&val))
                {
                    headers.insert(hname, hval);
                }
            }
        } else if let Ok(items_list) = hdr_attr.call_method0("items") {
            if let Ok(list) = items_list.downcast::<pyo3::types::PyList>() {
                for item in list.iter() {
                    if let Ok((key, val)) = item.extract::<(String, String)>() {
                        if let (Ok(hname), Ok(hval)) =
                            (HeaderName::try_from(&*key), HeaderValue::from_str(&val))
                        {
                            headers.insert(hname, hval);
                        }
                    }
                }
            }
        }
    }

    // Grab the body_iterator as a Py<PyAny> to pass across threads.
    let iter_bound = obj
        .getattr(pyo3::intern!(py, "body_iterator"))
        .expect("StreamingResponse missing body_iterator");

    // Detect async vs sync here rather than inside the streaming task — we
    // already hold the GIL, and the task can then skip two hasattr probes
    // on its critical first-chunk path.
    let is_async = iter_bound
        .hasattr(pyo3::intern!(py, "__anext__"))
        .unwrap_or(false);

    // Pre-drain the first chunk synchronously so hyper can coalesce the
    // response headers and the first data frame into a single TCP write.
    // For async generators we skip this optimization entirely: probing
    // `__anext__()` with a partial `send(None)` leaves the generator in
    // a non-recoverable state if it suspends on real I/O (asyncio.sleep,
    // DB reads) — subsequent `__anext__()` calls then raise "asynchronous
    // generator already running" and the body ends up empty. The
    // thread-local loop in `iterate_async_generator` handles chunk #0
    // reliably, at the cost of ~5µs extra TTFB vs the fast path.
    let first_chunk: Option<bytes::Bytes> = if is_async {
        None
    } else {
        drain_one_sync_chunk(&iter_bound)
    };

    let iterator: Py<PyAny> = iter_bound.unbind();

    // Create a channel-backed stream.
    let (tx, rx) = mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(32);

    // Prime the channel with the pre-drained first chunk so it's ready
    // before the streaming task wakes up.
    if let Some(chunk) = first_chunk {
        // try_send never blocks here — channel is empty and has capacity 32.
        let _ = tx.try_send(Ok(chunk));
    }

    // Spawn a blocking task that iterates the Python generator and pushes
    // the remaining chunks through the channel.
    tokio::task::spawn_blocking(move || {
        Python::attach(|py| {
            if is_async {
                iterate_async_generator(py, &iterator, &tx);
            } else {
                let iter_obj = iterator.bind(py);
                iterate_sync_generator(py, iter_obj, &tx);
            }
        });
    });

    let stream = ReceiverStream::new(rx);
    let body = Body::from_stream(stream);

    (status, headers, body).into_response()
}

/// Drive a sync iterator one step WITHOUT resetting state. Only safe when
/// the object is already an iterator (has `__next__` — e.g. a generator) so
/// the body task's subsequent `__iter__()` call returns `self` and continues
/// from the next element. For plain iterables like lists we'd duplicate the
/// first chunk, so we skip the fast path there.
///
/// Non-``StopIteration`` exceptions raised during the first-chunk probe are
/// captured onto ``app._captured_server_exceptions`` so TestClient surfaces
/// them (FA parity with streaming-body yield-dep teardown errors).
fn drain_one_sync_chunk(iter_bound: &Bound<'_, PyAny>) -> Option<bytes::Bytes> {
    let py = iter_bound.py();
    if !iter_bound.hasattr(pyo3::intern!(py, "__next__")).unwrap_or(false) {
        return None;
    }
    match iter_bound.call_method0(pyo3::intern!(py, "__next__")) {
        Ok(val) => Some(python_val_to_bytes(&val)),
        Err(e) => {
            if !e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                if let Ok(app_lock) = crate::router::APP_INSTANCE.read() {
                    if let Some(ref app_obj) = *app_lock {
                        if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
                            let _ = lst.call_method1(py, "append", (e.value(py),));
                        }
                    }
                }
            }
            None
        }
    }
}

/// Drive a single `__anext__` against an async generator without entering an
/// event loop. Only safe when the object already has `__anext__` — for true
/// async generators (`async def gen(): yield x`) this advances the generator
/// state, and the body task's subsequent `__aiter__()` returns `self`, so we
/// continue from chunk 2 without duplicating chunk 1. If the coroutine
/// suspends we return None WITHOUT closing the coro — closing would propagate
/// GeneratorExit to the async generator, destroying it.
fn drain_one_async_chunk_sync(py: Python<'_>, iter_bound: &Bound<'_, PyAny>) -> Option<bytes::Bytes> {
    let anext_name = pyo3::intern!(py, "__anext__");
    if !iter_bound.hasattr(anext_name).unwrap_or(false) {
        return None;
    }
    let coro = iter_bound.call_method0(anext_name).ok()?;
    match coro.call_method1("send", (py.None(),)) {
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
            let v = e.value(py);
            v.getattr("value")
                .ok()
                .map(|val| python_val_to_bytes(&val))
        }
        Err(_) => None,
        Ok(_) => {
            // Coroutine suspended — do NOT close it (closing propagates
            // GeneratorExit to the async generator). Just return None and
            // let iterate_async_generator handle it via run_until_complete.
            None
        }
    }
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
            eprintln!("fastapi-turbo: failed to iterate streaming body: {e}");
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
                // Capture the exception in ``app._captured_server_exceptions``
                // so TestClient's ``raise_server_exceptions=True`` mode
                // surfaces it to the caller (FA parity — streaming-body
                // failures must reach the test just like synchronous
                // handler failures).
                let py = py_iter.py();
                if let Ok(app_lock) = crate::router::APP_INSTANCE.read() {
                    if let Some(ref app_obj) = *app_lock {
                        if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
                            let _ = lst.call_method1(py, "append", (e.value(py),));
                        }
                    }
                }
                break;
            }
        }
    }
}

/// Iterate an async Python generator chunk-by-chunk on a thread-local event
/// loop, pushing each chunk to the mpsc channel as soon as it's yielded.
/// This is the hot path for LLM token streaming (vLLM / SGLang) — every
/// token must reach the client immediately; buffering defeats the purpose.
///
/// Strategy: try the fast sync probe (send(None)) first. If the generator
/// suspends on real I/O, switch to run_until_complete for ALL remaining
/// chunks permanently. This avoids destroying the generator state.
fn iterate_async_generator(
    py: Python<'_>,
    iterator: &Py<PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) {
    // Get or create a thread-local event loop. We're inside `spawn_blocking`
    // so each stream owns its loop for the duration of the response — no
    // cross-thread scheduling for __anext__.
    use std::cell::RefCell;
    thread_local! {
        static STREAM_LOOP: RefCell<Option<Py<PyAny>>> = const { RefCell::new(None) };
    }

    let loop_obj = match STREAM_LOOP.with(|cell| -> PyResult<Py<PyAny>> {
        let mut opt = cell.borrow_mut();
        if opt.is_none() {
            let asyncio = py.import("asyncio")?;
            let new_loop = match py.import("uvloop") {
                Ok(uvloop) => uvloop.call_method0("new_event_loop")?,
                Err(_) => asyncio.call_method0("new_event_loop")?,
            };
            asyncio.call_method1("set_event_loop", (&new_loop,))?;
            *opt = Some(new_loop.unbind());
        }
        Ok(opt.as_ref().unwrap().clone_ref(py))
    }) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("fastapi-turbo: streaming loop init failed: {e}");
            return;
        }
    };

    // Convert async-gen → async iterator once.
    let aiter = match iterator.bind(py).call_method0("__aiter__") {
        Ok(it) => it.unbind(),
        Err(e) => {
            eprintln!("fastapi-turbo: __aiter__ failed: {e}");
            return;
        }
    };

    // Drive each __anext__ through run_until_complete on the thread-local
    // event loop. This is correct for ALL async generators — both those that
    // do real async I/O (asyncio.sleep, DB queries) and those that complete
    // synchronously. The overhead of run_until_complete for sync generators
    // is ~5μs per chunk — negligible compared to the network I/O cost of
    // streaming. The pre-drain above already captured the first chunk via
    // the fast send(None) path to minimize TTFB.
    loop {
        let coro = match aiter.call_method0(py, "__anext__") {
            Ok(c) => c,
            Err(e) => {
                if !e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                    eprintln!("fastapi-turbo: __anext__ spawn error: {e}");
                }
                break;
            }
        };

        match loop_obj.call_method1(py, "run_until_complete", (coro.bind(py),)) {
            Ok(val) => {
                let chunk = python_val_to_bytes(val.bind(py));
                if tx.blocking_send(Ok(chunk)).is_err() {
                    break;
                }
            }
            Err(e) => {
                if e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                    break;
                }
                eprintln!("fastapi-turbo: run_until_complete streaming error: {e}");
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
