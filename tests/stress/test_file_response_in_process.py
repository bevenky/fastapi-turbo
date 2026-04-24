"""``FileResponse`` must work over the in-process ASGI path.

Since ``TestClient`` auto-falls-back to in-process when loopback
bind fails, a user relying on ``FileResponse`` for downloads /
static asset serving would silently get an empty body in sandboxed
envs. The Rust path worked because the response's ``path`` attribute
was read server-side; the ASGI shim emitted the empty ``.body``
placeholder."""
from __future__ import annotations

import asyncio
import os

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import AsyncClient, ASGITransport, TestClient


def _run(coro):
    return asyncio.run(coro)


def test_file_response_small_via_asgi(tmp_path):
    payload = b"hello world"
    f = tmp_path / "s.txt"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f), media_type="text/plain")

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f")
            assert r.status_code == 200
            assert r.content == payload
            assert int(r.headers["content-length"]) == len(payload)

    _run(go())


def test_file_response_large_streams_via_asgi(tmp_path):
    """2 MiB → above the 256 KiB buffered threshold → stream path.
    ASGI serialisation must emit multiple chunks (more_body=True)."""
    payload = os.urandom(2 * 1024 * 1024)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f")
            assert r.status_code == 200
            assert r.content == payload

    _run(go())


def test_file_response_via_testclient_in_process(tmp_path):
    """``TestClient(app, in_process=True)`` goes through the same
    ASGI shim, so the same bug exposes here."""
    payload = b"test client in_process payload"
    f = tmp_path / "tc.txt"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        r = c.get("/f")
        assert r.status_code == 200
        assert r.content == payload


def test_file_response_404_via_asgi(tmp_path):
    """Missing-file path must still return 404 via the in-process path."""
    app = FastAPI()

    @app.get("/missing")
    def _m():
        return FileResponse(str(tmp_path / "nope.bin"))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/missing")
            assert r.status_code == 404

    _run(go())
