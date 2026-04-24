"""Audit regression: the ASGI adapter used by
``httpx.AsyncClient(transport=ASGITransport(app=app))`` must:

  1. Preserve **duplicate request headers** through to the handler.
     Previously we hashed headers into a dict at both the adapter and
     the ``Headers`` datastructure, so ``X-Forwarded-For: a / b / c``
     collapsed to just ``c``.
  2. **Stream large request bodies** instead of buffering the whole
     payload in memory before hand-off to the Rust server.
  3. Preserve **duplicate response headers** (``Set-Cookie`` with
     multiple values) on the way back out.

The localhost-proxy implementation (used when our app is dispatched
as a plain ASGI callable) still requires loopback connectivity — the
socket-restricted-env use case is not covered by this test."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Request, Response
from fastapi.testclient import AsyncClient, ASGITransport


def _run(coro):
    return asyncio.run(coro)


def test_duplicate_request_headers_reach_handler():
    app = FastAPI()

    @app.get("/h")
    def _h(request: Request):
        return {"xs": request.headers.getlist("x-dupe")}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get(
                "/h",
                headers=[("x-dupe", "a"), ("x-dupe", "b"), ("x-dupe", "c")],
            )
            assert r.status_code == 200
            assert r.json() == {"xs": ["a", "b", "c"]}

    _run(go())


def test_large_streamed_request_body_roundtrips():
    app = FastAPI()

    @app.post("/echo")
    async def _echo(request: Request):
        body = await request.body()
        return {"len": len(body)}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            payload = b"x" * (1024 * 1024)  # 1 MiB
            r = await cli.post("/echo", content=payload)
            assert r.status_code == 200
            assert r.json() == {"len": len(payload)}

    _run(go())


def test_duplicate_response_set_cookie_preserved():
    app = FastAPI()

    @app.get("/cookies")
    def _cookies(response: Response):
        response.set_cookie("a", "1", path="/")
        response.set_cookie("b", "2", path="/")
        return {"ok": True}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/cookies")
            assert r.status_code == 200
            cookies = [
                v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"
            ]
            assert any("a=1" in c for c in cookies), cookies
            assert any("b=2" in c for c in cookies), cookies

    _run(go())
