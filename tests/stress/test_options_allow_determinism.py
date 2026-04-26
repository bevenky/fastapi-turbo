"""OPTIONS Allow header parity with upstream FastAPI.

Upstream Starlette / FastAPI's matcher stops at the FIRST registered
route whose path matches and reports that route's methods in the
405 Allow header. The in-process ASGI dispatch path mirrors that.

The Rust server's matcher (matchit + axum) currently uses a
DIFFERENT rule — most-specific literal template wins — so OPTIONS
to ``/items/special`` returns ``Allow: POST`` on the Rust path
even though upstream returns ``Allow: GET``. That's a known
different-by-design divergence (Rust router internals; flagged in
COMPATIBILITY.md). To assert upstream parity here we force
``in_process=True`` so the tests exercise the path that matches
upstream behavior."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app():
    app = FastAPI()

    @app.get("/items/{id}")
    def _g(id: int):
        return {}

    @app.post("/items/special")
    def _p():
        return {}

    return app


def test_options_uses_first_matching_route_allow():
    """OPTIONS /items/special: the FIRST registered route that
    matches the path is ``/items/{id}`` (matches ``special`` as
    ``{id}``). Upstream emits ``Allow: GET`` from that route's
    methods only — in-process turbo must match."""
    c = TestClient(_app(), in_process=True)
    r = c.request("OPTIONS", "/items/special")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET", r.headers["allow"]


def test_options_param_path_when_no_literal():
    c = TestClient(_app(), in_process=True)
    r = c.request("OPTIONS", "/items/xyz")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET"


def test_options_allow_is_deterministic():
    """Same input → same Allow value across repeated requests
    (the matcher must not depend on hash iteration order)."""
    c = TestClient(_app(), in_process=True)
    results = {
        c.request("OPTIONS", "/items/special").headers["allow"]
        for _ in range(25)
    }
    assert results == {"GET"}


def test_options_three_way_overlap_first_match_wins():
    """Three routes registered in the order:
    ``/a/{x}/{y}`` (GET) → ``/a/{x}/lit`` (POST) → ``/a/lit/lit`` (PUT).

    OPTIONS /a/lit/lit: upstream matches ``/a/{x}/{y}`` first
    (matches ``lit`` as ``{x}`` and ``lit`` as ``{y}``), so
    Allow: GET — NOT PUT or POST. In-process turbo must mirror
    the first-match semantics."""
    app = FastAPI()

    @app.get("/a/{x}/{y}")
    def _a(x: str, y: str):
        return {}

    @app.post("/a/{x}/lit")
    def _b(x: str):
        return {}

    @app.put("/a/lit/lit")
    def _c():
        return {}

    tc = TestClient(app, in_process=True)
    r = tc.request("OPTIONS", "/a/lit/lit")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET", r.headers["allow"]
