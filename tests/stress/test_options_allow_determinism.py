"""OPTIONS Allow header parity with upstream FastAPI.

Upstream Starlette / FastAPI's matcher stops at the FIRST registered
route whose path matches and reports that route's methods in the
405 Allow header. Both the in-process ASGI dispatch path and the
Rust server now mirror that (R27 — the Rust path's
``non_preflight_options_middleware`` walks templates in
registration order and the per-path 405 fallback uses a
first-match-wins Allow header).

These tests still pin ``in_process=True`` because the in-process
path is the cleanest place to assert ASGI dispatch behaviour without
spinning up a real loopback server; ``test_r27_regressions.py``
covers the same parity over the real Rust server (under
``requires_loopback``)."""
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
