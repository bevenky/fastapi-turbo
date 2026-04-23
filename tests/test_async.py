"""Phase 4 integration tests: async handler support."""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import json
import socket
import subprocess
import sys
import textwrap
import time

import pytest


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server_app(tmp_path):
    """Start a fastapi_turbo server with the given app code, return (proc, base_url).

    Usage: write app code to a file, start it, yield base_url, kill on cleanup.
    """
    procs = []

    def _start(code: str):
        port = _free_port()
        code = code.replace("__PORT__", str(port))
        app_file = tmp_path / "app.py"
        app_file.write_text(textwrap.dedent(code))
        proc = subprocess.Popen(
            [sys.executable, str(app_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(proc)
        # Wait for server to be ready
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
                if proc.poll() is not None:
                    out = proc.stdout.read().decode()
                    err = proc.stderr.read().decode()
                    pytest.fail(f"Server died on startup.\nstdout: {out}\nstderr: {err}")
        else:
            proc.kill()
            pytest.fail("Server did not start in time")
        return f"http://127.0.0.1:{port}"

    yield _start

    for p in procs:
        p.kill()
        p.wait()


# ── Async handler tests ─────────────────────────────────────────────


def test_async_handler(server_app):
    """Basic async handler works."""
    import httpx

    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/async-hello")
        async def hello():
            return {"message": "async hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/async-hello")
    assert r.status_code == 200
    assert r.json() == {"message": "async hello"}


def test_async_with_await(server_app):
    """Async handler that actually awaits something."""
    import httpx

    url = server_app("""
        import asyncio
        import fastapi_turbo  # noqa: F401 — installs compat shim
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/delayed")
        async def delayed():
            await asyncio.sleep(0.01)
            return {"message": "done"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/delayed")
    assert r.status_code == 200
    assert r.json() == {"message": "done"}


def test_async_with_path_params(server_app):
    """Async handler with path params."""
    import httpx

    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/users/{user_id}")
        async def get_user(user_id: int):
            return {"user_id": user_id}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/users/99")
    assert r.json() == {"user_id": 99}


def test_async_exception(server_app):
    """Async handler that raises HTTPException."""
    import httpx

    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI, HTTPException
        app = FastAPI()

        @app.get("/fail")
        async def fail():
            raise HTTPException(status_code=401, detail="Unauthorized")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/fail")
    assert r.status_code == 401
    assert r.json() == {"detail": "Unauthorized"}


def test_mixed_sync_async(server_app):
    """Mix of sync and async handlers in same app."""
    import httpx

    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/sync")
        def sync_handler():
            return {"type": "sync"}

        @app.get("/async")
        async def async_handler():
            return {"type": "async"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r1 = httpx.get(f"{url}/sync")
    assert r1.json() == {"type": "sync"}
    r2 = httpx.get(f"{url}/async")
    assert r2.json() == {"type": "async"}
