"""Regression: ``FileResponse`` must cap the number of ranges and the
total body size built for a multipart/byteranges response.

Without caps, a client can send a ``Range: bytes=0-N, 0-N, 0-N, ...``
with thousands of copies of the whole file and trigger a many-GB
in-memory multipart body — a trivial DoS vector. Starlette's
``FileResponse`` refuses multi-range when the parts overflow
reasonable bounds (falls back to 416 or the full 200 body)."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


def _app(tmp_path, size):
    p = tmp_path / "f.bin"
    p.write_bytes(b"x" * size)
    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(p))

    return app


def test_many_range_requests_are_refused(tmp_path):
    """100 full-file ranges in one header MUST not produce a 100×-sized
    response body."""
    size = 1024
    c = TestClient(_app(tmp_path, size))
    many = ",".join([f"0-{size - 1}"] * 100)
    r = c.get("/f", headers={"Range": f"bytes={many}"})
    # Either 416 (refused) or 200 (full single body) are acceptable.
    # 206 with a 100× multipart body is NOT acceptable.
    if r.status_code == 206:
        assert len(r.content) < 10 * size, (
            f"multi-range body {len(r.content)}B is a >10× DoS amplification"
        )


def test_range_count_cap_enforced(tmp_path):
    """Even with a moderate count just above the cap, we fall back
    (either to a capped slice or single-range)."""
    size = 4096
    c = TestClient(_app(tmp_path, size))
    # 32 ranges — above the 16-range cap.
    ranges = ",".join(f"{i * 100}-{i * 100 + 99}" for i in range(32))
    r = c.get("/f", headers={"Range": f"bytes={ranges}"})
    # Server must not amplify: body should be ≤ 2× the file size
    # (single copy + boundary overhead), not 32× 100-byte slices in
    # multipart form.
    assert r.status_code in (200, 206, 416)
    assert len(r.content) < 2 * size + 4096


def test_total_byte_sum_cap_enforced(tmp_path):
    """10 overlapping full-file ranges — sum of range-lengths = 10×
    the file. Must be capped before we allocate a 10× buffer."""
    size = 8192
    c = TestClient(_app(tmp_path, size))
    ranges = ",".join([f"0-{size - 1}"] * 10)
    r = c.get("/f", headers={"Range": f"bytes={ranges}"})
    assert r.status_code in (200, 206, 416)
    assert len(r.content) < 3 * size  # hard ceiling: no amplification
