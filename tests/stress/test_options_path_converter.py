"""Regression: OPTIONS/Allow matching for routes with the ``{name:path}``
Starlette path converter must consume multiple URL segments.

Route template: ``/files/{full_path:path}`` — a request to
``/files/a/b/c`` should match and surface the route's methods in the
``Allow`` header. Previously ``options_path_matches`` required equal
segment counts and treated any ``{...}`` as a single segment, so
``/files/{full_path:path}`` matched only a single-segment tail."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_options_on_deep_path_converter_finds_allow_header():
    app = FastAPI()

    @app.get("/files/{full_path:path}")
    def _g(full_path: str):
        return {"p": full_path}

    c = TestClient(app)

    # Sanity: GET works with multi-segment path.
    r_get = c.get("/files/a/b/c.png")
    assert r_get.status_code == 200

    # OPTIONS on the same multi-segment path must return 405 with
    # ``Allow: GET``, not 404 (which is what happens when the middleware
    # fails to match any registered template).
    r_opt = c.request("OPTIONS", "/files/a/b/c.png")
    assert r_opt.status_code == 405, r_opt.status_code
    assert r_opt.headers.get("allow") == "GET", r_opt.headers


def test_options_on_single_segment_path_converter_still_matches():
    app = FastAPI()

    @app.post("/api/{name:path}")
    def _p(name: str):
        return {}

    c = TestClient(app)
    r = c.request("OPTIONS", "/api/v1/users/42")
    assert r.status_code == 405
    assert r.headers.get("allow") == "POST"


def test_options_with_cors_on_path_converter_returns_405_not_200():
    """The real audit bug only surfaces when ``CORSMiddleware`` is
    installed — then the OPTIONS-bypass middleware is the one that
    must locate the route template. Without the ``:path`` handling,
    the CORS layer intercepted and returned 200 instead of the
    correct 405 + Allow: GET."""
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"])

    @app.get("/files/{full_path:path}")
    def _g(full_path: str):
        return {}

    c = TestClient(app)
    # Non-preflight OPTIONS (no Origin + Access-Control-Request-Method).
    r = c.request("OPTIONS", "/files/a/b/c")
    assert r.status_code == 405, r.status_code
    assert r.headers.get("allow") == "GET", r.headers.get("allow")
