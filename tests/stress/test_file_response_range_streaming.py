"""Range FileResponse must stream large slices without buffering the
whole file. Previously ``file_response_with_range`` did
``std::fs::read(path)`` unconditionally; the audit flagged that large
video seeks / concurrent partial downloads would allocate full files
per request."""
from __future__ import annotations

import os

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


def test_large_range_returns_exact_slice(tmp_path):
    # 4 MiB file — well above the 256 KiB stream threshold.
    payload = os.urandom(4 * 1024 * 1024)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        # Middle-of-file range: 1 MiB starting at offset 2 MiB.
        r = c.get("/f", headers={"Range": "bytes=2097152-3145727"})
        assert r.status_code == 206
        assert r.headers["content-range"] == "bytes 2097152-3145727/4194304"
        assert int(r.headers["content-length"]) == 1048576
        assert r.content == payload[2097152:3145728]


def test_concurrent_large_ranges_dont_buffer_whole_file(tmp_path):
    """10 concurrent clients each requesting a 1 MiB range from a
    4 MiB file. If the streaming path works, per-request memory is
    bounded by the ReaderStream chunk size; the test's sanity check
    is just that all 10 get the correct byte slice."""
    payload = os.urandom(4 * 1024 * 1024)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    import concurrent.futures
    with TestClient(app) as c:
        def _fetch(i):
            offset = i * 100 * 1024
            r = c.get(
                "/f",
                headers={"Range": f"bytes={offset}-{offset + 1048575}"},
            )
            return r, offset

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(_fetch, range(10)))

        for r, offset in responses:
            assert r.status_code == 206
            assert r.content == payload[offset:offset + 1048576]


def test_suffix_range_works_on_large_file(tmp_path):
    """``Range: bytes=-N`` for the last N bytes — the seek+take path
    must compute the start correctly from the file size."""
    payload = os.urandom(2 * 1024 * 1024)
    f = tmp_path / "big.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        # Last 512 KiB.
        r = c.get("/f", headers={"Range": "bytes=-524288"})
        assert r.status_code == 206
        assert len(r.content) == 524288
        assert r.content == payload[-524288:]
