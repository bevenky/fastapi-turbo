"""Regression tests for issue #1.

Three dispatch paths in ``applications.py`` previously ran on the
``_async_worker`` loop while async HTTP handlers ran on the loop
awaiting ``__call__``:

1. ``_asgi_lifespan`` invoked the sync ``_run_lifespan_*`` /
   ``_run_*_handlers`` helpers, which submit through
   ``_async_worker.submit``. Asyncio resources created in lifespan
   (asyncpg pools, ``redis.asyncio`` clients, aiohttp sessions) ended
   up bound to the worker thread's loop. The first request from a
   handler awaiting one of those resources hit
   ``RuntimeError: ... got Future attached to a different loop``.
2. ``BackgroundTasks`` queued by an in-process handler ran via
   ``bg_injected.run_sync()``, which submits async tasks to the
   worker loop. Same loop-affinity break against lifespan resources.
3. ``_asgi_emit_exception`` re-raised whenever ``matched_cls is
   Exception`` — so a user-registered ``@app.exception_handler(
   Exception)`` that caught an HTTPException via MRO produced a
   re-raise after the response was queued; uvicorn dropped the
   transport and the client timed out with no response.
"""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401  # install compat shims
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def _record_loop(app: FastAPI, key: str) -> None:
    app.state.loop_ids = getattr(app.state, "loop_ids", {})
    app.state.loop_ids[key] = id(asyncio.get_running_loop())


def test_lifespan_async_resource_is_usable_from_handler_in_process():
    """Lifespan must run on the same loop as the handler so an asyncio
    primitive created at startup works under ``await`` from the
    request path. A different-loop binding raises
    ``RuntimeError: ... attached to a different loop``."""

    async def lifespan(app: FastAPI):
        _record_loop(app, "lifespan")
        # ``asyncio.Lock`` lazily binds on first ``acquire``; if
        # lifespan ran on a different loop than the handler, the
        # acquire below would raise.
        app.state.lock = asyncio.Lock()
        yield
        _record_loop(app, "shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/lock")
    async def use_lock():
        _record_loop(app, "handler")
        async with app.state.lock:
            await asyncio.sleep(0)
        return {
            "lifespan_loop": app.state.loop_ids["lifespan"],
            "handler_loop": app.state.loop_ids["handler"],
        }

    with TestClient(app, in_process=True) as client:
        r = client.get("/lock")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifespan_loop"] == body["handler_loop"]


def test_background_task_can_use_lifespan_async_resource_in_process():
    """BackgroundTasks scheduled by the handler must run on the same
    loop as the handler/lifespan so they can reuse pool/client objects
    created at startup. The pre-fix path went through ``run_sync()``
    which submits to ``_async_worker`` — different loop, real-world
    pools surface ``another operation in progress`` /
    ``Timeout context manager should be used inside a task``."""

    async def lifespan(app: FastAPI):
        app.state.lifespan_loop_id = id(asyncio.get_running_loop())
        app.state.bg_event = asyncio.Event()
        app.state.bg_loop_id = None
        yield

    app = FastAPI(lifespan=lifespan)

    async def bg_set_event(state) -> None:
        state.bg_loop_id = id(asyncio.get_running_loop())
        # If the BG ran on a different loop than the handler, this
        # ``set()`` raises because ``asyncio.Event`` lazily bound to
        # the lifespan loop on construction.
        state.bg_event.set()

    @app.post("/bg")
    async def schedule_bg(bg_tasks: BackgroundTasks):
        bg_tasks.add_task(bg_set_event, app.state)
        return {"handler_loop": id(asyncio.get_running_loop())}

    with TestClient(app, in_process=True) as client:
        r = client.post("/bg")
    assert r.status_code == 200, r.text
    handler_loop = r.json()["handler_loop"]
    assert app.state.lifespan_loop_id == handler_loop
    assert app.state.bg_event.is_set(), "BG task did not run"
    assert app.state.bg_loop_id == handler_loop


def test_exception_handler_for_exception_catching_httpexception_returns_response():
    """A user-registered ``@app.exception_handler(Exception)`` catches
    HTTPException via MRO. The response MUST be delivered with no
    re-raise — Starlette's ExceptionMiddleware would have intercepted
    HTTPException before ServerErrorMiddleware ever saw it. Pre-fix
    turbo re-raised on ``matched_cls is Exception`` regardless of the
    exception's actual type, so uvicorn dropped the transport after
    the response was queued."""

    app = FastAPI()

    @app.exception_handler(Exception)
    async def catch_all(request, exc):
        if isinstance(exc, HTTPException):
            return JSONResponse(
                {"caught": "http", "detail": exc.detail},
                status_code=exc.status_code,
            )
        return JSONResponse({"caught": "generic"}, status_code=500)

    @app.get("/raise-http")
    async def raise_http():
        raise HTTPException(status_code=418, detail="i am a teapot")

    with TestClient(app, in_process=True, raise_server_exceptions=True) as client:
        r = client.get("/raise-http")
    assert r.status_code == 418, r.text
    assert r.json() == {"caught": "http", "detail": "i am a teapot"}


def test_exception_handler_for_generic_exception_still_re_raises():
    """Symmetric guarantee: a true unhandled ``Exception`` (not
    HTTPException / RVE / WebSocketException) MUST still re-raise so
    ``raise_server_exceptions=True`` test transports surface the
    original failure rather than silently masking it as a 500."""

    app = FastAPI()

    @app.exception_handler(Exception)
    async def catch_all(request, exc):
        return JSONResponse({"caught": "generic"}, status_code=500)

    @app.get("/boom")
    async def boom():
        raise ValueError("kaboom")

    with TestClient(app, in_process=True, raise_server_exceptions=True) as client:
        with pytest.raises(ValueError, match="kaboom"):
            client.get("/boom")


def test_failed_lifespan_startup_poisons_app_under_async_dispatch():
    """The async-variant startup path must keep the same failure
    guard as the sync variant: a handler that raised at startup time
    leaves the app in ``"failed"`` state, and the next call to
    ``_async_run_startup_handlers`` raises a RuntimeError instead of
    silently re-running."""

    app = FastAPI()

    fired = {"count": 0}

    @app.on_event("startup")
    async def boom():
        fired["count"] += 1
        raise RuntimeError("startup blew up")

    async def _drive():
        with pytest.raises(RuntimeError, match="startup blew up"):
            await app._async_run_startup_handlers()
        with pytest.raises(RuntimeError, match="failed state"):
            await app._async_run_startup_handlers()

    asyncio.run(_drive())
    assert fired["count"] == 1
