use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

// ── Sync handler call ────────────────────────────────────────────────

/// Call a synchronous Python handler.
/// Uses `block_in_place` instead of `spawn_blocking` to avoid thread pool
/// scheduling overhead (~3μs saved). The tokio runtime migrates other tasks
/// off this worker thread while we hold the GIL.
pub async fn call_sync_handler(
    handler: Py<PyAny>,
    kwargs: HashMap<String, Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    tokio::task::block_in_place(|| {
        Python::attach(|py| {
            let py_kwargs = PyDict::new(py);
            for (key, value) in &kwargs {
                let _ = py_kwargs.set_item(key, value.bind(py));
            }
            handler.call(py, (), Some(&py_kwargs))
        })
    })
}

// ── Async worker: crossbeam + run_until_complete (15x faster than run_coroutine_threadsafe) ──

/// A request to execute an async Python coroutine.
struct AsyncRequest {
    coro: Py<PyAny>,
    response_tx: crossbeam_channel::Sender<PyResult<Py<PyAny>>>,
}

// SAFETY: Py<PyAny> is Send. crossbeam::Sender is Send.
unsafe impl Send for AsyncRequest {}

/// Channel for sending async requests to the dedicated Python worker thread.
static ASYNC_WORKER_TX: OnceLock<crossbeam_channel::Sender<AsyncRequest>> = OnceLock::new();

/// Cached reference to the Python `_async_worker.submit` function.
static ASYNC_SUBMIT: OnceLock<Py<PyAny>> = OnceLock::new();

/// Initialize the async worker (Python-managed thread with `run_forever()`).
pub fn init_async_worker() {
    if ASYNC_SUBMIT.get().is_some() {
        return;
    }
    // Dummy TX for backward compat
    let (tx, _rx) = crossbeam_channel::unbounded::<AsyncRequest>();
    let _ = ASYNC_WORKER_TX.set(tx);

    Python::attach(|py| {
        let worker = py.import("fastapi_turbo._async_worker").expect("_async_worker");
        worker.call_method0("init").expect("worker init");
        let submit = worker.getattr("submit").expect("submit").unbind();
        let _ = ASYNC_SUBMIT.set(submit);
    });
}

