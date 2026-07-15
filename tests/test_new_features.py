"""Feature-gap pins NOT covered by the upstream suite.

CONSOLIDATION (coverage-differential, pool-1): baseline = retained local
suite + upstream FastAPI 0.138.1 suite under the shim, per-test coverage
contexts over ``python/fastapi_turbo`` + grep evidence in the 0.138.1 clone.
Deleted-as-redundant (every deleted test had an EMPTY unique-line
differential AND a named twin):

  * set_cookie all-options kwargs      → local tests/test_fastapi_compat.py::
                                         test_positional_all_args (same attrs)
  * delete_cookie → Max-Age=0          → local tests/test_fastapi_compat.py::
                                         test_delete_cookie_positional
  * response_class per route wraps     → tests/test_default_response_class.py
                                         + custom_response tutorial suites
  * response_description              → tests/test_tutorial/
                                         test_path_operation_configurations
  * responses dict merges (+model)     → tests/test_additional_responses_router.py,
                                         tests/test_additional_responses_custom_
                                         model_in_callback.py
  * Body(media_type=)                  → tests/test_request_body_parameters_
                                         media_type.py
  * include_in_schema=False route      → tests/test_tutorial/test_path_operation_
                                         advanced_configurations/test_tutorial003.py
                                         + tests/test_param_include_in_schema.py
  * openapi_extra merge                → tests/test_openapi_route_extensions.py
  * url_path_for value                 → tests/test_starlette_urlconvertors.py
  * root_path stored + openapi servers → tests/test_openapi_cache_root_path.py,
                                         tests/test_tutorial/test_behind_a_proxy
  * HTTPDigest import                  → tests/test_security_http_digest.py
                                         (+_optional/_description)
  * @app.exception_handler(cls) registers / invoked through client
                                       → tests/test_validation_error_context.py
                                         (decorator form) +
                                         tests/test_exception_handlers.py
                                         (custom handler status via client)
  * @app.middleware("http") registers / wraps endpoint / chain order
                                       → local tests/stress/test_broad_starlette_
                                         parity.py::test_http_middleware_
                                         registration_order_parity (3-MW A/B
                                         order oracle vs upstream) + upstream
                                         tests/test_dependency_contextvars.py

KEPT (no twin or unique-line carrier): set_cookie DEFAULT attrs (Path=/,
SameSite=lax — upstream never asserts the defaults), multiple-cookies
preserved (two Set-Cookie headers), returned-Response-wins-over-
response_class, per-route ``security=`` kwarg (turbo extension — upstream
routing.py has no such parameter; sole cover of applications.py:3184),
callbacks-in-openapi (sole cover of applications.py callback merge),
url_path_for LookupError + root_path prefix (turbo-specific shapes),
status-code-keyed exception handler (no upstream status-key test; the
8721f5a twin anchor), handler MRO lookup (turbo ``_lookup_exception_handler``),
and @app.middleware("https") ValueError (sole cover of applications.py:1897).

KEPT AS LOCAL-COVERAGE CARRIERS (twins exist upstream —
tests/test_schema_extra_examples.py, tests/test_openapi_examples.py,
tests/test_security_http_digest.py — but the local fast suite would lose
the param_functions.py example/examples arcs and the security.py
HTTPDigest arcs; ≤0.2% local-coverage gate): TestExamples (both) and
TestHTTPDigest.test_digest_model.
"""

from __future__ import annotations

import fastapi_turbo  # noqa: F401 — installs compat shim for `from fastapi ...` / `from starlette ...`

import pytest


# ── Response.set_cookie ────────────────────────────────────────────


def _set_cookies(resp):
    """Decoded Set-Cookie header values from a (real Starlette) Response.
    ``raw_headers`` is ``list[tuple[bytes, bytes]]`` and also carries
    content-length, so filter by name and decode."""
    out = []
    for k, v in resp.raw_headers:
        kk = k.decode("latin-1") if isinstance(k, bytes) else str(k)
        if kk.lower() == "set-cookie":
            out.append(v.decode("latin-1") if isinstance(v, bytes) else str(v))
    return out


class TestCookies:
    def test_set_cookie_basic(self):
        from fastapi.responses import Response

        r = Response(content="hi")
        r.set_cookie("session", "abc123")
        cookies = _set_cookies(r)
        assert len(cookies) == 1
        assert "session=abc123" in cookies[0]
        assert "Path=/" in cookies[0]
        assert "SameSite=lax" in cookies[0]

    def test_multiple_cookies_preserved(self):
        from fastapi.responses import Response

        r = Response()
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        cookies = _set_cookies(r)
        assert len(cookies) == 2
        assert "a=1" in cookies[0]
        assert "b=2" in cookies[1]


# ── response_class per route ──────────────────────────────────────


class TestResponseClass:
    def test_response_class_ignores_existing_response(self):
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse

        app = FastAPI()

        @app.get("/", response_class=HTMLResponse)
        def h():
            return JSONResponse({"x": 1})  # user-returned Response should win

        routes = app._collect_all_routes()
        result = routes[0]["endpoint"]()
        # Should still be JSONResponse (not wrapped in HTML)
        assert result.media_type == "application/json"


