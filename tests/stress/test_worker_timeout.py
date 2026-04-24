"""Regression: the async worker's submit() must

1. default to NO timeout (match FastAPI's behaviour),
2. honour ``FASTAPI_TURBO_WORKER_TIMEOUT`` env var + per-app
   ``worker_timeout=...`` setting,
3. cancel the underlying task when the timeout fires instead of
   leaving it running in the background.
"""
from __future__ import annotations

import asyncio
import os

import pytest

import fastapi_turbo  # noqa: F401
from fastapi_turbo import _async_worker as aw


def test_default_has_no_timeout(monkeypatch):
    """With no env var and no app-level setting, ``submit`` should not
    raise TimeoutError on a handler that takes longer than the legacy
    30s hard cap."""
    monkeypatch.delenv("FASTAPI_TURBO_WORKER_TIMEOUT", raising=False)

    async def fast():
        await asyncio.sleep(0.01)
        return "ok"

    r = aw.submit(fast())
    assert r == "ok"
    assert aw._default_timeout() is None


def test_env_var_controls_timeout(monkeypatch):
    monkeypatch.setenv("FASTAPI_TURBO_WORKER_TIMEOUT", "0.05")

    async def slow():
        await asyncio.sleep(1.0)
        return "should-not-reach"

    with pytest.raises(TimeoutError):
        aw.submit(slow())


def test_timeout_cancels_underlying_task(monkeypatch):
    """After a timeout, the underlying coroutine must actually be
    cancelled on the worker loop — not left running in the background
    to eventually deliver its result long after the caller gave up."""
    monkeypatch.setenv("FASTAPI_TURBO_WORKER_TIMEOUT", "0.05")

    reached = {"v": False}

    async def slow():
        try:
            await asyncio.sleep(0.5)
            reached["v"] = True
        except asyncio.CancelledError:
            raise

    with pytest.raises(TimeoutError):
        aw.submit(slow())

    # Give the worker loop more than enough time to run the task through
    # to the "reached" mutation if cancellation had failed.
    import time
    time.sleep(0.8)
    assert reached["v"] is False, "task was NOT cancelled after timeout"


def test_explicit_none_skips_env(monkeypatch):
    """Passing timeout=None explicitly should override the env var (no
    timeout)."""
    monkeypatch.setenv("FASTAPI_TURBO_WORKER_TIMEOUT", "0.01")

    async def moderate():
        await asyncio.sleep(0.05)
        return "finished"

    r = aw.submit(moderate(), timeout=None)
    assert r == "finished"
