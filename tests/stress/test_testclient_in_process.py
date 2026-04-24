"""``TestClient(app, in_process=True)`` must dispatch via the in-
process ASGI path — no loopback socket bound. Matches Starlette's
TestClient semantics for sandboxed / hermetic / serverless envs.

Verification: block the loopback server-start path. If the TestClient
still works, we know in-process was used."""
from __future__ import annotations

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel


# Module-level so ``from __future__ import annotations`` doesn't
# stringify the field annotation to something pydantic can't resolve
# (would fail under upstream FA too — matches FA's known limitation).
class Payload(BaseModel):
    n: int


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    """Break the loopback-server path so any test that accidentally
    falls through will loudly fail rather than silently starting a
    socket-bound server."""

    async def _boom(self):
        raise RuntimeError("in_process=True but TestClient tried to start loopback")

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def test_in_process_get_returns_200():
    app = FastAPI()

    @app.get("/ping")
    def _ping():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.get("/ping")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


def test_in_process_missing_query_is_422():
    app = FastAPI()

    @app.get("/q")
    def _q(name: str = Query(...)):
        return {"name": name}

    with TestClient(app, in_process=True) as c:
        r = c.get("/q")
        assert r.status_code == 422


def test_env_var_flips_default_to_in_process(monkeypatch):
    monkeypatch.setenv("FASTAPI_TURBO_TESTCLIENT_IN_PROCESS", "1")
    app = FastAPI()

    @app.get("/p")
    def _p():
        return {"mode": "env"}

    with TestClient(app) as c:  # no explicit in_process=
        r = c.get("/p")
        assert r.status_code == 200
        assert r.json() == {"mode": "env"}


def test_post_body_roundtrips_in_process():
    app = FastAPI()

    @app.post("/add")
    def _add(p: Payload):
        return {"doubled": p.n * 2}

    with TestClient(app, in_process=True) as c:
        r = c.post("/add", json={"n": 21})
        assert r.status_code == 200
        assert r.json() == {"doubled": 42}
