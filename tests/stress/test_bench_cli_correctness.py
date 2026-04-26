"""bench_client correctness regressions: argument validation,
work-distribution, and per-response status gate."""
from __future__ import annotations

import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
# Spawns subprocess servers / runs the ``fastapi-turbo-bench`` binary
# against a live loopback port — needs ``socket.bind('127.0.0.1', 0)``
# to succeed. Skip cleanly in sandboxes that deny bind.
pytestmark = pytest.mark.requires_loopback


import fastapi_turbo  # noqa: F401

from fastapi import FastAPI

BENCH_BIN = Path(__file__).resolve().parents[2] / "target" / "release" / "fastapi-turbo-bench"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start(port: int):
    app = FastAPI()

    @app.get("/ok")
    def _ok():
        return {"ok": True}

    @app.get("/sometimes")
    def _sometimes():
        # A server that returns 500 deterministically — the bench
        # client should NOT count these as successful.
        from fastapi.responses import JSONResponse
        return JSONResponse({"err": True}, status_code=500)

    t = threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port), daemon=True)
    t.start()
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail("server never came up")


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench binary not built")
def test_bench_rejects_zero_connections():
    proc = subprocess.run(
        [str(BENCH_BIN), "127.0.0.1", "8000", "/x", "--connections", "0"],
        capture_output=True, text=True, timeout=5,
    )
    assert proc.returncode == 2
    assert "connections" in proc.stderr


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench binary not built")
def test_bench_rejects_zero_requests():
    port = _free_port()
    _start(port)
    proc = subprocess.run(
        [str(BENCH_BIN), "127.0.0.1", str(port), "/ok", "--requests", "0"],
        capture_output=True, text=True, timeout=5,
    )
    assert proc.returncode == 2
    assert "requests" in proc.stderr


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench binary not built")
def test_bench_rejects_connections_exceeding_requests():
    port = _free_port()
    _start(port)
    proc = subprocess.run(
        [str(BENCH_BIN), "127.0.0.1", str(port), "/ok",
         "--requests", "5", "--connections", "10"],
        capture_output=True, text=True, timeout=5,
    )
    assert proc.returncode == 2
    assert "connections" in proc.stderr and "requests" in proc.stderr


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench binary not built")
def test_bench_distributes_remainder():
    """100 requests / 7 connections = 14 r14 — 2 workers get 15, 5 get
    14 → total 100 samples (not 98)."""
    port = _free_port()
    _start(port)
    proc = subprocess.run(
        [str(BENCH_BIN), "127.0.0.1", str(port), "/ok",
         "--requests", "100", "--warmup", "0", "--connections", "7"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    # Output line format: "  conn=N req=N | ...". ``req=`` reports the
    # actual sample count.
    import re
    m = re.search(r"req=(\d+)", proc.stdout)
    assert m is not None, f"no req= in output: {proc.stdout!r}"
    assert int(m.group(1)) == 100, f"expected 100 samples, got {m.group(1)}"


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench binary not built")
def test_bench_excludes_5xx_responses_from_latency():
    """Server that returns steady 500s: warmup aborts with rc=2."""
    port = _free_port()
    _start(port)
    proc = subprocess.run(
        [str(BENCH_BIN), "127.0.0.1", str(port), "/sometimes",
         "--requests", "50", "--warmup", "5"],
        capture_output=True, text=True, timeout=10,
    )
    # Warmup catches the 500 and aborts with rc=2.
    assert proc.returncode == 2
    assert "bad status" in proc.stderr.lower()
