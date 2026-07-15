"""fastapi-turbo local coverage for FastAPI 0.138's ``app.frontend()``.

Upstream ``tests/test_frontend.py`` (run under the shim in the external compat
gate) is the exhaustive spec; these tests pin the two turbo-specific serving
paths end-to-end:

  * the in-process door path (``TestClient`` → the Rust oneshot door → the 404
    fallback that serves frontend / low-priority routes), and
  * the real ``app.run()`` loopback socket server (``server_app`` fixture),
    where the same serving runs through the Rust ``run_server`` 404 handler.

Shim pattern: ``import fastapi_turbo`` first, then ``from fastapi import ...``.
"""

import httpx

import fastapi_turbo  # noqa: F401 — installs the ``from fastapi import ...`` shim
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient


def _write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_frontend_serving_and_openapi_exclusion_via_testclient(tmp_path):
    dist = tmp_path / "dist"
    _write(dist, "index.html", "APP SHELL")
    _write(dist, "assets/app.js", "console.log('ok')")
    _write(dist, "404.html", "NOPE")

    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"pong": True}

    app.frontend("/", directory=dist, fallback="index.html")

    client = TestClient(app)

    # A normal API route always outranks the catch-all frontend.
    assert client.get("/api/ping").json() == {"pong": True}
    # Static assets are served with the real StaticFiles machinery.
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "console.log('ok')"
    assert "etag" in asset.headers
    # A browser navigation (Accept: text/html, no extension) gets the SPA shell.
    nav = client.get("/dashboard/settings", headers={"accept": "text/html"})
    assert nav.status_code == 200
    assert nav.text == "APP SHELL"
    # An asset-like miss (has an extension) is a plain 404, not the shell.
    miss = client.get("/assets/missing.js", headers={"accept": "*/*"})
    assert miss.status_code == 404
    assert miss.text != "APP SHELL"
    # Frontend routes never appear in the OpenAPI schema.
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {"/api/ping"}


def test_frontend_include_router_prefix_via_testclient(tmp_path):
    dist = tmp_path / "admin"
    _write(dist, "index.html", "ADMIN SHELL")
    _write(dist, "logo.svg", "<svg/>")

    router = APIRouter()
    router.frontend("/", directory=dist, fallback="index.html")
    app = FastAPI()
    app.include_router(router, prefix="/admin")

    client = TestClient(app)

    # Longest-prefix asset serving under the include prefix.
    logo = client.get("/admin/logo.svg")
    assert logo.status_code == 200
    assert logo.text == "<svg/>"
    # SPA fallback under the include prefix.
    nav = client.get("/admin/users/42", headers={"accept": "text/html"})
    assert nav.status_code == 200
    assert nav.text == "ADMIN SHELL"
    # A path outside the frontend prefix is a normal 404.
    assert client.get("/other", headers={"accept": "text/html"}).status_code == 404


def test_frontend_via_real_app_run_socket(server_app, tmp_path):
    dist = tmp_path / "site"
    _write(dist, "index.html", "SOCKET SHELL")
    _write(dist, "asset.txt", "hello-from-socket")

    code = f"""
    import fastapi_turbo  # noqa: F401
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {{"ok": True}}

    app.frontend("/", directory={str(dist)!r}, fallback="index.html")

    app.run(host="127.0.0.1", port=__PORT__)
    """
    url = server_app(code)

    # Asset served by the Rust socket server's 404 fallback.
    asset = httpx.get(f"{url}/asset.txt")
    assert asset.status_code == 200
    assert asset.text == "hello-from-socket"
    # Normal API route still served directly by the door.
    assert httpx.get(f"{url}/api/health").json() == {"ok": True}
    # SPA navigation fallback over the real socket.
    nav = httpx.get(f"{url}/deep/link", headers={"accept": "text/html"})
    assert nav.status_code == 200
    assert nav.text == "SOCKET SHELL"
