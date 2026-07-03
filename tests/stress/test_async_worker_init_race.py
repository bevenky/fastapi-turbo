"""Regression: concurrent first submits must spawn exactly ONE worker loop.

Bug: ``_async_worker.init()`` had no lock around the ``if _loop is not
None: return`` fast path. First submits arrive concurrently from multiple
Rust/tokio threads; each racer that observed ``_loop is None`` spawned its
OWN worker thread + event loop, and the ``_loop`` module global was
overwritten by whichever ``_run`` published last. Connection pools
(asyncpg) then got created on one loop while later requests ran on another
— intermittent "got Future attached to a different loop" 500s at high
concurrency, plus historic redis-async variance.

The race only exists on FIRST touch, so the hammer must run in a FRESH
process: we exec a subprocess whose threads barrier-align on ``submit``
and then compare the running-loop id every coroutine actually executed on,
the ``_loops_started`` debug counter, ``id(get_loop())``, and the live
worker-thread count.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

_HAMMER = r"""
import asyncio, json, threading

import fastapi_turbo  # noqa: F401 — production import order (shims installed)
from fastapi_turbo import _async_worker

N_THREADS = 16
PER_THREAD = 25

barrier = threading.Barrier(N_THREADS)
seen_by_thread = [None] * N_THREADS
errors = []


async def whoami():
    return id(asyncio.get_running_loop())


def hammer(i):
    # Line every thread up so all N first-submits race init() together.
    barrier.wait()
    seen = set()
    try:
        for _ in range(PER_THREAD):
            seen.add(_async_worker.submit(whoami()))
    except BaseException as e:  # noqa: BLE001 — reported to the parent
        errors.append(repr(e))
    seen_by_thread[i] = seen


threads = [threading.Thread(target=hammer, args=(i,)) for i in range(N_THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

all_ids = set()
for s in seen_by_thread:
    if s:
        all_ids |= s

workers = [
    t for t in threading.enumerate() if t.name == "fastapi-turbo-async-worker"
]

print(json.dumps({
    "loop_ids": sorted(all_ids),
    "loops_started": _async_worker._loops_started,
    "get_loop_id": id(_async_worker.get_loop()),
    "worker_thread_count": len(workers),
    "errors": errors,
}))
"""


@pytest.mark.parametrize("attempt", range(3))
def test_concurrent_first_submit_creates_exactly_one_loop(attempt):
    """16 barrier-aligned threads submit 25 trivial coros each into a fresh
    process. Every coroutine must run on the SAME loop, exactly one loop
    must ever have been started, and exactly one worker thread must exist."""
    proc = subprocess.run(
        [sys.executable, "-c", _HAMMER],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"hammer subprocess died:\n{proc.stderr}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["errors"] == [], (
        f"submits raised in the fresh process: {report['errors']}"
    )
    assert report["loops_started"] == 1, (
        f"init race spawned {report['loops_started']} worker loops "
        f"(attempt {attempt})"
    )
    assert len(report["loop_ids"]) == 1, (
        f"coroutines ran on {len(report['loop_ids'])} distinct loops: "
        f"{report['loop_ids']} (attempt {attempt})"
    )
    assert report["loop_ids"][0] == report["get_loop_id"], (
        "get_loop() returned a different loop than the one serving submits"
    )
    assert report["worker_thread_count"] == 1, (
        f"{report['worker_thread_count']} fastapi-turbo-async-worker threads alive"
    )
