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

    // For async streams, decide up front whether the gen NEVER awaits a
    // loop-needing primitive (``_stream_is_noawait`` — bytecode ``GET_AWAITABLE``
    // absence on the real user gen). When true, the streaming task drives each
    // ``__anext__`` with a bare ``send(None)`` reaching ``StopIteration`` —
    // pushing the chunk INLINE, skipping the per-stream ``run_until_complete``
    // event-loop tax. A gen that DOES await falls to the existing single
    // ``run_until_complete`` driver (``_drive_stream``) — unchanged behavior.
    // Resolved here under the GIL we already hold, off the streaming task's
    // critical path. The probe MUST be conservative: a bare ``send(None)`` on a
    // loop-needing gen raises ``RuntimeError`` and CORRUPTS it, so we only fast-
    // path gens proven await-free.
    let noawait = is_async
        && py
            .import("fastapi_turbo.responses")
            .and_then(|m| m.getattr("_stream_is_noawait"))
            .and_then(|f| f.call1((obj,)))
            .and_then(|r| r.extract::<bool>())
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

    // Mechanism 2: an async gen with REAL awaits multiplexes as a TASK on the
    // shared `_async_worker` loop — no per-stream thread, no per-stream event
    // loop, no `run_until_complete` machinery (~122µs → the loop amortizes it
    // across every in-flight stream). Falls back to the legacy dedicated-
    // thread driver when the loop is unavailable (closed / no tokio runtime
    // context) or when `FASTAPI_TURBO_STREAM_THREAD=1` forces it.
    let worker_scheduled = is_async
        && !noawait
        && !stream_thread_forced()
        && schedule_stream_on_worker_loop(py, &iterator, &tx);

    // Legacy paths: spawn a blocking task that iterates the Python generator
    // and pushes the remaining chunks through the channel. (Sync gens and
    // proven-no-await async gens ALWAYS take this path — unchanged.)
    if !worker_scheduled {
        tokio::task::spawn_blocking(move || {
            Python::attach(|py| {
                if is_async {
                    if noawait {
                        iterate_async_generator_inline(py, &iterator, &tx);
                    } else {
                        iterate_async_generator(py, &iterator, &tx);
                    }
                } else {
                    let iter_obj = iterator.bind(py);
                    iterate_sync_generator(py, iter_obj, &tx);
                }
            });
        });
    }

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

    /// Drop the Sender clone — called by `_drive_stream` on NORMAL completion.
    /// On the legacy path this is a harmless early drop (the driving closure
    /// still holds its own Sender until after exception capture); on the
    /// worker-loop path the counterpart (`LoopChunkPush::close`) is what lets
    /// the HTTP body EOF skip the done-callback hop.
    fn close(&mut self) {
        self.tx.take();
    }
}

// ═══ Mechanism 2: await-streams multiplex as tasks on the shared worker loop ═══
//
// The legacy driver burns a `spawn_blocking` thread + a thread-local event
// loop + one `run_until_complete` per stream (~80µs of GIL-held loop
// machinery per response). Instead, `_drive_stream(aiter, push)` is scheduled
// as a plain TASK on the persistent `_async_worker` loop — the same loop that
// runs needs-worker async handlers — via one `call_soon_threadsafe` enqueue
// (a Rust `StreamJob` pyclass, mirroring router.rs's async-inline `InlineJob`).
//
// The one hard rule on the shared loop: NEVER block it. The legacy
// `ChunkPush` does `py.detach + blocking_send`, which on a full body channel
// would stall EVERY in-flight request in the process. `LoopChunkPush` uses
// `try_send`; on `Full` it hands the driver an asyncio Future and spawns a
// tokio waiter that awaits channel capacity (`Sender::reserve`), sends the
// pending chunk itself (order preserved — the driver can't push again until
// the future resolves), and resolves the future via `call_soon_threadsafe`
// (True = keep going, False = receiver dropped while waiting).
//
// Everything else is preserved exactly: disconnect → `aclose()` throws
// GeneratorExit into the WRAPPED gen (request-scope yield-dep teardown via
// `_door_wrap_stream_teardown`); a mid-stream raise is captured onto
// `app._captured_server_exceptions` BEFORE the body channel closes
// (TestClient parity — the legacy closure's Sender clone gave the same
// ordering); SSE's `ensure_future` keepalive producer lands on the SAME loop
// as every `__anext__` naturally (it's all one task now). The no-await inline
// path and sync-gen path are untouched.

