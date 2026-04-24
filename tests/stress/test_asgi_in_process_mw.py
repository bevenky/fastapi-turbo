"""In-process ASGI must apply the registered middleware stacks
(raw-ASGI ``app.add_middleware(...)`` and Python HTTP middlewares)
around the matched endpoint. Without this, middlewares that mutate
headers, wrap exceptions, or implement CORS are silently skipped
when the app is dispatched via ASGITransport."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process fell back to the loopback proxy — middleware "
            "chain didn't run in-process"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_raw_asgi_middleware_sees_request_and_response():
    calls: list[str] = []

    class TracingMW:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                calls.append(f"enter {scope['path']}")

                async def _send(msg):
                    if msg.get("type") == "http.response.start":
                        calls.append(f"exit {msg['status']}")
                    await send(msg)

                await self.app(scope, _send_and_tap(scope, send, _send))
            else:
                await self.app(scope, receive, send)

    async def _send_and_tap(scope, outer, tap):
        # Helper so the MW can wrap ``send``. In practice MWs do this
        # inline; we factor out to keep the closure signature simple.
        return await tap

    # Using a simpler MW that doesn't need the helper:
    class TracingMW2:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                calls.append(f"enter {scope['path']}")

                async def _send(msg):
                    if msg.get("type") == "http.response.start":
                        calls.append(f"exit {msg['status']}")
                    await send(msg)

                await self.app(scope, receive, _send)
            else:
                await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(TracingMW2)

    @app.get("/ping")
    def _p():
        return {"ok": True}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/ping")
            assert r.status_code == 200
            assert r.json() == {"ok": True}

    _run(go())
    assert calls == ["enter /ping", "exit 200"], calls


def test_raw_asgi_middleware_can_short_circuit():
    """MW that returns a response without calling ``self.app`` must
    prevent the endpoint from running."""
    endpoint_ran = []

    class BlockingMW:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 418,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"blocked"})
                return
            await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(BlockingMW)

    @app.get("/ping")
    def _p():
        endpoint_ran.append(True)
        return {"ok": True}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/ping")
            assert r.status_code == 418
            assert r.content == b"blocked"

    _run(go())
    assert endpoint_ran == []


def test_raw_asgi_middleware_order_is_lifo():
    """``add_middleware`` applies outer-most last — LIFO composition
    matches Starlette / FastAPI semantics."""
    calls: list[str] = []

    def _make_mw(tag):
        class _MW:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    calls.append(f"enter {tag}")

                    async def _send(msg):
                        if msg.get("type") == "http.response.start":
                            calls.append(f"exit {tag}")
                        await send(msg)

                    await self.app(scope, receive, _send)
                else:
                    await self.app(scope, receive, send)

        return _MW

    app = FastAPI()
    app.add_middleware(_make_mw("inner"))
    app.add_middleware(_make_mw("outer"))

    @app.get("/ping")
    def _p():
        return {}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            await cli.get("/ping")

    _run(go())
    assert calls == [
        "enter outer",
        "enter inner",
        "exit inner",
        "exit outer",
    ], calls
