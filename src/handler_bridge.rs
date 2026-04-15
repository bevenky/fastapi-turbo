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

/// Initialize the dedicated async worker thread.
/// This thread owns an asyncio event loop and processes handlers via run_until_complete().
/// Called once at module init.
pub fn init_async_worker() {
    if ASYNC_WORKER_TX.get().is_some() {
        return; // Already initialized
    }

    let (tx, rx) = crossbeam_channel::unbounded::<AsyncRequest>();
    ASYNC_WORKER_TX.set(tx).ok();

    std::thread::Builder::new()
        .name("fastapi-rs-async-worker".to_string())
        .spawn(move || {
            // Acquire GIL once for the entire worker lifetime
            Python::attach(|py| {
                // Use uvloop if available (2-3x faster than standard asyncio event loop)
                let asyncio = py.import("asyncio").expect("asyncio");
                let loop_obj = match py.import("uvloop") {
                    Ok(uvloop) => {
                        let lp = uvloop.call_method0("new_event_loop").expect("uvloop.new_event_loop");
                        lp
                    }
                    Err(_) => asyncio.call_method0("new_event_loop").expect("new_event_loop"),
                };
                asyncio.call_method1("set_event_loop", (&loop_obj,)).expect("set_event_loop");

                loop {
                    // Release GIL while waiting for a request on the channel
                    let req = py.detach(|| {
                        rx.recv().ok()
                    });
                    let Some(req) = req else { break };

                    // GIL is re-acquired here — run the coroutine
                    let result = loop_obj.call_method1("run_until_complete", (req.coro.bind(py),));
                    let _ = req.response_tx.send(result.map(|r| r.unbind()));
                }
            });
        })
        .expect("failed to spawn async worker");
}

/// Run an async Python handler on the dedicated async worker thread.
///
/// Cost: ~10μs (crossbeam send ~100ns + thread wakeup ~2μs + run_until_complete ~5-8μs)
/// vs ~150μs for run_coroutine_threadsafe.
///
/// All asyncpg/redis awaits resolve natively on the worker thread's event loop.
pub fn call_async_on_local_loop(
    py: Python<'_>,
    handler: &Py<PyAny>,
    kwargs: &Bound<'_, PyDict>,
) -> PyResult<Py<PyAny>> {
    init_async_worker();

    // Create coroutine (needs GIL — we have it)
    let coro = handler.call(py, (), Some(kwargs))?;

    let (result_tx, result_rx) = crossbeam_channel::bounded::<PyResult<Py<PyAny>>>(1);
    let tx = ASYNC_WORKER_TX.get()
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("Async worker not initialized"))?;

    tx.send(AsyncRequest { coro, response_tx: result_tx })
        .map_err(|_| pyo3::exceptions::PyRuntimeError::new_err("Async worker channel closed"))?;

    // CRITICAL: Release GIL so the worker thread can acquire it to run the coroutine.
    // Then wait for the result on the crossbeam channel (no GIL needed for channel recv).
    py.detach(|| {
        // Block on crossbeam recv — worker thread runs the coroutine while we wait
        result_rx.recv()
            .unwrap_or_else(|_| Err(pyo3::exceptions::PyRuntimeError::new_err("Async worker died")))
    })
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
                .name("fastapi-rs-asyncio".to_string())
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
/// Uses `fastapi_rs._async_bridge.schedule_on_loop()` which wraps the handler call
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
        let bridge = py.import("fastapi_rs._async_bridge")?;

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