/// `FASTAPI_TURBO_STREAM_THREAD=1` forces the legacy per-stream
/// dedicated-thread driver (kill switch, read once).
fn stream_thread_forced() -> bool {
    static FLAG: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *FLAG.get_or_init(|| {
        std::env::var("FASTAPI_TURBO_STREAM_THREAD")
            .map(|v| matches!(v.trim(), "1" | "true" | "True" | "TRUE" | "yes" | "on"))
            .unwrap_or(false)
    })
}

/// Cached `fastapi_turbo.responses._resolve_stream_future` — the
/// `call_soon_threadsafe` target that resolves a backpressure future
/// (`done()`-guarded so a cancelled future never raises InvalidStateError).
fn stream_future_resolver(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static RESOLVER: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
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

/// Cached `fastapi_turbo.responses._drive_stream` (per-stream import+getattr
/// would land on the request hot path).
fn drive_stream_fn(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static DRIVE: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
    if let Some(f) = DRIVE.get() {
        return Ok(f);
    }
    let f = py
        .import("fastapi_turbo.responses")?
        .getattr("_drive_stream")?
        .unbind();
    let _ = DRIVE.set(f);
    Ok(DRIVE.get().expect("just set"))
}

/// Cached `contextvars.Context` type (fresh empty context per stream enqueue).
fn contextvars_context_type(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CTX: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
    if let Some(c) = CTX.get() {
        return Ok(c);
    }
    let c = py.import("contextvars")?.getattr("Context")?.unbind();
    let _ = CTX.set(c);
    Ok(CTX.get().expect("just set"))
}

/// Cached `fastapi_turbo.responses._spawn_stream_task` — the loop-thread task
/// spawner (eager-start on 3.12+, `create_task` fallback).
fn stream_task_spawner(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static SPAWN: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
    if let Some(f) = SPAWN.get() {
        return Ok(f);
    }
    let f = py
        .import("fastapi_turbo.responses")?
        .getattr("_spawn_stream_task")?
        .unbind();
    let _ = SPAWN.set(f);
    Ok(SPAWN.get().expect("just set"))
}

/// The canonical Python bool singleton as an owned `Py<PyAny>` — the driver
/// discriminates the push result via `is True` / `is False`.
fn py_bool(py: Python<'_>, v: bool) -> Py<PyAny> {
    pyo3::types::PyBool::new(py, v).to_owned().into_any().unbind()
}

/// Capture a streaming exception onto the GIVEN app's
/// `_captured_server_exceptions` (TestClient `raise_server_exceptions=True`
/// parity). The app is resolved ONCE at stream-creation time on the request
/// thread — the worker loop thread has no per-thread `CURRENT_APP` binding,
/// so resolving there could hit the wrong app in multi-server processes.
fn capture_stream_err_on_app(py: Python<'_>, app: Option<&Py<PyAny>>, e: &PyErr) {
    if let Some(app_obj) = app {
        if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
            let _ = lst.call_method1(py, "append", (e.value(py),));
        }
    }
}

/// Loop-native per-chunk push handed to `_drive_stream` — MUST NOT block the
/// shared worker loop. `__call__(item)` returns:
///   * `True`  — chunk sent (`try_send` succeeded);
///   * `False` — receiver dropped (client disconnect / door closed the body);
///   * an asyncio `Future` — channel full; a tokio waiter task delivers the
///     pending chunk when capacity frees and resolves the future
///     (True = delivered, False = receiver dropped while waiting).
///
/// `tx` is `Option` so `StreamCompleter` can explicitly drop the Sender when
/// the driver task finishes: a raised driver frame can be GC-cycle-retained,
/// and channel close must never depend on GC timing (the HTTP body would
/// hang). `rt` is the request runtime's handle, captured at stream-creation
/// time on the tokio side — the loop thread has no runtime context to spawn
/// the backpressure waiter from.
#[pyclass]
struct LoopChunkPush {
    tx: Option<mpsc::Sender<Result<bytes::Bytes, std::io::Error>>>,
    rt: tokio::runtime::Handle,
}

