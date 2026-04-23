"""Tests for P2 (nice-to-have) feature gaps."""

import asyncio
import json

import pytest


# ===========================================================================
# P2 #1: response_model_include / response_model_exclude
# ===========================================================================


class TestResponseModelIncludeExclude:
    """Tests for response_model_include and response_model_exclude."""

    def test_route_stores_include_exclude(self):
        """APIRoute stores response_model_include and response_model_exclude."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        route = APIRoute(
            "/test",
            handler,
            response_model_include={"name"},
            response_model_exclude={"secret"},
        )
        assert route.response_model_include == {"name"}
        assert route.response_model_exclude == {"secret"}

    def test_route_defaults_none(self):
        """response_model_include/exclude default to None."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        route = APIRoute("/test", handler)
        assert route.response_model_include is None
        assert route.response_model_exclude is None

    def test_apply_response_model_include(self):
        """_apply_response_model with include only returns included fields."""
        from pydantic import BaseModel
        from fastapi_rs.applications import _apply_response_model

        class UserOut(BaseModel):
            name: str
            email: str
            age: int = 0

        result = _apply_response_model(
            {"name": "Alice", "email": "a@b.com", "age": 30},
            UserOut,
            include={"name"},
        )
        assert result == {"name": "Alice"}

    def test_apply_response_model_exclude(self):
        """_apply_response_model with exclude omits excluded fields."""
        from pydantic import BaseModel
        from fastapi_rs.applications import _apply_response_model

        class UserOut(BaseModel):
            name: str
            email: str
            age: int = 0

        result = _apply_response_model(
            {"name": "Alice", "email": "a@b.com", "age": 30},
            UserOut,
            exclude={"email"},
        )
        assert "name" in result
        assert "age" in result
        assert "email" not in result

    def test_include_exclude_via_decorator(self):
        """response_model_include/exclude work through the FastAPI decorator."""
        from pydantic import BaseModel
        from fastapi_rs import FastAPI

        class UserOut(BaseModel):
            name: str
            email: str
            age: int = 0

        app = FastAPI()

        @app.get("/user", response_model=UserOut, response_model_include={"name"})
        def get_user():
            return {"name": "Alice", "email": "a@b.com", "age": 30}

        route = app.router.routes[0]
        assert route.response_model_include == {"name"}


# ===========================================================================
# P2 #2: response_model_exclude_unset / exclude_defaults / exclude_none
# ===========================================================================


class TestResponseModelExcludeOptions:
    """Tests for exclude_unset, exclude_defaults, exclude_none."""

    def test_route_stores_exclude_flags(self):
        """APIRoute stores the exclude_unset/defaults/none flags."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        route = APIRoute(
            "/test",
            handler,
            response_model_exclude_unset=True,
            response_model_exclude_defaults=True,
            response_model_exclude_none=True,
        )
        assert route.response_model_exclude_unset is True
        assert route.response_model_exclude_defaults is True
        assert route.response_model_exclude_none is True

    def test_route_defaults_false(self):
        """exclude_unset/defaults/none default to False."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        route = APIRoute("/test", handler)
        assert route.response_model_exclude_unset is False
        assert route.response_model_exclude_defaults is False
        assert route.response_model_exclude_none is False

    def test_apply_response_model_exclude_unset(self):
        """_apply_response_model with exclude_unset omits unset fields."""
        from pydantic import BaseModel
        from fastapi_rs.applications import _apply_response_model

        class ItemOut(BaseModel):
            name: str
            description: str | None = None
            price: float = 0.0

        result = _apply_response_model(
            {"name": "Widget"},
            ItemOut,
            exclude_unset=True,
        )
        assert result == {"name": "Widget"}

    def test_apply_response_model_exclude_defaults(self):
        """_apply_response_model with exclude_defaults omits default-valued fields."""
        from pydantic import BaseModel
        from fastapi_rs.applications import _apply_response_model

        class ItemOut(BaseModel):
            name: str
            price: float = 0.0

        result = _apply_response_model(
            {"name": "Widget", "price": 0.0},
            ItemOut,
            exclude_defaults=True,
        )
        assert result == {"name": "Widget"}

    def test_apply_response_model_exclude_none(self):
        """_apply_response_model with exclude_none omits None-valued fields."""
        from pydantic import BaseModel
        from fastapi_rs.applications import _apply_response_model

        class ItemOut(BaseModel):
            name: str
            description: str | None = None

        result = _apply_response_model(
            {"name": "Widget", "description": None},
            ItemOut,
            exclude_none=True,
        )
        assert result == {"name": "Widget"}


