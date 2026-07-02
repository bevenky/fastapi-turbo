"""Dedicated async worker thread with a continuously-running event loop.

Provides ``submit(coro)`` which schedules a coroutine on the worker's
loop and blocks until it completes. The loop runs ``run_forever()`` so
background tasks (asyncpg pool housekeeping, redis reconnects) execute
naturally.

Performance notes:
    The hot path is the cross-thread handoff. We bypass
    ``concurrent.futures.Future`` (allocates a Future, installs a
    callback, uses a condition variable) and instead:

    * park the caller on a Rust ``SubmitGate`` (std Mutex+Condvar with
      the GIL detached; ``threading.Event`` fallback), reused from a
      lock-free deque pool — cuts the per-wait ``Condition.wait``
      waiter-lock allocation AND ~5 μs per submit vs allocating fresh
    * schedule via ``loop.call_soon_threadsafe`` directly — avoids
      the ``run_coroutine_threadsafe`` wrapper's extra layer
    * stash result/exception in a plain list — avoids
      Future.set_result/Future.result machinery
    * module-level ``_runner``/``_kickoff``/``_cancel_in_box`` helpers
      take the box/event as arguments — no per-submit closure function
      objects (3 MAKE_FUNCTION + cells saved per request)
    * ``submit_fast(coro, timeout)`` — positional entry point for the
      Rust door, which resolves the effective timeout ONCE at route
      build; skips the per-request ``_default_timeout`` env/getattr
      chain entirely

    Net: ~25 μs per submit vs ~40 μs for the stdlib
    ``run_coroutine_threadsafe`` path.

    Correctness: the Event wait() is interruptible via signal (like
    stdlib futures); exceptions from the coroutine are re-raised on
    the caller's thread; the Event is only returned to the pool after
    a successful completion (failure paths drop it to avoid cross-
    contamination).

This is the correct pattern for asyncpg/redis.asyncio compatibility —
all async I/O runs on ONE event loop, matching uvicorn's architecture.
"""
from __future__ import annotations

import asyncio
import os
import threading
from collections import deque

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()
# Serializes worker startup. The first submits can arrive concurrently from
# multiple Rust/tokio threads; without this lock each racer that observed
# ``_loop is None`` spawned its OWN worker thread + loop and the ``_loop``
# global was overwritten — connection pools then got created on a different
# loop than later requests ran on (asyncpg binds futures to the creating
# loop → intermittent "got Future attached to a different loop" 500s).
_init_lock = threading.Lock()
# Debug/regression counter: how many worker loops this process has ever
# started. Correct behaviour is exactly 1 (per init cycle) no matter how
# many threads race the first submit.
_loops_started = 0

# Distinct sentinel so ``submit(coro, timeout=None)`` can mean "no
# timeout" distinctly from "caller didn't pass a timeout, use the
# default from env / app config".
_SENTINEL = object()

# Completion gate for the cross-thread submit handoff. The Rust
# ``SubmitGate`` (std Mutex+Condvar parked with the GIL detached) is a
# drop-in for ``threading.Event`` — same ``set``/``clear``/``wait``
# surface, same signal interruptibility (it slices the park and runs
# ``PyErr_CheckSignals``) — but skips ``Condition.wait``'s per-wait
# waiter-lock allocation and Python-frame bounces on both the wait and
# the ``set()`` side. ``FASTAPI_TURBO_PY_EVENT_GATE=1`` forces the pure
# Python Event (safety valve / A-B benching).
if os.environ.get("FASTAPI_TURBO_PY_EVENT_GATE"):
    _Gate = threading.Event
else:
    try:
        from fastapi_turbo._fastapi_turbo_core import SubmitGate as _Gate
    except Exception:  # noqa: BLE001 — core unavailable (source tree only)
        _Gate = threading.Event

# Lock-free-ish pool of reusable gate objects. ``deque``'s ``.append`` /
# ``.pop`` are atomic under the GIL for single-element ops, so no
# explicit lock is needed for the common get/put cycle. Under heavy
# contention a miss just allocates a fresh gate.
_event_pool: deque = deque()