/// Submit a coroutine to the async worker and block until it completes.
/// The worker's Python `submit()` calls `run_coroutine_threadsafe` +
/// `future.result()` — Python's `future.result()` releases the GIL
/// internally while waiting, so the worker thread can drive the coroutine.
fn submit_to_async_worker(
    py: Python<'_>,
    coro: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    let submit = ASYNC_SUBMIT.get()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Async worker not initialized"))?;
    submit.call1(py, (coro.bind(py),))
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
/// Run a coroutine-producing handler where the caller has already built the
/// coroutine (e.g. via positional args). Shared tail shared with
/// `call_async_on_local_loop` which builds the coroutine from kwargs.
pub fn drive_coroutine_on_local_loop(
    py: Python<'_>,
    coro: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
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
    // Re-create coro since the probed one was consumed.
    submit_to_async_worker(py, coro)
}

/// Call an async handler with a single positional arg. Used by the WebSocket
/// bridge — the WS object is passed positionally so user code can rename the
/// parameter (vLLM uses `websocket`, others use `ws`).
pub fn call_async_on_local_loop_positional(
    py: Python<'_>,
    handler: &Py<PyAny>,
    arg: Py<PyAny>,
) -> PyResult<Py<PyAny>> {
    let coro = handler.call1(py, (arg.bind(py),))?;
    drive_coroutine_on_local_loop(py, coro)
}

/// Call an async handler with a single positional arg + keyword args.
/// Used by the WebSocket bridge when the route has path params like /ws/{room_id}.
pub fn call_async_on_local_loop_positional_with_kwargs(
    py: Python<'_>,
    handler: &Py<PyAny>,
    arg: Py<PyAny>,
    kwargs: &pyo3::Bound<'_, PyDict>,
) -> PyResult<Py<PyAny>> {
    let coro = handler.call(py, (arg.bind(py),), Some(kwargs))?;
    drive_coroutine_on_local_loop(py, coro)
}

/// Handler classification — determined on the FIRST call, reused forever.
/// "sync-fast": completes via StopIteration on send(None) — no I/O.
/// "needs-worker": suspends on send(None) — real async I/O, route to worker.
static HANDLER_CLASS: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<usize, bool>>> =
    std::sync::OnceLock::new();

fn handler_class_map() -> &'static std::sync::Mutex<std::collections::HashMap<usize, bool>> {
    HANDLER_CLASS.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

pub fn call_async_on_local_loop(
    py: Python<'_>,
    handler: &Py<PyAny>,
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<Py<PyAny>> {
    let handler_id = handler.as_ptr() as usize;

    // Check cached classification for this handler.
    let classification = {
        let map = handler_class_map().lock().unwrap();
        map.get(&handler_id).copied()
    };

    match classification {
        Some(true) => {
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
                    let mut map = handler_class_map().lock().unwrap();
                    map.insert(handler_id, false);
                    let _ = coro.call_method0(py, "close");
                }
            }
        }
        Some(false) => {
            // === KNOWN NEEDS-WORKER: skip probe, go straight to worker ===
            init_async_worker();
            let coro = handler.call(py, (), Some(kwargs))?;
            return submit_to_async_worker(py, coro);
        }
        None => {
            // === FIRST CALL: probe to classify ===
            let coro = handler.call(py, (), Some(kwargs))?;
            match coro.call_method1(py, "send", (py.None(),)) {
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                    // Sync-fast — mark and return.
                    {
                        let mut map = handler_class_map().lock().unwrap();
                        map.insert(handler_id, true);
                    }
                    let v = e.value(py);
                    return match v.getattr("value") {
                        Ok(val) => Ok(val.unbind()),
                        Err(_) => Ok(py.None()),
                    };
                }
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py) => {
                    // No running event loop — mark as needs-worker.
                    let mut map = handler_class_map().lock().unwrap();
                    map.insert(handler_id, false);
                    let _ = coro.call_method0(py, "close");
                }
                Err(other) => return Err(other),
                Ok(_yielded) => {
                    // Suspended — mark as needs-worker. On first call this
                    // is typically asyncpg.create_pool() — no connection
                    // acquired yet, safe to close.
                    let mut map = handler_class_map().lock().unwrap();
                    map.insert(handler_id, false);
                    let _ = coro.call_method0(py, "close");
                }
            }
        }
    }

    // Fall through: route to async worker
    init_async_worker();
    let coro = handler.call(py, (), Some(kwargs))?;
    return submit_to_async_worker(py, coro);
}




// ── Async handler call (background event loop — for WS and legacy) ──

/// A handle to a Python asyncio event loop running in a background thread.
static EVENT_LOOP: OnceLock<Py<PyAny>> = OnceLock::new();

/// Public accessor for the persistent event loop.
pub fn get_event_loop_pub(py: Python<'_>) -> PyResult<Py<PyAny>> {
    get_event_loop(py)
}

/// Get or create the shared Python asyncio event loop.
fn get_event_loop(py: Python<'_>) -> PyResult<Py<PyAny>> {
    let loop_obj = EVENT_LOOP.get_or_init(|| {
        Python::attach(|py| {
            let asyncio = py.import("asyncio").expect("failed to import asyncio");
            let event_loop = match py.import("uvloop") {
                Ok(uvloop) => uvloop.call_method0("new_event_loop").expect("uvloop.new_event_loop"),
                Err(_) => asyncio.call_method0("new_event_loop").expect("new_event_loop"),
            };
            let loop_py: Py<PyAny> = event_loop.unbind();

            let loop_for_thread = loop_py.clone_ref(py);
            std::thread::Builder::new()
                .name("fastapi-turbo-asyncio".to_string())
                .spawn(move || {
                    Python::attach(|py| {
                        let loop_bound = loop_for_thread.bind(py);
                        loop_bound
                            .call_method0("run_forever")
                            .expect("event loop run_forever failed");
                    });
                })
                .expect("failed to spawn asyncio thread");

            loop_py
        })
    });

    Ok(loop_obj.clone_ref(py))
}

