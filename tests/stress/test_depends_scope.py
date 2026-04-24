"""``Depends(scope=…)`` parity with upstream FastAPI.

Upstream accepts ``scope="function"`` / ``"request"`` (``Literal`` type
hint) and uses it to control when a ``yield``-dependency's teardown runs
relative to the response. From a TestClient's perspective the
observable event ordering is identical across both scopes — the
difference only matters for streaming responses / background tasks. We
lock in the observable-parity contract so future refactors don't drift.
"""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def _run_with_scope(scope):
    events: list[str] = []

    def yield_dep():
        events.append(f"{scope}-enter")
        yield "v"
        events.append(f"{scope}-exit")

    app = FastAPI()

    @app.get("/x")
    def _h(v: str = Depends(yield_dep, scope=scope)):
        events.append("handler")
        return {"v": v}

    c = TestClient(app)
    r = c.get("/x")
    assert r.status_code == 200
    return events


def test_depends_scope_function_accepted_and_teardown_after_handler():
    events = _run_with_scope("function")
    assert events == ["function-enter", "handler", "function-exit"]


def test_depends_scope_request_accepted_and_teardown_after_handler():
    events = _run_with_scope("request")
    assert events == ["request-enter", "handler", "request-exit"]


def test_depends_scope_none_is_default():
    app = FastAPI()

    def dep():
        return 1

    # scope=None is the documented default; accepting it must not error.
    @app.get("/x")
    def _h(v: int = Depends(dep, scope=None)):
        return {"v": v}

    c = TestClient(app)
    r = c.get("/x")
    assert r.status_code == 200
    assert r.json() == {"v": 1}


def test_depends_scope_preserved_on_marker_object():
    """The Depends object exposes .scope for introspection tools
    (e.g., third-party middleware that inspects dep graphs)."""
    d = Depends(lambda: 1, scope="function")
    assert d.scope == "function"
    d2 = Depends(lambda: 1, scope="request")
    assert d2.scope == "request"
    d3 = Depends(lambda: 1)
    assert d3.scope is None