def init():
    """Start the worker thread if not already running.

    Double-checked lock: the unlocked fast-path check keeps the steady
    state free (one pointer read), while the locked re-check guarantees
    exactly ONE worker thread/loop is ever created even when the first
    submits race in from multiple threads. Racing callers block on the
    lock until the winner's ready-wait completes, then see ``_loop`` set
    and return — so every caller leaves ``init()`` with the ONE loop up.
    """
    global _loop, _thread
    if _loop is not None:
        return
    with _init_lock:
        if _loop is not None:
            return
        _ready.clear()
        _thread = threading.Thread(target=_run, daemon=True, name="fastapi-turbo-async-worker")
        _thread.start()
        _ready.wait(timeout=10)
        # Warm the gate pool so the first N submits avoid allocations.
        for _ in range(64):
            _event_pool.append(_Gate())


def _run():
    global _loop, _loops_started
    try:
        import uvloop
        loop = uvloop.new_event_loop()
    except ImportError:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # Eager task factory (3.12+): the first step of every task created on
    # this loop runs synchronously at create_task time. On the submit path
    # ``_kickoff`` runs on the loop thread, so the handler coroutine starts
    # (and for I/O-bound handlers reaches its first real await) inside the
    # same callback — killing the first-poll scheduling hop (a kevent
    # wakeup, −13-14 µs conn=1 on async endpoints, audited). Verified to
    # work on both stock asyncio and uvloop loops; guarded for older
    # Pythons.
    if hasattr(asyncio, "eager_task_factory"):
        try:
            loop.set_task_factory(asyncio.eager_task_factory)
        except Exception:  # noqa: BLE001 — factory is an optimization only
            pass
    _loops_started += 1
    # Publish the loop only after it is fully constructed — unlocked
    # fast-path readers (``submit_fast``/``get_loop``) must never observe
    # a half-initialized loop.
    _loop = loop
    _ready.set()
    loop.run_forever()


def _acquire_event():
    try:
        ev = _event_pool.pop()
        ev.clear()
        return ev
    except IndexError:
        return _Gate()


def _release_event(ev) -> None:
    # Cap pool to avoid unbounded growth under a spike.
    if len(_event_pool) < 128:
        _event_pool.append(ev)


