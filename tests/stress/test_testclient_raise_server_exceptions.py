"""``TestClient(raise_server_exceptions=False)`` must surface
unhandled errors as 500 responses — both in the real-HTTP path AND
when we're on the in-process ASGI shim. Upstream Starlette's
TestClient threads the flag to ``raise_app_exceptions``; ours
ignored it on the in-process path so every unhandled exception
propagated through as a raw Python exception and broke tests that
were supposed to assert on the 500 response."""
from __future__ import annotations

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_raise_false_in_process_returns_500():
    app = FastAPI()

    @app.get("/boom")
    def _b():
        raise ValueError("unhandled")

    with TestClient(app, in_process=True, raise_server_exceptions=False) as c:
        r = c.get("/boom")
        assert r.status_code == 500


def test_raise_true_in_process_propagates():
    app = FastAPI()

    @app.get("/boom")
    def _b():
        raise ValueError("unhandled")

    with TestClient(app, in_process=True, raise_server_exceptions=True) as c:
        with pytest.raises(ValueError, match="unhandled"):
            c.get("/boom")


def test_raise_false_auto_fallback_returns_500(monkeypatch):
    """Same contract when the in-process path is hit via the auto-
    fallback branch (bind failure)."""
    app = FastAPI()

    @app.get("/boom")
    def _b():
        raise ValueError("unhandled")

    def _boom(self):
        raise PermissionError("sandbox")

    monkeypatch.setattr(TestClient, "_find_free_port", _boom)

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
        assert r.status_code == 500
