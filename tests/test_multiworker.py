"""Multi-worker (``app.run(workers=N)``) integration tests.

Spawns a real subprocess server that forks an fd-passing acceptor + N workers,
and drives it over loopback to assert the load-distribution + transport
guarantees:

  * **HTTP load spreads** across workers (round-robin among the least-loaded).
  * **File upload** (multipart) works through a worker — the worker owns the
    fd-passed socket directly, so large bodies stream in with no proxying.
  * **WebSocket is connection-sticky** — a connection's whole session stays on
    the one worker the fd was passed to (connection-oriented).

Skipped when loopback binds are denied or the platform has no ``os.fork``. macOS
needs ``OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`` for fork after framework init
(set for the child below).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.requires_loopback

if not hasattr(os, "fork"):
    pytest.skip("multi-worker mode needs os.fork", allow_module_level=True)

_SERVER = r"""
import os, hashlib, fastapi_turbo
from fastapi import FastAPI, WebSocket, UploadFile, File
app = FastAPI()

@app.get("/pid")
def pid():
    return {"pid": os.getpid()}

@app.post("/upload")
async def upload(f: UploadFile = File(...)):
    data = await f.read()
    return {"size": len(data), "md5": hashlib.md5(data).hexdigest(), "pid": os.getpid()}

@app.websocket("/ws")
async def ws(s: WebSocket):
    await s.accept()
    try:
        while True:
            m = await s.receive_text()
            await s.send_text(f"{os.getpid()}:{m}")
    except Exception:
        pass

app.run(host="127.0.0.1", port=PORT, workers=3)
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _server(port: int):
    env = dict(os.environ)
    env.pop("FASTAPI_TURBO_WORKERS", None)  # allow the multi-worker default/explicit
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"  # macOS fork-after-init
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER.replace("PORT", str(port))],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("multi-worker server did not bind in time")
        time.sleep(0.5)  # let all workers register with the acceptor
        yield
    finally:
        proc.send_signal(2)  # SIGINT → acceptor drains + reaps workers
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_multiworker_spread_upload_and_ws_sticky():
    httpx = pytest.importorskip("httpx")
    websockets = pytest.importorskip("websockets")
    port = _free_port()

    with _server(port):
        base = f"http://127.0.0.1:{port}"

        # (1) HTTP load spreads across workers (no keep-alive → each request is a
        # fresh fd-passed connection routed round-robin among the least-loaded).
        pids: dict[int, int] = {}
        with httpx.Client(headers={"connection": "close"}, timeout=5) as c:
            for _ in range(30):
                r = c.get(f"{base}/pid")
                assert r.status_code == 200
                p = r.json()["pid"]
                pids[p] = pids.get(p, 0) + 1
        assert len(pids) >= 2, f"HTTP did not spread across workers: {pids}"

        # (2) File upload (multipart) through a worker — size + content intact.
        blob = os.urandom(2 * 1024 * 1024)
        want = hashlib.md5(blob).hexdigest()
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"{base}/upload",
                files={"f": ("big.bin", blob, "application/octet-stream")},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["size"] == len(blob)
        assert body["md5"] == want

        # (3) WebSocket is connection-sticky: one connection's frames all hit the
        # SAME worker (the fd is passed once per connection).
        async def ws_worker_pids(label: str) -> set[str]:
            seen: set[str] = set()
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                for i in range(8):
                    await ws.send(f"{label}-{i}")
                    reply = await asyncio.wait_for(ws.recv(), timeout=5)
                    seen.add(reply.split(":", 1)[0])
            return seen

        async def run_ws() -> tuple[set[str], set[str]]:
            return await ws_worker_pids("A"), await ws_worker_pids("B")

        a, b = asyncio.run(run_ws())
        assert len(a) == 1, f"WS connection A not sticky to one worker: {a}"
        assert len(b) == 1, f"WS connection B not sticky to one worker: {b}"
