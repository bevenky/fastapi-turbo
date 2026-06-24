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
        if let Ok(dict) = hdr_attr.cast::<PyDict>() {
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
            if let Ok(list) = items_list.cast::<pyo3::types::PyList>() {
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
    let mut iter_bound = obj
        .getattr(pyo3::intern!(py, "body_iterator"))
        .expect("StreamingResponse missing body_iterator");

    // Sync-generator fast path: Starlette wraps sync stream content via
    // ``iterate_in_threadpool`` (shim-patched to turbo's), whose wrapper
    // exposes the ORIGINAL sync iterator as ``_fastapi_turbo_sync_source``.
    // When present, drive that source directly via the Rust ``__next__`` loop
    // on a blocking thread — no event loop, no per-chunk threadpool hop.
    // Otherwise the wrapper's ``__anext__`` is an async iterator → the slow
    // (threadpool-per-chunk) async path. Detecting the source here lets the
    // first-chunk pre-drain and ``iterate_sync_generator`` run unchanged.
    if let Ok(src) = iter_bound.getattr(pyo3::intern!(py, "_fastapi_turbo_sync_source")) {
        iter_bound = src;
    }

    // Detect async vs sync here rather than inside the streaming task — we
    // already hold the GIL, and the task can then skip two hasattr probes
    // on its critical first-chunk path. A swapped-in sync source has no
    // ``__anext__`` → sync path; the threadpool wrapper does → async path.
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
    if !iter_bound
        .hasattr(pyo3::intern!(py, "__next__"))
        .unwrap_or(false)
    {
        return None;
    }
    match iter_bound.call_method0(pyo3::intern!(py, "__next__")) {
        Ok(val) => Some(python_val_to_bytes(&val)),
        Err(e) => {
            if !e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                if let Some(app_obj) = crate::router::current_app(py) {
                    if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
                        let _ = lst.call_method1(py, "append", (e.value(py),));
                    }
                }
            }
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

    let py = py_iter.py();
    loop {
        match py_iter.call_method0("__next__") {
            Ok(val) => {
                let chunk = python_val_to_bytes(&val);
                // Release the GIL while the downstream channel might
                // block (slow client backpressure). Holding the GIL
                // through ``blocking_send`` stalls every other Python
                // thread in the process — a slow consumer could pin
                // the interpreter for seconds.
                let send_err = py.detach(|| tx.blocking_send(Ok(chunk)).is_err());
                if send_err {
                    // Receiver dropped (client disconnected / door closed the
                    // stream) — run the generator's GeneratorExit cleanup so
                    // its try/finally + ``except GeneratorExit`` fire (parity
                    // with the dispatcher's streaming-cancellation).
                    let _ = py_iter.call_method0("close");
                    break;
                }
            }
            Err(e) => {
                // StopIteration means we're done
                if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                    break;
                }
                // Capture the exception in ``app._captured_server_exceptions``
                // so TestClient's ``raise_server_exceptions=True`` mode
                // surfaces it to the caller (FA parity — streaming-body
                // failures must reach the test just like synchronous
                // handler failures).
                if let Some(app_obj) = crate::router::current_app(py) {
                    if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
                        let _ = lst.call_method1(py, "append", (e.value(py),));
                    }
                }
                break;
            }
        }
    }
}

/// Per-chunk callback handed to the Python `_drive_stream` driver.
///
/// `__call__(item) -> bool` converts the yielded item to bytes and
/// blocking-sends it through the mpsc channel, returning `True` to keep
/// going and `False` when the receiver was dropped (client disconnect /
/// door closed the body). The driver breaks on `False` and runs `aclose()`.
///
/// The instance never crosses a thread boundary at the Rust level: it's
/// created inside the `spawn_blocking` closure under `Python::attach`,
/// handed to Python as a positional arg, and only `__call__`-ed from that
/// same blocking thread. `tx` is a cheap `.clone()` of the channel Sender
/// (an Arc bump). `__call__` never raises — it returns a bool — so the
/// driver's `async for` never sees a spurious error from the push.
///
/// `tx` is wrapped in `Option` so the driver function can explicitly drop it
/// (`take_tx`) after `run_until_complete` returns: when the driver coroutine
/// raises mid-stream, CPython may keep the coroutine frame (and thus this
/// callback) alive in a reference CYCLE that only the cyclic GC collects.
/// That would leave a `Sender` clone alive, holding the channel open so the
/// HTTP body never terminates and the client hangs on read. Explicitly
/// taking the sender breaks that dependency on GC timing.
#[pyclass]
struct ChunkPush {
    tx: Option<mpsc::Sender<Result<bytes::Bytes, std::io::Error>>>,
}

