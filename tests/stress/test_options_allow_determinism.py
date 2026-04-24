"""OPTIONS Allow header must be deterministic when multiple registered
templates match the incoming path.

Previously the middleware scanned a ``HashMap`` and emitted whichever
template iteration hit first — so overlapping routes like
``/items/{id}`` and ``/items/special`` could emit either's Allow list
depending on hash seed. Now we pick the most-specific template (fewest
``{param}`` segments; tie → lexicographic)."""
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


def test_literal_path_wins_over_param_path():
    c = TestClient(_app())
    r = c.request("OPTIONS", "/items/special")
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_param_path_matches_when_no_literal():
    c = TestClient(_app())
    r = c.request("OPTIONS", "/items/xyz")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET"


def test_repeated_requests_return_same_allow_header():
    c = TestClient(_app())
    results = {c.request("OPTIONS", "/items/special").headers["allow"] for _ in range(25)}
    assert results == {"POST"}


def test_three_way_overlap_prefers_most_specific():
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

    tc = TestClient(app)
    # Most-specific: /a/lit/lit → only matches /a/lit/lit (0 params)
    # and /a/{x}/lit (1 param, matches 'lit' as {x}) and
    # /a/{x}/{y} (2 params). Winner is the zero-param template.
    r = tc.request("OPTIONS", "/a/lit/lit")
    assert r.headers["allow"] == "PUT"
