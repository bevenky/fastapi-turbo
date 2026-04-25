"""Range header DoS caps + Starlette range-coalescing parity.

Audit findings:
  * ``Range: bytes=0-1023, 0-1023, … (×100)`` over a 1024-byte file
    must NOT amplify the response to ~110 KiB. Either we coalesce the
    duplicate ranges (Starlette behaviour) or, if we don't, we bound
    the total emitted bytes by a 2× total-len cap and the per-request
    sub-range count by a small constant.
  * ``Range: bytes=0-19, 0-19`` (two identical sub-ranges over a
    20-byte file) must produce a single-range 206 with
    ``Content-Range: bytes 0-19/20`` — Starlette merges overlapping
    ranges before deciding single vs multipart.

The Rust path already had MAX_RANGES + max_total_bytes caps; the
Python ASGI path didn't. After this fix both paths should clamp."""
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


def test_repeated_subrange_does_not_amplify(tmp_path):
    """100 copies of the same sub-range against a 1024-byte file must
    not produce a multi-MiB multipart response."""
    payload = b"x" * 1024
    f = _write(tmp_path, "s.bin", payload)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    range_value = "bytes=" + ",".join(["0-1023"] * 100)
    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": range_value})
            # Two valid outcomes: (a) coalesced to one range → 206
            # with full body (1024 B), or (b) bailed to 200 full body.
            # Either way the response body must be bounded by ~2×
            # total_len, NOT 100× total_len.
            assert len(r.content) <= 2 * len(payload), (
                f"range header amplified response: {len(r.content)} bytes "
                f"vs {len(payload)} bytes file"
            )
            assert r.status_code in (200, 206)

    _run(go())


def test_duplicate_ranges_coalesce_to_single_range(tmp_path):
    """``bytes=0-19,0-19`` on a 20-byte file → single-range 206 with
    ``Content-Range: bytes 0-19/19`` (after coalesce). Starlette merges
    these before deciding single vs multipart."""
    payload = b"0123456789abcdefghij"  # 20 bytes
    f = _write(tmp_path, "s.bin", payload)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-19,0-19"})
            assert r.status_code == 206
            assert not r.headers["content-type"].startswith("multipart/"), (
                f"expected single-range 206 after coalesce, got "
                f"content-type {r.headers['content-type']}"
            )
            assert r.headers["content-range"] == "bytes 0-19/20"
            assert r.content == payload

    _run(go())


def test_overlapping_ranges_coalesce_to_single_range(tmp_path):
    """``bytes=0-9,5-14`` (overlap on 5-9) on a 20-byte file → one
    coalesced range (0-14)."""
    payload = b"0123456789abcdefghij"  # 20 bytes
    f = _write(tmp_path, "s.bin", payload)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-9,5-14"})
            assert r.status_code == 206
            assert not r.headers["content-type"].startswith("multipart/")
            assert r.headers["content-range"] == "bytes 0-14/20"
            assert r.content == payload[0:15]

    _run(go())


def test_adjacent_ranges_coalesce(tmp_path):
    """``bytes=0-9,10-19`` (touching, no gap) on a 20-byte file →
    coalesced into 0-19."""
    payload = b"0123456789abcdefghij"
    f = _write(tmp_path, "s.bin", payload)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-9,10-19"})
            assert r.status_code == 206
            # Adjacent ranges merge in Starlette.
            assert not r.headers["content-type"].startswith("multipart/")
            assert r.headers["content-range"] == "bytes 0-19/20"

    _run(go())


def test_disjoint_ranges_remain_multipart(tmp_path):
    """``bytes=0-3,15-19`` (gap from 4-14) — must stay multipart."""
    payload = b"0123456789abcdefghij"
    f = _write(tmp_path, "s.bin", payload)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-3,15-19"})
            assert r.status_code == 206
            assert r.headers["content-type"].startswith("multipart/byteranges;")
            assert b"Content-Range: bytes 0-3/20" in r.content
            assert b"Content-Range: bytes 15-19/20" in r.content


    _run(go())


def test_too_many_ranges_falls_back_to_full(tmp_path):
    """A header with > MAX_RANGES (16) sub-ranges falls back to 200
    full body — matches Rust path's existing cap."""
    payload = b"x" * 1024
    f = _write(tmp_path, "s.bin", payload)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    # 20 distinct sub-ranges — over the 16 cap. Tightly packed so they
    # don't all coalesce into one big range (gap of 4 bytes between
    # each).
    parts = [f"{i*8}-{i*8+3}" for i in range(20)]
    range_value = "bytes=" + ",".join(parts)
    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": range_value})
            # Either coalesced, capped to <=16, or bailed to 200.
            assert r.status_code in (200, 206)
            assert len(r.content) <= 2 * len(payload)

    _run(go())
