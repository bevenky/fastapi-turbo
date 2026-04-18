"""Dedicated async worker thread with a continuously-running event loop.

Provides `submit(coro)` which schedules a coroutine on the worker's loop
and blocks until it completes. The loop runs `run_forever()` so background
tasks (asyncpg pool housekeeping, redis reconnects) execute naturally.

This is the correct pattern for asyncpg/redis.asyncio compatibility —
all async I/O runs on ONE event loop, matching uvicorn's architecture.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()


def init():
    """Start the worker thread if not already running."""
    global _loop, _thread
    if _loop is not None:
        return
    _ready.clear()
    _thread = threading.Thread(target=_run, daemon=True, name="fastapi-rs-async-worker")
    _thread.start()
    _ready.wait(timeout=10)


def _run():
    global _loop
    try:
        import uvloop
        _loop = uvloop.new_event_loop()
    except ImportError:
        _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _ready.set()
    _loop.run_forever()


def submit(coro) -> object:
    """Schedule coro on the worker's loop and block until done.

    Returns the coroutine's result. Raises if the coroutine raised.
    """
    if _loop is None:
        init()
    future: Future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


def get_loop() -> asyncio.AbstractEventLoop:
    """Return the worker's event loop (init if needed)."""
    if _loop is None:
        init()
    return _loop
