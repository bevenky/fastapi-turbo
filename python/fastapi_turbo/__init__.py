"""fastapi-turbo: FastAPI-compatible web framework powered by Rust Axum."""

from fastapi_turbo._fastapi_turbo_core import core_version, rust_hello
from fastapi_turbo.applications import FastAPI
from fastapi_turbo.background import BackgroundTasks
from fastapi_turbo.dependencies import Depends, Security
from fastapi_turbo.encoders import jsonable_encoder
from fastapi_turbo.exceptions import HTTPException, RequestValidationError, ValidationException, WebSocketDisconnect, WebSocketException
from fastapi_turbo.param_functions import Body, Cookie, File, Form, Header, Path, Query, UploadFile
from fastapi_turbo.requests import Request
from fastapi_turbo.responses import (
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
from fastapi_turbo.sse import ServerSentEvent, format_sse_event, KEEPALIVE_COMMENT
from fastapi_turbo.routing import APIRoute, APIRouter
from fastapi_turbo.security import (
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
from fastapi_turbo.websockets import WebSocket, WebSocketState
from fastapi_turbo import status
from fastapi_turbo import authentication  # noqa: F401 (re-exported via module)

__version__ = "0.1.0"

# NOTE: we intentionally do NOT monkey-patch ``psycopg_pool.ConnectionPool``.
# Silently changing the ``autocommit`` default for every connection pool in
# the process — including ones from unrelated libraries — would reshape
# transaction semantics for code that never asked for it. Users who want
# the autocommit-by-default optimisation should construct pools via
# ``fastapi_turbo.db.create_pool(dsn)`` (an opt-in helper) or pass
# ``kwargs={"autocommit": True}`` to ``ConnectionPool`` explicitly.
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
# Disable with environment variable FASTAPI_TURBO_NO_SHIM=1.
import os as _os

if not _os.environ.get("FASTAPI_TURBO_NO_SHIM"):
    from fastapi_turbo.compat import install as _install_shims
    _install_shims()


# ── Coroutine-function parity for the Rust ``PyUploadFile`` ──────────
#
# Starlette's ``UploadFile.read`` / ``write`` / ``seek`` / ``close``
# are real ``async def`` methods, so ``inspect.iscoroutinefunction
# (file.read)`` returns ``True``. The Rust-bound methods on
# ``PyUploadFile`` return immediate awaitables (``ImmediateBytes`` /
# ``ImmediateNone``) — ``await file.read()`` works, but the FUNCTION
# itself isn't a coroutine function (no ``CO_COROUTINE`` flag), so
# libraries that introspect with ``inspect.iscoroutinefunction``
# (or pre-build the awaitable then await it later) saw different
# behaviour from upstream.
#
# Wrap each method with an ``async def`` shim so introspection
# matches Starlette. The body still drives the original immediate
# awaitable (``await rust_method(self, ...)``) — no extra scheduler
# hop, just a Python-level coroutine wrapper.
from fastapi_turbo._fastapi_turbo_core import PyUploadFile as _PyUploadFile

_rust_read = _PyUploadFile.read
_rust_write = _PyUploadFile.write
_rust_seek = _PyUploadFile.seek
_rust_close = _PyUploadFile.close


async def _async_upload_read(self, size: int = -1):
    return await _rust_read(self, size)


async def _async_upload_write(self, data) -> None:
    return await _rust_write(self, data)


async def _async_upload_seek(self, offset: int) -> None:
    return await _rust_seek(self, offset)


async def _async_upload_close(self) -> None:
    return await _rust_close(self)


_PyUploadFile.read = _async_upload_read
_PyUploadFile.write = _async_upload_write
_PyUploadFile.seek = _async_upload_seek
_PyUploadFile.close = _async_upload_close
