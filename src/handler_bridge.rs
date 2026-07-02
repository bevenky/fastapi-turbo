use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::OnceLock;

// ── Async worker: drives suspending Python coroutines on the shared worker loop ──

/// Cached reference to the Python `_async_worker.submit` function.
static ASYNC_SUBMIT: OnceLock<Py<PyAny>> = OnceLock::new();
/// Cached reference to `_async_worker.submit_fast` — positional entry point
/// (coro, timeout) used by the door's hot path. No kwargs dict, no per-request
/// `_default_timeout` resolution (the timeout was resolved at route build).
static ASYNC_SUBMIT_FAST: OnceLock<Py<PyAny>> = OnceLock::new();

/// The worker's event loop object (``_async_worker.get_loop()``) — used by the
/// async-inline path (`FASTAPI_TURBO_ASYNC_INLINE`) to `create_task` /
/// `call_later` from the loop thread itself.
static WORKER_LOOP: OnceLock<Py<PyAny>> = OnceLock::new();
/// Bound ``loop.call_soon_threadsafe`` — the async-inline enqueue entry point.
/// Cached once so the per-request enqueue is a single positional call.
static WORKER_CALL_SOON: OnceLock<Py<PyAny>> = OnceLock::new();

/// The worker's event loop (populated by `init_async_worker`).
pub fn worker_loop() -> Option<&'static Py<PyAny>> {
    WORKER_LOOP.get()
}

/// Bound `call_soon_threadsafe` on the worker's loop (populated by
/// `init_async_worker`).
pub fn worker_call_soon() -> Option<&'static Py<PyAny>> {
    WORKER_CALL_SOON.get()
}

/// Initialize the async worker (Python-managed thread with `run_forever()`).
pub fn init_async_worker() {
    if ASYNC_SUBMIT.get().is_some() {
        return;
    }
    Python::attach(|py| {
        let worker = py
            .import("fastapi_turbo._async_worker")
            .expect("_async_worker");
        worker.call_method0("init").expect("worker init");
        let submit = worker.getattr("submit").expect("submit").unbind();
        let submit_fast = worker.getattr("submit_fast").expect("submit_fast").unbind();
        // Cache the loop + bound call_soon_threadsafe BEFORE ASYNC_SUBMIT so a
        // racing thread that observes ASYNC_SUBMIT set also sees these.
        if let Ok(loop_obj) = worker.call_method0("get_loop") {
            if let Ok(call_soon) = loop_obj.getattr("call_soon_threadsafe") {
                let _ = WORKER_CALL_SOON.set(call_soon.unbind());
            }
            let _ = WORKER_LOOP.set(loop_obj.unbind());
        }
        let _ = ASYNC_SUBMIT_FAST.set(submit_fast);
        let _ = ASYNC_SUBMIT.set(submit);
    });
}

/// Submit a coroutine to the async worker and block until it completes.
/// The worker's Python `submit()` calls `run_coroutine_threadsafe` +
/// `future.result()` — Python's `future.result()` releases the GIL
/// internally while waiting, so the worker thread can drive the coroutine.
///
/// When ``app`` is supplied, we pass it as the ``app=`` kwarg so
/// Python-side ``_default_timeout(app)`` picks up this specific app's
/// ``worker_timeout`` rather than the class-level last-constructed
/// pointer. The Python wrapper ``_make_sync_wrapper(app=app)`` already
/// covers the common request path; this belt-and-suspenders plumbing
/// ensures any Rust submit — including WebSocket-bridge callers and
/// the needs-worker fallback — also carries per-app context.
fn submit_to_async_worker(
    py: Python<'_>,
    coro: Py<PyAny>,
    app: Option<&Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let submit = ASYNC_SUBMIT
        .get()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Async worker not initialized"))?;
    match app {
        None => submit.call1(py, (coro.bind(py),)),
        Some(app_ref) => {
            let kwargs = PyDict::new(py);
            kwargs.set_item("app", app_ref.bind(py))?;
            submit.call(py, (coro.bind(py),), Some(&kwargs))
        }
    }
}

