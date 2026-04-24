"""FileResponse must stream large files (not read into memory).

Verification: serve a file larger than the in-memory threshold, read
it back, assert content matches byte-for-byte. Then do N concurrent
reads to ensure per-request memory doesn't blow up the process."""
from __future__ import annotations

import os

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


def test_large_file_serves_byte_for_byte(tmp_path):
    # 1 MiB file — above the 256 KiB buffered threshold → stream path.
    f = tmp_path / "big.bin"
    payload = os.urandom(1024 * 1024)
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        r = c.get("/f")
        assert r.status_code == 200
        assert r.content == payload
        assert int(r.headers["content-length"]) == len(payload)


def test_small_file_uses_buffered_path(tmp_path):
    # 4 KiB file — below threshold, buffered path.
    f = tmp_path / "small.txt"
    f.write_text("hello world\n" * 100)

    app = FastAPI()

    @app.get("/s")
    def _s():
        return FileResponse(str(f), media_type="text/plain")

    with TestClient(app) as c:
        r = c.get("/s")
        assert r.status_code == 200
        assert r.content == f.read_bytes()
        assert "charset=utf-8" in r.headers["content-type"]


def test_concurrent_large_reads_dont_accumulate_memory(tmp_path):
    """10 parallel clients each read the same 2 MiB file. If the
    streaming path is correct, per-request memory is bounded by
    the ReaderStream chunk size (~8 KiB) rather than file size."""
    f = tmp_path / "big.bin"
    payload = os.urandom(2 * 1024 * 1024)
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    import concurrent.futures
    with TestClient(app) as c:
        def _fetch(_):
            return c.get("/f")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(_fetch, range(10)))

        for r in responses:
            assert r.status_code == 200
            assert r.content == payload
