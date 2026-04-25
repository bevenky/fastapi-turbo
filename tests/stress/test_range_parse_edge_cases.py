"""Range header edge cases — Rust and in-process ASGI paths must
match upstream Starlette/FastAPI semantics exactly.

Cases verified against Starlette 1.0.0:
  * Multi-range over ASGI emits 206 multipart/byteranges (not 200).
  * ``Bytes=`` (case-different unit token): accepted, returns 206.
  * ``items=…`` (non-bytes unit): 400 Bad Request.
  * ``bytes=-0`` (zero-length suffix): 416 (start = file_size, out of bounds).
  * ``bytes=0-5,100-200`` on a 5-byte file: 416 (any sub-range past EOF).
  * ``bytes=5-3`` (reversed): 400 Bad Request.
  * ``bytes=abc-def`` (non-numeric): 400 (no parseable sub-ranges).
"""
from __future__ import annotations

import asyncio

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import AsyncClient, ASGITransport, TestClient


def _run(coro):
    return asyncio.run(coro)


def _write(tmp, name, content):
    f = tmp / name
    f.write_bytes(content)
    return f


def test_asgi_multi_range_emits_206_multipart(tmp_path):
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-1,4-5,8-9"})
            assert r.status_code == 206
            assert r.headers["content-type"].startswith(
                "multipart/byteranges; boundary="
            )
            # All three ranges appear in the body.
            for window in (b"01", b"45", b"89"):
                assert window in r.content

    _run(go())


def test_wrong_unit_token_returns_400(tmp_path):
    """Non-``bytes`` unit token (case-insensitive comparison) →
    400 Bad Request. Matches upstream Starlette exactly."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "items=0-5"})
        assert r.status_code == 400


def test_case_insensitive_bytes_unit_accepted(tmp_path):
    """``Bytes=`` (capitalised B) is the same token as ``bytes=`` per
    HTTP token semantics — Starlette lower-cases the unit before
    comparing, and we match. → 206 single range."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "Bytes=0-1"})
        assert r.status_code == 206
        assert r.content == b"01"


def test_zero_length_suffix_returns_416(tmp_path):
    """``bytes=-0`` parses as ``start = file_size - 0 = file_size``,
    which fails the ``0 <= start < file_size`` bounds check →
    416 RangeNotSatisfiable. Matches Starlette."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=-0"})
        assert r.status_code == 416


def test_any_subrange_past_eof_returns_416(tmp_path):
    """One satisfiable + one past-EOF sub-range → 416, not a partial
    206. Starlette rejects the WHOLE header when any sub-range fails
    the bounds check."""
    f = _write(tmp_path, "s.bin", b"12345")  # 5 bytes
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=0-1,100-200"})
        assert r.status_code == 416


def test_reversed_range_returns_400(tmp_path):
    """``bytes=5-3`` (start > end) → 400 Bad Request. Starlette raises
    MalformedRangeHeader; we mirror that as a 400 status."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=5-3"})
        assert r.status_code == 400


def test_non_numeric_bounds_returns_400(tmp_path):
    """``bytes=abc-def`` parses no satisfiable sub-ranges → 400."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=abc-def"})
        assert r.status_code == 400


def test_asgi_two_range_multipart(tmp_path):
    f = _write(tmp_path, "s.bin", b"abcdefghijklmnop")  # 16 bytes
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-2,10-12"})
            assert r.status_code == 206
            assert r.headers["content-type"].startswith(
                "multipart/byteranges; boundary="
            )
            assert b"abc" in r.content
            assert b"klm" in r.content
            boundary = r.headers["content-type"].split("boundary=", 1)[1]
            # Closing boundary at end (no trailing CRLF — Starlette parity).
            assert r.content.endswith(f"--{boundary}--".encode())

    _run(go())
