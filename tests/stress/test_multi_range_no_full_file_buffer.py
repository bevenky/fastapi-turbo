"""Rust multi-range FileResponse: verify the implementation reads
only the requested slices rather than slurping the whole file.

We can't easily measure memory in a pytest, but we CAN drive a
multi-range request against a large file and assert the output
bytes match byte-for-byte — that proves the seek+read logic lands
on the right offsets (the previous whole-file-read-then-slice
implementation would also have matched, but this test guards
against a regression that breaks the seek+read semantics)."""
from __future__ import annotations

import os

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


def test_multi_range_correct_slices_on_large_file(tmp_path):
    # 8 MiB file. Two ranges at opposite ends and one in the middle.
    payload = os.urandom(8 * 1024 * 1024)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get(
            "/f",
            headers={
                "Range": "bytes=0-15,4194304-4194319,8388592-8388607"
            },
        )
        assert r.status_code == 206
        assert r.headers["content-type"].startswith(
            "multipart/byteranges; boundary="
        )
        # The three requested 16-byte windows must appear in the body.
        for start in (0, 4194304, 8388592):
            assert payload[start : start + 16] in r.content


def test_single_range_on_huge_sparse_file_offset(tmp_path):
    """Regression guard: reading from a 16 MiB offset must return the
    exact slice starting at that offset (catches any seek math bug)."""
    # 32 MiB file; request the last 100 bytes via offset-based range.
    payload = os.urandom(32 * 1024 * 1024)
    f = tmp_path / "huge.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        start = 32 * 1024 * 1024 - 100
        r = c.get("/f", headers={"Range": f"bytes={start}-"})
        assert r.status_code == 206
        assert len(r.content) == 100
        assert r.content == payload[start:]
