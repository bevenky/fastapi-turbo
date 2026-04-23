"""Verify all new features work with EXACT FastAPI syntax.

This test ensures that a user with existing FastAPI code can drop in
fastapi-turbo without any syntax changes.
"""

from __future__ import annotations

import pytest


# ── Response.set_cookie: positional-or-keyword (Starlette-compatible) ──


class TestStarletteCookieSignature:
    def test_positional_max_age(self):
        """Starlette users can call set_cookie positionally."""
        from fastapi_turbo.responses import Response

        r = Response()
        # Old Starlette call style: positional args
        r.set_cookie("sessionid", "abc123", 3600)
        value = r.raw_headers[0][1]
        assert "sessionid=abc123" in value
        assert "Max-Age=3600" in value

    def test_positional_all_args(self):
        """All positional args — matches Starlette."""
        from fastapi_turbo.responses import Response

        r = Response()
        r.set_cookie("k", "v", 3600, None, "/api", "example.com", True, True, "strict")
        value = r.raw_headers[0][1]
        assert "k=v" in value
        assert "Max-Age=3600" in value
        assert "Path=/api" in value
        assert "Domain=example.com" in value
        assert "Secure" in value
        assert "HttpOnly" in value
        assert "SameSite=strict" in value  # Starlette lowercases samesite

    def test_delete_cookie_positional(self):
        from fastapi_turbo.responses import Response

        r = Response()
        r.delete_cookie("k", "/", "example.com", True)
        value = r.raw_headers[0][1]
        assert "Path=/" in value
        assert "Domain=example.com" in value
        assert "Secure" in value
        assert "Max-Age=0" in value

    def test_partitioned_cookie(self):
        from fastapi_turbo.responses import Response

        r = Response()
        r.set_cookie("k", "v", partitioned=True)
        value = r.raw_headers[0][1]
        assert "Partitioned" in value


# ── Security() dependency (FastAPI-compatible) ────────────────────


class TestSecurity:
    def test_security_imports_from_fastapi_turbo(self):
        """from fastapi_turbo import Security — must work."""
        from fastapi_turbo import Security

        assert Security is not None

    def test_security_is_depends_subclass(self):
        from fastapi_turbo import Depends, Security

        s = Security(lambda: None, scopes=["me"])
        assert isinstance(s, Depends)
        assert s.scopes == ["me"]

    def test_security_in_endpoint(self):
        """FastAPI-compatible: `token: str = Security(scheme, scopes=[...])`"""
        from fastapi_turbo import FastAPI, Security
        from fastapi_turbo.security import OAuth2PasswordBearer

        oauth2 = OAuth2PasswordBearer(tokenUrl="/token")
        app = FastAPI()

        @app.get("/me")
        async def me(token: str = Security(oauth2, scopes=["me"])):
            return {"token": token}

        # Verify the route is registered
        routes = app._collect_all_routes()
        assert len(routes) == 1


# ── APIRoute.deprecated type (bool | None) ─────────────────────────


class TestDeprecatedNone:
    def test_deprecated_none_means_not_deprecated(self):
        """FastAPI-compatible: deprecated defaults to None (meaning: inherit/not deprecated)."""
        from fastapi_turbo import FastAPI

        app = FastAPI()

        @app.get("/a")  # no deprecated kwarg
        def a():
            return {}

        @app.get("/b", deprecated=None)
        def b():
            return {}

        @app.get("/c", deprecated=True)
        def c():
            return {}

        routes = app._collect_all_routes()
        assert routes[0]["deprecated"] is False
        assert routes[1]["deprecated"] is False
        assert routes[2]["deprecated"] is True


# ── Body.embed default is None (FastAPI-compat) ────────────────────


class TestBodyEmbedDefault:
    def test_body_embed_defaults_to_none(self):
        """FastAPI defaults Body.embed to None (means auto-detect)."""
        from fastapi_turbo.param_functions import Body

        b = Body()
        assert b.embed is None

    def test_body_embed_true_still_works(self):
        from fastapi_turbo.param_functions import Body

        b = Body(embed=True)
        assert b.embed is True


# ── url_path_for returns URLPath (str subclass with make_absolute_url) ──


class TestUrlPathFor:
    def test_returns_urlpath(self):
        from fastapi_turbo import FastAPI
        from fastapi_turbo.applications import URLPath

        app = FastAPI()

        @app.get("/users/{user_id}")
        def get_user(user_id: int):
            return {}

        url = app.url_path_for("get_user", user_id=42)
        assert isinstance(url, URLPath)
        assert isinstance(url, str)
        assert url == "/users/42"

    def test_make_absolute_url(self):
        """Starlette URLPath.make_absolute_url should work."""
        from fastapi_turbo import FastAPI

        app = FastAPI()

        @app.get("/users/{user_id}")
        def get_user(user_id: int):
            return {}

        url = app.url_path_for("get_user", user_id=1)
        abs_url = url.make_absolute_url("http://example.com")
        assert abs_url == "http://example.com/users/1"


# ── FastAPI.__init__: exception_handlers kwarg accepted ────────────


