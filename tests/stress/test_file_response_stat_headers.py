"""FileResponse parity: Last-Modified + ETag stamping, plus If-Range
gating. Starlette auto-stamps both on every serve (via ``set_stat_headers``
called from ``__call__``) and honours If-Range so clients can re-validate
partial downloads safely.

Verified against upstream Starlette / FastAPI behaviour."""
from __future__ import annotations

import asyncio

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import AsyncClient, ASGITransport, TestClient


def _write(tmp, name, content):
    f = tmp / name
    f.write_bytes(content)
    return f


def _run(coro):
    return asyncio.run(coro)


def test_stat_headers_stamped_on_full_response(tmp_path):
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        assert "last-modified" in r.headers
        assert "etag" in r.headers
        # ETag format: quoted string (per RFC 7232).
        assert r.headers["etag"].startswith('"') and r.headers["etag"].endswith('"')


def test_stat_headers_stamped_on_range_response(tmp_path):
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=0-1"})
        assert r.status_code == 206
        assert "last-modified" in r.headers
        assert "etag" in r.headers


def test_if_range_matching_etag_serves_206(tmp_path):
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            # First fetch to get the ETag.
            r0 = await cli.get("/f")
            etag = r0.headers["etag"]
            # Same ETag on If-Range → server honours Range.
            r = await cli.get(
                "/f",
                headers={"Range": "bytes=0-1", "If-Range": etag},
            )
            assert r.status_code == 206
            assert r.content == b"01"

    _run(go())


def test_if_range_mismatched_etag_falls_back_to_200(tmp_path):
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get(
                "/f",
                headers={
                    "Range": "bytes=0-1",
                    "If-Range": '"bogus-etag"',
                },
            )
            # Mismatched validator → server ignores Range, serves 200 full.
            assert r.status_code == 200
            assert r.content == b"0123456789"

    _run(go())


def test_if_range_matching_last_modified_serves_206(tmp_path):
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r0 = await cli.get("/f")
            lm = r0.headers["last-modified"]
            r = await cli.get(
                "/f",
                headers={"Range": "bytes=0-1", "If-Range": lm},
            )
            assert r.status_code == 206
            assert r.content == b"01"

    _run(go())