# ===========================================================================
# P2 #3: ORJSONResponse / UJSONResponse
# ===========================================================================


class TestORJSONResponse:
    """Tests for ORJSONResponse."""

    def test_import(self):
        """ORJSONResponse is importable from responses module."""
        from fastapi_rs.responses import ORJSONResponse
        assert ORJSONResponse is not None

    def test_import_from_top_level(self):
        """ORJSONResponse is importable from fastapi_rs."""
        from fastapi_rs import ORJSONResponse
        assert ORJSONResponse is not None

    def test_renders_json(self):
        """ORJSONResponse renders content as JSON bytes."""
        from fastapi_rs.responses import ORJSONResponse
        from fastapi_rs.exceptions import FastAPIDeprecationWarning
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore", FastAPIDeprecationWarning)
            r = ORJSONResponse(content={"hello": "world"})
        data = json.loads(r.body)
        assert data == {"hello": "world"}

    def test_media_type(self):
        """ORJSONResponse has application/json media type."""
        from fastapi_rs.responses import ORJSONResponse
        assert ORJSONResponse.media_type == "application/json"

    def test_status_code(self):
        """ORJSONResponse accepts custom status code."""
        from fastapi_rs.responses import ORJSONResponse
        from fastapi_rs.exceptions import FastAPIDeprecationWarning
        import warnings as _w
        with _w.catch_warnings():
            _w.simplefilter("ignore", FastAPIDeprecationWarning)
            r = ORJSONResponse(content={"ok": True}, status_code=201)
        assert r.status_code == 201


class TestUJSONResponse:
    """Tests for UJSONResponse."""

    def test_import(self):
        """UJSONResponse is importable from responses module."""
        from fastapi_rs.responses import UJSONResponse
        assert UJSONResponse is not None

    def test_import_from_top_level(self):
        """UJSONResponse is importable from fastapi_rs."""
        from fastapi_rs import UJSONResponse
        assert UJSONResponse is not None

    def test_renders_json(self):
        """UJSONResponse renders content as JSON bytes."""
        import warnings
        from fastapi_rs.exceptions import FastAPIDeprecationWarning
        from fastapi_rs.responses import UJSONResponse
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FastAPIDeprecationWarning)
            r = UJSONResponse(content={"hello": "world"})
        data = json.loads(r.body)
        assert data == {"hello": "world"}

    def test_media_type(self):
        """UJSONResponse has application/json media type."""
        from fastapi_rs.responses import UJSONResponse
        assert UJSONResponse.media_type == "application/json"


# ===========================================================================
# P2 #4: AsyncTestClient
# ===========================================================================


class TestAsyncTestClient:
    """Tests for AsyncTestClient."""

    def test_import(self):
        """AsyncTestClient is importable from testclient module."""
        from fastapi_rs.testclient import AsyncTestClient
        assert AsyncTestClient is not None

    def test_has_async_context_manager(self):
        """AsyncTestClient implements __aenter__ and __aexit__."""
        from fastapi_rs.testclient import AsyncTestClient
        from fastapi_rs import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        assert hasattr(client, "__aenter__")
        assert hasattr(client, "__aexit__")

    def test_has_http_methods(self):
        """AsyncTestClient has get, post, put, delete, patch, options, head, request methods."""
        from fastapi_rs.testclient import AsyncTestClient
        from fastapi_rs import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        for method in ("get", "post", "put", "delete", "patch", "options", "head", "request"):
            assert hasattr(client, method)
            assert callable(getattr(client, method))

    def test_methods_are_async(self):
        """AsyncTestClient methods are coroutine functions."""
        import inspect
        from fastapi_rs.testclient import AsyncTestClient
        from fastapi_rs import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        for method in ("get", "post", "put", "delete", "patch", "options", "head", "request"):
            assert inspect.iscoroutinefunction(getattr(client, method))

    def test_init_stores_app(self):
        """AsyncTestClient stores the app reference."""
        from fastapi_rs.testclient import AsyncTestClient
        from fastapi_rs import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        assert client.app is app


# ===========================================================================
# P2 #5: OpenAPI servers, terms_of_service, contact, license_info
# ===========================================================================


