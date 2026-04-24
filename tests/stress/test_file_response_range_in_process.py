"""``FileResponse`` must honour the ``Range:`` header when dispatched
via the in-process ASGI path — same contract as the Rust server.

Previously ``_send_file_response_asgi`` streamed the full file
regardless of the request's ``Range`` header. A client asking for
``bytes=2-5`` got ``200`` with the whole body; upstream FastAPI /
Starlette return ``206`` + ``Content-Range: bytes 2-5/<total>``
+ the sliced bytes. Real drop-in risk for video players, download
resumers, and any test suite that exercises range semantics via
``TestClient`` / ``ASGITransport``."""
from __future__ import annotations

import asyncio
import os

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import AsyncClient, ASGITransport, TestClient


def _run(coro):
    return asyncio.run(coro)


def test_range_bytes_slice(tmp_path):
    payload = b"0123456789"
    f = tmp_path / "s.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=2-5"})
            assert r.status_code == 206
            assert r.headers["content-range"] == "bytes 2-5/10"
            assert r.content == b"2345"
            assert int(r.headers["content-length"]) == 4

    _run(go())


def test_suffix_range_in_process(tmp_path):
    payload = b"abcdefghij"
    f = tmp_path / "s.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app, in_process=True) as c:
        r = c.get("/f", headers={"Range": "bytes=-3"})
        assert r.status_code == 206
        assert r.content == b"hij"
        assert r.headers["content-range"] == "bytes 7-9/10"


def test_open_end_range_in_process(tmp_path):
    payload = b"0123456789"
    f = tmp_path / "s.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app, in_process=True) as c:
        r = c.get("/f", headers={"Range": "bytes=4-"})
        assert r.status_code == 206
        assert r.content == b"456789"
        assert r.headers["content-range"] == "bytes 4-9/10"


def test_unsatisfiable_range_in_process(tmp_path):
    payload = b"short"
    f = tmp_path / "s.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app, in_process=True) as c:
        r = c.get("/f", headers={"Range": "bytes=100-200"})
        assert r.status_code == 416
        assert r.headers.get("content-range") == "bytes */5"


def test_no_range_still_serves_full_file_in_process(tmp_path):
    """Regression: absence of Range header must continue to serve 200
    with the complete body."""
    payload = os.urandom(1024)
    f = tmp_path / "s.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app, in_process=True) as c:
        r = c.get("/f")
        assert r.status_code == 200
        assert r.content == payload
