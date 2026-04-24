"""Explicit ``in_process=False`` must DISABLE auto-fallback.

Users who pass it are saying "I want to test the Rust/Tower path
specifically — a bind failure should SURFACE, not silently switch
to the Python ASGI path behind my back." Default (``None``) still
auto-falls-back for sandbox-friendliness."""
from __future__ import annotations

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_explicit_false_surfaces_bind_failure(monkeypatch):
    app = FastAPI()

    @app.get("/p")
    def _p():
        return {}

    def _boom(self):
        raise PermissionError("[Errno 1] Operation not permitted")

    monkeypatch.setattr(TestClient, "_find_free_port", _boom)

    # Explicit False: bind failure must raise, NOT fall back.
    with pytest.raises((PermissionError, OSError)):
        with TestClient(app, in_process=False) as c:
            c.get("/p")


def test_default_none_still_auto_falls_back(monkeypatch):
    """Regression for the auto-fallback path — untouched by the
    explicit-False fix."""
    app = FastAPI()

    @app.get("/p")
    def _p():
        return {"ok": True}

    def _boom(self):
        raise PermissionError("[Errno 1] Operation not permitted")

    monkeypatch.setattr(TestClient, "_find_free_port", _boom)

    # No explicit in_process → auto-fallback to in-process.
    with TestClient(app) as c:
        r = c.get("/p")
        assert r.status_code == 200
        assert c._in_process is True
