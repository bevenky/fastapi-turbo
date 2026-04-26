"""Phase 4 integration tests: async handler support."""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import json
import socket
import subprocess
import sys
import textwrap
import time

import pytest




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