class TestOpenAPIExtendedInfo:
    """Tests for extended OpenAPI info fields."""

    def test_app_stores_servers(self):
        """FastAPI stores servers parameter."""
        from fastapi_rs import FastAPI
        servers = [{"url": "https://api.example.com", "description": "Production"}]
        app = FastAPI(servers=servers)
        assert app.servers == servers

    def test_app_stores_terms_of_service(self):
        """FastAPI stores terms_of_service parameter."""
        from fastapi_rs import FastAPI
        app = FastAPI(terms_of_service="https://example.com/tos")
        assert app.terms_of_service == "https://example.com/tos"

    def test_app_stores_contact(self):
        """FastAPI stores contact parameter."""
        from fastapi_rs import FastAPI
        contact = {"name": "Support", "email": "support@example.com"}
        app = FastAPI(contact=contact)
        assert app.contact == contact

    def test_app_stores_license_info(self):
        """FastAPI stores license_info parameter."""
        from fastapi_rs import FastAPI
        license_info = {"name": "MIT", "url": "https://opensource.org/licenses/MIT"}
        app = FastAPI(license_info=license_info)
        assert app.license_info == license_info

    def test_openapi_includes_servers(self):
        """OpenAPI schema includes servers when set."""
        from fastapi_rs import FastAPI
        servers = [{"url": "https://api.example.com"}]
        app = FastAPI(servers=servers)

        @app.get("/test")
        def test_route():
            return {}

        schema = app.openapi()
        assert "servers" in schema
        assert schema["servers"] == servers

    def test_openapi_includes_terms_of_service(self):
        """OpenAPI schema includes termsOfService in info when set."""
        from fastapi_rs import FastAPI
        app = FastAPI(terms_of_service="https://example.com/tos")

        @app.get("/test")
        def test_route():
            return {}

        schema = app.openapi()
        assert schema["info"]["termsOfService"] == "https://example.com/tos"

    def test_openapi_includes_contact(self):
        """OpenAPI schema includes contact in info when set."""
        from fastapi_rs import FastAPI
        contact = {"name": "Support", "email": "support@example.com"}
        app = FastAPI(contact=contact)

        @app.get("/test")
        def test_route():
            return {}

        schema = app.openapi()
        assert schema["info"]["contact"] == contact

    def test_openapi_includes_license(self):
        """OpenAPI schema includes license in info when set."""
        from fastapi_rs import FastAPI
        license_info = {"name": "MIT"}
        app = FastAPI(license_info=license_info)

        @app.get("/test")
        def test_route():
            return {}

        schema = app.openapi()
        assert schema["info"]["license"] == license_info

    def test_openapi_defaults_no_extra_fields(self):
        """OpenAPI schema omits servers/contact/etc when not set."""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/test")
        def test_route():
            return {}

        schema = app.openapi()
        assert "servers" not in schema
        assert "termsOfService" not in schema["info"]
        assert "contact" not in schema["info"]
        assert "license" not in schema["info"]


# ===========================================================================
# P2 #6: Security schemes in OpenAPI output
# ===========================================================================


class TestOpenAPISecuritySchemes:
    """Tests for security schemes in OpenAPI output."""

    def test_oauth2_scheme_in_openapi(self):
        """OAuth2PasswordBearer appears in securitySchemes."""
        from fastapi_rs import FastAPI, Depends
        from fastapi_rs.security import OAuth2PasswordBearer

        app = FastAPI()
        oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

        @app.get("/protected")
        def protected(token: str = Depends(oauth2_scheme)):
            return {"token": token}

        schema = app.openapi()
        assert "components" in schema
        assert "securitySchemes" in schema["components"]
        schemes = schema["components"]["securitySchemes"]
        assert "OAuth2PasswordBearer" in schemes
        assert schemes["OAuth2PasswordBearer"]["type"] == "oauth2"

    def test_http_bearer_in_openapi(self):
        """HTTPBearer appears in securitySchemes."""
        from fastapi_rs import FastAPI, Depends
        from fastapi_rs.security import HTTPBearer

        app = FastAPI()
        bearer = HTTPBearer()

        @app.get("/protected")
        def protected(creds=Depends(bearer)):
            return {"ok": True}

        schema = app.openapi()
        schemes = schema.get("components", {}).get("securitySchemes", {})
        assert "HTTPBearer" in schemes
        assert schemes["HTTPBearer"]["type"] == "http"
        assert schemes["HTTPBearer"]["scheme"] == "bearer"

    def test_api_key_header_in_openapi(self):
        """APIKeyHeader appears in securitySchemes."""
        from fastapi_rs import FastAPI, Depends
        from fastapi_rs.security import APIKeyHeader

        app = FastAPI()
        api_key = APIKeyHeader(name="X-API-Key")

        @app.get("/protected")
        def protected(key=Depends(api_key)):
            return {"ok": True}

        schema = app.openapi()
        schemes = schema.get("components", {}).get("securitySchemes", {})
        assert "APIKeyHeader" in schemes
        assert schemes["APIKeyHeader"]["type"] == "apiKey"
        assert schemes["APIKeyHeader"]["in"] == "header"

    def test_no_security_no_schemes(self):
        """OpenAPI without security deps has no securitySchemes."""
        from fastapi_rs import FastAPI

        app = FastAPI()

        @app.get("/public")
        def public():
            return {"ok": True}

        schema = app.openapi()
        components = schema.get("components", {})
        assert "securitySchemes" not in components