#[pymethods]
impl ChunkPush {
    fn __call__(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> bool {
        let Some(tx) = self.tx.as_ref() else {
            return false;
        };
        let chunk = python_val_to_bytes(item);
        // Release the GIL across the (possibly backpressure-blocking) send —
        // holding it through ``blocking_send`` stalls every other Python
        // thread; a slow consumer could pin the interpreter for seconds.
        py.detach(|| tx.blocking_send(Ok(chunk)).is_ok())
    }
}

/// Iterate an async Python generator on a thread-local event loop, pushing
/// each chunk to the mpsc channel as soon as it's yielded. This is the hot
/// path for LLM token streaming (vLLM / SGLang) — every token must reach
/// the client immediately; buffering defeats the purpose.
///
/// Single-driver: instead of driving each `__anext__` through its own
/// `run_until_complete` (one full asyncio loop iteration per chunk,
/// ~37µs/chunk), we run the whole async iterator under ONE
/// `run_until_complete` over the Python driver coroutine
/// `fastapi_turbo.responses._drive_stream(aiter, push)`. The driver
/// `async for`s the iterator and hands each item to the `ChunkPush`
/// callback; the loop machinery is amortized across every chunk.
///
/// `aclose()` on disconnect, the request-scope teardown wrap (consumed via
/// `async for`), and SSE's internal producer/`wait_for` all compose under
/// the single outer `run_until_complete`. A mid-stream raise propagates out
/// of the driver coroutine and surfaces here as a Rust `Err`, captured onto
/// `app._captured_server_exceptions` for TestClient parity.
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

    // Build the per-chunk push callback over a cloned Sender, then the
    // driver coroutine: ``_drive_stream(body_iterator, push)``. Passing
    // ``body_iterator`` itself (not its ``__aiter__()``) means the driver's
    // ``aclose()`` fires on the WRAPPED gen — preserving request-scope
    // yield-dep teardown (``_door_wrap_stream_teardown``'s ``finally``).
    let push = match Py::new(
        py,
        ChunkPush {
            tx: Some(tx.clone()),
        },
    ) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("fastapi-turbo: ChunkPush alloc failed: {e}");
            return;
        }
    };

    let runner_coro = match (|| -> PyResult<Py<PyAny>> {
        let responses = py.import("fastapi_turbo.responses")?;
        let drive = responses.getattr("_drive_stream")?;
        Ok(drive.call1((iterator.bind(py), push.clone_ref(py)))?.unbind())
    })() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("fastapi-turbo: building stream driver failed: {e}");
            return;
        }
    };

    // ONE run_until_complete drives the whole stream. The first chunk is
    // pushed the instant the driver's ``async for`` yields it (TTFB is equal
    // or better than the per-chunk loop). On a non-StopAsyncIteration error
    // (mid-stream raise), capture it onto the app for TestClient parity.
    let result = loop_obj.call_method1(py, "run_until_complete", (runner_coro.bind(py),));

    // Explicitly drop the callback's Sender clone now (before any GC-cycle
    // retention of the coroutine frame can keep it alive). Together with the
    // closure's own `tx` going out of scope at function return, this closes
    // the channel so the HTTP body terminates and the client doesn't hang.
    push.borrow_mut(py).tx.take();

    if let Err(e) = result {
        if !e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
            if let Some(app_obj) = crate::router::current_app(py) {
                if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
                    let _ = lst.call_method1(py, "append", (e.value(py),));
                }
            }
            eprintln!("fastapi-turbo: run_until_complete streaming error: {e}");
        }
    }
}

/// Convert a Python str or bytes value to `bytes::Bytes`.
fn python_val_to_bytes(val: &Bound<'_, PyAny>) -> bytes::Bytes {
    // bytes chunks first (most common for streaming) — single memcpy, and no
    // String-extract exception overhead. `extract::<Vec<u8>>` on a bytes object
    // iterates per-element (~15ns/byte); `as_bytes` is a straight slice copy.
    if let Ok(b) = val.cast::<pyo3::types::PyBytes>() {
        bytes::Bytes::copy_from_slice(b.as_bytes())
    } else if let Ok(s) = val.extract::<String>() {
        bytes::Bytes::from(s)
    } else if let Ok(b) = val.extract::<Vec<u8>>() {
        bytes::Bytes::from(b)
    } else {
        let s = val.str().map(|s| s.to_string()).unwrap_or_default();
        bytes::Bytes::from(s)
    }
}
