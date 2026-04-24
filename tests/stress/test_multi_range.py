"""FileResponse multi-range (RFC 7233 `multipart/byteranges`) parity.

Upstream FastAPI/Starlette's FileResponse honours `Range: bytes=a-b,c-d`
with a 206 multipart/byteranges body. fastapi_turbo must match the same
envelope (boundary + Content-Type/Content-Range per part + closing
boundary) so tools that parse the response (video players, downloaders)
don't break."""
from __future__ import annotations

import os
import tempfile

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


@pytest.fixture
def file20(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"0123456789ABCDEFGHIJ")  # 20 bytes
    return str(p)


def _app(path):
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(path)

    return app


def test_single_range_unchanged(file20):
    c = TestClient(_app(file20))
    r = c.get("/f", headers={"Range": "bytes=0-4"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-4/20"
    assert r.content == b"01234"
    assert not r.headers["content-type"].startswith("multipart/")


def test_multi_range_two(file20):
    c = TestClient(_app(file20))
    r = c.get("/f", headers={"Range": "bytes=0-4,10-14"})
    assert r.status_code == 206
    ct = r.headers["content-type"]
    assert ct.startswith("multipart/byteranges; boundary=")
    boundary = ct.split("boundary=", 1)[1]
    # Per-part Content-Range headers present
    assert b"Content-Range: bytes 0-4/20" in r.content
    assert b"Content-Range: bytes 10-14/20" in r.content
    # Payload bytes present
    assert b"01234" in r.content
    assert b"ABCDE" in r.content
    # Closing boundary at end
    assert r.content.endswith(f"--{boundary}--".encode())
    # Content-Length matches body
    assert int(r.headers["content-length"]) == len(r.content)


def test_multi_range_three_with_suffix(file20):
    """`bytes=0-1,5-6,-3` → first 2 bytes + middle 2 + last 3 bytes."""
    c = TestClient(_app(file20))
    r = c.get("/f", headers={"Range": "bytes=0-1,5-6,-3"})
    assert r.status_code == 206
    assert r.headers["content-type"].startswith("multipart/byteranges; boundary=")
    assert b"Content-Range: bytes 0-1/20" in r.content
    assert b"Content-Range: bytes 5-6/20" in r.content
    assert b"Content-Range: bytes 17-19/20" in r.content
    assert b"01" in r.content
    assert b"56" in r.content
    assert b"HIJ" in r.content


def test_multi_range_unsatisfiable_returns_416(file20):
    c = TestClient(_app(file20))
    r = c.get("/f", headers={"Range": "bytes=100-200,300-400"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */20"


def test_multi_range_skips_unsatisfiable_subrange(file20):
    """One good sub-range + one past-EOF sub-range → serve the good one."""
    c = TestClient(_app(file20))
    r = c.get("/f", headers={"Range": "bytes=0-4,100-200"})
    assert r.status_code == 206
    # Only the satisfiable sub-range is delivered. Since it's the sole
    # range, it's a plain single-range 206 (not multipart).
    assert r.headers["content-range"] == "bytes 0-4/20"
    assert r.content == b"01234"


def test_unique_boundary_across_responses(file20):
    c = TestClient(_app(file20))
    r1 = c.get("/f", headers={"Range": "bytes=0-1,3-4"})
    r2 = c.get("/f", headers={"Range": "bytes=0-1,3-4"})
    b1 = r1.headers["content-type"].split("boundary=", 1)[1]
    b2 = r2.headers["content-type"].split("boundary=", 1)[1]
    assert b1 != b2
