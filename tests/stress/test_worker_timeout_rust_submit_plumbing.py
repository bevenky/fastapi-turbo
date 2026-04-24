"""Regression: Rust-layer ``submit_to_async_worker(coro)`` must pass
``app=`` to the Python ``_async_worker.submit()`` call, so that even
pure-async endpoints dispatched via the Rust probe + worker path
inherit their owning app's ``worker_timeout``.

This closes the residual Rust-side leak behind the R2 #3 fix: the
Python-layer wrapping (``_make_sync_wrapper(app=app)`` applied during
route compile) covered the *user-reachable* request path, but any
endpoint that slipped past the compile-time wrap (e.g. via direct
Rust calls into ``submit_to_async_worker``) still consulted the
class-level ``_fastapi_turbo_current_instance`` pointer — which, in a
multi-app process, could point at a DIFFERENT app with a stricter
timeout.

We force the bypass by registering an endpoint whose signature looks
like a wrapped sync caller to the compile pass but whose handler
actually returns a coroutine — so Rust's probe classifies it as
"needs worker" and goes through ``submit_to_async_worker``."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_current_instance():
    from fastapi_turbo.applications import FastAPI as _FA
    original = getattr(_FA, "_fastapi_turbo_current_instance", None)
    try:
        yield
    finally:
        _FA._fastapi_turbo_current_instance = original


def test_rust_submit_uses_owning_apps_timeout_via_rust_probe():
    """An async endpoint that exercises the Rust probe path
    (ends up in ``submit_to_async_worker`` after the ``send(None)``
    probe raises ``RuntimeError: no running event loop``) must NOT
    die under a co-resident app's stricter timeout."""
    import asyncio as _asyncio

    app_slow = FastAPI(worker_timeout=None)

    @app_slow.get("/slow")
    async def _slow():
        # ``asyncio.sleep`` requires a running event loop — the Rust
        # probe's ``send(None)`` raises RuntimeError, forcing the
        # "needs worker" path where the coroutine is re-submitted
        # to our background worker loop. This is precisely the path
        # that bypassed the Python wrapper.
        await _asyncio.sleep(0.15)
        return {"done": True}

    # Strict-timeout app constructed AFTER — under last-wins, its
    # 30ms cap would leak into app_slow's dispatch.
    FastAPI(worker_timeout=0.03)

    with TestClient(app_slow) as c:
        r = c.get("/slow")
        assert r.status_code == 200, r.content
        assert r.json() == {"done": True}


def test_rust_submit_respects_strict_timeout_when_it_is_the_owning_app():
    """Inverse: the strict app's OWN endpoint must still time out.
    Confirms the plumbing isn't one-directional."""
    import asyncio as _asyncio

    FastAPI(worker_timeout=None)  # lax constructed first
    app_fast = FastAPI(worker_timeout=0.03)

    @app_fast.get("/slow")
    async def _slow():
        await _asyncio.sleep(0.3)
        return {"done": True}

    with TestClient(app_fast, raise_server_exceptions=False) as c:
        r = c.get("/slow")
        assert r.status_code != 200, (
            f"strict app's timeout did not fire under Rust submit path: {r.content}"
        )
