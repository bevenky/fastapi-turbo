"""Multi-app isolation for ``FastAPI(worker_timeout=…)``.

Previously ``_default_timeout()`` read ``FastAPI._fastapi_turbo_current_instance``
— whichever app was constructed last owned the process-wide default.
After the fix, ``_async_worker.submit(coro, app=<app>)`` lets each
per-request dispatch resolve its own app's timeout, so two apps in the
same process don't clobber each other.

The fallback (single-app convenience without plumbing ``app=``) is
preserved: if no ``app=`` is passed, the last-constructed pointer is
still consulted — documented trade-off for ergonomics."""
from __future__ import annotations

import time

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_current_instance_pointer():
    """Each test leaves ``_fastapi_turbo_current_instance`` clean so
    later tests in the stress suite see ``_default_timeout() is None``
    (the expected single-app default)."""
    from fastapi_turbo.applications import FastAPI as _FA

    original = getattr(_FA, "_fastapi_turbo_current_instance", None)
    try:
        yield
    finally:
        _FA._fastapi_turbo_current_instance = original


def test_submit_with_explicit_app_uses_that_apps_timeout():
    from fastapi_turbo._async_worker import _default_timeout

    fast = FastAPI(worker_timeout=0.1)
    slow = FastAPI(worker_timeout=5.0)

    assert _default_timeout(app=fast) == 0.1
    assert _default_timeout(app=slow) == 5.0


def test_env_var_overrides_explicit_app_timeout(monkeypatch):
    from fastapi_turbo._async_worker import _default_timeout

    monkeypatch.setenv("FASTAPI_TURBO_WORKER_TIMEOUT", "7.5")
    app = FastAPI(worker_timeout=0.1)
    assert _default_timeout(app=app) == 7.5


def test_fallback_global_still_works_when_no_app_kwarg():
    """Single-app users shouldn't need to plumb ``app=`` everywhere."""
    from fastapi_turbo._async_worker import _default_timeout

    app = FastAPI(worker_timeout=2.0)
    # No explicit app passed → falls back to last-constructed instance,
    # which is the one we just built.
    assert _default_timeout() == 2.0


def test_python_layer_async_dep_uses_explicit_app_kwarg():
    """``_worker_submit(coro, app=<app>)`` call sites isolate their
    timeout resolution.

    This covers the Python dispatch path that runs for endpoints with
    async yield-dependencies. The Rust fast-path (pure-async endpoint
    with no deps) still consults the process-global pointer because
    ``APP_INSTANCE`` / ``_fastapi_turbo_current_instance`` is last-run-
    wins; callers that need strict multi-app isolation should set
    ``FASTAPI_TURBO_WORKER_TIMEOUT`` uniformly across the process.
    """
    import asyncio

    from fastapi_turbo._async_worker import submit as _submit

    # Build two apps with different timeouts. We call submit directly
    # with explicit ``app=`` to prove the per-call resolution works.
    app_slow = FastAPI(worker_timeout=None)
    app_fast = FastAPI(worker_timeout=0.05)

    async def sleep_02():
        await asyncio.sleep(0.2)
        return "done"

    # Aggressive-timeout app's timeout must NOT apply when submit is
    # invoked with ``app=app_slow``.
    result = _submit(sleep_02(), app=app_slow)
    assert result == "done"

    # Inverse: when we explicitly opt into app_fast's aggressive
    # timeout, it fires.
    with pytest.raises(TimeoutError):
        _submit(sleep_02(), app=app_fast)