# ===========================================================================
# P2 #7: Tag descriptions in OpenAPI
# ===========================================================================


class TestOpenAPITagDescriptions:
    """Tests for openapi_tags with descriptions."""

    def test_app_stores_openapi_tags(self):
        """FastAPI stores openapi_tags parameter."""
        from fastapi_rs import FastAPI
        tags = [
            {"name": "items", "description": "Operations with items"},
            {"name": "users", "description": "Operations with users"},
        ]
        app = FastAPI(openapi_tags=tags)
        assert app.openapi_tags == tags

    def test_openapi_tags_in_schema(self):
        """OpenAPI schema includes tags array when openapi_tags is set."""
        from fastapi_rs import FastAPI
        tags = [
            {"name": "items", "description": "Operations with items"},
        ]
        app = FastAPI(openapi_tags=tags)

        @app.get("/items", tags=["items"])
        def get_items():
            return []

        schema = app.openapi()
        assert "tags" in schema
        assert schema["tags"] == tags

    def test_openapi_no_tags_when_not_set(self):
        """OpenAPI schema omits tags when openapi_tags is not set."""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.get("/test")
        def test_route():
            return {}

        schema = app.openapi()
        assert "tags" not in schema


# ===========================================================================
# P2 #8: WebSocketDisconnect exception
# ===========================================================================


class TestWebSocketDisconnect:
    """Tests for WebSocketDisconnect exception."""

    def test_import_from_exceptions(self):
        """WebSocketDisconnect is importable from exceptions module."""
        from fastapi_rs.exceptions import WebSocketDisconnect
        assert WebSocketDisconnect is not None

    def test_import_from_top_level(self):
        """WebSocketDisconnect is importable from fastapi_rs."""
        from fastapi_rs import WebSocketDisconnect
        assert WebSocketDisconnect is not None

    def test_default_code(self):
        """WebSocketDisconnect defaults to code 1000."""
        from fastapi_rs.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect()
        assert exc.code == 1000

    def test_custom_code(self):
        """WebSocketDisconnect accepts custom code."""
        from fastapi_rs.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect(code=1001)
        assert exc.code == 1001

    def test_reason(self):
        """WebSocketDisconnect accepts a reason."""
        from fastapi_rs.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect(code=1000, reason="Normal closure")
        assert exc.reason == "Normal closure"

    def test_is_exception(self):
        """WebSocketDisconnect is an Exception subclass."""
        from fastapi_rs.exceptions import WebSocketDisconnect
        assert issubclass(WebSocketDisconnect, Exception)

    def test_default_reason_none(self):
        """WebSocketDisconnect defaults reason to None."""
        from fastapi_rs.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect()
        assert exc.reason is None


# ===========================================================================
# P2 #9: iter_text / iter_bytes / iter_json on WebSocket
# ===========================================================================


class TestWebSocketIterators:
    """Tests for iter_text, iter_bytes, iter_json on WebSocket."""

    def test_websocket_has_iter_text(self):
        """WebSocket has iter_text method."""
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        assert hasattr(ws, "iter_text")

    def test_websocket_has_iter_bytes(self):
        """WebSocket has iter_bytes method."""
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        assert hasattr(ws, "iter_bytes")

    def test_websocket_has_iter_json(self):
        """WebSocket has iter_json method."""
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        assert hasattr(ws, "iter_json")

    def test_iter_text_is_async_generator(self):
        """iter_text returns an async generator."""
        import inspect
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_text()
        assert inspect.isasyncgen(gen)

    def test_iter_bytes_is_async_generator(self):
        """iter_bytes returns an async generator."""
        import inspect
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_bytes()
        assert inspect.isasyncgen(gen)

    def test_iter_json_is_async_generator(self):
        """iter_json returns an async generator."""
        import inspect
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_json()
        assert inspect.isasyncgen(gen)

    def test_iter_json_accepts_mode(self):
        """iter_json accepts a mode parameter."""
        import inspect
        from fastapi_rs.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_json(mode="binary")
        assert inspect.isasyncgen(gen)