/// Hot-path submit: positional `(coro, timeout)` into `_async_worker.submit_fast`.
/// The effective timeout was resolved ONCE at route build (env var →
/// `app.worker_timeout` → last-constructed fallback) and lives on the
/// RouteState — no per-request kwargs dict, no `_default_timeout` env read /
/// getattr chain under the GIL.
fn submit_to_async_worker_fast(
    py: Python<'_>,
    coro: Py<PyAny>,
    timeout: Option<f64>,
) -> PyResult<Py<PyAny>> {
    let submit = ASYNC_SUBMIT_FAST
        .get()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Async worker not initialized"))?;
    submit.call1(py, (coro.bind(py), timeout))
}

/// Run an async Python handler — tries FAST path first (same-thread), falls back to SLOW path (worker thread).
///
/// FAST path: thread-local event loop + run_until_complete on the CURRENT thread.
/// Zero cross-thread GIL transfers. Same speed as sync handlers.
/// Works for ALL async handlers — 1 await, 10 awaits, asyncio.gather, everything.
///
/// SLOW path (fallback): If handler's DB pool was created on a different event loop
/// (e.g., in on_event("startup")), we get an event loop mismatch error.
/// Fall back to the dedicated async worker thread.
///
/// Probe a coroutine with send(None); if it suspends, close it and build
/// a **fresh** coroutine via ``make_coro`` to submit to the async worker.
///
/// ``make_coro`` is a closure that produces a new coroutine object on
/// each call. Required for the worker fallback: a closed coroutine
/// cannot be resumed, so we must reinvoke the handler to get a fresh
/// one. Previous signature took a pre-built coro and reused it after
/// close — any async handler that suspended produced a 500 in the
/// worker-fallback path (e.g. WebSocket handler with ``await
/// asyncio.sleep(0)`` before ``accept()``).
pub fn drive_coroutine_on_local_loop_with_app<F>(
    py: Python<'_>,
    mut make_coro: F,
    app: Option<&Py<PyAny>>,
) -> PyResult<Py<PyAny>>
where
    F: FnMut(Python<'_>) -> PyResult<Py<PyAny>>,
{
    let coro = make_coro(py)?;
    match coro.call_method1(py, "send", (py.None(),)) {
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
            let v = e.value(py);
            return match v.getattr("value") {
                Ok(val) => Ok(val.unbind()),
                Err(_) => Ok(py.None()),
            };
        }
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py) => {
            let _ = coro.call_method0(py, "close");
        }
        Err(other) => return Err(other),
        Ok(_) => {
            let _ = coro.call_method0(py, "close");
        }
    }
    // Handler suspended — route to the dedicated async worker where
    // loop.run_forever() keeps background tasks (pool housekeeping) alive.
    init_async_worker();
    // Fresh coroutine for the worker — the probed one is closed and a
    // closed coroutine cannot be resumed.
    let fresh = make_coro(py)?;
    submit_to_async_worker(py, fresh, app)
}

/// Call an async handler with a single positional arg. Used by the WebSocket
/// bridge — the WS object is passed positionally so user code can rename the
/// parameter (vLLM uses `websocket`, others use `ws`).
pub fn call_async_on_local_loop_positional(
    py: Python<'_>,
    handler: &Py<PyAny>,
    arg: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    call_async_on_local_loop_positional_with_app(py, handler, arg, None)
}

pub fn call_async_on_local_loop_positional_with_app(
    py: Python<'_>,
    handler: &Py<PyAny>,
    arg: Py<PyAny>,
    app: Option<&Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let arg_clone = arg.clone_ref(py);
    drive_coroutine_on_local_loop_with_app(
        py,
        move |py| handler.call1(py, (arg_clone.bind(py),)),
        app,
    )
}

/// Call an async handler with a single positional arg + keyword args.
/// Used by the WebSocket bridge when the route has path params like /ws/{room_id}.
pub fn call_async_on_local_loop_positional_with_kwargs(
    py: Python<'_>,
    handler: &Py<PyAny>,
    arg: Py<PyAny>,
    kwargs: &pyo3::Bound<'_, PyDict>,
) -> PyResult<Py<PyAny>> {
    call_async_on_local_loop_positional_with_kwargs_and_app(py, handler, arg, kwargs, None)
}

