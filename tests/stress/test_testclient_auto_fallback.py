"""TestClient must auto-fallback to in-process ASGI when the
loopback bind fails (sandboxed envs, serverless containers, missing
CAP_NET_BIND_SERVICE, etc.). Previously it raised PermissionError
and broke user test suites that worked under Starlette's TestClient."""
from __future__ import annotations

import socket

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_fallback_when_bind_fails(monkeypatch):
    """Simulate a sandbox by making ``_find_free_port`` raise
    PermissionError — exactly the error CoreOS / certain Docker
    runtimes emit when bind(AF_INET) is denied."""
    app = FastAPI()

    @app.get("/p")
    def _p():
        return {"ok": True}

    def _boom(self):
        raise PermissionError("[Errno 1] Operation not permitted")

    monkeypatch.setattr(TestClient, "_find_free_port", _boom)

    # Without auto-fallback the bind error would bubble out here.
    with TestClient(app) as c:
        r = c.get("/p")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        # Confirm we're on the in-process path (not real HTTP).
        assert c._port is None
        assert c._in_process is True


def test_fallback_handles_oserror_too(monkeypatch):
    """Non-permission bind failures (exhausted ephemeral ports,
    a seccomp filter stripping socket()) also fall through."""
    app = FastAPI()

    @app.get("/q")
    def _q():
        return {"v": 1}

    def _boom(self):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(TestClient, "_find_free_port", _boom)

    with TestClient(app) as c:
        r = c.get("/q")
        assert r.status_code == 200
        assert r.json() == {"v": 1}
