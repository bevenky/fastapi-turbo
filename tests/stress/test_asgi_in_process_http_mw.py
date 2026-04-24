"""``@app.middleware('http')`` — FastAPI's ``(request, call_next)``
middleware shape must run around the matched endpoint when the app
dispatches in-process via ASGITransport."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process fell back to the loopback proxy — HTTP MW chain "
            "didn't run in-process"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_http_middleware_injects_response_header():
    app = FastAPI()

    @app.middleware("http")
    async def add_header(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-MW"] = "traced"
        return resp

    @app.get("/p")
    def _p():
        return {"ok": True}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/p")
            assert r.status_code == 200
            assert r.headers.get("x-mw") == "traced"

    _run(go())


def test_http_middleware_can_short_circuit_with_own_response():
    app = FastAPI()

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if request.headers.get("x-forbid"):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    ran = []

    @app.get("/p")
    def _p():
        ran.append(True)
        return {}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/p", headers={"x-forbid": "1"})
            assert r.status_code == 403
            assert r.json() == {"detail": "forbidden"}
            assert ran == []
            # Without the header, the endpoint runs.
            r2 = await cli.get("/p")
            assert r2.status_code == 200
            assert ran == [True]

    _run(go())
