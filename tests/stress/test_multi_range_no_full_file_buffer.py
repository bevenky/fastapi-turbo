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


def _range_diag(r, app, expect):
    """Build a self-explaining message for a range-response failure.

    A bare ``assert r.status_code == 206`` hides *why* a large-file Range
    request didn't return 206 (it was once seen failing under the full
    suite and the ``-q`` log gave no cause). Surface the status,
    content-type, content-length, a body head, and — for the in-process
    TestClient path — any server-side exception stashed on the app, so the
    next failure is diagnosable instead of silent.
    """
    captured = getattr(app, "_captured_server_exceptions", None)
    exc = repr(captured[-1]) if captured else "none"
    return (
        f"{expect}\n"
        f"  status={r.status_code} content-type={r.headers.get('content-type')!r}\n"
        f"  content-length={r.headers.get('content-length')!r} body_len={len(r.content)}\n"
        f"  body_head={r.content[:200]!r}\n"
        f"  captured_server_exc={exc}"
    )


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
        assert r.status_code == 206, _range_diag(r, app, "expected 206 multi-range")
        assert r.headers["content-type"].startswith(
            "multipart/byteranges; boundary="
        ), _range_diag(r, app, "expected multipart/byteranges content-type")
        # The three requested 16-byte windows must appear in the body.
        for start in (0, 4194304, 8388592):
            assert payload[start : start + 16] in r.content, _range_diag(
                r, app, f"window at offset {start} missing from body"
            )


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
        assert r.status_code == 206, _range_diag(r, app, "expected 206 single-range")
        assert len(r.content) == 100, _range_diag(r, app, "expected 100-byte slice")
        assert r.content == payload[start:], _range_diag(
            r, app, "single-range slice bytes mismatch"
        )