#[pymethods]
impl LoopChunkPush {
    fn __call__(&self, py: Python<'_>, item: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let Some(tx) = self.tx.as_ref() else {
            return Ok(py_bool(py, false));
        };
        let chunk = python_val_to_bytes(item);
        match tx.try_send(Ok(chunk)) {
            Ok(()) => Ok(py_bool(py, true)),
            Err(mpsc::error::TrySendError::Closed(_)) => Ok(py_bool(py, false)),
            Err(mpsc::error::TrySendError::Full(pending)) => {
                // Backpressure: hand the driver a Future to await. We are ON
                // the loop thread (the driver task called us synchronously),
                // so `create_future()` here is loop-safe.
                let loop_obj = crate::handler_bridge::worker_loop().ok_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("async worker loop not initialized")
                })?;
                let call_soon = crate::handler_bridge::worker_call_soon()
                    .ok_or_else(|| {
                        pyo3::exceptions::PyRuntimeError::new_err(
                            "async worker loop not initialized",
                        )
                    })?
                    .clone_ref(py);
                let fut = loop_obj.call_method0(py, "create_future")?;
                let resolver = stream_future_resolver(py)?.clone_ref(py);
                let fut_for_waiter = fut.clone_ref(py);
                let tx_for_waiter = tx.clone();
                self.rt.spawn(async move {
                    // `reserve()` resolves when a slot frees; sending via the
                    // permit (not the driver) preserves chunk order — the
                    // driver is suspended on the future until we resolve it.
                    let ok = match tx_for_waiter.reserve().await {
                        Ok(permit) => {
                            permit.send(pending);
                            true
                        }
                        Err(_) => false,
                    };
                    Python::attach(|py| {
                        let _ = call_soon.call1(
                            py,
                            (resolver.bind(py), fut_for_waiter.bind(py), ok),
                        );
                    });
                });
                Ok(fut)
            }
        }
    }

    /// Drop the Sender — called by `_drive_stream` on NORMAL completion so the
    /// HTTP body's EOF doesn't wait for the task done-callback hop. The
    /// `StreamCompleter` still runs (idempotent take) and owns the
    /// exception-path close, which must happen AFTER capture.
    fn close(&mut self) {
        self.tx.take();
    }
}

/// Enqueued via `loop.call_soon_threadsafe(job)` with a FRESH empty
/// `contextvars.Context` (parity with the legacy dedicated thread, whose
/// driver never saw the request thread's ambient contextvars). The
/// `_drive_stream` coroutine object is pre-built on the REQUEST thread
/// (creation doesn't execute the body) — `__call__` on the loop thread only
/// does `create_task` + completer wiring, keeping the shared loop's
/// per-stream burden minimal.
#[pyclass]
struct StreamJob {
    coro: Option<Py<PyAny>>,
    push: Option<Py<LoopChunkPush>>,
    app: Option<Py<PyAny>>,
}

#[pymethods]
impl StreamJob {
    fn __call__(&mut self, py: Python<'_>) {
        let (Some(coro), Some(push)) = (self.coro.take(), self.push.take()) else {
            return;
        };
        if let Err(e) = start_stream_task_on_loop(py, &coro, &push, &self.app) {
            // Capture FIRST (TestClient must see the error once the body
            // ends), THEN drop the Sender so the HTTP body terminates. The
            // coro is closed/cancelled inside start_stream_task_on_loop —
            // ownership is unambiguous there (pre- vs post-create_task).
            capture_stream_err_on_app(py, self.app.as_ref(), &e);
            eprintln!("fastapi-turbo: worker-loop stream start failed: {e}");
            push.borrow_mut(py).tx.take();
        }
    }
}

/// Loop-thread body of `StreamJob`: spawn the pre-built
/// `_drive_stream(body_iterator, push)` coroutine as a task on the worker
/// loop via `_spawn_stream_task` (eager-start — a cooperative-only stream
/// completes synchronously inside this call, channel already closed by the
/// driver's `push.close()`). The driver was built over `body_iterator` itself
/// (not its `__aiter__()`) so its `aclose()` fires on the WRAPPED gen —
/// preserving request-scope yield-dep teardown (`_door_wrap_stream_teardown`'s
/// `finally`), exactly like the legacy driver.
fn start_stream_task_on_loop(
    py: Python<'_>,
    coro: &Py<PyAny>,
    push: &Py<LoopChunkPush>,
    app: &Option<Py<PyAny>>,
) -> PyResult<()> {
    let loop_obj = crate::handler_bridge::worker_loop().ok_or_else(|| {
        let _ = coro.call_method0(py, "close");
        pyo3::exceptions::PyRuntimeError::new_err("async worker loop not initialized")
    })?;
    // Build the completer BEFORE spawning: once the task exists, every
    // failure path must still guarantee the Sender gets dropped.
    let completer = match Py::new(
        py,
        StreamCompleter {
            push: push.clone_ref(py),
            app: app.as_ref().map(|a| a.clone_ref(py)),
        },
    ) {
        Ok(c) => c,
        Err(e) => {
            let _ = coro.call_method0(py, "close");
            return Err(e);
        }
    };
    let spawner = match stream_task_spawner(py) {
        Ok(s) => s,
        Err(e) => {
            let _ = coro.call_method0(py, "close");
            return Err(e);
        }
    };
    if let Err(e) = spawner.call1(
        py,
        (loop_obj.bind(py), coro.bind(py), completer.bind(py)),
    ) {
        // Task construction failed before it took ownership — close the
        // coroutine (no-op if an eager step already finished it; throws
        // GeneratorExit for teardown if it half-started). The caller then
        // captures the error and closes the body channel.
        let _ = coro.call_method0(py, "close");
        return Err(e);
    }
    Ok(())
}