# ===========================================================================
# P2 #11: generate_unique_id_function
# ===========================================================================


class TestGenerateUniqueIdFunction:
    """Tests for generate_unique_id_function on APIRoute."""

    def test_default_no_custom_id(self):
        """Without generate_unique_id_function, operation_id is None by default."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        route = APIRoute("/test", handler, methods=["GET"])
        assert route.operation_id is None

    def test_explicit_operation_id_takes_precedence(self):
        """Explicit operation_id takes precedence over generate_unique_id_function."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        def custom_id(route, method):
            return "custom_id"

        route = APIRoute(
            "/test", handler,
            methods=["GET"],
            operation_id="my_explicit_id",
            generate_unique_id_function=custom_id,
        )
        assert route.operation_id == "my_explicit_id"

    def test_generate_unique_id_function_called(self):
        """generate_unique_id_function is called to set operation_id."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        def custom_id(route, method):
            return f"{route.name}_custom_{method}"

        route = APIRoute(
            "/items", handler,
            methods=["POST"],
            generate_unique_id_function=custom_id,
        )
        assert route.operation_id == "handler_custom_POST"

    def test_default_generate_unique_id(self):
        """_default_generate_unique_id generates sensible IDs."""
        from fastapi_rs.routing import _default_generate_unique_id, APIRoute

        def my_handler():
            return {}

        route = APIRoute("/test", my_handler, methods=["GET"])
        result = _default_generate_unique_id(route, "GET")
        assert result == "my_handler_get"

    def test_generate_unique_id_function_stored(self):
        """generate_unique_id_function is stored on the route."""
        from fastapi_rs.routing import APIRoute

        def handler():
            return {}

        def custom_id(route, method):
            return "custom"

        route = APIRoute(
            "/test", handler,
            generate_unique_id_function=custom_id,
        )
        assert route.generate_unique_id_function is custom_id


# ===========================================================================
# P2 #12: @app.trace method
# ===========================================================================


class TestTraceMethod:
    """Tests for the TRACE HTTP method on FastAPI and APIRouter."""

    def test_fastapi_has_trace(self):
        """FastAPI has a trace() method."""
        from fastapi_rs import FastAPI
        app = FastAPI()
        assert hasattr(app, "trace")
        assert callable(app.trace)

    def test_router_has_trace(self):
        """APIRouter has a trace() method."""
        from fastapi_rs.routing import APIRouter
        router = APIRouter()
        assert hasattr(router, "trace")
        assert callable(router.trace)

    def test_trace_registers_route(self):
        """app.trace() registers a TRACE route."""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.trace("/debug")
        def debug_trace():
            return {"method": "TRACE"}

        routes = app.router.routes
        assert len(routes) == 1
        assert "TRACE" in routes[0].methods

    def test_trace_on_router(self):
        """router.trace() registers a TRACE route."""
        from fastapi_rs.routing import APIRouter
        router = APIRouter()

        @router.trace("/debug")
        def debug_trace():
            return {"method": "TRACE"}

        routes = router.routes
        assert len(routes) == 1
        assert "TRACE" in routes[0].methods

    def test_trace_route_endpoint(self):
        """TRACE route stores the correct endpoint."""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.trace("/debug")
        def debug_trace():
            return {"method": "TRACE"}

        route = app.router.routes[0]
        assert route.endpoint is debug_trace

    def test_trace_with_kwargs(self):
        """TRACE route accepts kwargs like tags."""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.trace("/debug", tags=["debug"])
        def debug_trace():
            return {"method": "TRACE"}

        route = app.router.routes[0]
        assert "debug" in route.tags

    def test_trace_multiple_routes(self):
        """Multiple TRACE routes can be registered."""
        from fastapi_rs import FastAPI
        app = FastAPI()

        @app.trace("/debug1")
        def debug1():
            return {}

        @app.trace("/debug2")
        def debug2():
            return {}

        assert len(app.router.routes) == 2
        assert app.router.routes[0].path == "/debug1"
        assert app.router.routes[1].path == "/debug2"
