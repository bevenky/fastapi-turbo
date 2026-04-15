"""Phase 5 integration tests: Depends() dependency injection."""

import json
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server_app(tmp_path):
    """Start a fastapi_rs server with the given app code, return base_url."""
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


# ── Basic dependency injection ──────────────────────────────────────


def test_simple_depends(server_app):
    """Basic dependency injection."""
    url = server_app("""
        from fastapi_rs import FastAPI, Depends
        app = FastAPI()

        def get_db():
            return {"connected": True}

        @app.get("/check")
        def check(db=Depends(get_db)):
            return {"db": db}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/check")
    assert r.status_code == 200
    assert r.json() == {"db": {"connected": True}}


def test_depends_with_path_params(server_app):
    """Dependency alongside path params."""
    url = server_app("""
        from fastapi_rs import FastAPI, Depends
        app = FastAPI()

        def get_db():
            return "db_conn"

        @app.get("/users/{user_id}")
        def get_user(user_id: int, db=Depends(get_db)):
            return {"user_id": user_id, "db": db}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/users/42")
    assert r.status_code == 200
    assert r.json() == {"user_id": 42, "db": "db_conn"}


def test_nested_depends(server_app):
    """Two-level dependency chain."""
    url = server_app("""
        from fastapi_rs import FastAPI, Depends, Header
        app = FastAPI()

        def get_db():
            return {"db": "connected"}

        def get_current_user(db=Depends(get_db), authorization: str = Header()):
            return {"user": "alice", "db": db["db"], "token": authorization}

        @app.get("/me")
        def me(user=Depends(get_current_user)):
            return user

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/me", headers={"authorization": "Bearer xyz"})
    assert r.status_code == 200
    assert r.json() == {"user": "alice", "db": "connected", "token": "Bearer xyz"}


def test_shared_dependency(server_app):
    """Same dependency used by multiple consumers, resolved once (cached)."""
    url = server_app("""
        from fastapi_rs import FastAPI, Depends
        app = FastAPI()

        call_count = 0

        def get_db():
            global call_count
            call_count += 1
            return {"calls": call_count}

        @app.get("/check")
        def check(db1=Depends(get_db), db2=Depends(get_db)):
            return {"db1": db1, "db2": db2}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/check")
    assert r.status_code == 200
    data = r.json()
    # With caching, both should be the same object (called once)
    assert data["db1"]["calls"] == data["db2"]["calls"]


def test_async_depends(server_app):
    """Async dependency function."""
    url = server_app("""
        from fastapi_rs import FastAPI, Depends
        app = FastAPI()

        async def get_db():
            return {"async_db": True}

        @app.get("/check")
        async def check(db=Depends(get_db)):
            return db

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/check")
    assert r.status_code == 200
    assert r.json() == {"async_db": True}


def test_depends_with_query_params(server_app):
    """Dependency that consumes query parameters."""
    url = server_app("""
        from fastapi_rs import FastAPI, Depends
        app = FastAPI()

        def pagination(skip: int = 0, limit: int = 10):
            return {"skip": skip, "limit": limit}

        @app.get("/items")
        def list_items(paging=Depends(pagination)):
            return {"paging": paging}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/items?skip=5&limit=20")
    assert r.status_code == 200
    assert r.json() == {"paging": {"skip": 5, "limit": 20}}
