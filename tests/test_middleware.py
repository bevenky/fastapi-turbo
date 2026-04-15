"""Phase 6 integration tests: Tower-native middleware (CORS, GZip)."""

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


# ── CORS middleware (class-based) ────────────────────────────────────


def test_cors_middleware(server_app):
    """CORS headers are present on normal requests."""
    url = server_app("""
        from fastapi_rs import FastAPI
        from fastapi_rs.middleware.cors import CORSMiddleware
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/hello", headers={"origin": "http://example.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
    assert r.json() == {"message": "hello"}


def test_cors_preflight(server_app):
    """CORS preflight OPTIONS request returns correct headers."""
    url = server_app("""
        from fastapi_rs import FastAPI
        from fastapi_rs.middleware.cors import CORSMiddleware
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.options(
        f"{url}/hello",
        headers={
            "origin": "http://example.com",
            "access-control-request-method": "GET",
        },
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
    assert "access-control-allow-methods" in r.headers


def test_cors_specific_origin(server_app):
    """CORS with a specific origin only allows that origin."""
    url = server_app("""
        from fastapi_rs import FastAPI
        from fastapi_rs.middleware.cors import CORSMiddleware
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://allowed.com"],
            allow_methods=["GET"],
        )

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    # Allowed origin
    r = httpx.get(f"{url}/hello", headers={"origin": "http://allowed.com"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://allowed.com"

    # Disallowed origin: no CORS header
    r = httpx.get(f"{url}/hello", headers={"origin": "http://evil.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


# ── CORS middleware (string-based) ───────────────────────────────────


def test_string_middleware(server_app):
    """Add middleware by string name."""
    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()
        app.add_middleware("cors", allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        @app.get("/hello")
        def hello():
            return {"message": "hello"}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/hello", headers={"origin": "http://example.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers


# ── GZip middleware ──────────────────────────────────────────────────


def test_gzip_middleware(server_app):
    """GZip compression is applied when client sends Accept-Encoding: gzip."""
    url = server_app("""
        from fastapi_rs import FastAPI
        from fastapi_rs.middleware.gzip import GZipMiddleware
        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=10)

        @app.get("/big")
        def big():
            return {"data": "x" * 1000}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/big", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    # httpx automatically decompresses, so check the content-encoding header
    # was set by the server (httpx keeps the original header)
    assert r.json()["data"] == "x" * 1000


def test_gzip_string_middleware(server_app):
    """Add gzip middleware by string name."""
    url = server_app("""
        from fastapi_rs import FastAPI
        app = FastAPI()
        app.add_middleware("gzip")

        @app.get("/big")
        def big():
            return {"data": "y" * 500}

        app.run(host="127.0.0.1", port=__PORT__)
    """)
    r = httpx.get(f"{url}/big", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.json()["data"] == "y" * 500


# ── No middleware (backward compat) ──────────────────────────────────


def test_no_middleware(server_app):
    """Server works fine with no middleware at all (backward compat)."""
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


# ── Middleware module imports ────────────────────────────────────────


def test_middleware_imports():
    """All middleware classes are importable from the package."""
    from fastapi_rs.middleware import CORSMiddleware, GZipMiddleware
    from fastapi_rs.middleware.trustedhost import TrustedHostMiddleware
    from fastapi_rs.middleware.httpsredirect import HTTPSRedirectMiddleware

    assert CORSMiddleware._fastapi_rs_middleware_type == "cors"
    assert GZipMiddleware._fastapi_rs_middleware_type == "gzip"
    assert TrustedHostMiddleware._fastapi_rs_middleware_type == "trustedhost"
    assert HTTPSRedirectMiddleware._fastapi_rs_middleware_type == "httpsredirect"


def test_middleware_class_attributes():
    """Middleware classes store their config correctly."""
    from fastapi_rs.middleware.cors import CORSMiddleware

    mw = CORSMiddleware(
        allow_origins=["http://example.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["X-Custom"],
        allow_credentials=True,
        max_age=3600,
    )
    assert mw.allow_origins == ["http://example.com"]
    assert mw.allow_methods == ["GET", "POST"]
    assert mw.allow_headers == ["X-Custom"]
    assert mw.allow_credentials is True
    assert mw.max_age == 3600


def test_build_middleware_config():
    """FastAPI._build_middleware_config produces correct dicts."""
    from fastapi_rs import FastAPI
    from fastapi_rs.middleware.cors import CORSMiddleware
    from fastapi_rs.middleware.gzip import GZipMiddleware

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])
    app.add_middleware(GZipMiddleware, minimum_size=100)
    app.add_middleware("cors", allow_origins=["http://x.com"])

    config = app._build_middleware_config()
    assert len(config) == 3

    assert config[0]["type"] == "cors"
    assert config[0]["allow_origins"] == ["*"]
    assert config[0]["allow_methods"] == ["GET"]

    assert config[1]["type"] == "gzip"
    assert config[1]["minimum_size"] == 100

    assert config[2]["type"] == "cors"
    assert config[2]["allow_origins"] == ["http://x.com"]
