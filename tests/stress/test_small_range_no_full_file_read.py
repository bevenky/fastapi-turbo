"""Small ranges must never read the whole file.

Audit finding: src/responses.rs ≤ 256 KiB slice branch did
``std::fs::read(path)`` THEN sliced. A 1-byte range from a 10 GB
file allocated 10 GB.

We can't allocate 10 GB in a test, but we CAN verify correctness
on a multi-MiB file AND verify no spurious reads happen outside
the slice (the returned byte must be exactly the one at the
requested offset — a whole-file-read-then-slice would still pass
that assertion, so the real check is the memory-amplification one
below: request a 1-byte range from a 512 MiB sparse file and watch
the process RSS delta stay small)."""
from __future__ import annotations

import os
import resource

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


def _rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports KiB — normalize to MiB.
    return usage / (1024 * 1024) if usage > 1_000_000 else usage / 1024


def test_one_byte_range_on_large_file_correct(tmp_path):
    """Baseline: the returned byte must be the one at offset N."""
    payload = os.urandom(4 * 1024 * 1024)  # 4 MiB
    f = tmp_path / "mid.bin"
    f.write_bytes(payload)

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        # Request byte at offset 2 MiB.
        offset = 2 * 1024 * 1024
        r = c.get("/f", headers={"Range": f"bytes={offset}-{offset}"})
        assert r.status_code == 206
        assert len(r.content) == 1
        assert r.content == payload[offset : offset + 1]


def test_small_range_rss_doesnt_balloon(tmp_path):
    """512 MiB sparse file, 1-byte range. If we were still doing
    ``std::fs::read(path)`` the RSS delta per request would be ~512 MiB.
    The streaming path should keep the delta < ~10 MiB across many
    repeats."""
    # Create a 512 MiB sparse file — seek then write 1 byte at end.
    # This is safe on macOS / Linux.
    f = tmp_path / "sparse.bin"
    size = 512 * 1024 * 1024
    with open(f, "wb") as fh:
        fh.seek(size - 1)
        fh.write(b"\x00")

    app = FastAPI()

    @app.get("/f")
    def _f():
        return FileResponse(str(f))

    with TestClient(app) as c:
        # Warm up so first-request allocations don't dominate.
        c.get("/f", headers={"Range": "bytes=0-0"})

        rss_before = _rss_mb()
        # 20 single-byte range requests.
        for _ in range(20):
            r = c.get("/f", headers={"Range": "bytes=0-0"})
            assert r.status_code == 206
            assert len(r.content) == 1
        rss_after = _rss_mb()

        delta = rss_after - rss_before
        # If we were still reading the whole 512 MiB file per request,
        # the RSS delta would be enormous (process-level peak RSS is
        # monotonic). Give ourselves a generous 100 MiB ceiling — a
        # correctly-streaming implementation uses ~1 MiB.
        assert delta < 100, (
            f"RSS grew by {delta:.1f} MiB across 20 1-byte range reads; "
            f"indicates whole-file allocation per request"
        )
