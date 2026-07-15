"""Multi-worker (``app.run(workers=N)``) SIGTERM graceful-shutdown tests.

systemd / docker / k8s all stop services with SIGTERM. In multi-worker mode
the parent process IS the fd-passing acceptor (``run_acceptor`` in Rust) and
the forked children run ``run_worker`` — so a graceful stop must:

  * catch SIGTERM in the acceptor (not die with the default disposition),
  * forward SIGTERM to the workers, which finish their in-flight requests
    (bounded by ``FASTAPI_TURBO_GRACE_SECONDS``, default 10s) before exiting,
  * reap every worker and exit the parent with code 0, and
  * complete immediately when idle — draining must not turn an idle Ctrl-C /
    SIGTERM into a multi-second hang.

Spawns a real ``workers=2`` subprocess server (same pattern as
``test_multiworker.py``). Skipped when loopback binds are denied or the
platform has no ``os.fork``. macOS needs
``OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`` for fork after framework init.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.requires_loopback

if not hasattr(os, "fork"):
    pytest.skip("multi-worker mode needs os.fork", allow_module_level=True)

# Grace window the server subprocess runs with (seconds). Small enough to keep
# the test bounded, large enough that the 2s in-flight request always fits.
GRACE = 6.0

_SERVER = r"""
import time, fastapi_turbo
from fastapi import FastAPI
app = FastAPI()

@app.get("/ping")
def ping():
    return {"ok": True}

@app.get("/slow")
def slow():
    time.sleep(2.0)
    return {"done": True}

app.run(host="127.0.0.1", port=PORT, workers=2)
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _child_pids(parent_pid: int) -> list[int]:
    """Direct children of ``parent_pid`` (the forked workers)."""
    psutil = pytest.importorskip("psutil")
    try:
        return [c.pid for c in psutil.Process(parent_pid).children()]
    except psutil.NoSuchProcess:
        return []


def _pid_alive(pid: int) -> bool:
    """True while ``pid`` is a live (non-zombie) process. A zombie has
    already exited — it just awaits its parent's reap — so it counts as gone
    for "every worker exited" purposes."""
    psutil = pytest.importorskip("psutil")
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@contextmanager
def _server(port: int):
    httpx = pytest.importorskip("httpx")
    env = dict(os.environ)
    env.pop("FASTAPI_TURBO_WORKERS", None)
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"  # macOS fork-after-init
    env["FASTAPI_TURBO_GRACE_SECONDS"] = str(GRACE)
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER.replace("PORT", str(port))],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        base = f"http://127.0.0.1:{port}"
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/ping", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("multi-worker server did not come up in time")
        time.sleep(0.5)  # let all workers register with the acceptor
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if proc.stderr is not None:
            proc.stderr.close()


def test_sigterm_drains_inflight_request_and_exits_clean():
    """SIGTERM mid-request: the in-flight request completes with 200, every
    worker exits within the grace window, and the parent exits 0."""
    httpx = pytest.importorskip("httpx")
    port = _free_port()

    with _server(port) as proc:
        workers = _child_pids(proc.pid)
        assert len(workers) == 2, f"expected 2 forked workers, found: {workers}"

        result: dict[str, object] = {}

        def fire_slow() -> None:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/slow", timeout=GRACE + 10)
                result["status"] = r.status_code
                result["body"] = r.json()
            except Exception as exc:  # dropped connection ⇒ recorded, asserted below
                result["error"] = repr(exc)

        t = threading.Thread(target=fire_slow)
        t.start()
        time.sleep(0.7)  # request is now in flight inside a worker (2s handler)

        os.kill(proc.pid, signal.SIGTERM)

        # (a) The in-flight request completes with 200 — not dropped.
        t.join(timeout=GRACE + 12)
        assert not t.is_alive(), "slow request never returned"
        assert result.get("error") is None, (
            f"in-flight request was dropped on SIGTERM: {result['error']}"
        )
        assert result.get("status") == 200
        assert result.get("body") == {"done": True}

        # (c) Parent exited cleanly (drained, reaped workers) — code 0, not -SIGTERM.
        try:
            rc = proc.wait(timeout=GRACE + 8)
        except subprocess.TimeoutExpired:
            pytest.fail(f"acceptor did not exit within grace window ({GRACE}s + margin)")
        stderr_tail = proc.stderr.read()[-2000:] if proc.stderr else ""
        assert rc == 0, f"acceptor exit code {rc} != 0; stderr: {stderr_tail}"

        # (b) Every worker process is gone within the grace window + margin.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(_pid_alive(pid) for pid in workers):
                break
            time.sleep(0.1)
        leftovers = [pid for pid in workers if _pid_alive(pid)]
        for pid in leftovers:  # don't leak processes out of a failing test
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not leftovers, f"workers still alive after grace window: {leftovers}"


def test_sigterm_idle_exits_promptly():
    """Idle server: SIGTERM exits promptly (well under the grace window) with
    code 0 — the drain has an idle fast-path, exactly like axum's graceful
    shutdown, so Ctrl-C/SIGTERM never hangs an idle server for ``grace``."""
    port = _free_port()

    with _server(port) as proc:
        workers = _child_pids(proc.pid)
        start = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)
        try:
            rc = proc.wait(timeout=GRACE - 2)  # must NOT take the whole grace window
        except subprocess.TimeoutExpired:
            pytest.fail("idle SIGTERM took >= grace window — no idle fast-path")
        elapsed = time.monotonic() - start
        stderr_tail = proc.stderr.read()[-2000:] if proc.stderr else ""
        assert rc == 0, f"idle acceptor exit code {rc} != 0; stderr: {stderr_tail}"
        assert elapsed < GRACE - 2, f"idle shutdown took {elapsed:.1f}s"

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(_pid_alive(pid) for pid in workers):
                break
            time.sleep(0.1)
        leftovers = [pid for pid in workers if _pid_alive(pid)]
        for pid in leftovers:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert not leftovers, f"workers still alive after idle SIGTERM: {leftovers}"
