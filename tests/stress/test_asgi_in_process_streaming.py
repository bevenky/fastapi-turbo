"""In-process ASGI must stream ``StreamingResponse`` bodies
(sync or async iterables) as multiple ``http.response.body``
messages with ``more_body=True`` — matching the ASGI contract.

Previously ``_send_asgi_response`` raised ``NotImplementedError``
when the response body wasn't a plain bytes object, which
triggered a fallback to the loopback proxy — blocking SSE /
streaming endpoints in sandboxed environments."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError("fell back to loopback — streaming test")

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_streaming_response_sync_generator():
    app = FastAPI()

    @app.get("/stream")
    def _s():
        def gen():
            for i in range(5):
                yield f"chunk-{i}\n".encode()
        return StreamingResponse(gen(), media_type="text/plain")

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/stream")
            assert r.status_code == 200
            body = r.content
            assert body.count(b"chunk-") == 5
            assert body == b"chunk-0\nchunk-1\nchunk-2\nchunk-3\nchunk-4\n"

    _run(go())


def test_streaming_response_async_generator():
    app = FastAPI()

    @app.get("/s2")
    def _s2():
        async def gen():
            for i in range(3):
                await asyncio.sleep(0)
                yield f"a{i}|".encode()
        return StreamingResponse(gen(), media_type="text/plain")

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/s2")
            assert r.status_code == 200
            assert r.content == b"a0|a1|a2|"

    _run(go())


def test_streaming_response_preserves_content_type_header():
    app = FastAPI()

    @app.get("/sse")
    def _sse():
        def gen():
            yield b"data: hello\n\n"
            yield b"data: world\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/sse")
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
            assert r.content == b"data: hello\n\ndata: world\n\n"

    _run(go())
