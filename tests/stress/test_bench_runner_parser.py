"""Regression: ``benchmarks/run_bench.py`` must parse the ``fastapi-
turbo-bench`` binary's current output format.

Bug: after the bench client CLI was updated, its output layout shifted
from ``"  client p50=... p99=... min=... | N req/s"`` to
``"  conn=X req=Y | p50=... p90=... | N req/s | M MB/s"``. The Python
runner kept looking for ``p50=`` in the *left* of the first pipe —
which now contains only ``conn=N req=N`` — and silently returned
``None`` or emitted ``-1`` stats in the generated markdown tables.

Parsing a real binary invocation against a live server is the only
way to catch a regression between the two sides.
"""
from __future__ import annotations

import importlib.util
import socket
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

ROOT = Path(__file__).resolve().parents[2]
BENCH_BIN = ROOT / "target" / "release" / "fastapi-turbo-bench"
RUNNER_PY = ROOT / "benchmarks" / "run_bench.py"


def _load_runner():
    import sys
    spec = importlib.util.spec_from_file_location("bench_runner_mod", RUNNER_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # ``dataclasses`` needs to find the module in ``sys.modules`` when
    # it resolves string annotations at class-creation time. Register
    # before exec.
    sys.modules["bench_runner_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_server(port: int):
    app = FastAPI()

    @app.get("/ping")
    def _ping():
        return {"ok": True}

    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port), daemon=True
    )
    t.start()
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail("server did not come up")


@pytest.mark.skipif(not BENCH_BIN.exists(), reason="release bench bin not built")
def test_runner_parses_current_bench_output_format():
    runner = _load_runner()

    port = _free_port()
    _start_server(port)

    # Point the runner's module-level constants at our test fixture.
    runner.BENCH_BIN = BENCH_BIN
    runner.HOST = "127.0.0.1"
    runner.N_REQS = 200
    runner.N_WARMUP = 50

    ep = runner.Endpoint(
        name="ping",
        path="/ping",
        method="GET",
        body="",
        content_type="application/json",
    )
    stats = runner._run_bench(port, ep)
    assert stats is not None, "parser returned None — format drift"
    # Every field must be populated with a real positive number.
    assert stats["p50"] > 0
    assert stats["p99"] > 0
    assert stats["min"] > 0
    assert stats["rps"] > 0