pub fn call_async_on_local_loop_positional_with_kwargs_and_app(
    py: Python<'_>,
    handler: &Py<PyAny>,
    arg: Py<PyAny>,
    kwargs: &pyo3::Bound<'_, PyDict>,
    app: Option<&Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let arg_clone = arg.clone_ref(py);
    let kwargs_unbound: Py<PyDict> = kwargs.clone().unbind();
    drive_coroutine_on_local_loop_with_app(
        py,
        move |py| {
            let kw = kwargs_unbound.bind(py);
            handler.call(py, (arg_clone.bind(py),), Some(kw))
        },
        app,
    )
}

/// Handler classification — determined on the FIRST call, reused forever.
/// The slot is an `AtomicU8` living on the per-route state (RouteState for
/// handlers, ParamInfo for async deps) — no process-global `Mutex<HashMap>`
/// taken by every async request on every tokio thread. Stale values are
/// self-correcting: a "sync-fast" that suspends is reclassified + resubmitted;
/// a "needs-worker" that is actually fast still runs correctly on the worker.
pub const ASYNC_CLASS_UNKNOWN: u8 = 0;
pub const ASYNC_CLASS_SYNC_FAST: u8 = 1;
pub const ASYNC_CLASS_NEEDS_WORKER: u8 = 2;

pub fn call_async_on_local_loop_classified(
    py: Python<'_>,
    handler: &Py<PyAny>,
    kwargs: &Bound<'_, PyDict>,
    class_slot: &AtomicU8,
    timeout: Option<f64>,
) -> PyResult<Py<PyAny>> {
    match class_slot.load(Ordering::Relaxed) {
        ASYNC_CLASS_SYNC_FAST => {
            // === KNOWN SYNC-FAST: probe safely ===
            let coro = handler.call(py, (), Some(kwargs))?;
            match coro.call_method1(py, "send", (py.None(),)) {
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                    let v = e.value(py);
                    return match v.getattr("value") {
                        Ok(val) => Ok(val.unbind()),
                        Err(_) => Ok(py.None()),
                    };
                }
                _ => {
                    // Was fast, now isn't — reclassify as needs-worker.
                    class_slot.store(ASYNC_CLASS_NEEDS_WORKER, Ordering::Relaxed);
                    let _ = coro.call_method0(py, "close");
                }
            }
        }
        ASYNC_CLASS_NEEDS_WORKER => {
            // === KNOWN NEEDS-WORKER: skip probe, go straight to worker ===
            init_async_worker();
            let coro = handler.call(py, (), Some(kwargs))?;
            return submit_to_async_worker_fast(py, coro, timeout);
        }
        _ => {
            // === FIRST CALL: probe to classify ===
            let coro = handler.call(py, (), Some(kwargs))?;
            match coro.call_method1(py, "send", (py.None(),)) {
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                    // Sync-fast — mark and return.
                    class_slot.store(ASYNC_CLASS_SYNC_FAST, Ordering::Relaxed);
                    let v = e.value(py);
                    return match v.getattr("value") {
                        Ok(val) => Ok(val.unbind()),
                        Err(_) => Ok(py.None()),
                    };
                }
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py) => {
                    // No running event loop — mark as needs-worker.
                    class_slot.store(ASYNC_CLASS_NEEDS_WORKER, Ordering::Relaxed);
                    let _ = coro.call_method0(py, "close");
                }
                Err(other) => return Err(other),
                Ok(_yielded) => {
                    // Suspended — mark as needs-worker. On first call this
                    // is typically asyncpg.create_pool() — no connection
                    // acquired yet, safe to close.
                    class_slot.store(ASYNC_CLASS_NEEDS_WORKER, Ordering::Relaxed);
                    let _ = coro.call_method0(py, "close");
                }
            }
        }
    }

    // Fall through: route to async worker
    init_async_worker();
    let coro = handler.call(py, (), Some(kwargs))?;
    submit_to_async_worker_fast(py, coro, timeout)
}
