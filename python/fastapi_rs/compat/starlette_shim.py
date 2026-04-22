"""Starlette compatibility shim.

Creates fake module objects so ``from starlette.* import ...`` resolves
to fastapi-rs implementations.
"""

from __future__ import annotations

import types
import typing
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

    import fastapi_rs._starlette_compat as _sc

    # ── starlette (top-level) ──────────────────────────────────────
    starlette = _mod("starlette")
    # Expose a version string so `starlette.__version__` works.
    starlette.__version__ = "1.0.0"  # type: ignore[attr-defined]
    modules["starlette"] = starlette

    # ── starlette.requests ─────────────────────────────────────────
    starlette_requests = _mod("starlette.requests")
    starlette_requests.Request = _requests.Request  # type: ignore[attr-defined]
    starlette_requests.HTTPConnection = _requests.HTTPConnection  # type: ignore[attr-defined]
    starlette_requests.ClientDisconnect = _requests.ClientDisconnect  # type: ignore[attr-defined]
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
    # Starlette's ``responses`` module imports ``json`` from the stdlib
    # at module level. ``test_dump_json_fast_path`` monkey-patches
    # ``starlette.responses.json.dumps`` to observe when the default
    # response class uses stdlib JSON vs the orjson fast path.
    import json as _json_stdlib
    starlette_responses.json = _json_stdlib  # type: ignore[attr-defined]
    modules["starlette.responses"] = starlette_responses

    # ── starlette.routing ──────────────────────────────────────────
    starlette_routing = _mod("starlette.routing")
    starlette_routing.Route = _sc.Route  # type: ignore[attr-defined]
    starlette_routing.WebSocketRoute = _sc.WebSocketRoute  # type: ignore[attr-defined]
    starlette_routing.Mount = _sc.Mount  # type: ignore[attr-defined]
    starlette_routing.Host = _sc.Host  # type: ignore[attr-defined]
    starlette_routing.Router = _routing.APIRouter  # type: ignore[attr-defined]
    starlette_routing.APIRoute = _routing.APIRoute  # type: ignore[attr-defined]
    starlette_routing.APIRouter = _routing.APIRouter  # type: ignore[attr-defined]
    # Stubs for BaseRoute, Match, NoMatchFound
    import enum as _enum
    class BaseRoute:
        pass
    class Match(_enum.Enum):
        NONE = 0
        PARTIAL = 1
        FULL = 2
    class NoMatchFound(Exception):
        pass
    starlette_routing.BaseRoute = BaseRoute  # type: ignore[attr-defined]
    starlette_routing.Match = Match  # type: ignore[attr-defined]
    starlette_routing.NoMatchFound = NoMatchFound  # type: ignore[attr-defined]
    # compile_path — used by fastapi-jsonrpc, Netflix dispatch
    import re as _re
    def compile_path(path: str):
        """Convert a path template to a regex pattern + format string."""
        path_regex = "^"
        path_format = ""
        idx = 0
        param_convertors = {}
        for match in _re.finditer(r"\{(\w+)(?::(\w+))?\}", path):
            param_name, convertor = match.groups("str")
            path_regex += _re.escape(path[idx:match.start()])
            if convertor == "path":
                path_regex += f"(?P<{param_name}>.+)"
            else:
                path_regex += f"(?P<{param_name}>[^/]+)"
            path_format += path[idx:match.start()] + "{" + param_name + "}"
            param_convertors[param_name] = convertor
            idx = match.end()
        path_regex += _re.escape(path[idx:]) + "$"
        path_format += path[idx:]
        return _re.compile(path_regex), path_format, param_convertors
    starlette_routing.compile_path = compile_path  # type: ignore[attr-defined]
    modules["starlette.routing"] = starlette_routing

    # ── starlette.exceptions ───────────────────────────────────────
    starlette_exceptions = _mod("starlette.exceptions")
    starlette_exceptions.HTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    starlette_exceptions.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
    modules["starlette.exceptions"] = starlette_exceptions

    # ── starlette.websockets ───────────────────────────────────────
    starlette_websockets = _mod("starlette.websockets")
    starlette_websockets.WebSocket = _websockets.WebSocket  # type: ignore[attr-defined]
    starlette_websockets.WebSocketState = _websockets.WebSocketState  # type: ignore[attr-defined]
    starlette_websockets.WebSocketDisconnect = _exceptions.WebSocketDisconnect  # type: ignore[attr-defined]
    starlette_websockets.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]

    class WebSocketClose:
        """Starlette-compatible WebSocketClose -- an ASGI response that sends
        a websocket.close message when called."""
        def __init__(self, code: int = 1000, reason: str | None = None):
            self.code = code
            self.reason = reason
        async def __call__(self, scope, receive, send):
            await send({"type": "websocket.close", "code": self.code, "reason": self.reason or ""})
    starlette_websockets.WebSocketClose = WebSocketClose  # type: ignore[attr-defined]

    modules["starlette.websockets"] = starlette_websockets

    # ── starlette.datastructures ───────────────────────────────────
    starlette_ds = _mod("starlette.datastructures")
    starlette_ds.URL = _datastructures.URL  # type: ignore[attr-defined]
    starlette_ds.URLPath = _datastructures.URLPath  # type: ignore[attr-defined]
    starlette_ds.Headers = _datastructures.Headers  # type: ignore[attr-defined]
    starlette_ds.MutableHeaders = _datastructures.MutableHeaders  # type: ignore[attr-defined]
    starlette_ds.QueryParams = _datastructures.QueryParams  # type: ignore[attr-defined]
    starlette_ds.Address = _datastructures.Address  # type: ignore[attr-defined]
    starlette_ds.State = _datastructures.State  # type: ignore[attr-defined]
    starlette_ds.FormData = _datastructures.FormData  # type: ignore[attr-defined]
    starlette_ds.Secret = _datastructures.Secret  # type: ignore[attr-defined]
    starlette_ds.UploadFile = fastapi_rs.UploadFile  # type: ignore[attr-defined]
    # Stubs for ImmutableMultiDict, MultiDict, CommaSeparatedStrings
    class ImmutableMultiDict(dict):
        """Stub for starlette.datastructures.ImmutableMultiDict."""
        def getlist(self, key):
            val = self.get(key)
            if val is None:
                return []
            if isinstance(val, list):
                return val
            return [val]
        def multi_items(self):
            for k, v in self.items():
                if isinstance(v, list):
                    for item in v:
                        yield k, item
                else:
                    yield k, v
    class MultiDict(ImmutableMultiDict):
        """Mutable variant of ImmutableMultiDict."""
        pass
    class CommaSeparatedStrings:
        """String that splits on commas, iterates over parts."""
        def __init__(self, value=""):
            if isinstance(value, (list, tuple)):
                self._items = list(value)
            else:
                self._items = [item.strip() for item in str(value).split(",") if item.strip()]
        def __iter__(self):
            return iter(self._items)
        def __len__(self):
            return len(self._items)
        def __repr__(self):
            return f"CommaSeparatedStrings({self._items!r})"
    starlette_ds.ImmutableMultiDict = ImmutableMultiDict  # type: ignore[attr-defined]
    starlette_ds.MultiDict = MultiDict  # type: ignore[attr-defined]
    starlette_ds.CommaSeparatedStrings = CommaSeparatedStrings  # type: ignore[attr-defined]
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
    starlette_concurrency.run_until_first_complete = _concurrency.run_until_first_complete  # type: ignore[attr-defined]
    modules["starlette.concurrency"] = starlette_concurrency

    # ── starlette.background ───────────────────────────────────────
    starlette_background = _mod("starlette.background")
    starlette_background.BackgroundTask = _background.BackgroundTask  # type: ignore[attr-defined]
    starlette_background.BackgroundTasks = _background.BackgroundTasks  # type: ignore[attr-defined]
    modules["starlette.background"] = starlette_background

    # ── starlette.middleware ───────────────────────────────────────
    starlette_middleware = _mod("starlette.middleware")
    starlette_middleware.Middleware = _sc.Middleware  # type: ignore[attr-defined]
    modules["starlette.middleware"] = starlette_middleware

    # ── starlette.middleware.errors ────────────────────────────────
    starlette_mw_errors = _mod("starlette.middleware.errors")
    starlette_mw_errors.ServerErrorMiddleware = _sc.ServerErrorMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.errors"] = starlette_mw_errors

    # ── starlette.middleware.exceptions ────────────────────────────
    starlette_mw_exc = _mod("starlette.middleware.exceptions")
    starlette_mw_exc.ExceptionMiddleware = _sc.ExceptionMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.exceptions"] = starlette_mw_exc

    # ── starlette.middleware.wsgi ──────────────────────────────────
    starlette_mw_wsgi = _mod("starlette.middleware.wsgi")
    starlette_mw_wsgi.WSGIMiddleware = _sc.WSGIMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.wsgi"] = starlette_mw_wsgi

    # ── starlette.types ────────────────────────────────────────────
    starlette_types = _mod("starlette.types")
    starlette_types.ASGIApp = _sc.ASGIApp  # type: ignore[attr-defined]
    starlette_types.Receive = _sc.Receive  # type: ignore[attr-defined]
    starlette_types.Send = _sc.Send  # type: ignore[attr-defined]
    starlette_types.Scope = _sc.Scope  # type: ignore[attr-defined]
    starlette_types.Message = _sc.Message  # type: ignore[attr-defined]
    starlette_types.Lifespan = typing.Any  # type: ignore[attr-defined]
    starlette_types.StatefulLifespan = typing.Any  # type: ignore[attr-defined]
    starlette_types.StatelessLifespan = typing.Any  # type: ignore[attr-defined]
    starlette_types.ExceptionHandler = typing.Any  # type: ignore[attr-defined]
    starlette_types.HTTPExceptionHandler = typing.Any  # type: ignore[attr-defined]
    starlette_types.WebSocketExceptionHandler = typing.Any  # type: ignore[attr-defined]
    starlette_types.AppType = typing.TypeVar("AppType")  # type: ignore[attr-defined]
    modules["starlette.types"] = starlette_types

    # ── starlette.convertors ───────────────────────────────────────
    starlette_convertors = _mod("starlette.convertors")
    starlette_convertors.Convertor = _sc.Convertor  # type: ignore[attr-defined]
    starlette_convertors.StringConvertor = _sc.StringConvertor  # type: ignore[attr-defined]
    starlette_convertors.PathConvertor = _sc.PathConvertor  # type: ignore[attr-defined]
    starlette_convertors.IntegerConvertor = _sc.IntegerConvertor  # type: ignore[attr-defined]
    starlette_convertors.FloatConvertor = _sc.FloatConvertor  # type: ignore[attr-defined]
    starlette_convertors.UUIDConvertor = _sc.UUIDConvertor  # type: ignore[attr-defined]
    starlette_convertors.CONVERTOR_TYPES = _sc.CONVERTOR_TYPES  # type: ignore[attr-defined]
    modules["starlette.convertors"] = starlette_convertors

    # ── starlette.formparsers ──────────────────────────────────────
    starlette_formparsers = _mod("starlette.formparsers")
    starlette_formparsers.FormParser = _sc.FormParser  # type: ignore[attr-defined]
    starlette_formparsers.MultiPartParser = _sc.MultiPartParser  # type: ignore[attr-defined]
    modules["starlette.formparsers"] = starlette_formparsers

    # ── starlette.endpoints ────────────────────────────────────────
    starlette_endpoints = _mod("starlette.endpoints")
    starlette_endpoints.HTTPEndpoint = _sc.HTTPEndpoint  # type: ignore[attr-defined]
    starlette_endpoints.WebSocketEndpoint = _sc.WebSocketEndpoint  # type: ignore[attr-defined]
    modules["starlette.endpoints"] = starlette_endpoints

    # ── starlette.schemas ──────────────────────────────────────────
    starlette_schemas = _mod("starlette.schemas")
    starlette_schemas.SchemaGenerator = _sc.SchemaGenerator  # type: ignore[attr-defined]
    # EndpointInfo — apitally uses this
    class EndpointInfo:
        def __init__(self, path="", http_method="", func=None, name=""):
            self.path = path
            self.http_method = http_method
            self.func = func
            self.name = name
    starlette_schemas.EndpointInfo = EndpointInfo  # type: ignore[attr-defined]
    modules["starlette.schemas"] = starlette_schemas

    # ── starlette.applications ─────────────────────────────────────
    # FastAPI subclasses Starlette — for `isinstance(app, Starlette)`
    # checks, we alias Starlette to our FastAPI class.
    import fastapi_rs.applications as _applications
    starlette_applications = _mod("starlette.applications")
    starlette_applications.Starlette = _applications.FastAPI  # type: ignore[attr-defined]
    modules["starlette.applications"] = starlette_applications

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
    import fastapi_rs.middleware.base as _base
    starlette_middleware_base = _mod("starlette.middleware.base")
    starlette_middleware_base.BaseHTTPMiddleware = _base.BaseHTTPMiddleware  # type: ignore[attr-defined]
    starlette_middleware_base.RequestResponseEndpoint = typing.Callable  # type: ignore[attr-defined]
    starlette_middleware_base.DispatchFunction = typing.Callable  # type: ignore[attr-defined]
    modules["starlette.middleware.base"] = starlette_middleware_base

    # ── starlette.middleware.sessions ──────────────────────────────
    import fastapi_rs.middleware.sessions as _sessions
    starlette_sessions = _mod("starlette.middleware.sessions")
    starlette_sessions.SessionMiddleware = _sessions.SessionMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.sessions"] = starlette_sessions

    # ── starlette.authentication ───────────────────────────────────
    import fastapi_rs.authentication as _auth
    starlette_auth = _mod("starlette.authentication")
    starlette_auth.AuthenticationBackend = _auth.AuthenticationBackend  # type: ignore[attr-defined]
    starlette_auth.AuthenticationError = _auth.AuthenticationError  # type: ignore[attr-defined]
    starlette_auth.AuthCredentials = _auth.AuthCredentials  # type: ignore[attr-defined]
    starlette_auth.BaseUser = _auth.BaseUser  # type: ignore[attr-defined]
    starlette_auth.SimpleUser = _auth.SimpleUser  # type: ignore[attr-defined]
    starlette_auth.UnauthenticatedUser = _auth.UnauthenticatedUser  # type: ignore[attr-defined]
    starlette_auth.requires = _auth.requires  # type: ignore[attr-defined]
    starlette_auth.has_required_scope = _sc.has_required_scope  # type: ignore[attr-defined]
    modules["starlette.authentication"] = starlette_auth

    # ── starlette.middleware.authentication ────────────────────────
    starlette_auth_mw = _mod("starlette.middleware.authentication")
    starlette_auth_mw.AuthenticationMiddleware = _auth.AuthenticationMiddleware  # type: ignore[attr-defined]
    modules["starlette.middleware.authentication"] = starlette_auth_mw

    # ── starlette.staticfiles ──────────────────────────────────────
    import fastapi_rs.staticfiles as _staticfiles
    starlette_staticfiles = _mod("starlette.staticfiles")
    starlette_staticfiles.StaticFiles = _staticfiles.StaticFiles  # type: ignore[attr-defined]
    modules["starlette.staticfiles"] = starlette_staticfiles

    # ── starlette.templating ───────────────────────────────────────
    import fastapi_rs.templating as _templating
    starlette_templating = _mod("starlette.templating")
    starlette_templating.Jinja2Templates = _templating.Jinja2Templates  # type: ignore[attr-defined]
    modules["starlette.templating"] = starlette_templating

    # ── starlette.testclient ───────────────────────────────────────
    starlette_testclient = _mod("starlette.testclient")
    from fastapi_rs.testclient import TestClient, _WebSocketTestSession
    starlette_testclient.TestClient = TestClient  # type: ignore[attr-defined]
    starlette_testclient.WebSocketTestSession = _WebSocketTestSession  # type: ignore[attr-defined]
    modules["starlette.testclient"] = starlette_testclient

    # ── starlette.config ────────────────────────────────────────────
    starlette_config = _mod("starlette.config")
    import os as _os
    class Config:
        """Minimal starlette.config.Config stub."""
        def __init__(self, env_file=None, environ=None):
            self.environ = environ or _os.environ
        def __call__(self, key, *, cast=None, default=None):
            val = self.environ.get(key, default)
            if cast is not None and val is not None:
                val = cast(val)
            return val
        def get(self, key, *, cast=None, default=None):
            return self(key, cast=cast, default=default)
    starlette_config.Config = Config  # type: ignore[attr-defined]
    modules["starlette.config"] = starlette_config

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
