"""P2 feature pins NOT covered by the upstream suite.

CONSOLIDATION (coverage-differential, pool-1): baseline = retained local
suite + upstream FastAPI 0.138.1 suite under the shim, per-test coverage
contexts over ``python/fastapi_turbo`` + grep evidence in the 0.138.1 clone.
Deleted-as-redundant (every deleted test had an EMPTY unique-line
differential AND a named twin):

  * response_model_include/exclude stored on route / defaults / decorator
                                → tests/test_response_model_include_exclude.py,
                                  tests/test_serialize_response_model.py
  * exclude_unset/defaults/none stored on route / defaults
                                → tests/test_skip_defaults.py,
                                  tests/test_serialize_response_model.py
  * ORJSONResponse import/render/media_type/status
                                → tests/test_orjson_response_class.py,
                                  tests/test_default_response_class.py,
                                  tests/test_deprecated_responses.py
  * UJSONResponse import/render/media_type
                                → tests/test_deprecated_responses.py
                                  (FastAPI(default_response_class=UJSONResponse))
  * AsyncTestClient import / has-http-methods
                                → retained AsyncTestClient pins below
                                  (test_methods_are_async checks the same 8
                                  methods, strictly stronger)
  * FastAPI servers/terms_of_service/contact/license_info stored + in schema
                                → tests/test_openapi_servers.py,
                                  tests/test_tutorial/test_metadata/
                                  test_tutorial001.py
  * schema omits servers/contact/... when unset
                                → upstream full-schema snapshot asserts
                                  (e.g. tests/test_application.py)
  * securitySchemes for OAuth2PasswordBearer / HTTPBearer
    (+ absence without security deps)
                                → tests/test_security_oauth2.py,
                                  tests/test_security_http_bearer.py
                                  (44 upstream files assert securitySchemes)
  * openapi_tags stored + tags array (+ absence)
                                → tests/test_tutorial/test_metadata/
                                  test_tutorial004.py + schema snapshots
  * WebSocketDisconnect imports / custom code / is-Exception
                                → tests/test_ws_router.py (raises + asserts
                                  .code == WS_1000/WS_1008),
                                  tests/test_tutorial/test_websockets
  * APIRoute.operation_id default None / generate_unique_id_function stored
                                → tests/test_generate_unique_id_function.py +
                                  retained carrier below (same lines, asserts
                                  the effect not just storage)
  * router.trace / trace endpoint stored / trace kwargs / multiple
                                → tests/test_extra_routes.py (@app.trace route,
                                  TRACE request, openapi trace operation) +
                                  tests/test_operations_signatures.py

KEPT (unique-line carriers or no twin): the five ``_apply_response_model``
unit pins (sole cover of the include/exclude/unset/defaults/none branches in
_route_helpers.py — the door serves upstream response_model routes through a
different path), AsyncTestClient shape pins (turbo-only class — upstream has
no AsyncTestClient; sole cover of testclient.py:2096-2101),
WebSocketDisconnect constructor defaults (``reason or ""`` coercion has no
upstream constructor-level pin), the turbo WebSocket.iter_text/bytes/json
surface (sole pins — tests/test_websocket.py never touches iter_*), and the
generate_unique_id carriers (routing.py:27 + 206-209) with the
operation_id-precedence pin (upstream never combines operation_id= with
generate_unique_id_function).

KEPT AS LOCAL-COVERAGE CARRIERS (twins exist upstream —
tests/test_security_api_key_header.py, tests/test_extra_routes.py — but
the local fast suite would lose the security.py APIKeyHeader arcs and the
trace registration arcs (routing.py:368, applications.py:1356);
≤0.2% local-coverage gate): test_api_key_header_in_openapi and
test_trace_registers_route.
"""

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`


# ===========================================================================
# response_model_include / response_model_exclude (_apply_response_model)
# ===========================================================================


class TestResponseModelIncludeExclude:
    """Unit pins for _apply_response_model include/exclude branches."""

    def test_apply_response_model_include(self):
        """_apply_response_model with include only returns included fields."""
        from pydantic import BaseModel
        from fastapi_turbo.applications import _apply_response_model

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
        from fastapi_turbo.applications import _apply_response_model

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


# ===========================================================================
# response_model_exclude_unset / exclude_defaults / exclude_none
# ===========================================================================


class TestResponseModelExcludeOptions:
    """Unit pins for the exclude_unset/defaults/none branches."""

    def test_apply_response_model_exclude_unset(self):
        """_apply_response_model with exclude_unset omits unset fields."""
        from pydantic import BaseModel
        from fastapi_turbo.applications import _apply_response_model

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
        from fastapi_turbo.applications import _apply_response_model

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
        from fastapi_turbo.applications import _apply_response_model

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
# AsyncTestClient (turbo-only: real-socket async client)
# ===========================================================================


class TestAsyncTestClient:
    """Shape pins for the turbo-only AsyncTestClient."""

    def test_has_async_context_manager(self):
        """AsyncTestClient implements __aenter__ and __aexit__."""
        from fastapi.testclient import AsyncTestClient
        from fastapi import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        assert hasattr(client, "__aenter__")
        assert hasattr(client, "__aexit__")

    def test_methods_are_async(self):
        """AsyncTestClient methods are coroutine functions."""
        import inspect
        from fastapi.testclient import AsyncTestClient
        from fastapi import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        for method in ("get", "post", "put", "delete", "patch", "options", "head", "request"):
            assert inspect.iscoroutinefunction(getattr(client, method))

    def test_init_stores_app(self):
        """AsyncTestClient stores the app reference."""
        from fastapi.testclient import AsyncTestClient
        from fastapi import FastAPI

        app = FastAPI()
        client = AsyncTestClient(app)
        assert client.app is app


# ===========================================================================
# WebSocketDisconnect constructor defaults
# ===========================================================================


class TestWebSocketDisconnect:
    """Constructor-contract pins for WebSocketDisconnect."""

    def test_default_code(self):
        """WebSocketDisconnect defaults to code 1000."""
        from fastapi.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect()
        assert exc.code == 1000

    def test_reason(self):
        """WebSocketDisconnect accepts a reason."""
        from fastapi.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect(code=1000, reason="Normal closure")
        assert exc.reason == "Normal closure"

    def test_default_reason_empty(self):
        """WebSocketDisconnect defaults reason to "" (real Starlette coerces
        ``reason or ""``)."""
        from fastapi.exceptions import WebSocketDisconnect
        exc = WebSocketDisconnect()
        assert exc.reason == ""


# ===========================================================================
# iter_text / iter_bytes / iter_json on WebSocket
# ===========================================================================


class TestWebSocketIterators:
    """Tests for iter_text, iter_bytes, iter_json on WebSocket."""

    def test_websocket_has_iter_text(self):
        """WebSocket has iter_text method."""
        from starlette.websockets import WebSocket
        ws = WebSocket()
        assert hasattr(ws, "iter_text")

    def test_websocket_has_iter_bytes(self):
        """WebSocket has iter_bytes method."""
        from starlette.websockets import WebSocket
        ws = WebSocket()
        assert hasattr(ws, "iter_bytes")

    def test_websocket_has_iter_json(self):
        """WebSocket has iter_json method."""
        from starlette.websockets import WebSocket
        ws = WebSocket()
        assert hasattr(ws, "iter_json")

    def test_iter_text_is_async_generator(self):
        """iter_text returns an async generator."""
        import inspect
        from starlette.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_text()
        assert inspect.isasyncgen(gen)

    def test_iter_bytes_is_async_generator(self):
        """iter_bytes returns an async generator."""
        import inspect
        from starlette.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_bytes()
        assert inspect.isasyncgen(gen)

    def test_iter_json_is_async_generator(self):
        """iter_json returns an async generator."""
        import inspect
        from starlette.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_json()
        assert inspect.isasyncgen(gen)

    def test_iter_json_accepts_mode(self):
        """iter_json accepts a mode parameter."""
        import inspect
        from starlette.websockets import WebSocket
        ws = WebSocket()
        gen = ws.iter_json(mode="binary")
        assert inspect.isasyncgen(gen)


# ===========================================================================
# Local-coverage carriers: APIKeyHeader securityScheme + @app.trace
# ===========================================================================


class TestOpenAPISecuritySchemes:
    def test_api_key_header_in_openapi(self):
        """APIKeyHeader appears in securitySchemes."""
        from fastapi import FastAPI, Depends
        from fastapi.security import APIKeyHeader

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


class TestTraceMethod:
    def test_trace_registers_route(self):
        """app.trace() registers a TRACE route."""
        from fastapi import FastAPI
        app = FastAPI()

        @app.trace("/debug")
        def debug_trace():
            return {"method": "TRACE"}

        routes = app.router.routes
        assert len(routes) == 1
        assert "TRACE" in routes[0].methods


# ===========================================================================
# generate_unique_id_function
# ===========================================================================


class TestGenerateUniqueIdFunction:
    """Carrier pins for generate_unique_id_function on APIRoute."""

    def test_explicit_operation_id_takes_precedence(self):
        """Explicit operation_id takes precedence over generate_unique_id_function."""
        from fastapi.routing import APIRoute

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
        from fastapi.routing import APIRoute

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
        from fastapi.routing import _default_generate_unique_id, APIRoute

        def my_handler():
            return {}

        route = APIRoute("/test", my_handler, methods=["GET"])
        result = _default_generate_unique_id(route, "GET")
        assert result == "my_handler_get"
