"""Phase 8 integration tests: OpenAPI schema, Swagger UI, and ReDoc."""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

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
    """Start a fastapi_turbo server with the given app code, return base_url."""
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


# ── OpenAPI JSON schema ────────────────────────────────────────────


def test_openapi_json(server_app):
    """OpenAPI JSON schema is served at /openapi.json."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        from pydantic import BaseModel
        app = FastAPI(title="Test API", version="1.0.0")

        class Item(BaseModel):
            name: str
            price: float

        @app.get("/items/{item_id}")
        def get_item(item_id: int):
            return {"id": item_id}

        @app.post("/items")
        def create_item(item: Item):
            return {"name": item.name}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["openapi"] == "3.1.0"
    assert schema["info"]["title"] == "Test API"
    assert schema["info"]["version"] == "1.0.0"
    assert "/items/{item_id}" in schema["paths"]
    assert "/items" in schema["paths"]
    assert "get" in schema["paths"]["/items/{item_id}"]
    assert "post" in schema["paths"]["/items"]

    # GET /items/{item_id} should have a path parameter
    get_op = schema["paths"]["/items/{item_id}"]["get"]
    assert "parameters" in get_op
    param_names = [p["name"] for p in get_op["parameters"]]
    assert "item_id" in param_names

    # POST /items should have a request body
    post_op = schema["paths"]["/items"]["post"]
    assert "requestBody" in post_op


def test_openapi_query_params(server_app):
    """OpenAPI schema captures query parameters."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI(title="Query Test", version="0.1.0")

        @app.get("/search")
        def search(q: str, limit: int = 10):
            return {"q": q, "limit": limit}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/openapi.json")
    assert r.status_code == 200
    schema = r.json()

    get_op = schema["paths"]["/search"]["get"]
    params = get_op["parameters"]

    q_param = next(p for p in params if p["name"] == "q")
    assert q_param["in"] == "query"
    assert q_param["required"] is True

    limit_param = next(p for p in params if p["name"] == "limit")
    assert limit_param["in"] == "query"
    assert limit_param["required"] is False
    assert limit_param["schema"]["default"] == 10


# ── Swagger UI ─────────────────────────────────────────────────────


def test_swagger_ui(server_app):
    """Swagger UI HTML is served at /docs."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/docs")
    assert r.status_code == 200
    assert "swagger-ui" in r.text.lower()


# ── ReDoc ──────────────────────────────────────────────────────────


def test_redoc(server_app):
    """ReDoc HTML is served at /redoc."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/redoc")
    assert r.status_code == 200
    assert "redoc" in r.text.lower()


# ── Docs disabled ──────────────────────────────────────────────────


def test_openapi_disabled(server_app):
    """OpenAPI and docs can be disabled by setting URLs to None."""
    url = server_app("""
        import fastapi_turbo  # noqa: F401 — installs compat shim

        from fastapi import FastAPI
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/openapi.json")
    assert r.status_code == 404
    r = httpx.get(f"{url}/docs")
    assert r.status_code == 404
    r = httpx.get(f"{url}/redoc")
    assert r.status_code == 404

    # User routes should still work
    r = httpx.get(f"{url}/hello")
    assert r.status_code == 200
    assert r.json() == {"message": "hello"}
