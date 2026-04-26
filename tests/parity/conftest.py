"""Shared fixtures for parity tests: start FastAPI + fastapi-turbo once per session.

The parity matrix spawns SUBPROCESS servers for both upstream
FastAPI and turbo on real loopback ports — those processes need
``socket.bind(('127.0.0.1', 0))`` to succeed. In sandboxed
environments where loopback bind is denied, every parity test
would fail with ``PermissionError`` instead of a useful skip.
The collection hook below skips the entire parity directory in
that mode."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest


def pytest_collection_modifyitems(config: pytest.Config, items):
    """Skip every parity test when loopback bind is denied —
    they all spawn subprocess servers that need real ports."""
    try:
        from tests.conftest import LOOPBACK_DENIED
    except ImportError:
        # Older test trees without the suite-level conftest still
        # import this directly; fall back to a fresh probe.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
            return
        except (PermissionError, OSError):
            LOOPBACK_DENIED = True
    if not LOOPBACK_DENIED:
        return
    skipper = pytest.mark.skip(
        reason="parity tests need real loopback ports for subprocess "
        "servers (FastAPI + turbo); sandbox denies bind."
    )
    for item in items:
        # Only apply to items collected under tests/parity/.
        if "tests/parity" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skipper)


PYTHON = sys.executable
TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_for_server(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


class DualServers:
    """Manages FastAPI + fastapi-turbo server processes for a parity app."""

    def __init__(self):
        self.fa_port = _free_port()
        self.rs_port = _free_port()
        self.fa_proc = None
        self.rs_proc = None

    def start(self):
        self.fa_proc = subprocess.Popen(
            [PYTHON, "-c", f"""
import sys, os
os.environ["FASTAPI_TURBO_NO_SHIM"] = "1"
sys.path.insert(0, "{TEST_DIR}")
import uvicorn
from parity_app import app
uvicorn.run(app, host="127.0.0.1", port={self.fa_port}, log_level="error")
"""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        self.rs_proc = subprocess.Popen(
            [PYTHON, "-c", f"""
import sys, os
import fastapi_turbo.compat
fastapi_turbo.compat.install()
sys.path.insert(0, "{TEST_DIR}")
from parity_app import app
app.run("127.0.0.1", {self.rs_port})
"""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        assert _wait_for_server(self.fa_port), f"FastAPI failed to start on :{self.fa_port}"
        assert _wait_for_server(self.rs_port), f"fastapi-turbo failed to start on :{self.rs_port}"

    def stop(self):
        for proc in [self.fa_proc, self.rs_proc]:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass


@pytest.fixture(scope="session")
def dual_servers():
    """Session-scoped fixture: start both servers once."""
    servers = DualServers()
    servers.start()
    yield servers
    servers.stop()


@pytest.fixture(scope="session")
def client():
    """Session-scoped httpx client."""
    with httpx.Client(timeout=5.0) as c:
        yield c
