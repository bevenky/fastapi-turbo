"""Starlette compatibility shim.

Creates fake module objects so ``from starlette.* import ...`` resolves
to fastapi-rs implementations.
"""

from __future__ import annotations

import types
from typing import Any


def _build() -> dict[str, types.ModuleType]:
    # Lazy imports to avoid circular issues
    import fastapi_rs
    import fastapi_rs.responses as _responses
    import fastapi_rs.requests as _requests
    import fastapi_rs.routing as _routing
    import fastapi_rs.exceptions as _exceptions
    import fastapi_rs.datastructures as _datastructures
    import fastapi_rs.websockets as _websockets
    import fastapi_rs.middleware as _middleware
    import fastapi_rs.middleware.cors as _cors
    import fastapi_rs.middleware.gzip as _gzip
    import fastapi_rs.middleware.trustedhost as _trustedhost
    import fastapi_rs.middleware.httpsredirect as _httpsredirect
    import fastapi_rs.status as _status
    import fastapi_rs.concurrency as _concurrency
    import fastapi_rs.background as _background

    modules: dict[str, types.ModuleType] = {}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []  # type: ignore[attr-defined]
        m.__package__ = name
        return m

    # ── starlette (top-level) ──────────────────────────────────────
    starlette = _mod("starlette")
    modules["starlette"] = starlette

    # ── starlette.requests ─────────────────────────────────────────
    starlette_requests = _mod("starlette.requests")
    starlette_requests.Request = _requests.Request  # type: ignore[attr-defined]
    modules["starlette.requests"] = starlette_requests

    # ── starlette.responses ────────────────────────────────────────
    starlette_responses = _mod("starlette.responses")
    starlette_responses.Response = _responses.Response  # type: ignore[attr-defined]
    starlette_responses.JSONResponse = _responses.JSONResponse  # type: ignore[attr-defined]
    starlette_responses.HTMLResponse = _responses.HTMLResponse  # type: ignore[attr-defined]
    starlette_responses.PlainTextResponse = _responses.PlainTextResponse  # type: ignore[attr-defined]
    starlette_responses.RedirectResponse = _responses.RedirectResponse  # type: ignore[attr-defined]
    starlette_responses.StreamingResponse = _responses.StreamingResponse  # type: ignore[attr-defined]
    starlette_responses.FileResponse = _responses.FileResponse  # type: ignore[attr-defined]
    modules["starlette.responses"] = starlette_responses

    # ── starlette.routing ──────────────────────────────────────────
    starlette_routing = _mod("starlette.routing")
    starlette_routing.Route = _routing.APIRoute  # type: ignore[attr-defined]
    starlette_routing.Router = _routing.APIRouter  # type: ignore[attr-defined]
    starlette_routing.APIRoute = _routing.APIRoute  # type: ignore[attr-defined]
    starlette_routing.APIRouter = _routing.APIRouter  # type: ignore[attr-defined]
    modules["starlette.routing"] = starlette_routing

    # ── starlette.exceptions ───────────────────────────────────────
    starlette_exceptions = _mod("starlette.exceptions")
    starlette_exceptions.HTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    starlette_exceptions.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
    modules["starlette.exceptions"] = starlette_exceptions

    # ── starlette.websockets ───────────────────────────────────────
    starlette_websockets = _mod("starlette.websockets")
    starlette_websockets.WebSocket = _websockets.WebSocket  # type: ignore[attr-defined]
    starlette_websockets.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
    modules["starlette.websockets"] = starlette_websockets

    # ── starlette.datastructures ───────────────────────────────────
    starlette_ds = _mod("starlette.datastructures")
    starlette_ds.URL = _datastructures.URL  # type: ignore[attr-defined]
    starlette_ds.Headers = _datastructures.Headers  # type: ignore[attr-defined]
    starlette_ds.QueryParams = _datastructures.QueryParams  # type: ignore[attr-defined]
    starlette_ds.Address = _datastructures.Address  # type: ignore[attr-defined]
    starlette_ds.State = _datastructures.State  # type: ignore[attr-defined]
    starlette_ds.UploadFile = fastapi_rs.UploadFile  # type: ignore[attr-defined]
    modules["starlette.datastructures"] = starlette_ds

    # ── starlette.status ───────────────────────────────────────────
    starlette_status = _mod("starlette.status")
    # Copy all HTTP_* and WS_* constants
    for attr in dir(_status):
        if attr.startswith("HTTP_") or attr.startswith("WS_"):
            setattr(starlette_status, attr, getattr(_status, attr))
    modules["starlette.status"] = starlette_status

    # ── starlette.concurrency ──────────────────────────────────────
    starlette_concurrency = _mod("starlette.concurrency")
    starlette_concurrency.run_in_threadpool = _concurrency.run_in_threadpool  # type: ignore[attr-defined]
    starlette_concurrency.iterate_in_threadpool = _concurrency.iterate_in_threadpool  # type: ignore[attr-defined]
    modules["starlette.concurrency"] = starlette_concurrency

    # ── starlette.background ───────────────────────────────────────
    starlette_background = _mod("starlette.background")
    starlette_background.BackgroundTask = _background.BackgroundTask  # type: ignore[attr-defined]
    starlette_background.BackgroundTasks = _background.BackgroundTasks  # type: ignore[attr-defined]
    modules["starlette.background"] = starlette_background

    # ── starlette.middleware ───────────────────────────────────────
    starlette_middleware = _mod("starlette.middleware")
    modules["starlette.middleware"] = starlette_middleware

    # ── starlette.middleware.cors ──────────────────────────────────
    starlette_middleware_cors = _mod("starlette.middleware.cors")
    starlette_middleware_cors.CORSMiddleware = _cors.CORSMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.cors"] = starlette_middleware_cors

    # ── starlette.middleware.gzip ──────────────────────────────────
    starlette_middleware_gzip = _mod("starlette.middleware.gzip")
    starlette_middleware_gzip.GZipMiddleware = _gzip.GZipMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.gzip"] = starlette_middleware_gzip

    # ── starlette.middleware.trustedhost ────────────────────────────
    starlette_middleware_th = _mod("starlette.middleware.trustedhost")
    starlette_middleware_th.TrustedHostMiddleware = _trustedhost.TrustedHostMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.trustedhost"] = starlette_middleware_th

    # ── starlette.middleware.httpsredirect ──────────────────────────
    starlette_middleware_hr = _mod("starlette.middleware.httpsredirect")
    starlette_middleware_hr.HTTPSRedirectMiddleware = _httpsredirect.HTTPSRedirectMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.httpsredirect"] = starlette_middleware_hr

    # ── starlette.middleware.base ──────────────────────────────────
    # BaseHTTPMiddleware placeholder
    starlette_middleware_base = _mod("starlette.middleware.base")

    class BaseHTTPMiddleware:
        """Placeholder for Starlette BaseHTTPMiddleware."""
        def __init__(self, app=None, dispatch=None):
            self.app = app
            self.dispatch = dispatch

    starlette_middleware_base.BaseHTTPMiddleware = BaseHTTPMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.base"] = starlette_middleware_base

    # ── starlette.staticfiles ──────────────────────────────────────
    starlette_staticfiles = _mod("starlette.staticfiles")

    class StaticFiles:
        """Placeholder for StaticFiles (not yet implemented)."""
        def __init__(self, *, directory=None, packages=None, html=False, check_dir=True):
            self.directory = directory
            self.packages = packages
            self.html = html
            self.check_dir = check_dir

    starlette_staticfiles.StaticFiles = StaticFiles  # type: ignore[attr-defined]
    modules["starlette.staticfiles"] = starlette_staticfiles

    # ── starlette.templating ───────────────────────────────────────
    starlette_templating = _mod("starlette.templating")

    class Jinja2Templates:
        """Placeholder for Jinja2Templates (not yet implemented)."""
        def __init__(self, directory=None, **kwargs):
            self.directory = directory

        def TemplateResponse(self, name, context, **kwargs):
            raise NotImplementedError("Jinja2Templates not yet implemented in fastapi-rs")

    starlette_templating.Jinja2Templates = Jinja2Templates  # type: ignore[attr-defined]
    modules["starlette.templating"] = starlette_templating

    # ── starlette.testclient ───────────────────────────────────────
    starlette_testclient = _mod("starlette.testclient")
    from fastapi_rs.testclient import TestClient
    starlette_testclient.TestClient = TestClient  # type: ignore[attr-defined]
    modules["starlette.testclient"] = starlette_testclient

    # Set parent references
    starlette.requests = starlette_requests  # type: ignore[attr-defined]
    starlette.responses = starlette_responses  # type: ignore[attr-defined]
    starlette.routing = starlette_routing  # type: ignore[attr-defined]
    starlette.exceptions = starlette_exceptions  # type: ignore[attr-defined]
    starlette.websockets = starlette_websockets  # type: ignore[attr-defined]
    starlette.datastructures = starlette_ds  # type: ignore[attr-defined]
    starlette.status = starlette_status  # type: ignore[attr-defined]
    starlette.concurrency = starlette_concurrency  # type: ignore[attr-defined]
    starlette.background = starlette_background  # type: ignore[attr-defined]
    starlette.middleware = starlette_middleware  # type: ignore[attr-defined]
    starlette.staticfiles = starlette_staticfiles  # type: ignore[attr-defined]
    starlette.templating = starlette_templating  # type: ignore[attr-defined]
    starlette.testclient = starlette_testclient  # type: ignore[attr-defined]

    return modules


MODULES = _build()