/// Driver-task done-callback (runs on the loop thread). Captures a mid-stream
/// raise onto the app BEFORE dropping the body-channel Sender, so
/// `_captured_server_exceptions` is populated by the time the client observes
/// end-of-body — the same ordering the legacy thread driver had (its closure
/// still held a Sender clone while capturing).
#[pyclass]
struct StreamCompleter {
    push: Py<LoopChunkPush>,
    app: Option<Py<PyAny>>,
}

#[pymethods]
impl StreamCompleter {
    fn __call__(&self, py: Python<'_>, task: Bound<'_, PyAny>) {
        let cancelled = task
            .call_method0(pyo3::intern!(py, "cancelled"))
            .and_then(|v| v.extract::<bool>())
            .unwrap_or(false);
        if !cancelled {
            // `result()` re-raises the task's exception (mid-stream raise).
            if let Err(e) = task.call_method0(pyo3::intern!(py, "result")) {
                if !e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                    capture_stream_err_on_app(py, self.app.as_ref(), &e);
                    eprintln!("fastapi-turbo: worker-loop streaming error: {e}");
                }
            }
        }
        // Drop the Sender NOW — closes the mpsc channel so the HTTP body
        // terminates even if the driver frame is GC-cycle-retained.
        self.push.borrow_mut(py).tx.take();
    }
}

/// Tokio-side enqueue (one short GIL section, already held by the caller):
/// wrap the stream parts in a `StreamJob` and `call_soon_threadsafe` it onto
/// the shared worker loop. Returns `false` when the loop can't take it (no
/// tokio runtime context for the backpressure waiter, loop missing/closed,
/// alloc failure) — the caller then falls back to the legacy thread driver.
fn schedule_stream_on_worker_loop(
    py: Python<'_>,
    iterator: &Py<PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) -> bool {
    // The backpressure waiter needs a runtime to spawn onto; capture the
    // handle HERE (request thread) — the loop thread has no runtime context.
    let Ok(rt) = tokio::runtime::Handle::try_current() else {
        return false;
    };
    crate::handler_bridge::init_async_worker();
    let Some(call_soon) = crate::handler_bridge::worker_call_soon() else {
        return false;
    };
    // Resolve the capture target while the request thread's CURRENT_APP
    // binding is live (the loop thread's isn't) — see capture_stream_err_on_app.
    let app = crate::router::current_app(py);
    let Ok(push) = Py::new(
        py,
        LoopChunkPush {
            tx: Some(tx.clone()),
            rt,
        },
    ) else {
        return false;
    };
    // Build the `_drive_stream(body_iterator, push)` coroutine HERE on the
    // request thread — creating a coroutine object doesn't execute its body,
    // and it keeps the shared loop's job callback down to create_task+wiring.
    let Ok(drive) = drive_stream_fn(py) else {
        return false;
    };
    let Ok(coro) = drive.call1(py, (iterator.bind(py), push.bind(py))) else {
        return false;
    };
    let enqueued = (|| -> PyResult<()> {
        let job = Py::new(
            py,
            StreamJob {
                coro: Some(coro.clone_ref(py)),
                push: Some(push),
                app,
            },
        )?;
        // Fresh EMPTY contextvars.Context: the legacy driver ran on a
        // dedicated thread whose context never held the request thread's
        // ambient contextvars — `create_task` inside the job snapshots the
        // callback's context, so seed an empty one for parity.
        let ctx = contextvars_context_type(py)?.call0(py)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item(pyo3::intern!(py, "context"), ctx)?;
        call_soon.call(py, (job.bind(py),), Some(&kwargs))?;
        Ok(())
    })();
    match enqueued {
        Ok(()) => true,
        // Loop closed mid-shutdown / alloc failure — close the never-started
        // coroutine (silences "never awaited" + drops its iterator/push refs);
        // the legacy fallback still owns iterator + tx.
        Err(_) => {
            let _ = coro.call_method0(py, "close");
            false
        }
    }
}