class TestFastAPIInit:
    def test_exception_handlers_kwarg(self):
        """FastAPI(exception_handlers={...}) — standard init kwarg."""
        from fastapi_turbo import FastAPI, HTTPException

        def handle(request, exc):
            return {}

        app = FastAPI(exception_handlers={HTTPException: handle})
        assert HTTPException in app.exception_handlers

    def test_root_path_kwarg(self):
        from fastapi_turbo import FastAPI

        app = FastAPI(root_path="/api/v1")
        assert app.root_path == "/api/v1"
        assert app.root_path_in_servers is True


# ── Exception handler signature matches FastAPI ────────────────────


class TestExceptionHandlerSignature:
    def test_register_for_status_code(self):
        """FastAPI allows @app.exception_handler(404)."""
        from fastapi_turbo import FastAPI

        app = FastAPI()

        @app.exception_handler(404)
        def h(request, exc):
            return {}

        assert 404 in app.exception_handlers

    def test_register_for_exception_class(self):
        from fastapi_turbo import FastAPI, HTTPException

        app = FastAPI()

        @app.exception_handler(HTTPException)
        def h(request, exc):
            return {}

        assert HTTPException in app.exception_handlers

    def test_add_exception_handler_imperative(self):
        """Starlette-style: app.add_exception_handler(...)"""
        from fastapi_turbo import FastAPI, HTTPException

        def h(req, exc):
            return {}

        app = FastAPI()
        app.add_exception_handler(HTTPException, h)
        assert app.exception_handlers[HTTPException] is h


# ── Middleware decorator signature matches FastAPI ─────────────────


class TestMiddlewareSignature:
    def test_http_type_accepted(self):
        from fastapi_turbo import FastAPI

        app = FastAPI()

        @app.middleware("http")
        async def m(request, call_next):
            return await call_next(request)

        assert len(app._http_middlewares) == 1

    def test_non_http_raises(self):
        from fastapi_turbo import FastAPI

        app = FastAPI()
        with pytest.raises(ValueError):
            @app.middleware("websocket")
            async def m(r, cn):
                pass


# ── APIRoute kwargs — all must be keyword-only ────────────────────


class TestAPIRouteKwargs:
    def test_all_new_kwargs_accepted(self):
        """All 15 new features can be passed as keyword args in standard syntax."""
        from fastapi_turbo import APIRouter, FastAPI
        from fastapi_turbo.responses import HTMLResponse

        app = FastAPI()
        cb_router = APIRouter()

        @cb_router.post("/cb")
        def cb():
            return {}

        @app.get(
            "/complex",
            response_description="custom",
            responses={404: {"description": "NF"}},
            response_class=HTMLResponse,
            include_in_schema=False,
            openapi_extra={"x-foo": 1},
            security=[{"BearerAuth": []}],
            callbacks=[cb_router],
            deprecated=True,
            operation_id="custom_op",
            tags=["x"],
        )
        def h():
            return "<h1>hi</h1>"

        # If the above parsed, all kwargs are accepted
        assert True


# ── Body media_type FastAPI-compat ─────────────────────────────────


class TestBodyMediaType:
    def test_body_media_type_kwarg(self):
        from typing import Annotated

        from fastapi_turbo import FastAPI, Body

        app = FastAPI()

        @app.post("/upload")
        def h(data: Annotated[bytes, Body(media_type="application/octet-stream")] = b""):
            return {}

        # Route registration shouldn't fail
        routes = app._collect_all_routes()
        assert len(routes) == 1


# ── Ensure imports users might make all work ───────────────────────


class TestAllImports:
    def test_standard_fastapi_imports(self):
        """The complete FastAPI import surface."""
        # Core
        from fastapi_turbo import (
            APIRouter,
            BackgroundTasks,
            Body,
            Cookie,
            Depends,
            FastAPI,
            File,
            Form,
            Header,
            HTTPException,
            Path,
            Query,
            Request,
            Response,
            Security,
            UploadFile,
            WebSocket,
            status,
        )
        # Responses
        from fastapi_turbo.responses import (
            FileResponse,
            HTMLResponse,
            JSONResponse,
            ORJSONResponse,
            PlainTextResponse,
            RedirectResponse,
            Response as Resp,
            StreamingResponse,
            UJSONResponse,
        )
        # Security
        from fastapi_turbo.security import (
            APIKeyCookie,
            APIKeyHeader,
            APIKeyQuery,
            HTTPAuthorizationCredentials,
            HTTPBasic,
            HTTPBasicCredentials,
            HTTPBearer,
            HTTPDigest,
            OAuth2PasswordBearer,
            OAuth2PasswordRequestForm,
            SecurityScopes,
        )
        # Exceptions
        from fastapi_turbo.exceptions import (
            HTTPException as Exc,
            RequestValidationError,
            WebSocketDisconnect,
            WebSocketException,
        )
        # Encoders
        from fastapi_turbo.encoders import jsonable_encoder
        # Middleware
        from fastapi_turbo.middleware.cors import CORSMiddleware
        from fastapi_turbo.middleware.gzip import GZipMiddleware
        from fastapi_turbo.middleware.trustedhost import TrustedHostMiddleware

        assert FastAPI is not None
        assert Security is not None
        assert HTTPDigest is not None