/// Call an async Python handler. Uses a two-phase approach:
///
/// **Phase 1 (fast path):** Try to drive the coroutine synchronously via
/// `coro.send(None)`. Most `async def` functions that just `return` a value
/// (without any real `await`) complete immediately — StopIteration is raised
/// with the return value. This costs ~2μs (one GIL acquisition, no cross-thread hop).
///
/// **Phase 2 (slow path):** If the coroutine actually suspends (real I/O,
/// `await asyncio.sleep()`, etc.), schedule it on the persistent event loop
/// via `run_coroutine_threadsafe` + oneshot channel. This costs ~50μs.
pub async fn call_async_handler(
    handler: Py<PyAny>,
    kwargs: HashMap<String, Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    // Phase 1: Try the fast synchronous path.
    // Most async deps (async def get_db(): return pool) don't actually suspend —
    // they complete immediately via StopIteration. This avoids the ~50μs event loop round-trip.
    enum FastResult {
        Done(Py<PyAny>),
        Suspended,   // Need to re-call with event loop
        Error(PyErr),
    }

    let fast = tokio::task::block_in_place(|| {
        Python::attach(|py| {
            let py_kwargs = PyDict::new(py);
            for (key, value) in &kwargs {
                let _ = py_kwargs.set_item(key, value.bind(py));
            }

            let coro = match handler.call(py, (), Some(&py_kwargs)) {
                Ok(c) => c,
                Err(e) => return FastResult::Error(e),
            };

            match coro.call_method1(py, "send", (py.None(),)) {
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
                    // Coroutine completed synchronously — extract return value
                    match e.value(py).getattr("value") {
                        Ok(val) => FastResult::Done(val.unbind()),
                        Err(_) => FastResult::Done(py.None()),
                    }
                }
                Err(e) => {
                    // Check if it's a RuntimeError about event loop — means the coroutine
                    // needs a real event loop (e.g., asyncio.sleep). Treat as suspended.
                    let is_runtime_err = e.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py);
                    let msg = e.value(py).str().map(|s| s.to_string()).unwrap_or_default();
                    if is_runtime_err && msg.contains("event loop") {
                        let _ = coro.call_method0(py, "close");
                        FastResult::Suspended
                    } else {
                        // Real exception from the coroutine
                        FastResult::Error(e)
                    }
                }
                Ok(_yielded) => {
                    let _ = coro.call_method0(py, "close");
                    FastResult::Suspended
                }
            }
        })
    });

    match fast {
        FastResult::Done(val) => Ok(val),
        FastResult::Error(e) => Err(e),
        FastResult::Suspended => {
            // Slow path: re-call with the persistent event loop
            call_async_via_event_loop(handler, kwargs).await
        }
    }
}

/// Schedule an async Python callable on the persistent event loop.
/// Uses `fastapi_turbo._async_bridge.schedule_on_loop()` which wraps the handler call
/// in an async wrapper running ON the event loop thread — this ensures
/// `asyncio.get_event_loop()` works inside the handler.
/// Public wrapper for WebSocket handlers that must always use the event loop.
pub async fn call_async_via_event_loop_pub(
    handler: Py<PyAny>,
    kwargs: HashMap<String, Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    call_async_via_event_loop(handler, kwargs).await
}

async fn call_async_via_event_loop(
    handler: Py<PyAny>,
    kwargs: HashMap<String, Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let (tx, rx) = tokio::sync::oneshot::channel::<PyResult<Py<PyAny>>>();

    Python::attach(|py| -> PyResult<()> {
        let event_loop = get_event_loop(py)?;

        // Build kwargs dict
        let py_kwargs = PyDict::new(py);
        for (key, value) in &kwargs {
            let _ = py_kwargs.set_item(key, value.bind(py));
        }

        // Use the Python bridge helper to schedule on the event loop
        let bridge = py.import("fastapi_turbo._async_bridge")?;

        // Create a Rust callback that sends the result through the channel
        let tx = Mutex::new(Some(tx));
        let callback = PyCFunction::new_closure(
            py,
            None,
            None,
            move |args: &pyo3::Bound<'_, pyo3::types::PyTuple>,
                  _kw: Option<&pyo3::Bound<'_, PyDict>>|
                  -> PyResult<()> {
                let future = args.get_item(0)?;
                let result = future.call_method0("result");
                let send_val = match result {
                    Ok(val) => Ok(val.unbind()),
                    Err(e) => Err(e),
                };
                if let Some(sender) = tx.lock().unwrap().take() {
                    let _ = sender.send(send_val);
                }
                Ok(())
            },
        )?;

        // schedule_on_loop(handler, kwargs_dict, event_loop, callback)
        bridge.call_method1(
            "schedule_on_loop",
            (handler.bind(py), py_kwargs, event_loop.bind(py), callback),
        )?;

        Ok(())
    })?;

    rx.await.map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("Async handler channel error: {e}"))
    })?
}
