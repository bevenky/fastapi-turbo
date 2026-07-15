use axum::body::Body;
use axum::http::{HeaderMap, HeaderName, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use tokio::sync::mpsc;

use crate::responses::err_value_with_tb;

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
    // `getattr_opt`: raw async/sync generators MISS this attribute on every
    // request — the plain `getattr` materialized a full AttributeError
    // (instantiation + traceback machinery, ~1µs) per stream just to discard
    // it.
    if let Ok(Some(src)) = iter_bound.getattr_opt(pyo3::intern!(py, "_fastapi_turbo_sync_source")) {
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
    // loop-needing primitive (bytecode ``GET_AWAITABLE`` absence on the real
    // user gen). When true, the streaming task drives each ``__anext__`` with
    // a bare ``send(None)`` reaching ``StopIteration`` — pushing the chunk
    // INLINE, skipping the per-stream ``run_until_complete`` event-loop tax.
    // A gen that DOES await falls to the worker-loop driver (``_drive_stream``)
    // — unchanged behavior. Resolved here under the GIL we already hold, off
    // the streaming task's critical path. The probe MUST be conservative: a
    // bare ``send(None)`` on a loop-needing gen raises ``RuntimeError`` and
    // CORRUPTS it, so we only fast-path gens proven await-free.
    let noawait = is_async && stream_noawait_verdict(py, obj, &iter_bound);

    // Create a channel-backed stream up front — the inline budget-drain fills
    // it directly so no re-queue copy is ever needed.
    let (tx, rx) = mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(32);

    let legacy_forced = stream_thread_forced();

    // Inline budget-drain (the one-write fast path): short bodies from sync
    // generators and PROVEN no-await async generators are fully drained into
    // the channel HERE, and the Sender is dropped BEFORE `into_response` —
    // hyper then sees headers + every data frame + EOF in one poll cycle and
    // emits them as a SINGLE vectored write (verified raw-axum/Fastify wire
    // behavior: 1 write syscall, 1 client read). This kills the header/body
    // write split, the close→EOF gap, AND the per-stream driver dispatch for
    // the common short-stream case. Bounds: channel capacity (32 chunks) and
    // a wall-clock budget (~100µs) — anything left over falls to the existing
    // drivers unchanged. `FASTAPI_TURBO_STREAM_THREAD=1` skips the inline
    // drain entirely (legacy single-chunk pre-drain + dedicated thread).
    let mut drained_complete = false;
    // An `__anext__` coro that unexpectedly SUSPENDED during the no-await
    // inline drain (defense-in-depth; the bytecode verdict means this should
    // never fire). Handed to the blocking driver, which resumes it via
    // `_resume_anext` on its thread-local `stream_loop` — the SAME
    // conservative fallback the legacy inline driver uses.
    let mut pending_coro: Option<Py<PyAny>> = None;

    if legacy_forced {
        // Legacy behavior, unchanged: pre-drain ONE chunk for sync iterators
        // so hyper coalesces headers + first frame; the dedicated thread does
        // the rest.
        if !is_async {
            if let Some(chunk) = drain_one_sync_chunk(&iter_bound) {
                // try_send never blocks here — channel is empty (capacity 32).
                let _ = tx.try_send(Ok(chunk));
            }
        }
    } else if !is_async {
        drained_complete = inline_drain_sync(&iter_bound, &tx);
    } else if noawait {
        match inline_drain_noawait(py, &iter_bound, &tx) {
            InlineDrain::Complete => drained_complete = true,
            InlineDrain::Leftover => {}
            InlineDrain::Suspended(coro) => pending_coro = Some(coro),
        }
    }

    // Runtime-cooperative classification key for await-streams: the REAL
    // user gen's code object (stamped for teardown-wrapped responses; the
    // wrapper's own code object is shared across every wrapped route and
    // would alias verdicts). Resolved while `iter_bound` is still bound.
    let coop_code: Option<Py<PyAny>> =
        if !drained_complete && is_async && !noawait && !legacy_forced && trampoline_supported() {
            stream_code_key(py, obj, &iter_bound)
        } else {
            None
        };

    let iterator: Py<PyAny> = iter_bound.unbind();

    // Mechanism 3 (inline trampoline): a gen the worker loop has PROVEN
    // cooperative at runtime (one full eager completion without ever
    // yielding to the loop — e.g. `await sleep(0)` checkpoints only) is
    // driven right here on the request thread via an eager task on a
    // private non-running loop. Zero cross-thread hops: the w18-fleet
    // profile showed the two wakes (enqueue→loop, channel→hyper) — not CPU
    // — capping await-stream throughput at ~half the sync-stream ceiling.
    // A misprediction (data-dependent real await) is finished correctly via
    // `run_until_complete` and demotes the gen back to the worker loop.
    let mut trampolined = false;
    if let Some(code) = &coop_code {
        if coop_state_of(code.as_ptr() as usize) == Some(CoopState::Cooperative) {
            trampolined = run_stream_trampoline(py, &iterator, &tx, code);
        }
    }

    // Mechanism 2: an async gen with REAL awaits multiplexes as a TASK on the
    // shared `_async_worker` loop — no per-stream thread, no per-stream event
    // loop, no `run_until_complete` machinery (~122µs → the loop amortizes it
    // across every in-flight stream). Falls back to the legacy dedicated-
    // thread driver when the loop is unavailable (closed / no tokio runtime
    // context) or when `FASTAPI_TURBO_STREAM_THREAD=1` forces it. Passes the
    // classification key along so `_spawn_stream_task`'s eager-done signal
    // can record the gen's runtime-coop verdict for Mechanism 3.
    let worker_scheduled = !drained_complete
        && !trampolined
        && is_async
        && !noawait
        && !legacy_forced
        && schedule_stream_on_worker_loop(py, &iterator, &tx, coop_code);

    // Legacy paths: spawn a blocking task that iterates the Python generator
    // and pushes the remaining chunks through the channel. (Sync gens and
    // proven-no-await async gens with leftover chunks take this path.)
    if !drained_complete && !trampolined && !worker_scheduled {
        tokio::task::spawn_blocking(move || {
            Python::attach(|py| {
                if is_async {
                    if noawait {
                        iterate_async_generator_inline(py, &iterator, &tx, pending_coro);
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
    // When `drained_complete`, `tx` is dropped as this function returns —
    // the channel is already closed by the time hyper polls the body, so the
    // whole response (headers + coalesced chunks + EOF) leaves in one write.

    let body = Body::from_stream(CoalescingReceiver::new(rx));

    (status, headers, body).into_response()
}

/// Wall-clock budget for the inline drain at create-time. Bounds the extra
/// time-to-headers a multi-chunk generator can add; a SINGLE slow step can
/// still exceed it (same exposure `drain_one_sync_chunk` always had for
/// chunk #1) — the budget is checked between steps.
const INLINE_DRAIN_BUDGET: std::time::Duration = std::time::Duration::from_micros(100);

/// Outcome of the no-await inline drain.
enum InlineDrain {
    /// Generator exhausted (or errored — captured onto the app first): the
    /// caller drops the Sender before `into_response` → one-write response.
    Complete,
    /// Capacity/budget hit: chunks so far are queued; the leftover iterator
    /// continues on the existing driver.
    Leftover,
    /// A bare `send(None)` unexpectedly suspended — this ALREADY-STARTED
    /// `__anext__` coro must be resumed via `_resume_anext` (never re-sent
    /// from the front) before normal iteration continues.
    Suspended(Py<PyAny>),
}

/// Budget-drain a SYNC iterator into the body channel at create-time.
/// Returns `true` when the iterator finished (exhausted or errored+captured)
/// so the caller can close the channel before building the response. Skips
/// plain iterables (no `__next__`) — same rule as `drain_one_sync_chunk`:
/// the driver's `__iter__()` would restart them and duplicate chunks.
fn inline_drain_sync(
    iter_bound: &Bound<'_, PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) -> bool {
    let py = iter_bound.py();
    if !iter_bound
        .hasattr(pyo3::intern!(py, "__next__"))
        .unwrap_or(false)
    {
        return false;
    }
    let start = std::time::Instant::now();
    loop {
        // Reserve room BEFORE stepping the generator: nothing consumes the
        // channel until hyper polls the body (after we return), so a full
        // channel here means the leftover driver must take over.
        if tx.capacity() == 0 {
            return false;
        }
        match iter_bound.call_method0(pyo3::intern!(py, "__next__")) {
            Ok(val) => {
                let _ = tx.try_send(Ok(python_val_to_bytes(&val)));
                if start.elapsed() >= INLINE_DRAIN_BUDGET {
                    return false;
                }
            }
            Err(e) => {
                if !e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                    // Capture-then-close ordering: the exception lands on the
                    // app HERE; the Sender drops when create returns.
                    if let Some(app_obj) = crate::router::current_app(py) {
                        if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
                            let _ = lst.call_method1(py, "append", (err_value_with_tb(py, &e),));
                        }
                    }
                }
                return true;
            }
        }
    }
}

/// Budget-drain a PROVEN no-await async generator into the body channel at
/// create-time, driving each `__anext__` with a bare `send(None)` (no event
/// loop) exactly like `iterate_async_generator_inline` — just on the request
/// thread, under the GIL we already hold, with capacity/budget bounds.
fn inline_drain_noawait(
    py: Python<'_>,
    iter_bound: &Bound<'_, PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
) -> InlineDrain {
    let aiter = match iter_bound.call_method0(pyo3::intern!(py, "__aiter__")) {
        Ok(a) => a,
        Err(e) => {
            capture_or_eprint_stream_err(py, &e);
            return InlineDrain::Complete;
        }
    };
    let start = std::time::Instant::now();
    loop {
        if tx.capacity() == 0 {
            return InlineDrain::Leftover;
        }
        let coro = match aiter.call_method0(pyo3::intern!(py, "__anext__")) {
            Ok(c) => c,
            Err(e) => {
                capture_or_eprint_stream_err(py, &e);
                return InlineDrain::Complete;
            }
        };
        match coro.call_method1(pyo3::intern!(py, "send"), (py.None(),)) {
            // SUSPENDED (should never happen for a proven no-await gen):
            // hand the started coro to the blocking driver's conservative
            // `_resume_anext`-on-`stream_loop` fallback — resuming from the
            // suspension point is the only correct continuation.
            Ok(_yielded) => return InlineDrain::Suspended(coro.unbind()),
            Err(e) => {
                if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                    // Normal per-chunk completion: chunk is the StopIteration value.
                    match e.value(py).getattr(pyo3::intern!(py, "value")) {
                        Ok(v) if !v.is_none() => {
                            let _ = tx.try_send(Ok(python_val_to_bytes(&v)));
                        }
                        // StopIteration() with no value → empty chunk, skip.
                        _ => {}
                    }
                    if start.elapsed() >= INLINE_DRAIN_BUDGET {
                        return InlineDrain::Leftover;
                    }
                } else if e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                    return InlineDrain::Complete; // gen exhausted
                } else {
                    // RuntimeError (destructive bare-send on a loop-needing
                    // await) or a mid-stream raise — capture-then-close, same
                    // as the inline driver.
                    capture_or_eprint_stream_err(py, &e);
                    return InlineDrain::Complete;
                }
            }
        }
    }
}

/// Body-channel consumer that coalesces back-to-back-READY chunks.
///
/// The stream drivers (worker-loop `_drive_stream`, inline no-await, sync
/// generator loop) often push several small chunks between two hyper wakeups
/// — e.g. a cooperative-yield SSE gen emits its whole burst while the
/// consumer task is still scheduled. Emitting each as its own `Bytes` costs
/// one body-frame poll + chunked-encoding write per chunk. After one chunk
/// arrives, opportunistically `try_recv` chunks that are ALREADY queued and
/// emit them as a single `Bytes`.
///
/// Latency rule: NEVER wait for more chunks — only what is already buffered
/// is batched (`try_recv`, no extra poll registration). A lone chunk is
/// forwarded zero-copy (no `BytesMut` round trip). `COALESCE_MAX` caps the
/// batch so large-chunk streams (file bodies) keep their zero-copy
/// forwarding instead of paying a memcpy.
struct CoalescingReceiver {
    rx: mpsc::Receiver<Result<bytes::Bytes, std::io::Error>>,
    /// Item pulled by `try_recv` during a drain that must be emitted on the
    /// NEXT poll (an error chunk; data order is preserved).
    pending: Option<Result<bytes::Bytes, std::io::Error>>,
    /// Sender side observed closed during a drain — emit EOF on the next poll.
    done: bool,
}

/// Stop batching once the coalesced buffer reaches this size; bigger chunks
/// are cheaper to forward as-is than to memcpy (SSE/token streams are far
/// below this, file streams far above).
const COALESCE_MAX: usize = 16 * 1024;

impl CoalescingReceiver {
    fn new(rx: mpsc::Receiver<Result<bytes::Bytes, std::io::Error>>) -> Self {
        Self {
            rx,
            pending: None,
            done: false,
        }
    }
}

impl tokio_stream::Stream for CoalescingReceiver {
    type Item = Result<bytes::Bytes, std::io::Error>;

    fn poll_next(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Self::Item>> {
        use std::task::Poll;

        let this = self.get_mut();
        if let Some(item) = this.pending.take() {
            return Poll::Ready(Some(item));
        }
        if this.done {
            return Poll::Ready(None);
        }
        let first = match this.rx.poll_recv(cx) {
            Poll::Pending => return Poll::Pending,
            Poll::Ready(None) => return Poll::Ready(None),
            Poll::Ready(Some(Err(e))) => return Poll::Ready(Some(Err(e))),
            Poll::Ready(Some(Ok(chunk))) => chunk,
        };
        if first.len() >= COALESCE_MAX {
            return Poll::Ready(Some(Ok(first)));
        }
        // Batch chunks that are already queued. `buf` is only materialized
        // once a SECOND chunk exists — the lone-chunk fast path stays
        // zero-copy.
        let mut buf: Option<bytes::BytesMut> = None;
        loop {
            match this.rx.try_recv() {
                Ok(Ok(next)) => {
                    let b = buf.get_or_insert_with(|| bytes::BytesMut::from(first.as_ref()));
                    b.extend_from_slice(&next);
                    if b.len() >= COALESCE_MAX {
                        break;
                    }
                }
                Ok(Err(e)) => {
                    // Emit batched data first; the error keeps its position.
                    this.pending = Some(Err(e));
                    break;
                }
                Err(mpsc::error::TryRecvError::Empty) => break,
                Err(mpsc::error::TryRecvError::Disconnected) => {
                    this.done = true;
                    break;
                }
            }
        }
        match buf {
            Some(b) => Poll::Ready(Some(Ok(b.freeze()))),
            None => Poll::Ready(Some(Ok(first))),
        }
    }
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
                        let _ = lst.call_method1(py, "append", (err_value_with_tb(py, &e),));
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
                        let _ = lst.call_method1(py, "append", (err_value_with_tb(py, &e),));
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

// ═══ Mechanism 3: runtime-cooperative await-streams drive INLINE on the
// request thread (zero cross-thread hops) ═══
//
// The w18-fleet profile: /stream-await capped at 59.7k rps burning 7.0 cores
// while /stream-sync did 111k on 4.9 — box not saturated, worker-loop thread
// at 35% duty. The cap was the CRITICAL PATH: every await-stream request
// serialized two cross-thread wakes (call_soon_threadsafe → loop thread,
// then body channel → hyper task), each inflating to hundreds of µs of wall
// (and real CPU: kqueue wakes + context switches + GIL handoffs) under fleet
// thread pressure.
//
// A gen like `await sleep(0); yield chunk` can't be bytecode-proven no-await
// (`GET_AWAITABLE` present), and a bare `send(None)` probe would CORRUPT a
// gen that really awaits. But the worker loop already produces a safe
// runtime proof for free: `_spawn_stream_task`'s eager start either runs the
// WHOLE driver to completion inside one call (the gen never yielded to the
// loop — cooperative) or leaves the task pending (real awaits). That verdict
// is recorded per CODE OBJECT here; proven-cooperative gens skip the worker
// loop entirely on subsequent requests and drive inline on the request
// thread via `_drive_stream_inline` — an eager task on a PRIVATE non-running
// per-thread asyncio loop (poked to look running; see the Python helper).
// The eager step usually completes the whole body before `into_response`,
// so hyper sees headers + chunks + EOF in one poll — the sync-stream shape.
//
// A misprediction (a data-dependent REAL await in a previously-cooperative
// gen) is never destructive: the eager step suspends on a Future of the
// private loop, and `run_until_complete(task)` finishes the stream correctly
// right there (blocking this request thread — the legacy driver's semantic),
// then the code object is STICKILY demoted to the worker-loop path.

/// Runtime verdict for an await-stream generator's code object.
#[derive(Clone, Copy, PartialEq, Eq)]
enum CoopState {
    /// One full eager completion observed — drives inline (Mechanism 3).
    Cooperative,
    /// Suspended on a real await at least once — stays on the worker loop.
    Awaiting,
}

/// `FASTAPI_TURBO_STREAM_TRAMPOLINE=0` disables Mechanism 3 (default ON).
fn stream_trampoline_enabled() -> bool {
    static FLAG: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    *FLAG.get_or_init(|| {
        std::env::var("FASTAPI_TURBO_STREAM_TRAMPOLINE")
            .map(|v| !matches!(v.trim(), "0" | "false" | "False" | "FALSE" | "no" | "off"))
            .unwrap_or(true)
    })
}

/// Set once when the runtime can't support the trampoline (Python < 3.12
/// eager_start, or an event-loop class without `_thread_id`) — cheaper than
/// re-raising per stream.
static TRAMPOLINE_BROKEN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn trampoline_supported() -> bool {
    stream_trampoline_enabled() && !TRAMPOLINE_BROKEN.load(std::sync::atomic::Ordering::Relaxed)
}

/// code-object ptr → (verdict, pinned code ref). Pinning the `Py<PyAny>`
/// keeps the id from ever aliasing a freed code object (same discipline as
/// the no-await verdict map).
fn coop_map() -> &'static std::sync::Mutex<std::collections::HashMap<usize, (CoopState, Py<PyAny>)>>
{
    static MAP: std::sync::OnceLock<
        std::sync::Mutex<std::collections::HashMap<usize, (CoopState, Py<PyAny>)>>,
    > = std::sync::OnceLock::new();
    MAP.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

fn coop_state_of(key: usize) -> Option<CoopState> {
    coop_map().lock().ok()?.get(&key).map(|(s, _)| *s)
}

/// Record a verdict. First writer wins (concurrent first requests race
/// benignly); the ONLY overwrite allowed is the trampoline's misprediction
/// demote (`Cooperative` → `Awaiting`, `allow_demote`). Never promotes
/// `Awaiting` back — a once-suspending gen must not re-earn thread-blocking
/// inline drives from a lucky cooperative run.
fn record_coop_verdict(py: Python<'_>, code: &Py<PyAny>, verdict: CoopState, allow_demote: bool) {
    let key = code.as_ptr() as usize;
    if let Ok(mut map) = coop_map().lock() {
        use std::collections::hash_map::Entry;
        match map.entry(key) {
            Entry::Occupied(mut e) => {
                if allow_demote && verdict == CoopState::Awaiting {
                    e.get_mut().0 = CoopState::Awaiting;
                }
            }
            Entry::Vacant(v) => {
                v.insert((verdict, code.clone_ref(py)));
            }
        }
    }
}

/// Classification key: the REAL user gen's code object. A stamped
/// `_fastapi_turbo_stream_code` (set by `_door_wrap_stream_teardown`
/// alongside the no-await verdict) wins — the teardown wrapper's own
/// `ag_code` is one shared code object for EVERY wrapped route. Unstamped
/// responses use the raw gen's `ag_code`. `None` (custom async iterators,
/// no code object to key on) → no trampoline, unchanged behavior.
fn stream_code_key(
    py: Python<'_>,
    response: &Bound<'_, PyAny>,
    iter_bound: &Bound<'_, PyAny>,
) -> Option<Py<PyAny>> {
    if let Ok(Some(code)) = response.getattr_opt(pyo3::intern!(py, "_fastapi_turbo_stream_code")) {
        if !code.is_none() {
            return Some(code.unbind());
        }
    }
    if let Ok(Some(code)) = iter_bound.getattr_opt(pyo3::intern!(py, "ag_code")) {
        if !code.is_none() {
            return Some(code.unbind());
        }
    }
    None
}

/// Thread-local holder that CLOSES the trampoline loop when its thread dies.
/// Tokio reaps idle blocking-pool threads; an unclosed `BaseEventLoop` being
/// deallocated then emits `ResourceWarning: unclosed event loop` from
/// `__del__` — which pytest's unraisable-exception detector turns into a
/// failure of whatever unrelated test happens to be running (observed as a
/// rotating upstream-suite victim). Closing on thread exit is safe: pool
/// threads die mid-process (interpreter live), and the loop is never running
/// outside `run_stream_trampoline`'s stack frames on this same thread.
struct TrampLoopCell(Option<Py<PyAny>>);

impl Drop for TrampLoopCell {
    fn drop(&mut self) {
        if let Some(l) = self.0.take() {
            let _ = Python::attach(|py| l.call_method0(py, "close"));
        }
    }
}

/// Per-request-thread PRIVATE event loop for the inline trampoline. Plain
/// asyncio `SelectorEventLoop` on purpose (never uvloop, and deliberately
/// bypassing the policy): `_drive_stream_inline` pokes `_thread_id`, a
/// `BaseEventLoop` implementation detail, and the misprediction fallback's
/// `run_until_complete` is ~2x cheaper on asyncio than uvloop. NOT installed
/// via `set_event_loop` — nothing outside the drive may observe it.
fn trampoline_loop(py: Python<'_>) -> PyResult<Py<PyAny>> {
    use std::cell::RefCell;
    thread_local! {
        static TRAMP_LOOP: RefCell<TrampLoopCell> = const { RefCell::new(TrampLoopCell(None)) };
    }
    TRAMP_LOOP.with(|cell| -> PyResult<Py<PyAny>> {
        let mut holder = cell.borrow_mut();
        if holder.0.is_none() {
            let asyncio = py.import("asyncio")?;
            let new_loop = match asyncio.getattr("SelectorEventLoop") {
                Ok(cls) => cls.call0()?,
                Err(_) => asyncio.call_method0("new_event_loop")?,
            };
            if !new_loop.hasattr("_thread_id").unwrap_or(false) {
                TRAMPOLINE_BROKEN.store(true, std::sync::atomic::Ordering::Relaxed);
                let _ = new_loop.call_method0("close");
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "trampoline loop lacks _thread_id",
                ));
            }
            holder.0 = Some(new_loop.unbind());
        }
        Ok(holder.0.as_ref().unwrap().clone_ref(py))
    })
}

/// Cached `fastapi_turbo.responses._drive_stream_inline` (the eager-start
/// trampoline helper).
fn drive_stream_inline_fn(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static INLINE: std::sync::OnceLock<Py<PyAny>> = std::sync::OnceLock::new();
    if let Some(f) = INLINE.get() {
        return Ok(f);
    }
    let f = py
        .import("fastapi_turbo.responses")?
        .getattr("_drive_stream_inline")?
        .unbind();
    let _ = INLINE.set(f);
    Ok(INLINE.get().expect("just set"))
}

/// Drive a runtime-proven-cooperative await-stream INLINE on the request
/// thread. Returns `true` when the stream was fully handled here (eagerly
/// completed, or mispredicted → finished via `run_until_complete` +
/// demoted); `false` when the trampoline couldn't run at all — the coroutine
/// never started and the iterator is untouched, so the caller falls through
/// to the worker-loop/legacy paths.
fn run_stream_trampoline(
    py: Python<'_>,
    iterator: &Py<PyAny>,
    tx: &mpsc::Sender<Result<bytes::Bytes, std::io::Error>>,
    code: &Py<PyAny>,
) -> bool {
    // The (rare) backpressure waiter needs a runtime handle to spawn onto.
    let Ok(rt) = tokio::runtime::Handle::try_current() else {
        return false;
    };
    let Ok(loop_obj) = trampoline_loop(py) else {
        return false;
    };
    let Ok(call_soon) = loop_obj.getattr(py, "call_soon_threadsafe") else {
        return false;
    };
    let Ok(push) = Py::new(
        py,
        LoopChunkPush {
            tx: Some(tx.clone()),
            rt,
            private_loop: Some((loop_obj.clone_ref(py), call_soon)),
        },
    ) else {
        return false;
    };
    let Ok(drive) = drive_stream_fn(py) else {
        return false;
    };
    // fair=False: this driver owns a private loop — no other task to yield
    // to, and the fairness checkpoint would spuriously suspend >64-chunk
    // cooperative streams out of eager completion.
    let Ok(coro) = drive.call1(py, (iterator.bind(py), push.bind(py), false)) else {
        return false;
    };
    let helper = match drive_stream_inline_fn(py) {
        Ok(h) => h,
        Err(_) => {
            let _ = coro.call_method0(py, "close");
            return false;
        }
    };
    let task = match helper.call1(py, (loop_obj.bind(py), coro.bind(py))) {
        Ok(t) => t,
        Err(e) => {
            // TypeError → Python < 3.12 (no eager_start); AttributeError →
            // no `_thread_id`. Both are permanent for this process. Any
            // constructor failure happens before the coroutine body ran —
            // closing it leaves the iterator untouched for the fallback.
            if e.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
                || e.is_instance_of::<pyo3::exceptions::PyAttributeError>(py)
            {
                TRAMPOLINE_BROKEN.store(true, std::sync::atomic::Ordering::Relaxed);
            } else {
                eprintln!("fastapi-turbo: stream trampoline start failed: {e}");
            }
            let _ = coro.call_method0(py, "close");
            return false;
        }
    };
    let app = crate::router::current_app(py);
    let done = task
        .call_method0(py, pyo3::intern!(py, "done"))
        .and_then(|d| d.extract::<bool>(py))
        .unwrap_or(false);
    if !done {
        // MISPREDICTION: the gen really awaited this time. Demote FIRST so
        // concurrent requests stop trampolining, then finish THIS stream —
        // the suspension's Future lives on our private loop, so running that
        // loop is the only correct continuation. Blocks this request thread
        // for the stream's duration (the legacy dedicated-thread semantic,
        // paid at most once per code object).
        record_coop_verdict(py, code, CoopState::Awaiting, true);
        if let Err(e) = loop_obj.call_method1(py, "run_until_complete", (task.bind(py),)) {
            if !e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                capture_stream_err_on_app(py, app.as_ref(), &e);
                eprintln!("fastapi-turbo: trampoline streaming error: {e}");
            }
        }
        push.borrow_mut(py).tx.take();
        return true;
    }
    // Eagerly complete. Mirror `StreamCompleter`: capture a mid-stream raise
    // onto the app BEFORE dropping the Sender (TestClient must see the error
    // by the time the body ends). On clean completion the driver already
    // dropped the Sender via `push.close()`; the take below is idempotent.
    let cancelled = task
        .call_method0(py, pyo3::intern!(py, "cancelled"))
        .and_then(|v| v.extract::<bool>(py))
        .unwrap_or(false);
    if !cancelled {
        if let Err(e) = task.call_method0(py, pyo3::intern!(py, "result")) {
            if !e.is_instance_of::<pyo3::exceptions::PyStopAsyncIteration>(py) {
                capture_stream_err_on_app(py, app.as_ref(), &e);
                eprintln!("fastapi-turbo: trampoline streaming error: {e}");
            }
        }
    }
    push.borrow_mut(py).tx.take();
    true
}

/// No-await verdict for an async streaming body, read as one precomputed
/// flag instead of a per-request Python call (`_stream_is_noawait` cost:
/// module import + getattr + call frame + getattr chain + dict ops, ~1.5-2µs
/// on the TTFB hot path). Order mirrors the Python helper exactly:
///
///   1. A stamped `_fastapi_turbo_stream_noawait` bool on the RESPONSE wins —
///      set once by `_door_wrap_stream_teardown` from the WRAPPED user gen. A
///      teardown wrapper's own bytecode has no `GET_AWAITABLE` (it only does
///      `async for ... yield`), so it must NEVER be bytecode-analyzed; the
///      stamp carries the real gen's verdict. `getattr_opt` — no exception
///      materialized on the (common) unstamped miss.
///   2. Otherwise the verdict is keyed on the generator's CODE OBJECT in a
///      Rust-side map — one getattr + one hash lookup per request. On a cold
///      code object, `_gen_is_noawait` (Python, dis-based) runs ONCE; the map
///      entry pins the code object (`Py<PyAny>` ref) so a freed code object's
///      id can never alias a stale verdict.
fn stream_noawait_verdict(
    py: Python<'_>,
    response: &Bound<'_, PyAny>,
    iter_bound: &Bound<'_, PyAny>,
) -> bool {
    use std::collections::HashMap;
    use std::sync::{Mutex, OnceLock};

    // 1. Response-stamped verdict (teardown-wrapped streams).
    if let Ok(Some(flag)) = response.getattr_opt(pyo3::intern!(py, "_fastapi_turbo_stream_noawait"))
    {
        if !flag.is_none() {
            return flag.extract::<bool>().unwrap_or(false);
        }
    }

    // 2. Code-object-keyed cache. No ag_code → not an async generator we can
    //    prove anything about → conservative false (same as _gen_is_noawait).
    let Ok(Some(code)) = iter_bound.getattr_opt(pyo3::intern!(py, "ag_code")) else {
        return false;
    };
    if code.is_none() {
        return false;
    }
    static VERDICTS: OnceLock<Mutex<HashMap<usize, (bool, Py<PyAny>)>>> = OnceLock::new();
    let cache = VERDICTS.get_or_init(|| Mutex::new(HashMap::new()));
    let key = code.as_ptr() as usize;
    if let Ok(map) = cache.lock() {
        if let Some((verdict, _)) = map.get(&key) {
            return *verdict;
        }
    }
    // Cold code object: one Python-side dis analysis, then memoize.
    let verdict = py
        .import("fastapi_turbo.responses")
        .and_then(|m| m.getattr("_gen_is_noawait"))
        .and_then(|f| f.call1((iter_bound,)))
        .and_then(|r| r.extract::<bool>())
        .unwrap_or(false);
    if let Ok(mut map) = cache.lock() {
        map.insert(key, (verdict, code.unbind()));
    }
    verdict
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
    pyo3::types::PyBool::new(py, v)
        .to_owned()
        .into_any()
        .unbind()
}

/// Capture a streaming exception onto the GIVEN app's
/// `_captured_server_exceptions` (TestClient `raise_server_exceptions=True`
/// parity). The app is resolved ONCE at stream-creation time on the request
/// thread — the worker loop thread has no per-thread `CURRENT_APP` binding,
/// so resolving there could hit the wrong app in multi-server processes.
fn capture_stream_err_on_app(py: Python<'_>, app: Option<&Py<PyAny>>, e: &PyErr) {
    if let Some(app_obj) = app {
        if let Ok(lst) = app_obj.getattr(py, "_captured_server_exceptions") {
            let _ = lst.call_method1(py, "append", (err_value_with_tb(py, e),));
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
    /// Trampoline mode (Mechanism 3): backpressure futures are created on —
    /// and resolved via — the request thread's PRIVATE loop instead of the
    /// shared worker loop: `(loop, its call_soon_threadsafe)`. The future is
    /// then awaited under that loop's `run_until_complete` misprediction
    /// fallback (a non-running loop still queues `call_soon_threadsafe`
    /// callbacks and wakes its self-pipe correctly).
    private_loop: Option<(Py<PyAny>, Py<PyAny>)>,
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
                // the thread that owns the target loop (worker-loop task or
                // trampoline eager step), so `create_future()` is loop-safe.
                let (loop_obj, call_soon) = match self.private_loop.as_ref() {
                    Some((l, cs)) => (l.clone_ref(py), cs.clone_ref(py)),
                    None => (
                        crate::handler_bridge::worker_loop()
                            .ok_or_else(|| {
                                pyo3::exceptions::PyRuntimeError::new_err(
                                    "async worker loop not initialized",
                                )
                            })?
                            .clone_ref(py),
                        crate::handler_bridge::worker_call_soon()
                            .ok_or_else(|| {
                                pyo3::exceptions::PyRuntimeError::new_err(
                                    "async worker loop not initialized",
                                )
                            })?
                            .clone_ref(py),
                    ),
                };
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
                        let _ =
                            call_soon.call1(py, (resolver.bind(py), fut_for_waiter.bind(py), ok));
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
    /// Classification key (the REAL gen's code object) for the runtime-coop
    /// verdict recorded off `_spawn_stream_task`'s eager-done signal.
    code: Option<Py<PyAny>>,
}

#[pymethods]
impl StreamJob {
    fn __call__(&mut self, py: Python<'_>) {
        let (Some(coro), Some(push)) = (self.coro.take(), self.push.take()) else {
            return;
        };
        if let Err(e) = start_stream_task_on_loop(py, &coro, &push, &self.app, self.code.take()) {
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
    code: Option<Py<PyAny>>,
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
    match spawner.call1(py, (loop_obj.bind(py), coro.bind(py), completer.bind(py))) {
        Ok(eager_clean) => {
            // Runtime-coop verdict (Mechanism 3): True ⇔ the eager start ran
            // the whole stream to clean completion — the gen never yielded
            // to the loop. Vacant-only writes: the trampoline's demote is
            // the single allowed overwrite.
            if let Some(code) = code {
                let verdict = if eager_clean.extract::<bool>(py).unwrap_or(false) {
                    CoopState::Cooperative
                } else {
                    CoopState::Awaiting
                };
                record_coop_verdict(py, &code, verdict, false);
            }
            Ok(())
        }
        Err(e) => {
            // Task construction failed before it took ownership — close the
            // coroutine (no-op if an eager step already finished it; throws
            // GeneratorExit for teardown if it half-started). The caller then
            // captures the error and closes the body channel.
            let _ = coro.call_method0(py, "close");
            Err(e)
        }
    }
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
    code: Option<Py<PyAny>>,
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
            private_loop: None,
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
                code,
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
        Ok(drive
            .call1((iterator.bind(py), push.clone_ref(py)))?
            .unbind())
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
                    let _ = lst.call_method1(py, "append", (err_value_with_tb(py, &e),));
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
    pending: Option<Py<PyAny>>,
) {
    let aiter = match iterator
        .bind(py)
        .call_method0(pyo3::intern!(py, "__aiter__"))
    {
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

    // An `__anext__` coro the create-time inline drain left SUSPENDED —
    // resume it from its suspension point first (never re-send from the
    // front), push its chunk, then continue normal iteration.
    if let Some(started) = pending {
        let started_bound = started.bind(py);
        match resume_suspended_anext(py, started_bound, &mut loop_obj, &mut resume_helper) {
            Ok(Some(chunk)) => {
                let alive = py.detach(|| tx.blocking_send(Ok(chunk)).is_ok());
                if !alive {
                    close_aiter(py, &aiter, &mut loop_obj);
                    return;
                }
            }
            Ok(None) => return, // StopAsyncIteration — done
            Err(e) => {
                capture_or_eprint_stream_err(py, &e);
                return;
            }
        }
    }

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
                match resume_suspended_anext(py, &coro, &mut loop_obj, &mut resume_helper) {
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
            let _ = lst.call_method1(py, "append", (err_value_with_tb(py, e),));
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