/// LEGACY FALLBACK: iterate an async Python generator on a thread-local event
/// loop, pushing each chunk to the mpsc channel as soon as it's yielded.
/// Await-streams normally multiplex on the shared worker loop instead
/// (`schedule_stream_on_worker_loop`, Mechanism 2); this per-stream
/// thread+loop driver runs only when that enqueue is unavailable (no tokio
/// runtime context / loop closed) or `FASTAPI_TURBO_STREAM_THREAD=1`.
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
/// Get or create the per-thread streaming event loop. We're inside
/// `spawn_blocking` so each stream owns its loop for the duration of the
/// response — no cross-thread scheduling for `__anext__`. The no-await inline
/// driver and the `_drive_stream`/`_resume_anext` paths all share this SAME
/// loop so SSE's `ensure_future` producer task survives across chunks.
fn stream_loop(py: Python<'_>) -> PyResult<Py<PyAny>> {
    use std::cell::RefCell;
    thread_local! {
        static STREAM_LOOP: RefCell<Option<Py<PyAny>>> = const { RefCell::new(None) };
    }
    STREAM_LOOP.with(|cell| -> PyResult<Py<PyAny>> {
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
    })
}

fn iterate_async_generator(
    py: Python<'_>,
    iterator: &Py<PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) {
    let loop_obj = match stream_loop(py) {
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

/// Drive a PROVEN no-await async generator INLINE, skipping the per-stream
/// `run_until_complete` event-loop tax. The caller (`create_streaming_response`)
/// only routes here when `_stream_is_noawait` returned true — i.e. the real user
/// gen contains no `await` (bytecode `GET_AWAITABLE` absent), so every
/// `aiter.__anext__()` reaches `StopIteration(chunk)` on a single bare
/// `coro.send(None)` with NO running loop.
///
/// Correctness boundary (verified): a bare `send(None)` on a gen that awaits a
/// loop-needing primitive RAISES `RuntimeError("no running event loop")` and
/// CORRUPTS the gen (the partly-advanced `__anext__` can't be resumed, and the
/// gen then reports `StopAsyncIteration`, silently truncating the body). That is
/// why we NEVER probe-then-drive an unknown gen here — the no-await verdict is
/// decided statically up front. As a defense-in-depth fallback, if `send(None)`
/// unexpectedly SUSPENDS (returns a value) instead of stopping, we resume that
/// SAME already-started coro via `_resume_anext` on the shared `stream_loop`
/// (the only correct resume — `await started` continues from the suspension
/// point, never re-sending from the front). A `RuntimeError` on the bare send is
/// the destructive case: the chunk is unrecoverable, so we capture + stop.
///
/// `__aiter__()` on an async-gen returns self, so the teardown wrapper's
/// `finally` runs on normal exhaustion, and `aclose()` on disconnect throws
/// `GeneratorExit` into the WRAPPED gen (request-scope yield-dep teardown
/// parity). Mid-stream raises propagate out of `send`/`run_until_complete` and
/// are captured onto `app._captured_server_exceptions` (TestClient parity).
fn iterate_async_generator_inline(
    py: Python<'_>,
    iterator: &Py<PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) {
    let aiter = match iterator.bind(py).call_method0(pyo3::intern!(py, "__aiter__")) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("fastapi-turbo: inline stream __aiter__ failed: {e}");
            return;
        }
    };

    // Lazily created only if a chunk unexpectedly suspends (the await-free
    // verdict should mean this never fires, but the fallback must be correct).
    let mut loop_obj: Option<Py<PyAny>> = None;
    let mut resume_helper: Option<Py<PyAny>> = None;

    loop {
        let coro = match aiter.call_method0(pyo3::intern!(py, "__anext__")) {
            Ok(c) => c,
            Err(e) => {
                capture_or_eprint_stream_err(py, &e);
                break;
            }
        };

        // Bare send(None): for a no-await gen this reaches StopIteration(chunk).
        let send_result = coro.call_method1(pyo3::intern!(py, "send"), (py.None(),));

        let chunk: bytes::Bytes = match send_result {
            // The gen SUSPENDED (returned a value) — the proven-no-await path
            // shouldn't reach here, but resume the SAME started coro correctly.
            Ok(_yielded) => {
                match resume_suspended_anext(
                    py,
                    &coro,
                    &mut loop_obj,
                    &mut resume_helper,
                ) {
                    Ok(Some(c)) => c,
                    Ok(None) => break, // StopAsyncIteration — done
                    Err(e) => {
                        capture_or_eprint_stream_err(py, &e);
                        break;
                    }
                }
            }
            Err(e) => {
                if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                    // Normal per-chunk completion: chunk is the StopIteration value.
                    match e.value(py).getattr(pyo3::intern!(py, "value")) {
                        Ok(v) if !v.is_none() => python_val_to_bytes(&v),
                        // StopIteration() with no value → treat as empty chunk skip.
                        _ => continue,
                    }
                } else if e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                    break; // gen exhausted
                } else if e.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py) {
                    // Destructive case: a loop-needing await raised on bare send.
                    // The coro is unrecoverable; capture and stop so we never
                    // ship a corrupt/truncated body silently.
                    capture_or_eprint_stream_err(py, &e);
                    break;
                } else {
                    // Mid-stream raise (ValueError, etc.) — capture for parity.
                    capture_or_eprint_stream_err(py, &e);
                    break;
                }
            }
        };

        // Push inline. Release the GIL across the (possibly backpressure-
        // blocking) send so a slow consumer can't pin the interpreter.
        let alive = py.detach(|| tx.blocking_send(Ok(chunk)).is_ok());
        if !alive {
            // Receiver dropped (client disconnect / door closed the body) —
            // aclose() throws GeneratorExit into the WRAPPED gen so its
            // try/finally + teardown fire (streaming-cancellation parity).
            close_aiter(py, &aiter, &mut loop_obj);
            break;
        }
    }
}

