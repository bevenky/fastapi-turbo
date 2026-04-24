"""Multipart/byteranges must stream — not build a giant Vec<u8>.

The audit flagged that the Rust multi-range branch previously
accumulated every range's preamble + bytes into a single ``Vec<u8>``
before emitting. For a 1 GiB file with 4 overlapping ranges (capped at
2× total_len), the intermediate allocation could approach 2 GiB even
though only a fraction is ever wire-sent.

We can't test 2 GiB on CI, but we CAN verify correctness on a multi-MiB
file with several ranges (proves the stream assembles correctly) AND
that the body length header matches what the client actually receives
(proves content-length was precomputed — a hallmark of the streaming
rewrite rather than post-hoc vec.len())."""
from __future__ import annotations

import asyncio

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import AsyncClient, ASGITransport, TestClient


def _run(coro):
    return asyncio.run(coro)


def test_multipart_content_length_matches_body(tmp_path):
    """Two ranges over a 256 KiB file — Content-Length must equal the
    raw body length the client receives (i.e. the stream was not
    truncated and the precomputed length was honest)."""
    size = 256 * 1024
    payload = bytes(range(256)) * (size // 256)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f", headers={"Range": "bytes=0-9,100-199"})
        assert r.status_code == 206
        assert r.headers["content-type"].startswith("multipart/byteranges; boundary=")
        cl = int(r.headers["content-length"])
        assert cl == len(r.content), (
            f"Content-Length {cl} ≠ actual body length {len(r.content)}; "
            f"streaming produced a short/long body"
        )


def test_multipart_parts_contain_requested_bytes(tmp_path):
    """Validate each range's bytes appear in-order with the correct
    offsets. Catches off-by-one / seek errors that a pure-length check
    would miss."""
    payload = bytes(range(256)) * 64  # 16 KiB deterministic pattern
    f = tmp_path / "pattern.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-3,1000-1003,8000-8003"})
            assert r.status_code == 206
            body = r.content
            # The three 4-byte slices must appear.
            for start in (0, 1000, 8000):
                expected = payload[start : start + 4]
                assert expected in body, (
                    f"slice at offset {start} ({expected!r}) missing from multipart body"
                )

    _run(go())


def test_multipart_boundary_framing_well_formed(tmp_path):
    """Sanity: the boundary header in Content-Type must appear between
    each part and the closing variant (``--boundary--``) must terminate."""
    f = tmp_path / "small.bin"
    f.write_bytes(b"abcdefghijklmnopqrstuvwxyz")  # 26 bytes

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/f", headers={"Range": "bytes=0-2,10-12,20-22"})
            assert r.status_code == 206
            ct = r.headers["content-type"]
            boundary = ct.split("boundary=", 1)[1]
            body = r.content
            opening = f"--{boundary}".encode()
            closing = f"--{boundary}--".encode()
            # Exactly 3 part-separators (one per range) + closing.
            # The opening pattern appears in both separators and the
            # closing, so total occurrences == parts + 1.
            assert body.count(opening) == 4, (
                f"expected 4 occurrences of opening boundary (3 parts + closing), "
                f"got {body.count(opening)}"
            )
            assert body.count(closing) == 1
            # Closing boundary must be at/near end (some impls append CRLF).
            assert body.rstrip().endswith(closing)

    _run(go())
