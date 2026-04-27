"""FastAPI compatibility shim.

Creates fake module objects so ``from fastapi import ...`` resolves
to fastapi-turbo implementations.
"""

from __future__ import annotations

import types
from typing import Any


def _build() -> dict[str, types.ModuleType]:
    # Lazy imports to avoid circular issues
    import fastapi_turbo
    import fastapi_turbo.responses as _responses
    import fastapi_turbo.requests as _requests
    import fastapi_turbo.routing as _routing
    import fastapi_turbo.exceptions as _exceptions
    import fastapi_turbo.param_functions as _params
    import fastapi_turbo.dependencies as _dependencies
    import fastapi_turbo.applications as _applications
    import fastapi_turbo.security as _security
    import fastapi_turbo.background as _background
    import fastapi_turbo.encoders as _encoders
    import fastapi_turbo.status as _status
    import fastapi_turbo.datastructures as _datastructures
    import fastapi_turbo.websockets as _websockets
    import fastapi_turbo.concurrency as _concurrency
    import fastapi_turbo.middleware as _middleware
    import fastapi_turbo.middleware.cors as _cors

    modules: dict[str, types.ModuleType] = {}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []  # type: ignore[attr-defined]
        m.__package__ = name
        return m

    # ── fastapi (top-level) ────────────────────────────────────────
    fastapi = _mod("fastapi")
    # Report the FastAPI version we target compat against, not our own
    # 0.1.0. Third-party libraries (sentry-sdk, slowapi, fastapi-users)
    # gate features on FastAPI version — reporting the real target
    # ensures they take the modern code path.
    fastapi.__version__ = "0.136.0"  # type: ignore[attr-defined]
    fastapi.fastapi_turbo_version = fastapi_turbo.__version__  # type: ignore[attr-defined]

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

    # WebSocketDisconnect — commonly imported from top level
    fastapi.WebSocketDisconnect = _exceptions.WebSocketDisconnect  # type: ignore[attr-defined]

    # Security (commonly imported from top-level too)
    fastapi.Security = _dependencies.Security  # type: ignore[attr-defined]

    # Mirror every top-level export from ``fastapi_turbo`` onto the
    # ``fastapi`` shim module. Covers extensions we add (HTTPDigest,
    # ORJSONResponse, UJSONResponse, EventSourceResponse, WebSocketState,
    # ServerSentEvent, OAuth2ClientCredentials, etc.) so
    # ``from fastapi import X`` resolves for every symbol that worked
    # via ``from fastapi_turbo import X``.
    for _name in getattr(fastapi_turbo, "__all__", ()):
        if not hasattr(fastapi, _name):
            _val = getattr(fastapi_turbo, _name, None)
            if _val is not None:
                try:
                    setattr(fastapi, _name, _val)
                except Exception:  # noqa: BLE001
                    pass

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
    fastapi_responses.ORJSONResponse = _responses.ORJSONResponse  # type: ignore[attr-defined]
    fastapi_responses.UJSONResponse = _responses.UJSONResponse  # type: ignore[attr-defined]
    fastapi_responses.EventSourceResponse = _responses.EventSourceResponse  # type: ignore[attr-defined]
    modules["fastapi.responses"] = fastapi_responses

    # ── fastapi.sse ───────────────────────────────────────────────
    import fastapi_turbo.sse as _sse
    fastapi_sse = _mod("fastapi.sse")
    fastapi_sse.EventSourceResponse = _responses.EventSourceResponse  # type: ignore[attr-defined]
    fastapi_sse.ServerSentEvent = _sse.ServerSentEvent  # type: ignore[attr-defined]
    fastapi_sse.format_sse_event = _sse.format_sse_event  # type: ignore[attr-defined]
    fastapi_sse.KEEPALIVE_COMMENT = _sse.KEEPALIVE_COMMENT  # type: ignore[attr-defined]
    modules["fastapi.sse"] = fastapi_sse

    # ── fastapi.applications ───────────────────────────────────────
    fastapi_applications = _mod("fastapi.applications")
    fastapi_applications.FastAPI = _applications.FastAPI  # type: ignore[attr-defined]
    modules["fastapi.applications"] = fastapi_applications

    # ── fastapi.routing ────────────────────────────────────────────
    fastapi_routing = _mod("fastapi.routing")
    fastapi_routing.APIRouter = _routing.APIRouter  # type: ignore[attr-defined]
    fastapi_routing.APIRoute = _routing.APIRoute  # type: ignore[attr-defined]
    # Private helpers tests sometimes import directly.
    try:
        fastapi_routing._default_generate_unique_id = _routing._default_generate_unique_id  # type: ignore[attr-defined]
    except AttributeError:
        pass
    # APIWebSocketRoute stub — fastapi-turbo registers WS routes directly
    # via @app.websocket() rather than a dedicated class, but third-party
    # code uses this class name for isinstance checks.
    class APIWebSocketRoute:
        def __init__(self, path: str, endpoint, *, name: str | None = None):
            self.path = path
            self.endpoint = endpoint
            self.name = name or endpoint.__name__
    fastapi_routing.APIWebSocketRoute = APIWebSocketRoute  # type: ignore[attr-defined]
    # request_response — stac-fastapi, fastapi-pagination use this.
    # Upstream is a SYNC function returning an ASGI callable (the
    # callable itself is async). The returned callable builds a
    # Request, calls the handler, awaits the response, and dispatches
    # the response's ASGI events (status + body).
    #
    # Earlier shim was ``async def``: calling ``request_response
    # (handler)`` returned a coroutine instead of the ASGI app —
    # ``app(scope, ...)`` raised ``TypeError: 'coroutine' object
    # is not callable``. R25 changed it to ``def`` but left the body
    # as ``pass`` — fixed the type error but produced an empty
    # response (zero ASGI messages, the test client hung). R26
    # gives it real semantics: build Request → call handler → send
    # Response, mirroring upstream's structure (without the
    # AsyncExitStack instrumentation since fastapi_turbo manages
    # its own lifecycle elsewhere).
    def request_response(func):
        """Wrap a ``func(request) -> Response`` into an ASGI app.

        Matches the shape of upstream FastAPI's
        ``fastapi.routing.request_response``: sync wrapper,
        returns an ``async def app(scope, receive, send)`` that
        builds the Request, calls ``func``, and sends the
        Response."""
        import asyncio
        import inspect
        from fastapi_turbo.requests import Request as _Request

        is_async = inspect.iscoroutinefunction(func)

        async def app(scope, receive, send):
            request = _Request(scope, receive=receive, send=send)
            if is_async:
                response = await func(request)
            else:
                # Sync handler — run in the default executor so we
                # don't block the event loop. Mirrors upstream's
                # ``functools.partial(run_in_threadpool, func)``.
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, func, request)
            # Dispatch the Response through our shared ASGI sender.
            # ``fastapi_turbo.responses.Response`` doesn't implement
            # the ``async __call__(scope, receive, send)`` protocol
            # directly (unlike Starlette's Response); the dispatcher
            # uses ``_send_asgi_response`` to translate it into ASGI
            # ``http.response.start`` + ``http.response.body``
            # frames. Reuse that here so streaming / file / JSON
            # responses all serialise through one path.
            if not hasattr(response, "status_code"):
                # Best-effort: a handler that returned a raw dict gets
                # wrapped in JSONResponse.
                from fastapi_turbo.responses import JSONResponse as _JR
                response = _JR(content=response)
            from fastapi_turbo.applications import _send_asgi_response
            await _send_asgi_response(send, response, scope=scope)

        return app
    fastapi_routing.request_response = request_response  # type: ignore[attr-defined]

    # ``get_request_handler`` — sentry_sdk.integrations.fastapi patches this
    # at setup_once() to wrap every FastAPI route with its tracing layer.
    # Our router compiles routes via its own pipeline and never calls it,
    # so the monkey-patch is harmless here; we just need the attribute to
    # exist for the patch to install. Sentry's actual tracing arrives via
    # the ``SentryAsgiMiddleware`` path (``app.add_middleware(...)``),
    # which the ASGI middleware chain invokes correctly.
    def get_request_handler(
        dependant=None,
        body_field=None,
        status_code=None,
        response_class=None,
        response_field=None,
        response_model_include=None,
        response_model_exclude=None,
        response_model_by_alias=True,
        response_model_exclude_unset=False,
        response_model_exclude_defaults=False,
        response_model_exclude_none=False,
        dependency_overrides_provider=None,
        embed_body_fields=False,
    ):
        """Stub — Sentry / slowapi monkey-patch this at setup time.
        Returns a no-op async ASGI-style app; our router never calls it.
        """
        async def _noop_app(request):
            return None
        return _noop_app

    fastapi_routing.get_request_handler = get_request_handler  # type: ignore[attr-defined]

    # ``serialize_response`` — sentry_sdk + response-serialisation libs
    # patch this. Provide a simple pass-through so the patch site exists.
    def serialize_response(
        *,
        field=None,
        response_content,
        include=None,
        exclude=None,
        by_alias=True,
        exclude_unset=False,
        exclude_defaults=False,
        exclude_none=False,
        is_coroutine=False,
    ):
        return response_content

    fastapi_routing.serialize_response = serialize_response  # type: ignore[attr-defined]

    # FA tests monkeypatch ``fastapi.routing._PING_INTERVAL`` to speed
    # up keepalive pings. Re-export the value from ``fastapi.sse`` so
    # ``monkeypatch.setattr("fastapi.routing._PING_INTERVAL", 0.05)``
    # works.
    fastapi_routing._PING_INTERVAL = _sse._PING_INTERVAL  # type: ignore[attr-defined]
    # Mount — Airflow uses `from fastapi.routing import Mount`
    try:
        from fastapi_turbo._starlette_compat import Mount as _Mount
        fastapi_routing.Mount = _Mount  # type: ignore[attr-defined]
    except ImportError:
        pass
    modules["fastapi.routing"] = fastapi_routing

    # ── fastapi.exceptions ─────────────────────────────────────────
    fastapi_exceptions = _mod("fastapi.exceptions")
    fastapi_exceptions.HTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    fastapi_exceptions.RequestValidationError = _exceptions.RequestValidationError  # type: ignore[attr-defined]
    fastapi_exceptions.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
    fastapi_exceptions.WebSocketDisconnect = _exceptions.WebSocketDisconnect  # type: ignore[attr-defined]
    fastapi_exceptions.WebSocketRequestValidationError = _exceptions.WebSocketRequestValidationError  # type: ignore[attr-defined]
    fastapi_exceptions.FastAPIError = _exceptions.FastAPIError  # type: ignore[attr-defined]
    fastapi_exceptions.ResponseValidationError = _exceptions.ResponseValidationError  # type: ignore[attr-defined]
    fastapi_exceptions.ValidationException = _exceptions.ValidationException  # type: ignore[attr-defined]
    fastapi_exceptions.StarletteHTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    fastapi_exceptions.StarletteWebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
    fastapi_exceptions.DependencyScopeError = _exceptions.DependencyScopeError  # type: ignore[attr-defined]
    fastapi_exceptions.PydanticV1NotSupportedError = _exceptions.PydanticV1NotSupportedError  # type: ignore[attr-defined]
    fastapi_exceptions.FastAPIDeprecationWarning = _exceptions.FastAPIDeprecationWarning  # type: ignore[attr-defined]
    fastapi_exceptions.RequestErrorModel = _exceptions.RequestErrorModel  # type: ignore[attr-defined]
    fastapi_exceptions.WebSocketErrorModel = _exceptions.WebSocketErrorModel  # type: ignore[attr-defined]
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
    fastapi_params.Security = _dependencies.Security  # type: ignore[attr-defined]
    # Stubs for Param / ParamTypes used by advanced introspection
    import enum as _enum
    class ParamTypes(str, _enum.Enum):
        query = "query"
        header = "header"
        path = "path"
        cookie = "cookie"
    fastapi_params.Param = _params.Param  # type: ignore[attr-defined]
    fastapi_params.ParamTypes = ParamTypes  # type: ignore[attr-defined]
    modules["fastapi.params"] = fastapi_params

    # ── fastapi.param_functions ────────────────────────────────────
    # Mirror of fastapi.params — FastAPI splits them but we expose both.
    fastapi_paramfunctions = _mod("fastapi.param_functions")
    for _name in ("Query", "Path", "Header", "Cookie", "Body", "Form", "File"):
        setattr(fastapi_paramfunctions, _name, getattr(_params, _name))
    fastapi_paramfunctions.Depends = _dependencies.Depends  # type: ignore[attr-defined]
    fastapi_paramfunctions.Security = _dependencies.Security  # type: ignore[attr-defined]
    modules["fastapi.param_functions"] = fastapi_paramfunctions

    # ── fastapi.security ───────────────────────────────────────────
    fastapi_security = _mod("fastapi.security")
    fastapi_security.OAuth2PasswordBearer = _security.OAuth2PasswordBearer  # type: ignore[attr-defined]
    fastapi_security.OAuth2PasswordRequestForm = _security.OAuth2PasswordRequestForm  # type: ignore[attr-defined]
    # OAuth2 base class — parent of all OAuth2* flavours for isinstance checks
    fastapi_security.OAuth2 = _security.OAuth2  # type: ignore[attr-defined]
    # Strict variant: requires grant_type="password"
    fastapi_security.OAuth2PasswordRequestFormStrict = _security.OAuth2PasswordRequestFormStrict  # type: ignore[attr-defined]
    fastapi_security.HTTPBearer = _security.HTTPBearer  # type: ignore[attr-defined]
    fastapi_security.HTTPDigest = _security.HTTPDigest  # type: ignore[attr-defined]
    fastapi_security.HTTPBasic = _security.HTTPBasic  # type: ignore[attr-defined]
    fastapi_security.OAuth2ClientCredentials = _security.OAuth2ClientCredentials  # type: ignore[attr-defined]
    fastapi_security.OAuth2AuthorizationCodeBearer = _security.OAuth2AuthorizationCodeBearer  # type: ignore[attr-defined]
    fastapi_security.OpenIdConnect = _security.OpenIdConnect  # type: ignore[attr-defined]
    fastapi_security.HTTPBasicCredentials = _security.HTTPBasicCredentials  # type: ignore[attr-defined]
    fastapi_security.HTTPAuthorizationCredentials = _security.HTTPAuthorizationCredentials  # type: ignore[attr-defined]
    fastapi_security.APIKeyHeader = _security.APIKeyHeader  # type: ignore[attr-defined]
    fastapi_security.APIKeyQuery = _security.APIKeyQuery  # type: ignore[attr-defined]
    fastapi_security.APIKeyCookie = _security.APIKeyCookie  # type: ignore[attr-defined]
    fastapi_security.SecurityScopes = _security.SecurityScopes  # type: ignore[attr-defined]
    modules["fastapi.security"] = fastapi_security

    # ── Security sub-modules ──────────────────────────────────────
    fastapi_security_oauth2 = _mod("fastapi.security.oauth2")
    fastapi_security_oauth2.OAuth2 = _security.OAuth2  # type: ignore[attr-defined]
    fastapi_security_oauth2.OAuth2PasswordBearer = _security.OAuth2PasswordBearer  # type: ignore[attr-defined]
    fastapi_security_oauth2.OAuth2AuthorizationCodeBearer = _security.OAuth2AuthorizationCodeBearer  # type: ignore[attr-defined]
    fastapi_security_oauth2.OAuth2PasswordRequestForm = _security.OAuth2PasswordRequestForm  # type: ignore[attr-defined]
    fastapi_security_oauth2.OAuth2PasswordRequestFormStrict = _security.OAuth2PasswordRequestFormStrict  # type: ignore[attr-defined]
    modules["fastapi.security.oauth2"] = fastapi_security_oauth2

    fastapi_security_http = _mod("fastapi.security.http")
    fastapi_security_http.HTTPBase = _security.HTTPBase  # type: ignore[attr-defined]
    fastapi_security_http.HTTPBearer = _security.HTTPBearer  # type: ignore[attr-defined]
    fastapi_security_http.HTTPDigest = _security.HTTPDigest  # type: ignore[attr-defined]
    fastapi_security_http.HTTPBasic = _security.HTTPBasic  # type: ignore[attr-defined]
    fastapi_security_http.HTTPBasicCredentials = _security.HTTPBasicCredentials  # type: ignore[attr-defined]
    fastapi_security_http.HTTPAuthorizationCredentials = _security.HTTPAuthorizationCredentials  # type: ignore[attr-defined]
    modules["fastapi.security.http"] = fastapi_security_http

    fastapi_security_api_key = _mod("fastapi.security.api_key")
    fastapi_security_api_key.APIKeyHeader = _security.APIKeyHeader  # type: ignore[attr-defined]
    fastapi_security_api_key.APIKeyQuery = _security.APIKeyQuery  # type: ignore[attr-defined]
    fastapi_security_api_key.APIKeyCookie = _security.APIKeyCookie  # type: ignore[attr-defined]
    modules["fastapi.security.api_key"] = fastapi_security_api_key

    fastapi_security_open_id = _mod("fastapi.security.open_id_connect_url")
    fastapi_security_open_id.OpenIdConnect = _security.OpenIdConnect  # type: ignore[attr-defined]
    modules["fastapi.security.open_id_connect_url"] = fastapi_security_open_id

    fastapi_security_base = _mod("fastapi.security.base")
    class SecurityBase: pass
    fastapi_security_base.SecurityBase = SecurityBase  # type: ignore[attr-defined]
    modules["fastapi.security.base"] = fastapi_security_base

    fastapi_security_utils = _mod("fastapi.security.utils")
    def get_authorization_scheme_param(authorization_header_value):
        if not authorization_header_value:
            return "", ""
        scheme, _, param = authorization_header_value.partition(" ")
        return scheme, param
    fastapi_security_utils.get_authorization_scheme_param = get_authorization_scheme_param  # type: ignore[attr-defined]
    modules["fastapi.security.utils"] = fastapi_security_utils

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
    fastapi_ds.Address = _datastructures.Address  # type: ignore[attr-defined]
    fastapi_ds.FormData = _datastructures.FormData  # type: ignore[attr-defined]
    # FastAPI-specific: Default + DefaultPlaceholder sentinel classes used
    # to mark "not set" kwargs so routers can distinguish from explicit None.
    class DefaultPlaceholder:
        """Internal sentinel — FastAPI uses this to detect "not explicitly set"."""
        def __init__(self, value):
            self.value = value
        def __bool__(self) -> bool:
            return bool(self.value)
        def __eq__(self, other):
            if not isinstance(other, DefaultPlaceholder):
                return NotImplemented
            return self.value == other.value
        def __hash__(self):
            try:
                return hash(self.value)
            except TypeError:
                return id(self)
    def Default(value):
        return DefaultPlaceholder(value)
    fastapi_ds.Default = Default  # type: ignore[attr-defined]
    fastapi_ds.DefaultPlaceholder = DefaultPlaceholder  # type: ignore[attr-defined]
    modules["fastapi.datastructures"] = fastapi_ds

    # ── fastapi.concurrency ────────────────────────────────────────
    fastapi_concurrency = _mod("fastapi.concurrency")
    fastapi_concurrency.run_in_threadpool = _concurrency.run_in_threadpool  # type: ignore[attr-defined]
    fastapi_concurrency.iterate_in_threadpool = _concurrency.iterate_in_threadpool  # type: ignore[attr-defined]
    import contextlib as _contextlib
    @_contextlib.asynccontextmanager
    async def _contextmanager_in_threadpool(cm):
        exit_val = await _concurrency.run_in_threadpool(cm.__enter__)
        try:
            yield exit_val
        except Exception as exc:
            ok = await _concurrency.run_in_threadpool(cm.__exit__, type(exc), exc, exc.__traceback__)
            if not ok:
                raise
        else:
            await _concurrency.run_in_threadpool(cm.__exit__, None, None, None)
    fastapi_concurrency.contextmanager_in_threadpool = _contextmanager_in_threadpool  # type: ignore[attr-defined]
    modules["fastapi.concurrency"] = fastapi_concurrency

    # ── fastapi.background ─────────────────────────────────────────
    fastapi_background = _mod("fastapi.background")
    fastapi_background.BackgroundTasks = _background.BackgroundTasks  # type: ignore[attr-defined]
    modules["fastapi.background"] = fastapi_background

    # ── fastapi.testclient ─────────────────────────────────────────
    fastapi_testclient = _mod("fastapi.testclient")
    from fastapi_turbo.testclient import TestClient
    fastapi_testclient.TestClient = TestClient  # type: ignore[attr-defined]
    try:
        from fastapi_turbo.testclient import AsyncTestClient as _ATC
        fastapi_testclient.AsyncTestClient = _ATC  # type: ignore[attr-defined]
    except ImportError:
        pass
    # ``AsyncClient`` / ``ASGITransport`` — FastAPI docs tell users to
    # use ``httpx.AsyncClient(transport=ASGITransport(app=app))`` for
    # async tests. Mirror the shorthand so ``from fastapi.testclient
    # import AsyncClient`` works.
    try:
        from fastapi_turbo.testclient import AsyncClient as _AC, ASGITransport as _AT
        fastapi_testclient.AsyncClient = _AC  # type: ignore[attr-defined]
        fastapi_testclient.ASGITransport = _AT  # type: ignore[attr-defined]
    except ImportError:
        pass
    modules["fastapi.testclient"] = fastapi_testclient

    # ── fastapi.requests ──────────────────────────────────────────
    fastapi_requests_mod = _mod("fastapi.requests")
    fastapi_requests_mod.Request = _requests.Request  # type: ignore[attr-defined]
    fastapi_requests_mod.HTTPConnection = _requests.HTTPConnection  # type: ignore[attr-defined]
    fastapi_requests_mod.ClientDisconnect = _requests.ClientDisconnect  # type: ignore[attr-defined]
    modules["fastapi.requests"] = fastapi_requests_mod

    # ── fastapi.websockets ─────────────────────────────────────────
    fastapi_websockets = _mod("fastapi.websockets")
    fastapi_websockets.WebSocket = _websockets.WebSocket  # type: ignore[attr-defined]
    fastapi_websockets.WebSocketDisconnect = _exceptions.WebSocketDisconnect  # type: ignore[attr-defined]
    fastapi_websockets.WebSocketState = _websockets.WebSocketState  # type: ignore[attr-defined]
    modules["fastapi.websockets"] = fastapi_websockets

    # ── fastapi.middleware ─────────────────────────────────────────
    import fastapi_turbo._starlette_compat as _sc
    fastapi_middleware = _mod("fastapi.middleware")
    fastapi_middleware.Middleware = _sc.Middleware  # type: ignore[attr-defined]
    modules["fastapi.middleware"] = fastapi_middleware

    # ── fastapi.middleware.wsgi ────────────────────────────────────
    fastapi_middleware_wsgi = _mod("fastapi.middleware.wsgi")
    fastapi_middleware_wsgi.WSGIMiddleware = _sc.WSGIMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.wsgi"] = fastapi_middleware_wsgi

    # ── fastapi.staticfiles ────────────────────────────────────────
    import fastapi_turbo.staticfiles as _staticfiles
    fastapi_staticfiles = _mod("fastapi.staticfiles")
    fastapi_staticfiles.StaticFiles = _staticfiles.StaticFiles  # type: ignore[attr-defined]
    modules["fastapi.staticfiles"] = fastapi_staticfiles

    # ── fastapi.templating ─────────────────────────────────────────
    import fastapi_turbo.templating as _templating
    fastapi_templating = _mod("fastapi.templating")
    fastapi_templating.Jinja2Templates = _templating.Jinja2Templates  # type: ignore[attr-defined]
    modules["fastapi.templating"] = fastapi_templating

    # ── fastapi.logger ─────────────────────────────────────────────
    import logging
    fastapi_logger = _mod("fastapi.logger")
    fastapi_logger.logger = logging.getLogger("fastapi")  # type: ignore[attr-defined]
    modules["fastapi.logger"] = fastapi_logger

    # ── fastapi.openapi.* ──────────────────────────────────────────
    import fastapi_turbo._openapi as _oa
    fastapi_openapi = _mod("fastapi.openapi")
    modules["fastapi.openapi"] = fastapi_openapi

    fastapi_openapi_utils = _mod("fastapi.openapi.utils")
    fastapi_openapi_utils.get_openapi = _oa.generate_openapi_schema  # type: ignore[attr-defined]
    modules["fastapi.openapi.utils"] = fastapi_openapi_utils

    # ── fastapi.openapi.constants ──────────────────────────────────
    fastapi_openapi_constants = _mod("fastapi.openapi.constants")
    fastapi_openapi_constants.REF_PREFIX = "#/components/schemas/"  # type: ignore[attr-defined]
    fastapi_openapi_constants.REF_TEMPLATE = "#/components/schemas/{model}"  # type: ignore[attr-defined]
    fastapi_openapi_constants.METHODS_WITH_BODY = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}  # type: ignore[attr-defined]
    modules["fastapi.openapi.constants"] = fastapi_openapi_constants

    # Swagger/ReDoc HTML helpers
    fastapi_openapi_docs = _mod("fastapi.openapi.docs")

    # FA uses fixed CDN URLs as the default for these kwargs — tests
    # assert that the default appears in the rendered body. Keep them
    # byte-for-byte identical to FA.
    _DEFAULT_SWAGGER_JS = (
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.29.0/swagger-ui-bundle.js"
    )
    _DEFAULT_SWAGGER_CSS = (
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.29.0/swagger-ui.css"
    )
    _DEFAULT_SWAGGER_FAVICON = "https://fastapi.tiangolo.com/img/favicon.png"
    _DEFAULT_REDOC_JS = (
        "https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"
    )
    _DEFAULT_REDOC_FAVICON = "https://fastapi.tiangolo.com/img/favicon.png"

    swagger_ui_default_parameters: dict = {
        "dom_id": "#swagger-ui",
        "layout": "BaseLayout",
        "deepLinking": True,
        "showExtensions": True,
        "showCommonExtensions": True,
    }

    def _html_safe_json(value) -> str:
        """HTML-safe JSON encoding — escapes ``< > &`` so the serialized
        value is safe to embed inside a ``<script>`` tag. Matches FA's
        helper in ``fastapi.openapi.docs._html_safe_json`` exactly;
        ``test_swagger_ui_escape.py`` asserts on these escapes.

        Uses ``JSONEncoder().encode`` instead of the module-level
        ``json.dumps`` so tests that monkey-patch ``json.dumps``
        (``test_dump_json_fast_path``) aren't tripped by our docs
        rendering.
        """
        import json as _json
        return (
            _json.JSONEncoder().encode(value)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )

    def get_swagger_ui_html(
        *,
        openapi_url,
        title="API docs",
        swagger_js_url=_DEFAULT_SWAGGER_JS,
        swagger_css_url=_DEFAULT_SWAGGER_CSS,
        swagger_favicon_url=_DEFAULT_SWAGGER_FAVICON,
        oauth2_redirect_url=None,
        init_oauth=None,
        swagger_ui_parameters=None,
    ):
        from fastapi_turbo.encoders import jsonable_encoder as _jenc
        from fastapi_turbo.responses import HTMLResponse
        js_url = swagger_js_url
        css_url = swagger_css_url
        favicon = ""
        if swagger_favicon_url:
            favicon = f'<link rel="icon" href="{swagger_favicon_url}">'

        current_params = dict(swagger_ui_default_parameters)
        if swagger_ui_parameters:
            current_params.update(swagger_ui_parameters)

        ui_body = f"url: '{openapi_url}',\n"
        for k, v in current_params.items():
            ui_body += f"{_html_safe_json(k)}: {_html_safe_json(_jenc(v))},\n"
        if oauth2_redirect_url:
            ui_body += (
                f"oauth2RedirectUrl: window.location.origin + "
                f"'{oauth2_redirect_url}',\n"
            )

        init_oauth_block = ""
        if init_oauth:
            init_oauth_block = (
                f"\nui.initOAuth({_html_safe_json(_jenc(init_oauth))})"
            )

        html = (
            f"<!DOCTYPE html><html><head><title>{title}</title>\n"
            f'<link rel="stylesheet" href="{css_url}">\n'
            f"{favicon}</head><body>\n"
            f'<div id="swagger-ui"></div>\n'
            f'<script src="{js_url}"></script>\n'
            f"<script>\nconst ui = SwaggerUIBundle({{\n{ui_body}"
            "presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],\n"
            f"}})"
            f"{init_oauth_block}\n"
            f"</script>\n"
            f"</body></html>"
        )
        return HTMLResponse(html)

    def get_redoc_html(
        *,
        openapi_url,
        title="API docs",
        redoc_js_url=_DEFAULT_REDOC_JS,
        redoc_favicon_url=_DEFAULT_REDOC_FAVICON,
        with_google_fonts=True,
    ):
        from fastapi_turbo.responses import HTMLResponse
        fonts = ""
        if with_google_fonts:
            fonts = (
                '<link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">'
            )
        html = (
            f"<!DOCTYPE html><html><head><title>{title}</title>"
            f'<link rel="shortcut icon" href="{redoc_favicon_url}">'
            f"{fonts}"
            f"</head><body>"
            f"<redoc spec-url='{openapi_url}'></redoc>"
            f'<script src="{redoc_js_url}"></script>'
            f"</body></html>"
        )
        return HTMLResponse(html)

    def get_swagger_ui_oauth2_redirect_html():
        from fastapi_turbo.responses import HTMLResponse
        return HTMLResponse(
            '<!doctype html><html><body>'
            '<script>window.onload=function(){'
            'var qp=window.location.hash?window.location.hash.substring(1):window.location.search.substring(1);'
            'var data={};qp.split("&").forEach(function(p){var kv=p.split("=");data[kv[0]]=decodeURIComponent(kv[1]||"");});'
            'window.opener.swaggerUIRedirectOauth2(data);window.close();'
            '}</script></body></html>'
        )

    fastapi_openapi_docs.get_swagger_ui_html = get_swagger_ui_html  # type: ignore[attr-defined]
    fastapi_openapi_docs.get_redoc_html = get_redoc_html  # type: ignore[attr-defined]
    fastapi_openapi_docs.get_swagger_ui_oauth2_redirect_html = get_swagger_ui_oauth2_redirect_html  # type: ignore[attr-defined]

    fastapi_openapi_docs.swagger_ui_default_parameters = {  # type: ignore[attr-defined]
        "dom_id": "#swagger-ui",
        "layout": "BaseLayout",
        "deepLinking": True,
        "showExtensions": True,
        "showCommonExtensions": True,
    }

    modules["fastapi.openapi.docs"] = fastapi_openapi_docs

    # OpenAPI models stubs -- dict subclasses that pass isinstance checks
    fastapi_openapi_models = _mod("fastapi.openapi.models")
    _openapi_model_names = [
        "OpenAPI", "Info", "Contact", "License", "Server", "ServerVariable",
        "PathItem", "Operation", "ExternalDocumentation", "Parameter",
        "RequestBody", "MediaType", "Encoding", "Response", "Responses",
        "Reference", "Discriminator", "XML", "Example",
        "Link", "Header", "Tag", "Components", "SecurityScheme",
        "OAuthFlow", "OAuthFlows", "SecurityBase", "Callback", "Webhook",
        "OAuthFlowImplicit", "OAuthFlowPassword", "OAuthFlowClientCredentials",
        "OAuthFlowAuthorizationCode", "OAuth2", "OpenIdConnect",
        "APIKey", "HTTPBase", "HTTPBearer",
    ]
    for _oai_name in _openapi_model_names:
        setattr(fastapi_openapi_models, _oai_name, type(_oai_name, (dict,), {}))
    # ``SchemaType`` is the Literal alias FA 0.115+ uses inside
    # ``Schema.type`` — third-party OpenAPI plugins import it.
    import typing as _oai_typing
    _SCHEMA_TYPE = _oai_typing.Literal[
        "array", "boolean", "integer", "number", "object", "string", "null"
    ]
    fastapi_openapi_models.SchemaType = _SCHEMA_TYPE  # type: ignore[attr-defined]
    # ``Schema`` — a minimal Pydantic model matching FA 0.115+. Tests
    # assert ``Schema(type="array").type == "array"`` and that invalid
    # types raise a ``ValueError``, which requires real validation.
    try:
        from pydantic import BaseModel as _OAI_BM, Field as _OAI_Field

        class Schema(_OAI_BM):
            type: _oai_typing.Optional[  # type: ignore[valid-type]
                _oai_typing.Union[_SCHEMA_TYPE, list[_SCHEMA_TYPE]]
            ] = None
            model_config = {"extra": "allow"}

        fastapi_openapi_models.Schema = Schema  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        fastapi_openapi_models.Schema = type("Schema", (dict,), {})  # type: ignore[attr-defined]
    modules["fastapi.openapi.models"] = fastapi_openapi_models

    # ── fastapi.middleware.cors ────────────────────────────────────
    fastapi_middleware_cors = _mod("fastapi.middleware.cors")
    fastapi_middleware_cors.CORSMiddleware = _cors.CORSMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.cors"] = fastapi_middleware_cors

    # ── fastapi.middleware.gzip ────────────────────────────────────
    from fastapi_turbo.middleware.gzip import GZipMiddleware as _GZipMiddleware
    fastapi_middleware_gzip = _mod("fastapi.middleware.gzip")
    fastapi_middleware_gzip.GZipMiddleware = _GZipMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.gzip"] = fastapi_middleware_gzip

    # ── fastapi.middleware.trustedhost ──────────────────────────────
    from fastapi_turbo.middleware.trustedhost import TrustedHostMiddleware as _TrustedHostMiddleware
    fastapi_middleware_th = _mod("fastapi.middleware.trustedhost")
    fastapi_middleware_th.TrustedHostMiddleware = _TrustedHostMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.trustedhost"] = fastapi_middleware_th

    # ── fastapi.middleware.httpsredirect ────────────────────────────
    from fastapi_turbo.middleware.httpsredirect import HTTPSRedirectMiddleware as _HTTPSRedirectMiddleware
    fastapi_middleware_hr = _mod("fastapi.middleware.httpsredirect")
    fastapi_middleware_hr.HTTPSRedirectMiddleware = _HTTPSRedirectMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.httpsredirect"] = fastapi_middleware_hr

    # ── fastapi.exception_handlers ───────────────────────────────────
    fastapi_exc_handlers = _mod("fastapi.exception_handlers")
    async def _http_exception_handler(request, exc):
        from fastapi_turbo.responses import JSONResponse, Response
        status_code = exc.status_code
        # 1xx, 204, 304 cannot have body
        if status_code < 200 or status_code in (204, 304):
            return Response(status_code=status_code, headers=exc.headers)
        return JSONResponse({"detail": exc.detail}, status_code=status_code, headers=exc.headers)
    async def _request_validation_exception_handler(request, exc):
        from fastapi_turbo.responses import JSONResponse
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    async def _websocket_request_validation_exception_handler(websocket, exc):
        await websocket.close(code=1008)
    fastapi_exc_handlers.http_exception_handler = _http_exception_handler  # type: ignore[attr-defined]
    fastapi_exc_handlers.request_validation_exception_handler = _request_validation_exception_handler  # type: ignore[attr-defined]
    fastapi_exc_handlers.websocket_request_validation_exception_handler = _websocket_request_validation_exception_handler  # type: ignore[attr-defined]
    modules["fastapi.exception_handlers"] = fastapi_exc_handlers

    # ── fastapi.dependencies ───────────────────────────────────────
    # FastAPI's `fastapi.dependencies.utils` is private implementation
    # detail (Dependant/solve_dependencies/get_dependant/…) that we
    # don't use — fastapi-turbo compiles resolution plans in Rust. Code
    # that reaches in here is usually introspecting FA's private
    # resolver. We provide importable no-op shims so the import path
    # exists; any function call raises ``NotImplementedError`` with a
    # clear pointer so the failure is loud (not silent, not a confusing
    # AttributeError on a Mock). Users who hit this should switch to
    # the public API (``Depends(...)``, ``request.scope["route"]``).
    fastapi_dependencies_mod = _mod("fastapi.dependencies")
    modules["fastapi.dependencies"] = fastapi_dependencies_mod

    fastapi_dependencies_utils = _mod("fastapi.dependencies.utils")

    class Dependant:
        """Import-time stub of ``fastapi.dependencies.utils.Dependant``.

        fastapi-turbo does not use FA's ``Dependant`` tree — dep
        resolution happens in Rust. Attribute access returns sentinels
        so introspecting code runs without exploding; calling into
        methods raises ``NotImplementedError`` pointing at public API.
        """
        def __init__(self, **kwargs):
            # Populate the minimum set of attributes FA tests look at
            # so ``isinstance(dep, Dependant)`` + ``dep.dependencies``
            # don't crash when a third-party library pokes at this.
            self.dependencies = kwargs.pop("dependencies", [])
            self.path_params = kwargs.pop("path_params", [])
            self.query_params = kwargs.pop("query_params", [])
            self.header_params = kwargs.pop("header_params", [])
            self.cookie_params = kwargs.pop("cookie_params", [])
            self.body_params = kwargs.pop("body_params", [])
            self.call = kwargs.pop("call", None)
            self.name = kwargs.pop("name", None)
            self.path = kwargs.pop("path", None)
            self.use_cache = kwargs.pop("use_cache", True)
            for k, v in kwargs.items():
                setattr(self, k, v)

    def _fa_internal_stub(name: str):
        """Build a callable that raises with a helpful pointer."""
        def _raise(*args, **kwargs):
            raise NotImplementedError(
                f"fastapi-turbo does not implement FastAPI's private "
                f"`{name}` helper — dependency resolution is compiled "
                f"in Rust. Use the public `Depends(...)` / "
                f"`@app.exception_handler(...)` API instead."
            )
        _raise.__name__ = name.rsplit(".", 1)[-1]
        return _raise

    fastapi_dependencies_utils.Dependant = Dependant  # type: ignore[attr-defined]
    fastapi_dependencies_utils.get_dependant = _fa_internal_stub("fastapi.dependencies.utils.get_dependant")  # type: ignore[attr-defined]
    fastapi_dependencies_utils.solve_dependencies = _fa_internal_stub("fastapi.dependencies.utils.solve_dependencies")  # type: ignore[attr-defined]
    def _get_typed_annotation(annotation, globalns):
        # Real FA helper: resolve a string forward-ref against a namespace.
        # ``"None"`` resolves to the singleton ``None`` per the test's
        # coverage assertion; everything else is eval'd normally.
        if annotation == "None":
            return None
        import typing as _t
        if isinstance(annotation, str):
            try:
                return _t._eval_type(  # type: ignore[attr-defined]
                    _t.ForwardRef(annotation), globalns, globalns,
                )
            except Exception:
                return annotation
        return annotation
    fastapi_dependencies_utils.get_typed_annotation = _get_typed_annotation  # type: ignore[attr-defined]
    fastapi_dependencies_utils.get_typed_signature = _fa_internal_stub("fastapi.dependencies.utils.get_typed_signature")  # type: ignore[attr-defined]
    fastapi_dependencies_utils.get_flat_dependant = _fa_internal_stub("fastapi.dependencies.utils.get_flat_dependant")  # type: ignore[attr-defined]
    fastapi_dependencies_utils.get_parameterless_sub_dependant = _fa_internal_stub("fastapi.dependencies.utils.get_parameterless_sub_dependant")  # type: ignore[attr-defined]
    fastapi_dependencies_utils.request_body_to_args = _fa_internal_stub("fastapi.dependencies.utils.request_body_to_args")  # type: ignore[attr-defined]
    fastapi_dependencies_utils.request_params_to_args = _fa_internal_stub("fastapi.dependencies.utils.request_params_to_args")  # type: ignore[attr-defined]
    modules["fastapi.dependencies.utils"] = fastapi_dependencies_utils

    fastapi_dependencies_models = _mod("fastapi.dependencies.models")
    fastapi_dependencies_models.Dependant = Dependant  # type: ignore[attr-defined]
    modules["fastapi.dependencies.models"] = fastapi_dependencies_models

    # ── fastapi.types ──────────────────────────────────────────────
    import typing as _typing
    fastapi_types = _mod("fastapi.types")
    fastapi_types.DecoratedCallable = _typing.TypeVar("DecoratedCallable", bound=_typing.Callable)  # type: ignore[attr-defined]
    fastapi_types.IncEx = _typing.Union[_typing.Set[int], _typing.Set[str], _typing.Dict[int, _typing.Any], _typing.Dict[str, _typing.Any], None]  # type: ignore[attr-defined]
    modules["fastapi.types"] = fastapi_types

    # ── fastapi.utils ──────────────────────────────────────────────
    fastapi_utils = _mod("fastapi.utils")
    def _generate_unique_id(route):
        name = getattr(route, "name", None) or getattr(route.endpoint, "__name__", "unknown")
        methods = getattr(route, "methods", None) or ["GET"]
        method = next(iter(methods))
        path = getattr(route, "path", "/")
        return f"{name}{path}{method}"
    fastapi_utils.generate_unique_id = _generate_unique_id  # type: ignore[attr-defined]
    modules["fastapi.utils"] = fastapi_utils

    # ── fastapi._compat ───────────────────────────────────────────
    # Third-party plugins (fastapi-jsonrpc, etc.) import from here
    try:
        import fastapi_turbo._compat_shim as _compat_mod
        modules["fastapi._compat"] = _compat_mod
        # Expose as a package so ``from fastapi._compat import shared``
        # works — needed by FA 0.115+'s own tests.
        _compat_mod.__path__ = []  # type: ignore[attr-defined]
        _compat_mod.__package__ = "fastapi._compat"  # type: ignore[attr-defined]
        # Sub-module ``fastapi._compat.shared`` re-exports the same
        # annotation helpers at a different path.
        fastapi_compat_shared = _mod("fastapi._compat.shared")
        for _name in (
            "is_uploadfile_or_nonable_uploadfile_annotation",
            "is_uploadfile_sequence_annotation",
            "is_bytes_or_nonable_bytes_annotation",
            "is_bytes_sequence_annotation",
            "is_sequence_field",
            "sequence_types",
            "value_is_sequence",
            "serialize_sequence_value",
        ):
            if hasattr(_compat_mod, _name):
                setattr(fastapi_compat_shared, _name, getattr(_compat_mod, _name))
        modules["fastapi._compat.shared"] = fastapi_compat_shared
        # Sub-module ``fastapi._compat.v2`` — same surface, different path.
        fastapi_compat_v2 = _mod("fastapi._compat.v2")
        fastapi_compat_v2.get_missing_field_error = _fa_internal_stub(  # type: ignore[attr-defined]
            "fastapi._compat.v2.get_missing_field_error"
        )
        # Minimal ``ModelField`` stub — tests in FA's suite construct
        # ``v2.ModelField(name="foo", field_info=field_info)`` and assert
        # ``.default`` tracks pydantic's ``PydanticUndefined``.
        from fastapi_turbo._compat_shim import Undefined as _Undef

        class _ModelField:
            def __init__(self, name, field_info=None, **kwargs):
                self.name = name
                self.field_info = field_info
                self.mode = kwargs.get("mode", "validation")

            @property
            def default(self):
                fi = self.field_info
                if fi is None:
                    return _Undef
                return getattr(fi, "default", _Undef)

            @property
            def required(self) -> bool:
                return self.default is _Undef

            @property
            def alias(self):
                fi = self.field_info
                return getattr(fi, "alias", None) if fi is not None else None

            @property
            def field_info_metadata(self):
                return self.field_info
        fastapi_compat_v2.ModelField = _ModelField  # type: ignore[attr-defined]
        modules["fastapi._compat.v2"] = fastapi_compat_v2
        # Expose submodules as attributes on the package so
        # ``from fastapi._compat import v2`` / ``import shared`` resolve.
        _compat_mod.shared = fastapi_compat_shared  # type: ignore[attr-defined]
        _compat_mod.v2 = fastapi_compat_v2  # type: ignore[attr-defined]
        # Re-export all _compat_shim symbols on the v2 submodule too —
        # ``from fastapi._compat.v2 import serialize_sequence_value`` etc.
        for _name in (
            "is_uploadfile_or_nonable_uploadfile_annotation",
            "is_uploadfile_sequence_annotation",
            "is_bytes_or_nonable_bytes_annotation",
            "is_bytes_sequence_annotation",
            "is_sequence_field",
            "sequence_types",
            "value_is_sequence",
            "serialize_sequence_value",
        ):
            if hasattr(_compat_mod, _name):
                setattr(fastapi_compat_v2, _name, getattr(_compat_mod, _name))
    except ImportError:
        pass

    # ── fastapi.cli ───────────────────────────────────────────────
    # FA ships a CLI (``fastapi dev``) — we don't, but the import
    # path must exist so `fastapi.cli` doesn't blow up. Import fails
    # loudly if someone tries to USE the CLI.
    fastapi_cli = _mod("fastapi.cli")

    # ``fastapi_cli.cli_main`` is the optional-install entry point. Do
    # NOT import ``fastapi_cli.cli`` eagerly — it transitively imports
    # ``fastapi_cloud_cli`` which pulls in ``sentry_sdk`` at module
    # load time, breaking third-party tests that assert ``sentry_sdk``
    # is pristine at startup (e.g. sentry-python's own test suite).
    # Defer to first-access via a module ``__getattr__``.
    _fastapi_cli_cache: dict = {}

    def _fastapi_cli_module_getattr(name: str):
        if name == "cli_main":
            if "cli_main" not in _fastapi_cli_cache:
                try:
                    from fastapi_cli.cli import app as _cli_main
                except Exception:  # noqa: BLE001
                    _cli_main = None
                _fastapi_cli_cache["cli_main"] = _cli_main
            return _fastapi_cli_cache["cli_main"]
        raise AttributeError(name)

    fastapi_cli.__getattr__ = _fastapi_cli_module_getattr  # type: ignore[attr-defined]

    def _cli_main_fn():
        """Real FA: invoke ``fastapi_cli`` if installed, else raise a
        RuntimeError directing the user to ``pip install fastapi[standard]``.
        Matches FA 0.120+'s ``fastapi.cli.main`` semantics exactly.
        """
        if getattr(fastapi_cli, "cli_main", None) is None:
            raise RuntimeError(
                'To use the fastapi command, please install '
                '"fastapi[standard]":\n\n\tpip install "fastapi[standard]"\n'
            )
        fastapi_cli.cli_main()
    fastapi_cli.main = _cli_main_fn  # type: ignore[attr-defined]
    modules["fastapi.cli"] = fastapi_cli

    # ── Backfill a couple more FA-internal stubs ──────────────────
    # FA's multipart-install error messages — tests import these to
    # match against ``pytest.raises(RuntimeError, match=...)``. We ship
    # the literal strings (not stubs) so the import + match both work.
    fastapi_dependencies_utils.multipart_not_installed_error = (  # type: ignore[attr-defined]
        'Form data requires "python-multipart" to be installed. \n'
        'You can install "python-multipart" with: \n\n'
        "pip install python-multipart\n"
    )
    fastapi_dependencies_utils.multipart_incorrect_install_error = (  # type: ignore[attr-defined]
        'Form data requires "python-multipart" to be installed. '
        'It seems you installed "multipart" instead. \n'
        'You can remove "multipart" with: \n\n'
        "pip uninstall multipart\n\n"
        'And then install "python-multipart" with: \n\n'
        "pip install python-multipart\n"
    )
    # FA's ensure_multipart_is_installed raises at decoration time
    # when Form/File is used without python-multipart. Port the logic:
    # checks for an incompatible ``multipart`` package first.
    def _ensure_multipart_is_installed() -> None:
        # Port of FA's ``ensure_multipart_is_installed``. Tests
        # monkeypatch ``python_multipart.__version__`` to ``"0.0.12"``
        # (or delete attrs on the legacy ``multipart`` package) and
        # expect a ``RuntimeError`` at route decoration time — mirror
        # that exact check sequence.
        try:
            from python_multipart import __version__
            assert __version__ > "0.0.12"
        except (ImportError, AssertionError):
            try:
                from multipart import __version__  # type: ignore[import-untyped]
                assert __version__
                try:
                    from multipart.multipart import parse_options_header  # type: ignore[import-untyped]  # noqa: F401
                    assert parse_options_header
                except (ImportError, AttributeError):
                    raise RuntimeError(
                        fastapi_dependencies_utils.multipart_incorrect_install_error
                    ) from None
            except ImportError:
                raise RuntimeError(
                    fastapi_dependencies_utils.multipart_not_installed_error
                ) from None
    fastapi_dependencies_utils.ensure_multipart_is_installed = _ensure_multipart_is_installed  # type: ignore[attr-defined]

    # ── starlette.routing.compile_path ────────────────────────────
    # Used by fastapi-jsonrpc and Netflix dispatch
    # Already in starlette shim if available

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
    fastapi.cli = fastapi_cli  # type: ignore[attr-defined]
    fastapi.websockets = fastapi_websockets  # type: ignore[attr-defined]
    fastapi.sse = fastapi_sse  # type: ignore[attr-defined]
    fastapi.middleware = fastapi_middleware  # type: ignore[attr-defined]

    return modules


MODULES = _build()
