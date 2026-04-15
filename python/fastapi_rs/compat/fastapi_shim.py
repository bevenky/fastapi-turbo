"""FastAPI compatibility shim.

Creates fake module objects so ``from fastapi import ...`` resolves
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
    import fastapi_rs.param_functions as _params
    import fastapi_rs.dependencies as _dependencies
    import fastapi_rs.applications as _applications
    import fastapi_rs.security as _security
    import fastapi_rs.background as _background
    import fastapi_rs.encoders as _encoders
    import fastapi_rs.status as _status
    import fastapi_rs.datastructures as _datastructures
    import fastapi_rs.websockets as _websockets
    import fastapi_rs.concurrency as _concurrency
    import fastapi_rs.middleware as _middleware
    import fastapi_rs.middleware.cors as _cors

    modules: dict[str, types.ModuleType] = {}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []  # type: ignore[attr-defined]
        m.__package__ = name
        return m

    # ── fastapi (top-level) ────────────────────────────────────────
    fastapi = _mod("fastapi")
    fastapi.__version__ = fastapi_rs.__version__  # type: ignore[attr-defined]

    # Application
    fastapi.FastAPI = _applications.FastAPI  # type: ignore[attr-defined]

    # Routing
    fastapi.APIRouter = _routing.APIRouter  # type: ignore[attr-defined]
    fastapi.APIRoute = _routing.APIRoute  # type: ignore[attr-defined]

    # Dependencies
    fastapi.Depends = _dependencies.Depends  # type: ignore[attr-defined]

    # Exceptions
    fastapi.HTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    fastapi.RequestValidationError = _exceptions.RequestValidationError  # type: ignore[attr-defined]
    fastapi.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]

    # Parameter functions
    fastapi.Query = _params.Query  # type: ignore[attr-defined]
    fastapi.Path = _params.Path  # type: ignore[attr-defined]
    fastapi.Header = _params.Header  # type: ignore[attr-defined]
    fastapi.Cookie = _params.Cookie  # type: ignore[attr-defined]
    fastapi.Body = _params.Body  # type: ignore[attr-defined]
    fastapi.Form = _params.Form  # type: ignore[attr-defined]
    fastapi.File = _params.File  # type: ignore[attr-defined]
    fastapi.UploadFile = _params.UploadFile  # type: ignore[attr-defined]

    # Responses
    fastapi.Response = _responses.Response  # type: ignore[attr-defined]
    fastapi.JSONResponse = _responses.JSONResponse  # type: ignore[attr-defined]
    fastapi.HTMLResponse = _responses.HTMLResponse  # type: ignore[attr-defined]
    fastapi.PlainTextResponse = _responses.PlainTextResponse  # type: ignore[attr-defined]
    fastapi.RedirectResponse = _responses.RedirectResponse  # type: ignore[attr-defined]
    fastapi.StreamingResponse = _responses.StreamingResponse  # type: ignore[attr-defined]
    fastapi.FileResponse = _responses.FileResponse  # type: ignore[attr-defined]

    # Request
    fastapi.Request = _requests.Request  # type: ignore[attr-defined]
    fastapi.WebSocket = _websockets.WebSocket  # type: ignore[attr-defined]

    # Background tasks
    fastapi.BackgroundTasks = _background.BackgroundTasks  # type: ignore[attr-defined]

    # Security (commonly imported from top-level too)
    fastapi.Security = _dependencies.Depends  # type: ignore[attr-defined]  # Security is an alias for Depends in practice

    # Encoders
    fastapi.encoders = None  # will be set below  # type: ignore[attr-defined]

    # Status
    fastapi.status = None  # will be set below  # type: ignore[attr-defined]

    modules["fastapi"] = fastapi

    # ── fastapi.responses ──────────────────────────────────────────
    fastapi_responses = _mod("fastapi.responses")
    fastapi_responses.Response = _responses.Response  # type: ignore[attr-defined]
    fastapi_responses.JSONResponse = _responses.JSONResponse  # type: ignore[attr-defined]
    fastapi_responses.HTMLResponse = _responses.HTMLResponse  # type: ignore[attr-defined]
    fastapi_responses.PlainTextResponse = _responses.PlainTextResponse  # type: ignore[attr-defined]
    fastapi_responses.RedirectResponse = _responses.RedirectResponse  # type: ignore[attr-defined]
    fastapi_responses.StreamingResponse = _responses.StreamingResponse  # type: ignore[attr-defined]
    fastapi_responses.FileResponse = _responses.FileResponse  # type: ignore[attr-defined]
    modules["fastapi.responses"] = fastapi_responses

    # ── fastapi.routing ────────────────────────────────────────────
    fastapi_routing = _mod("fastapi.routing")
    fastapi_routing.APIRouter = _routing.APIRouter  # type: ignore[attr-defined]
    fastapi_routing.APIRoute = _routing.APIRoute  # type: ignore[attr-defined]
    modules["fastapi.routing"] = fastapi_routing

    # ── fastapi.exceptions ─────────────────────────────────────────
    fastapi_exceptions = _mod("fastapi.exceptions")
    fastapi_exceptions.HTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    fastapi_exceptions.RequestValidationError = _exceptions.RequestValidationError  # type: ignore[attr-defined]
    fastapi_exceptions.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
    modules["fastapi.exceptions"] = fastapi_exceptions

    # ── fastapi.params ─────────────────────────────────────────────
    fastapi_params = _mod("fastapi.params")
    fastapi_params.Query = _params.Query  # type: ignore[attr-defined]
    fastapi_params.Path = _params.Path  # type: ignore[attr-defined]
    fastapi_params.Header = _params.Header  # type: ignore[attr-defined]
    fastapi_params.Cookie = _params.Cookie  # type: ignore[attr-defined]
    fastapi_params.Body = _params.Body  # type: ignore[attr-defined]
    fastapi_params.Form = _params.Form  # type: ignore[attr-defined]
    fastapi_params.File = _params.File  # type: ignore[attr-defined]
    fastapi_params.Depends = _dependencies.Depends  # type: ignore[attr-defined]
    modules["fastapi.params"] = fastapi_params

    # ── fastapi.security ───────────────────────────────────────────
    fastapi_security = _mod("fastapi.security")
    fastapi_security.OAuth2PasswordBearer = _security.OAuth2PasswordBearer  # type: ignore[attr-defined]
    fastapi_security.OAuth2PasswordRequestForm = _security.OAuth2PasswordRequestForm  # type: ignore[attr-defined]
    fastapi_security.HTTPBearer = _security.HTTPBearer  # type: ignore[attr-defined]
    fastapi_security.HTTPBasic = _security.HTTPBasic  # type: ignore[attr-defined]
    fastapi_security.HTTPBasicCredentials = _security.HTTPBasicCredentials  # type: ignore[attr-defined]
    fastapi_security.HTTPAuthorizationCredentials = _security.HTTPAuthorizationCredentials  # type: ignore[attr-defined]
    fastapi_security.APIKeyHeader = _security.APIKeyHeader  # type: ignore[attr-defined]
    fastapi_security.APIKeyQuery = _security.APIKeyQuery  # type: ignore[attr-defined]
    fastapi_security.APIKeyCookie = _security.APIKeyCookie  # type: ignore[attr-defined]
    fastapi_security.SecurityScopes = _security.SecurityScopes  # type: ignore[attr-defined]
    modules["fastapi.security"] = fastapi_security

    # ── fastapi.encoders ───────────────────────────────────────────
    fastapi_encoders = _mod("fastapi.encoders")
    fastapi_encoders.jsonable_encoder = _encoders.jsonable_encoder  # type: ignore[attr-defined]
    modules["fastapi.encoders"] = fastapi_encoders

    # ── fastapi.status ─────────────────────────────────────────────
    fastapi_status = _mod("fastapi.status")
    for attr in dir(_status):
        if attr.startswith("HTTP_") or attr.startswith("WS_"):
            setattr(fastapi_status, attr, getattr(_status, attr))
    modules["fastapi.status"] = fastapi_status

    # ── fastapi.datastructures ─────────────────────────────────────
    fastapi_ds = _mod("fastapi.datastructures")
    fastapi_ds.UploadFile = _params.UploadFile  # type: ignore[attr-defined]
    fastapi_ds.URL = _datastructures.URL  # type: ignore[attr-defined]
    fastapi_ds.Headers = _datastructures.Headers  # type: ignore[attr-defined]
    fastapi_ds.QueryParams = _datastructures.QueryParams  # type: ignore[attr-defined]
    fastapi_ds.State = _datastructures.State  # type: ignore[attr-defined]
    modules["fastapi.datastructures"] = fastapi_ds

    # ── fastapi.concurrency ────────────────────────────────────────
    fastapi_concurrency = _mod("fastapi.concurrency")
    fastapi_concurrency.run_in_threadpool = _concurrency.run_in_threadpool  # type: ignore[attr-defined]
    modules["fastapi.concurrency"] = fastapi_concurrency

    # ── fastapi.background ─────────────────────────────────────────
    fastapi_background = _mod("fastapi.background")
    fastapi_background.BackgroundTasks = _background.BackgroundTasks  # type: ignore[attr-defined]
    modules["fastapi.background"] = fastapi_background

    # ── fastapi.testclient ─────────────────────────────────────────
    fastapi_testclient = _mod("fastapi.testclient")
    from fastapi_rs.testclient import TestClient
    fastapi_testclient.TestClient = TestClient  # type: ignore[attr-defined]
    modules["fastapi.testclient"] = fastapi_testclient

    # ── fastapi.websockets ─────────────────────────────────────────
    fastapi_websockets = _mod("fastapi.websockets")
    fastapi_websockets.WebSocket = _websockets.WebSocket  # type: ignore[attr-defined]
    modules["fastapi.websockets"] = fastapi_websockets

    # ── fastapi.middleware ─────────────────────────────────────────
    fastapi_middleware = _mod("fastapi.middleware")
    modules["fastapi.middleware"] = fastapi_middleware

    # ── fastapi.middleware.cors ────────────────────────────────────
    fastapi_middleware_cors = _mod("fastapi.middleware.cors")
    fastapi_middleware_cors.CORSMiddleware = _cors.CORSMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.cors"] = fastapi_middleware_cors

    # ── fastapi.middleware.gzip ────────────────────────────────────
    from fastapi_rs.middleware.gzip import GZipMiddleware as _GZipMiddleware
    fastapi_middleware_gzip = _mod("fastapi.middleware.gzip")
    fastapi_middleware_gzip.GZipMiddleware = _GZipMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.gzip"] = fastapi_middleware_gzip

    # ── fastapi.middleware.trustedhost ──────────────────────────────
    from fastapi_rs.middleware.trustedhost import TrustedHostMiddleware as _TrustedHostMiddleware
    fastapi_middleware_th = _mod("fastapi.middleware.trustedhost")
    fastapi_middleware_th.TrustedHostMiddleware = _TrustedHostMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.trustedhost"] = fastapi_middleware_th

    # ── fastapi.middleware.httpsredirect ────────────────────────────
    from fastapi_rs.middleware.httpsredirect import HTTPSRedirectMiddleware as _HTTPSRedirectMiddleware
    fastapi_middleware_hr = _mod("fastapi.middleware.httpsredirect")
    fastapi_middleware_hr.HTTPSRedirectMiddleware = _HTTPSRedirectMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.httpsredirect"] = fastapi_middleware_hr

    # Set parent references
    fastapi.responses = fastapi_responses  # type: ignore[attr-defined]
    fastapi.routing = fastapi_routing  # type: ignore[attr-defined]
    fastapi.exceptions = fastapi_exceptions  # type: ignore[attr-defined]
    fastapi.params = fastapi_params  # type: ignore[attr-defined]
    fastapi.security = fastapi_security  # type: ignore[attr-defined]
    fastapi.encoders = fastapi_encoders  # type: ignore[attr-defined]
    fastapi.status = fastapi_status  # type: ignore[attr-defined]
    fastapi.datastructures = fastapi_ds  # type: ignore[attr-defined]
    fastapi.concurrency = fastapi_concurrency  # type: ignore[attr-defined]
    fastapi.background = fastapi_background  # type: ignore[attr-defined]
    fastapi.testclient = fastapi_testclient  # type: ignore[attr-defined]
    fastapi.websockets = fastapi_websockets  # type: ignore[attr-defined]
    fastapi.middleware = fastapi_middleware  # type: ignore[attr-defined]

    return modules


MODULES = _build()
