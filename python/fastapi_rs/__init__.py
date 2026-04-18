"""fastapi-rs: FastAPI-compatible web framework powered by Rust Axum."""

from fastapi_rs._fastapi_rs_core import core_version, rust_hello
from fastapi_rs.applications import FastAPI
from fastapi_rs.background import BackgroundTasks
from fastapi_rs.dependencies import Depends, Security
from fastapi_rs.encoders import jsonable_encoder
from fastapi_rs.exceptions import HTTPException, RequestValidationError, ValidationException, WebSocketDisconnect, WebSocketException
from fastapi_rs.param_functions import Body, Cookie, File, Form, Header, Path, Query, UploadFile
from fastapi_rs.requests import Request
from fastapi_rs.responses import (
    EventSourceResponse,
    FileResponse,
    HTMLResponse,
    JSONResponse,
    ORJSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    UJSONResponse,
)
from fastapi_rs.sse import ServerSentEvent, format_sse_event, KEEPALIVE_COMMENT
from fastapi_rs.routing import APIRoute, APIRouter
from fastapi_rs.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPAuthorizationCredentials,
    HTTPBearer,
    HTTPDigest,
    OAuth2,
    OAuth2AuthorizationCodeBearer,
    OAuth2ClientCredentials,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
    SecurityScopes,
)
from fastapi_rs.websockets import WebSocket, WebSocketState
from fastapi_rs import status
from fastapi_rs import authentication  # noqa: F401 (re-exported via module)

__version__ = "0.1.0"

# ── Auto-optimize psycopg3 connection pools ─────────────────────────────
# When psycopg_pool is available, patch ConnectionPool to default to
# autocommit=True. This eliminates BEGIN/COMMIT overhead per query (~46μs
# saved), matching Go's pgx behavior. Users who need explicit transactions
# can still pass autocommit=False or use conn.transaction().
try:
    import psycopg_pool as _pp

    _orig_pool_init = _pp.ConnectionPool.__init__

    def _patched_pool_init(self, conninfo="", *, kwargs=None, configure=None, **kw):
        # Default to autocommit=True unless user explicitly provided kwargs
        if kwargs is None:
            kwargs = {"autocommit": True}
        elif "autocommit" not in kwargs:
            kwargs = {**kwargs, "autocommit": True}

        _orig_pool_init(self, conninfo, kwargs=kwargs, configure=configure, **kw)

    _pp.ConnectionPool.__init__ = _patched_pool_init
except ImportError:
    pass
__all__ = [
    "FastAPI",
    "Depends",
    "Security",
    "APIRouter",
    "APIRoute",
    "Request",
    "WebSocket",
    "WebSocketState",
    "Response",
    "JSONResponse",
    "ORJSONResponse",
    "UJSONResponse",
    "HTMLResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "StreamingResponse",
    "EventSourceResponse",
    "FileResponse",
    "ServerSentEvent",
    "format_sse_event",
    "KEEPALIVE_COMMENT",
    "HTTPException",
    "RequestValidationError",
    "WebSocketDisconnect",
    "WebSocketException",
    "Query",
    "Path",
    "Header",
    "Cookie",
    "Body",
    "Form",
    "File",
    "UploadFile",
    "BackgroundTasks",
    "OAuth2",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "OAuth2ClientCredentials",
    "OAuth2AuthorizationCodeBearer",
    "OpenIdConnect",
    "HTTPBearer",
    "HTTPDigest",
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPAuthorizationCredentials",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIKeyCookie",
    "SecurityScopes",
    "jsonable_encoder",
    "status",
    "rust_hello",
    "core_version",
    "__version__",
]

# Auto-install compatibility shims so `from fastapi import ...` works.
# Disable with environment variable FASTAPI_RS_NO_SHIM=1.
import os as _os

if not _os.environ.get("FASTAPI_RS_NO_SHIM"):
    from fastapi_rs.compat import install as _install_shims
    _install_shims()
