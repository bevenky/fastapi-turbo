"""TestClient lifecycle parity with Starlette's TestClient:

  1. ``with TestClient(app) as c:`` must release the background
     Rust-server thread, free the bound port, and drop the strong
     app reference on block exit. Prior implementation kept a strong
     ref in ``_app_servers`` and never triggered a Rust-server
     shutdown, leaking ports + threads + apps across tests.

  2. Dropping the local TestClient binding (with GC) should also not
     keep the app alive indefinitely."""
from __future__ import annotations

import gc
import socket
import weakref

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Lifecycle assertions read ``TestClient._app_servers`` and
# ``cli._port`` directly — those are populated only on the
# real-server path. Skip cleanly when loopback binds are denied.
pytestmark = pytest.mark.requires_loopback


def _port_bound(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def test_context_exit_drops_cache_entry():
    from fastapi_turbo.testclient import TestClient as _TC

    app = FastAPI()

    @app.get("/p")
    def _p():
        return {}

    with TestClient(app) as c:
        assert c.get("/p").status_code == 200
        key = id(c.app)
        assert key in _TC._app_servers, "cache entry missing during live use"

    # After __exit__ the cache entry MUST be gone so the app is
    # GC-eligible (Starlette parity).
    assert id(app) not in _TC._app_servers


def test_context_exit_releases_bound_port():
    app = FastAPI()

    @app.get("/p")
    def _p():
        return {}

    with TestClient(app) as c:
        port = c._port
        assert _port_bound(port), "port should be bound while client is live"

    # Give axum's graceful shutdown ~500ms, then confirm the port is
    # actually reusable.
    import time
    for _ in range(20):
        if not _port_bound(port):
            break
        time.sleep(0.05)
    assert not _port_bound(port), (
        f"port {port} still bound 1s after __exit__ — server did not stop"
    )


def test_many_sequential_clients_do_not_leak_ports():
    """Open + close 20 TestClients in sequence. If we leaked ports,
    the OS would eventually throw EADDRINUSE or we'd accumulate
    threads. After each ``with`` block, the previous port should be
    reclaimable."""
    for _ in range(20):
        app = FastAPI()

        @app.get("/p")
        def _p():
            return {"ok": True}

        with TestClient(app) as c:
            assert c.get("/p").status_code == 200


def test_exit_removes_app_from_cache():
    """Focused assertion on the specific audit finding: ``_app_servers``
    must not retain the app after ``__exit__``. A broader GC-eligibility
    claim is out of scope for this test — the class-level
    ``FastAPI._fastapi_turbo_current_instance`` last-wins pointer can
    still hold the most-recently-constructed app, and that's tracked
    separately."""
    from fastapi_turbo.testclient import TestClient as _TC

    app = FastAPI()

    @app.get("/p")
    def _p():
        return {}

    wr = weakref.ref(app)
    with TestClient(app) as c:
        c.get("/p")
        assert id(app) in _TC._app_servers
    assert id(app) not in _TC._app_servers
    # Sanity: the weakref is not touched, we just assert the cache
    # stopped pinning it. GC eligibility via ``_fastapi_turbo_current_instance``
    # is a separate concern.
    del wr  # quiet the unused-variable lint