def _default_timeout(app=None) -> float | None:
    """Resolve the default worker-loop submit timeout.

    Priority:
      1. ``FASTAPI_TURBO_WORKER_TIMEOUT`` env var (process-wide override).
      2. If ``app`` is passed explicitly, ``app.worker_timeout``. Pass
         ``app=<current_app>`` at submit sites that live inside a specific
         app's pipeline to get per-app isolation in multi-app processes.
      3. The ``_fastapi_turbo_current_instance`` class-level pointer —
         last-constructed wins. Kept for single-app convenience so
         ``FastAPI(worker_timeout=...)`` works without plumbing ``app=``
         through every call site.
      4. ``None`` (no timeout — matches FastAPI's default).
    """
    env = os.environ.get("FASTAPI_TURBO_WORKER_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    # Explicit ``app=`` wins unconditionally. ``app.worker_timeout is
    # None`` means "no framework-imposed timeout on this app" — don't
    # fall through to the class-level fallback (which might carry a
    # different app's stricter timeout).
    if app is not None:
        t = getattr(app, "worker_timeout", "__MISSING__")
        if t != "__MISSING__":
            return float(t) if t is not None else None
    # No explicit app → fall back to the last-constructed FastAPI
    # pointer. Single-app users get ``FastAPI(worker_timeout=…)`` for
    # free; multi-app users must plumb ``app=`` explicitly.
    try:
        from fastapi_turbo.applications import FastAPI as _FA  # noqa: PLC0415
        fallback_app = getattr(_FA, "_fastapi_turbo_current_instance", None)
        if fallback_app is not None:
            t = getattr(fallback_app, "worker_timeout", None)
            if t is not None:
                return float(t)
    except Exception:  # noqa: BLE001
        pass
    return None


async def _runner(coro, box, ev):
    """Drive ``coro`` on the worker loop; stash result/exception in ``box``.

    Module-level (box/event passed as arguments) so submits don't pay a
    per-call MAKE_FUNCTION + closure-cell allocation.
    """
    try:
        box[0] = await coro
    except BaseException as e:  # noqa: BLE001 — re-raised on the caller
        box[1] = e
    finally:
        ev.set()


def _kickoff(coro, box, ev, loop, context):
    # Both ``_kickoff`` and the cancel scheduled from the caller run on
    # the worker loop thread, so they're serialized by asyncio. If the
    # caller already timed out (``box[3]`` is True), don't even start
    # the coroutine — close it cleanly and signal completion so the
    # event-pool slot gets released.
    if box[3]:
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass
        ev.set()
        return
    runner = _runner(coro, box, ev)
    task = (
        loop.create_task(runner, context=context)
        if context is not None
        else asyncio.ensure_future(runner, loop=loop)
    )
    box[2] = task
    # Between scheduling ``_kickoff`` and it actually running, the
    # caller may have timed out and requested cancellation. Check
    # the flag once more after task creation.
    if box[3]:
        task.cancel()


def _cancel_in_box(box):
    t = box[2]
    if t is not None and not t.done():
        t.cancel()


def submit_fast(coro, timeout=None, context=None) -> object:
    """Positional fast path: schedule ``coro`` on the worker's loop and
    block until done.

    Used by the Rust door, which resolves the effective timeout ONCE at
    route build (env var → ``app.worker_timeout`` → last-constructed
    fallback) and passes it positionally — no per-request
    ``_default_timeout`` env read / getattr chain, no kwargs dict.

    Semantics are identical to ``submit``: exceptions re-raise on the
    caller's thread; an elapsed ``timeout`` cancels the task on the
    worker loop (thread-safely) and raises ``TimeoutError``; the pooled
    Event is dropped (not reused) on failure.
    """
    if _loop is None:
        init()
    ev = _acquire_event()
    # box slots:
    #   [0] = result
    #   [1] = exception
    #   [2] = task handle (set by ``_kickoff`` on the loop)
    #   [3] = cancel_requested (set by caller before scheduling cancel)
    box: list = [None, None, None, False]
    loop = _loop
    loop.call_soon_threadsafe(_kickoff, coro, box, ev, loop, context)
    # Interruptible wait — honors signals, unlike a bare Condition.wait().
    # Positional: works for both the Rust SubmitGate and threading.Event.
    if not ev.wait(timeout):
        # Request cancellation. Both the ``cancel_requested`` flag and
        # the scheduled ``_cancel_in_box`` run on the same worker loop
        # thread, so whichever of (``_kickoff``, cancel) runs first, the
        # other sees the outcome — no race where the coroutine survives.
        box[3] = True
        loop.call_soon_threadsafe(_cancel_in_box, box)
        raise TimeoutError(
            f"fastapi-turbo worker-loop submit timed out after {timeout}s"
        )
    exc = box[1]
    if exc is not None:
        # Don't pool events that held a failure — conservative.
        raise exc
    _release_event(ev)
    return box[0]


def submit(
    coro,
    *,
    timeout: float | None = _SENTINEL,  # type: ignore[valid-type]
    app=None,
    context=None,
) -> object:
    """Schedule coro on the worker's loop and block until done.

    ``context`` (a ``contextvars.Context``), when passed, runs the coroutine in
    that context — so contextvar mutations the coroutine makes land in the caller-
    supplied context (used by the async-generator yield-dep bridge to propagate a
    dep's contextvars back to the request thread).

    ``timeout`` (seconds) controls how long we wait before giving up.
    When the timeout elapses, the background coroutine is **cancelled**
    via ``task.cancel()`` (scheduled on the worker loop thread-safely)
    so it doesn't keep running in the background. A ``TimeoutError`` is
    then raised in the caller.

    ``None`` means wait indefinitely — matching FastAPI/Starlette
    behaviour where long-running async handlers simply take as long
    as they need.

    ``app`` is an optional per-submit hint used to resolve the default
    timeout in multi-app processes: when passed, this app's
    ``worker_timeout`` is consulted before falling back to the
    class-level ``_fastapi_turbo_current_instance`` pointer.

    The sentinel default on ``timeout`` lets callers distinguish "I
    didn't pass a timeout" (use ``_default_timeout(app)``) from "I want
    no timeout" (pass ``None`` explicitly).
    """
    if timeout is _SENTINEL:  # type: ignore[comparison-overlap]
        timeout = _default_timeout(app)
    return submit_fast(coro, timeout, context)


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the worker's event loop (init if needed)."""
    if _loop is None:
        init()
    return _loop