# ── Per-route security (turbo extension kwarg) ────────────────────


class TestPerRouteSecurity:
    def test_explicit_security(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/protected", security=[{"BearerAuth": []}])
        def h():
            return {}

        schema = app.openapi()
        op = schema["paths"]["/protected"]["get"]
        assert op["security"] == [{"BearerAuth": []}]

    def test_empty_security_disables(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/public", security=[])
        def h():
            return {}

        schema = app.openapi()
        op = schema["paths"]["/public"]["get"]
        assert op["security"] == []


# ── example/examples in parameters (local-coverage carriers) ─────


class TestExamples:
    def test_param_example(self):
        from fastapi import FastAPI, Query
        from fastapi.exceptions import FastAPIDeprecationWarning
        import warnings as _w

        app = FastAPI()

        with _w.catch_warnings():
            _w.simplefilter("ignore", FastAPIDeprecationWarning)

            @app.get("/x")
            def h(name: str = Query(..., example="Alice")):
                return {}

        schema = app.openapi()
        op = schema["paths"]["/x"]["get"]
        params = op.get("parameters", [])
        assert any(p.get("example") == "Alice" for p in params)

    def test_param_examples_named(self):
        from fastapi import FastAPI, Query

        app = FastAPI()

        @app.get("/x")
        def h(name: str = Query(..., examples={"n1": {"value": "Alice"}, "n2": {"value": "Bob"}})):
            return {}

        schema = app.openapi()
        op = schema["paths"]["/x"]["get"]
        params = op.get("parameters", [])
        # ``Query(examples={named})`` named examples must appear in OpenAPI. Real
        # FastAPI places them in the parameter's JSON Schema (``schema.examples``);
        # the clone placed them at param level (``parameter.examples``) — accept
        # either so the assertion holds under both generators.
        has_examples = any(
            p.get("examples") or p.get("schema", {}).get("examples") for p in params
        )
        assert has_examples


# ── HTTPDigest (local-coverage carrier) ───────────────────────────


class TestHTTPDigest:
    def test_digest_model(self):
        from fastapi import HTTPDigest

        scheme = HTTPDigest()
        assert scheme.model["type"] == "http"
        assert scheme.model["scheme"] == "digest"


# ── callbacks ─────────────────────────────────────────────────────


class TestCallbacks:
    def test_callbacks_in_openapi(self):
        from fastapi import APIRouter, FastAPI

        cb_router = APIRouter()

        @cb_router.post("/cb")
        def cb_handler():
            return {}

        app = FastAPI()

        @app.post("/trigger", callbacks=[cb_router])
        def trigger():
            return {}

        schema = app.openapi()
        op = schema["paths"]["/trigger"]["post"]
        assert "callbacks" in op


# ── url_path_for ──────────────────────────────────────────────────


class TestUrlPathFor:
    def test_url_path_for_missing_name(self):
        from fastapi import FastAPI

        app = FastAPI()

        with pytest.raises(LookupError):
            app.url_path_for("nonexistent")

    def test_url_path_for_with_root_path(self):
        from fastapi import FastAPI

        app = FastAPI(root_path="/api/v1")

        @app.get("/items/{id}")
        def get_item(id: int):
            return {}

        url = app.url_path_for("get_item", id=5)
        assert url == "/api/v1/items/5"


# ── HTTPDigest ────────────────────────────────────────────────────


class TestHTTPDigest:
    def test_digest_import(self):
        from fastapi import HTTPDigest
        from fastapi.security import HTTPDigest as HTTPDigest2

        assert HTTPDigest is HTTPDigest2

    def test_digest_model(self):
        from fastapi import HTTPDigest

        # Real fastapi.security scheme: ``.model`` is the pydantic
        # SecurityScheme model (the clone's dict-shaped ``.model`` is retired).
        scheme = HTTPDigest()
        assert scheme.model.type_.value == "http"
        assert scheme.model.scheme == "digest"


# ── @app.exception_handler ────────────────────────────────────────


class TestExceptionHandler:
    def test_status_code_key(self):
        from fastapi import FastAPI

        app = FastAPI()

        @app.exception_handler(404)
        def handle_404(request, exc):
            return {"not_found": True}

        assert 404 in app.exception_handlers

    def test_mro_lookup(self):
        from fastapi import FastAPI

        class CustomException(Exception):
            pass

        class MoreSpecific(CustomException):
            pass

        app = FastAPI()

        @app.exception_handler(CustomException)
        def handle(request, exc):
            return {"caught": True}

        h = app._lookup_exception_handler(MoreSpecific())
        assert h is not None


# ── @app.middleware("http") ───────────────────────────────────────


class TestHTTPMiddleware:
    def test_unsupported_type_raises(self):
        from fastapi import FastAPI

        app = FastAPI()
        with pytest.raises(ValueError):
            @app.middleware("https")
            def mw(r, cn):
                pass
