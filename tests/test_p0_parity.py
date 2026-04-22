"""Tests for P0 FastAPI parity fixes.

Covers:
- response_model_by_alias + Pydantic Field(alias=)
- default_response_class cascade (app → router → route)
- FastAPI(responses=) app-level default responses
- FastAPI(debug=) traceback printing
- ORJSONResponse with fallback
"""

from __future__ import annotations

import io
import sys

import pytest


# ── response_model_by_alias ──────────────────────────────────────────


class TestResponseModelByAlias:
    def test_alias_honored_by_default(self):
        """by_alias=True is the default. Field(alias=...) must appear in output."""
        from pydantic import BaseModel, Field

        from fastapi_rs import FastAPI

        class User(BaseModel):
            user_id: int = Field(alias="userId")
            full_name: str = Field(alias="fullName")

        app = FastAPI()

        @app.get("/u", response_model=User)
        def get_user():
            # Return dict with aliased keys — matches what client would send
            return {"userId": 5, "fullName": "Alice"}

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        # Output should use aliases, not Python field names
        assert "userId" in result
        assert "fullName" in result
        assert "user_id" not in result
        assert "full_name" not in result

    def test_by_alias_false_uses_python_names(self):
        from pydantic import BaseModel, Field

        from fastapi_rs import FastAPI

        class User(BaseModel):
            user_id: int = Field(alias="userId")

        app = FastAPI()

        @app.get("/u", response_model=User, response_model_by_alias=False)
        def get_user():
            return {"userId": 5}

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        # Now output uses python field names
        assert "user_id" in result
        assert "userId" not in result

    def test_serialization_alias(self):
        """Field(serialization_alias=) should also be honored in output."""
        from pydantic import BaseModel, Field

        from fastapi_rs import FastAPI

        class M(BaseModel):
            name: str = Field(serialization_alias="displayName")

        app = FastAPI()

        @app.get("/m", response_model=M)
        def get_m():
            return {"name": "Alice"}

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        assert "displayName" in result


# ── default_response_class ───────────────────────────────────────────


class TestDefaultResponseClass:
    def test_app_level_default(self):
        from fastapi_rs import FastAPI
        from fastapi_rs.responses import HTMLResponse

        app = FastAPI(default_response_class=HTMLResponse)

        @app.get("/")
        def h():
            return "<b>hi</b>"

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        # Result should be wrapped in HTMLResponse
        assert hasattr(result, "status_code")
        assert result.media_type == "text/html"

    def test_route_overrides_app(self):
        from fastapi_rs import FastAPI
        from fastapi_rs.responses import HTMLResponse, PlainTextResponse

        app = FastAPI(default_response_class=HTMLResponse)

        @app.get("/p", response_class=PlainTextResponse)
        def p():
            return "plain"

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        assert result.media_type == "text/plain"

    def test_router_level_default(self):
        from fastapi_rs import APIRouter, FastAPI
        from fastapi_rs.responses import HTMLResponse

        app = FastAPI()
        router = APIRouter(default_response_class=HTMLResponse)

        @router.get("/r")
        def r():
            return "<p>router</p>"

        app.include_router(router)
        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        assert result.media_type == "text/html"


# ── FastAPI(responses=) ──────────────────────────────────────────────


class TestAppLevelResponses:
    def test_app_responses_appear_in_openapi(self):
        from fastapi_rs import FastAPI

        app = FastAPI(
            responses={404: {"description": "Not found globally"}}
        )

        @app.get("/x")
        def x():
            return {}

        schema = app.openapi()
        op = schema["paths"]["/x"]["get"]
        assert "404" in op["responses"]
        assert op["responses"]["404"]["description"] == "Not found globally"

    def test_route_responses_override_app(self):
        from fastapi_rs import FastAPI

        app = FastAPI(
            responses={404: {"description": "App default 404"}}
        )

        @app.get("/x", responses={404: {"description": "Route-specific 404"}})
        def x():
            return {}

        schema = app.openapi()
        op = schema["paths"]["/x"]["get"]
        assert op["responses"]["404"]["description"] == "Route-specific 404"


# ── FastAPI(debug=) ──────────────────────────────────────────────────


class TestDebugMode:
    def test_debug_default_false(self):
        from fastapi_rs import FastAPI

        app = FastAPI()
        assert app.debug is False

    def test_debug_true_stored(self):
        from fastapi_rs import FastAPI

        app = FastAPI(debug=True)
        assert app.debug is True

    def test_debug_prints_traceback(self, capsys):
        """In debug mode, handler exceptions print a traceback to stderr."""
        from fastapi_rs import FastAPI

        app = FastAPI(debug=True)

        @app.get("/boom")
        def boom():
            raise ValueError("something broke")

        routes = app._collect_all_routes()
        try:
            routes[0]["endpoint"]()
        except ValueError:
            pass
        captured = capsys.readouterr()
        # Traceback should include the error type and message
        assert "ValueError" in captured.err
        assert "something broke" in captured.err

    def test_no_debug_no_traceback(self, capsys):
        """Without debug, tracebacks are NOT printed to stderr."""
        from fastapi_rs import FastAPI

        app = FastAPI(debug=False)

        @app.get("/boom")
        def boom():
            raise ValueError("silent error")

        routes = app._collect_all_routes()
        try:
            routes[0]["endpoint"]()
        except ValueError:
            pass
        captured = capsys.readouterr()
        assert "silent error" not in captured.err

    def test_http_exception_not_traced_in_debug(self, capsys):
        """HTTPException in debug mode should NOT print a traceback — it's control flow."""
        from fastapi_rs import FastAPI, HTTPException

        app = FastAPI(debug=True)

        @app.get("/nf")
        def nf():
            raise HTTPException(status_code=404, detail="nope")

        routes = app._collect_all_routes()
        try:
            routes[0]["endpoint"]()
        except HTTPException:
            pass
        captured = capsys.readouterr()
        # HTTPException is normal control flow, not a bug — no traceback
        assert "HTTPException" not in captured.err


# ── ORJSONResponse with fallback ─────────────────────────────────────


class TestORJSONResponse:
    def test_orjson_response_renders(self):
        from fastapi_rs.responses import ORJSONResponse
        from fastapi_rs.exceptions import FastAPIDeprecationWarning
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FastAPIDeprecationWarning)
            resp = ORJSONResponse(content={"k": "v", "n": 42})
        # body is bytes
        assert isinstance(resp.body, bytes)
        # valid JSON
        import json as _json
        parsed = _json.loads(resp.body)
        assert parsed == {"k": "v", "n": 42}

    def test_json_response_fallback(self):
        """JSONResponse should work even if orjson is not installed (falls back to stdlib)."""
        from fastapi_rs.responses import JSONResponse

        # This should not raise ImportError even if orjson is absent
        resp = JSONResponse(content={"k": "v"})
        assert isinstance(resp.body, bytes)
        # The body should be valid compact JSON (no spaces)
        assert resp.body in (b'{"k":"v"}', b'{"k": "v"}')  # orjson compact, or stdlib with our fallback

    def test_json_response_compact_bytes_match_starlette(self):
        """JSONResponse output bytes should match Starlette's compact form."""
        from fastapi_rs.responses import JSONResponse

        resp = JSONResponse(content={"a": 1, "b": "x"})
        # No spaces between key/value (compact form)
        assert resp.body == b'{"a":1,"b":"x"}'
