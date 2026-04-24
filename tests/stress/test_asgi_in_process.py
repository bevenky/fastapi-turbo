"""In-process ASGI dispatch: the app must be usable as a plain ASGI
callable without binding a loopback socket.

Exercised via httpx's ``ASGITransport`` — same path that serverless
runtimes and sandboxed test harnesses use. Previously the HTTP leg
of ``FastAPI.__call__`` auto-started a Rust server + proxied requests
over 127.0.0.1 (``PermissionError: socket.bind`` in restricted envs,
plus a 200-ms+ hit on first request). The in-process dispatcher
handles the common cases (path match, query, JSON body, Pydantic
validation, Response injection) and falls back to the proxy only
for features it can't yet cover.

To make the "no loopback needed" claim testable on a normal dev
machine we monkeypatch ``_asgi_ensure_server`` to raise; if the
in-process path is working, the test never hits the fallback."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Request
from fastapi.testclient import AsyncClient, ASGITransport
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    qty: int = 1


@pytest.fixture(autouse=True)
def _block_loopback_server(monkeypatch):
    """Any fall-through to ``_asgi_ensure_server`` becomes a loud
    failure so we catch the "silently went back to the proxy" case."""

    async def _boom(self):
        raise RuntimeError(
            "in-process ASGI path silently fell back to the loopback "
            "proxy — the in-process dispatcher didn't handle this request"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_simple_get_dispatches_in_process():
    app = FastAPI()

    @app.get("/ping")
    def _ping():
        return {"ok": True}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/ping")
            assert r.status_code == 200
            assert r.json() == {"ok": True}

    _run(go())


def test_path_param_route_dispatches_in_process():
    app = FastAPI()

    @app.get("/users/{uid}")
    def _u(uid: int):
        return {"uid": uid, "kind": str(type(uid).__name__)}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/users/42")
            assert r.status_code == 200
            body = r.json()
            assert body == {"uid": 42, "kind": "int"}

    _run(go())


def test_query_param_dispatches_in_process():
    app = FastAPI()

    @app.get("/search")
    def _s(q: str = "default"):
        return {"q": q}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/search", params={"q": "hello"})
            assert r.status_code == 200
            assert r.json() == {"q": "hello"}

    _run(go())


def test_pydantic_body_dispatches_in_process():
    app = FastAPI()

    @app.post("/items")
    def _create(item: Item):
        return {"name": item.name, "qty": item.qty}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.post("/items", json={"name": "widget", "qty": 5})
            assert r.status_code == 200
            assert r.json() == {"name": "widget", "qty": 5}

    _run(go())


def test_request_injection_dispatches_in_process():
    app = FastAPI()

    @app.get("/echo")
    def _e(request: Request):
        return {"path": request.url.path, "method": request.method}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/echo")
            assert r.status_code == 200
            assert r.json() == {"path": "/echo", "method": "GET"}

    _run(go())
