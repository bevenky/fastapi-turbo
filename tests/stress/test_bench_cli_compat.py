"""Smoke test: the ``fastapi-turbo-bench`` binary accepts BOTH its
legacy positional form (HOST PORT PATH REQUESTS WARMUP [METHOD] [BODY]
[CT]) and the newer flag form (--requests / --method / --body / ...).

Audit finding: after the CLI flipped to flags, existing scripts
(benchmarks/run_bench.py, comparison/bench-app/run_benchmark_v3.sh)
silently ran every POST/PATCH/DELETE bench as GET because positional
method args were ignored. The backwards-compat parser now honours
positional METHOD/BODY/CT when the matching flag isn't set.

We also check the status-code sanity gate: a mistyped method or path
should abort instead of benchmarking a steady stream of 405s.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
# Spawns subprocess servers / runs the ``fastapi-turbo-bench`` binary
# against a live loopback port — needs ``socket.bind('127.0.0.1', 0)``
# to succeed. Skip cleanly in sandboxes that deny bind.
pytestmark = pytest.mark.requires_loopback


BENCH_BIN = Path(__file__).resolve().parents[2] / "target" / "release" / "fastapi-turbo-bench"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_server(port: int):
    import fastapi_turbo  # noqa: F401
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/ping")
    def _ping():
        return {"ok": True}

    @app.post("/echo")
    def _echo(body: dict):
        return body

    t = threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port), daemon=True)
    t.start()
    # Wait for it to listen.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"server did not come up on port {port}")


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench bin not built")
def test_bench_cli_positional_form_runs_post_as_post():
    port = _free_port()
    _start_server(port)
    # Old positional form — MUST be parsed correctly (not silently GET).
    proc = subprocess.run(
        [
            str(BENCH_BIN), "127.0.0.1", str(port), "/echo",
            "100", "20", "POST", '{"k":"v"}', "application/json",
        ],
        capture_output=True, text=True, timeout=15,
    )
    # Exit 0 means server answered 2xx to the warmup probe. GET against
    # the POST-only route would 405 and fail the status gate.
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "req=100" in proc.stdout


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench bin not built")
def test_bench_cli_flag_form_runs_post_as_post():
    port = _free_port()
    _start_server(port)
    proc = subprocess.run(
        [
            str(BENCH_BIN), "127.0.0.1", str(port), "/echo",
            "--requests", "100", "--warmup", "20",
            "--method", "POST", "--body", '{"k":"v"}',
            "--content-type", "application/json",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "req=100" in proc.stdout


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench bin not built")
def test_bench_cli_aborts_on_4xx_warmup():
    port = _free_port()
    _start_server(port)
    # /echo is POST-only; sending a GET must 405 and the bench must
    # exit with nonzero instead of silently benchmarking the error.
    proc = subprocess.run(
        [
            str(BENCH_BIN), "127.0.0.1", str(port), "/echo",
            "--requests", "100", "--warmup", "20",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode != 0
    assert "bad status" in (proc.stderr + proc.stdout).lower()
