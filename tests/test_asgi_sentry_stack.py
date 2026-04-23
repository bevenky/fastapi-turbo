"""End-to-end production stack: Sentry ASGI + Session + CORS + GZip + BaseHTTPMiddleware.

Mirrors the middleware layout used by real-world FastAPI apps (Netflix
Dispatch, Polar, Mealie). Verifies:

  * Sentry's ``SentryAsgiMiddleware`` captures exceptions and transaction
    spans through the full chain.
  * ``SessionMiddleware`` (signed cookie session) round-trips across
    requests made by the same client.
  * ``CORSMiddleware`` allows listed origins and emits preflight headers.
  * ``GZipMiddleware`` compresses response bodies above the threshold.
  * A custom ``BaseHTTPMiddleware`` subclass can gate requests behind a
    bearer token and decorate responses.
  * Custom ``@app.exception_handler(HTTPException)`` sees the real
    ``request.url.path`` (regression test for the ContextVar bridge).
  * WebSocket still works when this full middleware chain is present.

Skipped gracefully if ``sentry-sdk`` isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("sentry_sdk")

import fastapi_turbo  # noqa: F401 — installs shim for fastapi + starlette
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware


class _CapturingTransport(sentry_sdk.transport.Transport):
    """Records every envelope item Sentry would otherwise POST upstream."""

    def __init__(self, options=None):
        super().__init__(options)
        self.items: list = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            self.items.append(item)

    def flush(self, timeout, callback=None):
        pass


@pytest.fixture()
def sentry_capture():
    # Keep a reference to the transport instance so tests can inspect it.
    captured: list = []

    class _T(_CapturingTransport):
        def __init__(self, options=None):
            super().__init__(options)
            # Redirect the transport's internal buffer to the shared list
            # so tests can see items regardless of which client they touch.
            self.items = captured

    sentry_sdk.init(
        dsn="https://abc@127.0.0.1/1",
        transport=_T,
        traces_sample_rate=1.0,
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    yield captured
    # Reset to a no-op client so later tests aren't polluted.
    sentry_sdk.init(dsn=None, default_integrations=False, auto_enabling_integrations=False)


class _Order(BaseModel):
    sku: str
    qty: int


class _BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/secure"):
            if request.headers.get("authorization") != "Bearer letmein":
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        resp = await call_next(request)
        resp.headers["X-Auth-Checked"] = "1"
        return resp


def _build_app() -> FastAPI:
    app = FastAPI(title="production-stack")
    # Last-added = outermost: Sentry wraps everything.
    app.add_middleware(_BearerAuth)
    app.add_middleware(GZipMiddleware, minimum_size=20)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.example.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=True,
    )
    app.add_middleware(
        SessionMiddleware, secret_key="test-secret", session_cookie="sess"
    )
    app.add_middleware(SentryAsgiMiddleware)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        return JSONResponse(
            {"error": exc.detail, "code": exc.status_code, "path": request.url.path},
            status_code=exc.status_code,
        )

    def _version() -> str:
        return "v1"

    @app.get("/health")
    def health(v: str = Depends(_version)):
        return {"status": "ok", "version": v}

    @app.get("/secure/me")
    def secure_me(request: Request):
        visits = request.session.get("visits", 0) + 1
        request.session["visits"] = visits
        return {"visits": visits}

    @app.post("/secure/orders")
    def create_order(order: _Order, tasks: BackgroundTasks):
        return {"created": True, "sku": order.sku, "qty": order.qty}

    @app.get("/boom")
    def boom():
        raise ValueError("synthetic failure for Sentry")

    @app.get("/missing")
    def missing():
        raise HTTPException(status_code=404, detail="record missing")

    @app.websocket("/ws")
    async def ws_echo(ws: WebSocket):
        await ws.accept()
        m = await ws.receive_text()
        await ws.send_text(f"echo:{m}")
        await ws.close()

    return app


def test_health_middleware_chain(sentry_capture):
    app = _build_app()
    with TestClient(app) as cli:
        r = cli.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": "v1"}
        assert r.headers.get("x-auth-checked") == "1"


def test_cors_preflight(sentry_capture):
    app = _build_app()
    with TestClient(app) as cli:
        r = cli.options(
            "/health",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert r.status_code == 200
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_gzip_on_large_body(sentry_capture):
    app = _build_app()
    with TestClient(app) as cli:
        order = {"sku": "X" * 200, "qty": 3}
        r = cli.post(
            "/secure/orders",
            json=order,
            headers={"Authorization": "Bearer letmein", "Accept-Encoding": "gzip"},
        )
        assert r.status_code == 200
        assert r.headers.get("content-encoding") == "gzip"
        assert r.json()["sku"] == order["sku"]


def test_bearer_auth_middleware(sentry_capture):
    app = _build_app()
    with TestClient(app) as cli:
        assert cli.get("/secure/me").status_code == 401
        r = cli.get("/secure/me", headers={"Authorization": "Bearer letmein"})
        assert r.status_code == 200


def test_session_persists_across_requests(sentry_capture):
    app = _build_app()
    with TestClient(app) as cli:
        headers = {"Authorization": "Bearer letmein"}
        assert cli.get("/secure/me", headers=headers).json() == {"visits": 1}
        assert cli.get("/secure/me", headers=headers).json() == {"visits": 2}


def test_custom_http_exception_handler_sees_real_path(sentry_capture):
    """ContextVar-backed scope bridge — exception_handler's Request now
    carries the real path instead of ``/``."""
    app = _build_app()
    with TestClient(app) as cli:
        r = cli.get("/missing")
        assert r.status_code == 404
        assert r.json() == {"error": "record missing", "code": 404, "path": "/missing"}


def test_websocket_through_full_stack(sentry_capture):
    app = _build_app()
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"


def test_sentry_captures_error_and_transactions(sentry_capture):
    app = _build_app()
    with TestClient(app, raise_server_exceptions=False) as cli:
        cli.get("/health")
        cli.get("/missing")
        cli.get("/boom")
    sentry_sdk.flush(timeout=3)

    transactions: list = []
    errors: list = []
    for item in sentry_capture:
        if not hasattr(item, "payload") or item.payload is None:
            continue
        j = item.payload.json
        if j is None:
            continue
        if j.get("type") == "transaction":
            transactions.append(j)
        elif (j.get("exception") or {}).get("values"):
            errors.append(j)

    tx_paths = {t.get("transaction") for t in transactions}
    assert "/health" in tx_paths
    assert "/missing" in tx_paths
    assert "/boom" in tx_paths

    # Status code mapping
    by_path = {t.get("transaction"): t for t in transactions}
    assert (by_path["/missing"].get("contexts") or {}).get("trace", {}).get("status") == "not_found"
    assert (by_path["/boom"].get("contexts") or {}).get("trace", {}).get("status") == "internal_error"

    # Error event for the /boom ValueError
    err_types = [
        (e.get("exception", {}).get("values", [{}])[0].get("type"))
        for e in errors
    ]
    assert "ValueError" in err_types
