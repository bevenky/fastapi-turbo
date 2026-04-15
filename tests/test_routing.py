"""Phase 2 integration tests: server, routing, extractors, responses."""

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
    """Start a fastapi_rs server with the given app code, return (proc, base_url).

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


# ── Basic routing ────────────────────────────────────────────────────


def test_get_hello(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/hello")
    assert r.status_code == 200
    assert r.json() == {"message": "hello"}


def test_post_json_body(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        from pydantic import BaseModel

        app = FastAPI()

        class Item(BaseModel):
            name: str
            price: float

        @app.post("/items")
        def create_item(item: Item):
            return {"name": item.name, "price": item.price}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.post(f"{url}/items", json={"name": "widget", "price": 9.99})
    assert r.status_code == 200
    assert r.json() == {"name": "widget", "price": 9.99}


def test_path_params(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/users/{user_id}")
        def get_user(user_id: int):
            return {"user_id": user_id}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/users/42")
    assert r.status_code == 200
    assert r.json() == {"user_id": 42}


def test_query_params(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/search")
        def search(q: str, limit: int = 10):
            return {"q": q, "limit": limit}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/search?q=python&limit=5")
    assert r.status_code == 200
    assert r.json() == {"q": "python", "limit": 5}


def test_query_param_default(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/search")
        def search(q: str, limit: int = 10):
            return {"q": q, "limit": limit}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/search?q=python")
    assert r.status_code == 200
    assert r.json() == {"q": "python", "limit": 10}


# ── Error handling ───────────────────────────────────────────────────


def test_404_not_found(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/nonexistent")
    assert r.status_code == 404


def test_http_exception(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, HTTPException
        app = FastAPI()

        @app.get("/fail")
        def fail():
            raise HTTPException(status_code=403, detail="Forbidden")

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/fail")
    assert r.status_code == 403
    assert r.json() == {"detail": "Forbidden"}


def test_missing_required_query_param(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/search")
        def search(q: str):
            return {"q": q}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/search")
    assert r.status_code == 422


def test_pydantic_validation_error(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        from pydantic import BaseModel

        app = FastAPI()

        class Item(BaseModel):
            name: str
            price: float

        @app.post("/items")
        def create_item(item: Item):
            return {"name": item.name}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.post(f"{url}/items", json={"name": "x", "price": "bad"})
    assert r.status_code == 422


# ── Multiple methods and sub-routers ────────────────────────────────


def test_multiple_methods(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/items")
        def list_items():
            return {"items": []}

        @app.post("/items")
        def create_item():
            return {"created": True}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/items")
    assert r.json() == {"items": []}
    r = httpx.post(f"{url}/items")
    assert r.json() == {"created": True}


def test_include_router(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        from fastapi_rs.routing import APIRouter

        app = FastAPI()
        router = APIRouter()

        @router.get("/items")
        def list_items():
            return {"items": []}

        app.include_router(router, prefix="/api/v1")
        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/api/v1/items")
    assert r.status_code == 200
    assert r.json() == {"items": []}


# ── Response types ───────────────────────────────────────────────────


def test_return_string(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/hello")
        def hello():
            return "Hello, plain text!"

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/hello")
    assert r.status_code == 200
    assert "Hello, plain text!" in r.text


def test_return_none(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.delete("/items/1")
        def delete_item():
            return None

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.delete(f"{url}/items/1")
    assert r.status_code == 204


# ── Phase 3: Parameter markers, headers, cookies ────────────────────


def test_header_param(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Header
        app = FastAPI()

        @app.get("/check")
        def check(x_token: str = Header()):
            return {"token": x_token}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/check", headers={"x-token": "secret123"})
    assert r.status_code == 200
    assert r.json() == {"token": "secret123"}


def test_header_with_default(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Header
        app = FastAPI()

        @app.get("/check")
        def check(x_token: str = Header("fallback")):
            return {"token": x_token}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    # With header present
    r = httpx.get(f"{url}/check", headers={"x-token": "real"})
    assert r.json() == {"token": "real"}

    # Without header -- uses default
    r = httpx.get(f"{url}/check")
    assert r.json() == {"token": "fallback"}


def test_header_missing_required(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Header
        app = FastAPI()

        @app.get("/check")
        def check(x_token: str = Header()):
            return {"token": x_token}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/check")
    assert r.status_code == 422


def test_cookie_param(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Cookie
        app = FastAPI()

        @app.get("/session")
        def session(session_id: str = Cookie(None)):
            return {"session_id": session_id}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/session", cookies={"session_id": "abc123"})
    assert r.json() == {"session_id": "abc123"}


def test_cookie_with_default(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Cookie
        app = FastAPI()

        @app.get("/session")
        def session(session_id: str = Cookie("none")):
            return {"session_id": session_id}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    # Without cookie -- uses default
    r = httpx.get(f"{url}/session")
    assert r.json() == {"session_id": "none"}


def test_explicit_query_marker(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Query
        app = FastAPI()

        @app.get("/search")
        def search(q: str = Query(...), limit: int = Query(10)):
            return {"q": q, "limit": limit}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/search?q=test")
    assert r.status_code == 200
    assert r.json() == {"q": "test", "limit": 10}


def test_explicit_query_marker_missing_required(server_app):
    import httpx

    url = server_app("""
        from fastapi_rs import FastAPI, Query
        app = FastAPI()

        @app.get("/search")
        def search(q: str = Query(...)):
            return {"q": q}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/search")
    assert r.status_code == 422
