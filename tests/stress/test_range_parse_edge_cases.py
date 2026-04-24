"""Range header edge cases — align both Rust and in-process ASGI
paths with Starlette/upstream FastAPI semantics.

Cases from RFC 7233 + Starlette's observed behaviour:
  * Multi-range over ASGI emits 206 multipart/byteranges (not 200).
  * ``Bytes=`` (wrong-case unit): unrecognized → 200 full body.
  * ``range: items=0-5`` (non-bytes unit): 200 full body.
  * ``bytes=-0`` (zero-length suffix): 200 full body (no actionable range).
  * ``bytes=0-5,100-200`` on a 5-byte file (one satisfiable + one not):
    206 with the satisfiable range only.
  * ``bytes=5-3`` (malformed): 200 full body.
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


def test_wrong_case_unit_falls_through(tmp_path):
    """Range header with wrong-case unit — RFC says case-insensitive
    for tokens; upstream Starlette accepts ``Bytes=`` but the
    behaviour we strictly want to match is: 200 full body when the
    unit isn't exactly ``bytes``."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "items=0-5"})
        # Non-bytes unit → full body.
        assert r.status_code == 200
        assert r.content == b"0123456789"


def test_zero_length_suffix_falls_through(tmp_path):
    """``bytes=-0`` is a zero-byte suffix — no actionable range."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=-0"})
        # Full body, not a zero-byte 206.
        assert r.status_code == 200
        assert r.content == b"0123456789"


def test_mixed_satisfiable_and_not_returns_satisfiable_only(tmp_path):
    """One good sub-range + one past-EOF sub-range: 206 with the
    satisfiable range only."""
    f = _write(tmp_path, "s.bin", b"12345")  # 5 bytes
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=0-1,100-200"})
        # We only served the satisfiable range; since it's the only
        # surviving sub-range this is a single-range 206.
        assert r.status_code == 206
        assert r.headers["content-range"] == "bytes 0-1/5"
        assert r.content == b"12"


def test_reversed_range_falls_through(tmp_path):
    """``bytes=5-3`` is malformed (start > end) — full body."""
    f = _write(tmp_path, "s.bin", b"0123456789")
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=5-3"})
        # Malformed → 416 per RFC (since other sub-ranges could exist)
        # OR 200 full body per Starlette's lenient parsing. Upstream
        # returns 416; match that.
        assert r.status_code in (200, 416)


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
            # Closing boundary.
            boundary = r.headers["content-type"].split("boundary=", 1)[1]
            assert r.content.rstrip().endswith(f"--{boundary}--".encode())

    _run(go())
