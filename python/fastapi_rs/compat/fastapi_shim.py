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

    # WebSocketDisconnect — commonly imported from top level
    fastapi.WebSocketDisconnect = _exceptions.WebSocketDisconnect  # type: ignore[attr-defined]

    # Security (commonly imported from top-level too)
    fastapi.Security = _dependencies.Security  # type: ignore[attr-defined]

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
    import fastapi_rs.sse as _sse
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
    # APIWebSocketRoute stub — fastapi-rs registers WS routes directly
    # via @app.websocket() rather than a dedicated class, but third-party
    # code uses this class name for isinstance checks.
    class APIWebSocketRoute:
        def __init__(self, path: str, endpoint, *, name: str | None = None):
            self.path = path
            self.endpoint = endpoint
            self.name = name or endpoint.__name__
    fastapi_routing.APIWebSocketRoute = APIWebSocketRoute  # type: ignore[attr-defined]
    modules["fastapi.routing"] = fastapi_routing

    # ── fastapi.exceptions ─────────────────────────────────────────
    fastapi_exceptions = _mod("fastapi.exceptions")
    fastapi_exceptions.HTTPException = _exceptions.HTTPException  # type: ignore[attr-defined]
    fastapi_exceptions.RequestValidationError = _exceptions.RequestValidationError  # type: ignore[attr-defined]
    fastapi_exceptions.WebSocketException = _exceptions.WebSocketException  # type: ignore[attr-defined]
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
    class Param:
        def __init__(self, *, default=..., **kwargs):
            self.default = default
            for k, v in kwargs.items():
                setattr(self, k, v)
    fastapi_params.Param = Param  # type: ignore[attr-defined]
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
    from fastapi_rs.testclient import TestClient
    fastapi_testclient.TestClient = TestClient  # type: ignore[attr-defined]
    modules["fastapi.testclient"] = fastapi_testclient

    # ── fastapi.requests ──────────────────────────────────────────
    fastapi_requests_mod = _mod("fastapi.requests")
    fastapi_requests_mod.Request = _requests.Request  # type: ignore[attr-defined]
    modules["fastapi.requests"] = fastapi_requests_mod

    # ── fastapi.websockets ─────────────────────────────────────────
    fastapi_websockets = _mod("fastapi.websockets")
    fastapi_websockets.WebSocket = _websockets.WebSocket  # type: ignore[attr-defined]
    fastapi_websockets.WebSocketDisconnect = _exceptions.WebSocketDisconnect  # type: ignore[attr-defined]
    fastapi_websockets.WebSocketState = _websockets.WebSocketState  # type: ignore[attr-defined]
    modules["fastapi.websockets"] = fastapi_websockets

    # ── fastapi.middleware ─────────────────────────────────────────
    import fastapi_rs._starlette_compat as _sc
    fastapi_middleware = _mod("fastapi.middleware")
    fastapi_middleware.Middleware = _sc.Middleware  # type: ignore[attr-defined]
    modules["fastapi.middleware"] = fastapi_middleware

    # ── fastapi.middleware.wsgi ────────────────────────────────────
    fastapi_middleware_wsgi = _mod("fastapi.middleware.wsgi")
    fastapi_middleware_wsgi.WSGIMiddleware = _sc.WSGIMiddleware  # type: ignore[attr-defined]
    modules["fastapi.middleware.wsgi"] = fastapi_middleware_wsgi

    # ── fastapi.staticfiles ────────────────────────────────────────
    import fastapi_rs.staticfiles as _staticfiles
    fastapi_staticfiles = _mod("fastapi.staticfiles")
    fastapi_staticfiles.StaticFiles = _staticfiles.StaticFiles  # type: ignore[attr-defined]
    modules["fastapi.staticfiles"] = fastapi_staticfiles

    # ── fastapi.templating ─────────────────────────────────────────
    import fastapi_rs.templating as _templating
    fastapi_templating = _mod("fastapi.templating")
    fastapi_templating.Jinja2Templates = _templating.Jinja2Templates  # type: ignore[attr-defined]
    modules["fastapi.templating"] = fastapi_templating

    # ── fastapi.logger ─────────────────────────────────────────────
    import logging
    fastapi_logger = _mod("fastapi.logger")
    fastapi_logger.logger = logging.getLogger("fastapi")  # type: ignore[attr-defined]
    modules["fastapi.logger"] = fastapi_logger

    # ── fastapi.openapi.* ──────────────────────────────────────────
    import fastapi_rs._openapi as _oa
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

    def get_swagger_ui_html(
        *,
        openapi_url,
        title="API docs",
        swagger_js_url=None,
        swagger_css_url=None,
        swagger_favicon_url=None,
        oauth2_redirect_url=None,
        init_oauth=None,
        swagger_ui_parameters=None,
    ):
        js_url = swagger_js_url or "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
        css_url = swagger_css_url or "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"
        favicon = ""
        if swagger_favicon_url:
            favicon = f'<link rel="icon" href="{swagger_favicon_url}">'
        html = (
            f"<!DOCTYPE html><html><head><title>{title}</title>\n"
            f'<link rel="stylesheet" href="{css_url}">\n'
            f"{favicon}</head><body>\n"
            f'<div id="swagger-ui"></div>\n'
            f'<script src="{js_url}"></script>\n'
            f'<script>SwaggerUIBundle({{url:"{openapi_url}",dom_id:"#swagger-ui"}})</script>\n'
            f"</body></html>"
        )
        from fastapi_rs.responses import HTMLResponse
        return HTMLResponse(html)

    def _stub_redoc_html(*, openapi_url, title="API", **_):
        return f"<!DOCTYPE html><html><body><redoc spec-url='{openapi_url}'></redoc></body></html>"

    def get_swagger_ui_oauth2_redirect_html():
        from fastapi_rs.responses import HTMLResponse
        return HTMLResponse(
            '<!doctype html><html><body>'
            '<script>window.onload=function(){'
            'var qp=window.location.hash?window.location.hash.substring(1):window.location.search.substring(1);'
            'var data={};qp.split("&").forEach(function(p){var kv=p.split("=");data[kv[0]]=decodeURIComponent(kv[1]||"");});'
            'window.opener.swaggerUIRedirectOauth2(data);window.close();'
            '}</script></body></html>'
        )

    fastapi_openapi_docs.get_swagger_ui_html = get_swagger_ui_html  # type: ignore[attr-defined]
    fastapi_openapi_docs.get_redoc_html = _stub_redoc_html  # type: ignore[attr-defined]
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
        "Reference", "Discriminator", "XML", "Schema", "Example",
        "Link", "Header", "Tag", "Components", "SecurityScheme",
        "OAuthFlow", "OAuthFlows", "SecurityBase", "Callback", "Webhook",
    ]
    for _oai_name in _openapi_model_names:
        setattr(fastapi_openapi_models, _oai_name, type(_oai_name, (dict,), {}))
    modules["fastapi.openapi.models"] = fastapi_openapi_models

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

    # ── fastapi.exception_handlers ───────────────────────────────────
    fastapi_exc_handlers = _mod("fastapi.exception_handlers")
    async def _http_exception_handler(request, exc):
        from fastapi_rs.responses import JSONResponse, Response
        status_code = exc.status_code
        # 1xx, 204, 304 cannot have body
        if status_code < 200 or status_code in (204, 304):
            return Response(status_code=status_code, headers=exc.headers)
        return JSONResponse({"detail": exc.detail}, status_code=status_code, headers=exc.headers)
    async def _request_validation_exception_handler(request, exc):
        from fastapi_rs.responses import JSONResponse
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    async def _websocket_request_validation_exception_handler(websocket, exc):
        await websocket.close(code=1008)
    fastapi_exc_handlers.http_exception_handler = _http_exception_handler  # type: ignore[attr-defined]
    fastapi_exc_handlers.request_validation_exception_handler = _request_validation_exception_handler  # type: ignore[attr-defined]
    fastapi_exc_handlers.websocket_request_validation_exception_handler = _websocket_request_validation_exception_handler  # type: ignore[attr-defined]
    modules["fastapi.exception_handlers"] = fastapi_exc_handlers

    # ── fastapi.dependencies ───────────────────────────────────────
    # Stub module — real FastAPI has fastapi.dependencies.utils with Dependant
    fastapi_dependencies_mod = _mod("fastapi.dependencies")
    modules["fastapi.dependencies"] = fastapi_dependencies_mod

    fastapi_dependencies_utils = _mod("fastapi.dependencies.utils")
    class Dependant:
        """Stub for fastapi.dependencies.utils.Dependant."""
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    fastapi_dependencies_utils.Dependant = Dependant  # type: ignore[attr-defined]
    fastapi_dependencies_utils.get_dependant = lambda **kw: Dependant(**kw)  # type: ignore[attr-defined]
    fastapi_dependencies_utils.solve_dependencies = lambda **kw: {}  # type: ignore[attr-defined]
    modules["fastapi.dependencies.utils"] = fastapi_dependencies_utils

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
    fastapi.sse = fastapi_sse  # type: ignore[attr-defined]
    fastapi.middleware = fastapi_middleware  # type: ignore[attr-defined]

    return modules


MODULES = _build()