/// Resume an already-started `__anext__` coro that unexpectedly suspended,
/// driving it to its next item on the shared stream loop via `_resume_anext`.
/// Returns `Ok(Some(chunk))`, `Ok(None)` on StopAsyncIteration, or `Err` on a
/// mid-stream raise.
fn resume_suspended_anext(
    py: Python<'_>,
    coro: &Bound<'_, PyAny>,
    loop_obj: &mut Option<Py<PyAny>>,
    resume_helper: &mut Option<Py<PyAny>>,
) -> PyResult<Option<bytes::Bytes>> {
    if loop_obj.is_none() {
        *loop_obj = Some(stream_loop(py)?);
    }
    if resume_helper.is_none() {
        let responses = py.import("fastapi_turbo.responses")?;
        *resume_helper = Some(responses.getattr("_resume_anext")?.unbind());
    }
    let l = loop_obj.as_ref().unwrap().bind(py);
    let helper = resume_helper.as_ref().unwrap().bind(py);
    let resume_coro = helper.call1((coro,))?;
    match l.call_method1("run_until_complete", (resume_coro,)) {
        Ok(v) => Ok(Some(python_val_to_bytes(&v))),
        Err(e) => {
            if e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                Ok(None)
            } else {
                Err(e)
            }
        }
    }
}

/// Run `aiter.aclose()` on the shared stream loop (disconnect cleanup). aclose
/// returns a coroutine, so it needs the loop even for a no-await gen.
fn close_aiter(py: Python<'_>, aiter: &Bound<'_, PyAny>, loop_obj: &mut Option<Py<PyAny>>) {
    let aclose_coro = match aiter.call_method0(pyo3::intern!(py, "aclose")) {
        Ok(c) => c,
        Err(_) => return,
    };
    if loop_obj.is_none() {
        match stream_loop(py) {
            Ok(l) => *loop_obj = Some(l),
            Err(_) => return,
        }
    }
    let l = loop_obj.as_ref().unwrap().bind(py);
    let _ = l.call_method1("run_until_complete", (aclose_coro,));
}

/// Capture a streaming exception onto `app._captured_server_exceptions` (so
/// TestClient `raise_server_exceptions=True` surfaces it), mirroring the
/// single-driver path. StopAsyncIteration is never passed here.
fn capture_or_eprint_stream_err(py: Python<'_>, e: &PyErr) {
    if let Some(app_obj) = crate::router::current_app(py) {
        if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
            let _ = lst.call_method1(py, "append", (e.value(py),));
        }
    }
    eprintln!("fastapi-turbo: inline streaming error: {e}");
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
