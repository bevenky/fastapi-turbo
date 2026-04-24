"""End-to-end test: pure-async endpoint dispatched via the Rust hot
path must use the OWNING app's ``worker_timeout``, not whichever app
was last constructed in the process.

Previously only Python-layer submit sites were plumbed — async
endpoints that went ``Rust dispatch → Python handler coroutine →
worker.submit`` inherited the class-level ``_fastapi_turbo_current_instance``
pointer, so ``app_slow``'s requests died under ``app_fast``'s tight
timeout.

After the audit-R2 fix, ``_make_sync_wrapper`` closes over the app at
compile time so the generated handler passes ``app=`` into submit
regardless of which thread eventually runs it."""
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


def test_async_endpoint_uses_owning_apps_timeout_not_last_wins():
    """Construct a relaxed app, then a strict one; dispatch on the
    relaxed one. The strict one's timeout must NOT leak."""
    app_slow = FastAPI(worker_timeout=None)

    @app_slow.get("/slow")
    async def _slow():
        await asyncio.sleep(0.15)
        return {"done": True}

    # Construct the aggressive-timeout app AFTER — under last-wins this
    # would be the one ``_default_timeout()`` picked up.
    app_fast = FastAPI(worker_timeout=0.03)

    @app_fast.get("/noop")
    def _noop():
        return {}

    # app_slow's handler MUST complete — even though app_fast exists in
    # the process with a 30 ms cap.
    with TestClient(app_slow) as c:
        r = c.get("/slow")
        assert r.status_code == 200, r.content
        assert r.json() == {"done": True}


def test_strict_app_still_enforces_its_own_timeout():
    """Inverse: the strict app's own endpoint MUST see its own timeout
    trigger a 500 (rather than silently running under the lax app's
    ``None``)."""
    FastAPI(worker_timeout=None)  # lax constructed first
    app_fast = FastAPI(worker_timeout=0.03)

    @app_fast.get("/slow")
    async def _slow():
        await asyncio.sleep(0.3)
        return {"done": True}

    with TestClient(app_fast, raise_server_exceptions=False) as c:
        r = c.get("/slow")
        # Timeout path returns either 500 (raised TimeoutError) or
        # fails; the point is it does NOT return 200 OK like the
        # lax-timeout app would have allowed.
        assert r.status_code != 200, f"strict app's timeout did not fire: {r.content}"
