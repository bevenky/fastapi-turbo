"""External APM routing-patch detection → real-FastAPI delegation.

APM SDKs (Sentry / Datadog / New Relic) monkey-patch
``fastapi.routing.get_request_handler`` (and Starlette's
``request_response``) to observe requests. Turbo detects a live patch at
route-build time (identity check against import-time originals —
``_sentry_compat.external_routing_patch_active``) and declines the lean
adapter so the route delegates to real FastAPI's route handler chain,
where the patch actually runs.

These tests pin both sides of that contract:
* NO patch → the adapter path is taken (zero behavior change for a
  plain app).
* patch live → the adapter declines, the route still serves correctly
  via delegation, and the external patch demonstrably ran.
"""
import fastapi_turbo  # noqa: F401  (installs the shim)

import fastapi.routing as fa_routing
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_turbo import _sentry_compat


def _route_dict(app, path):
    rds = [rd for rd in app._collect_all_routes() if rd.get("path") == path]
    assert rds, f"route {path} not collected"
    return rds[0]


def test_no_patch_adapter_path_taken():
    """Plain app, nothing patched: detection is False and the lean
    adapter serves the route (no delegation decline)."""
    assert _sentry_compat.external_routing_patch_active() is False

    app = FastAPI()

    @app.get("/plain")
    def plain():
        return {"ok": True}

    adapted = app._adapter_route_info(_route_dict(app, "/plain"))
    assert adapted is not None, "adapter must serve a plain route when unpatched"

    with TestClient(app) as client:
        assert client.get("/plain").json() == {"ok": True}


def test_external_patch_detected_and_delegated(monkeypatch):
    """A third-party wrapper on ``fastapi.routing.get_request_handler``
    flips detection, makes the adapter decline, and the delegated route
    build actually invokes the wrapper (so APM instrumentation runs)."""
    calls = {"n": 0}
    orig = fa_routing.get_request_handler

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(fa_routing, "get_request_handler", wrapper)
    assert _sentry_compat.external_routing_patch_active() is True

    app = FastAPI()

    @app.get("/patched")
    def patched_ep():
        return {"ok": True}

    assert app._adapter_route_info(_route_dict(app, "/patched")) is None, (
        "adapter must decline while an external routing patch is live"
    )

    with TestClient(app) as client:
        assert client.get("/patched").json() == {"ok": True}
    assert calls["n"] >= 1, "delegated route build must run the external patch"


def test_patch_removed_restores_adapter_path(monkeypatch):
    """Detection is live state, not a latch: once the patch is gone,
    new route builds ride the adapter again."""
    orig = fa_routing.get_request_handler
    monkeypatch.setattr(
        fa_routing, "get_request_handler", lambda *a, **kw: orig(*a, **kw)
    )
    assert _sentry_compat.external_routing_patch_active() is True
    monkeypatch.setattr(fa_routing, "get_request_handler", orig)
    assert _sentry_compat.external_routing_patch_active() is False

    app = FastAPI()

    @app.get("/again")
    def again():
        return {"ok": True}

    assert app._adapter_route_info(_route_dict(app, "/again")) is not None
