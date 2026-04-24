"""In-process ASGI must resolve ``Depends(...)`` chains against the
matched endpoint — simple deps, nested deps, async deps, yield-deps
with teardown, and ``dependency_overrides``."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import Depends, FastAPI
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process fell back to the loopback proxy — dep graph "
            "didn't run in-process"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_simple_dep_resolves_in_process():
    app = FastAPI()

    def get_token():
        return "secret"

    @app.get("/me")
    def _me(token: str = Depends(get_token)):
        return {"token": token}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/me")
            assert r.status_code == 200
            assert r.json() == {"token": "secret"}

    _run(go())


def test_nested_dep_resolves_in_process():
    app = FastAPI()

    def get_db():
        return {"conn": 42}

    def get_user(db=Depends(get_db)):
        return {"id": 1, "db_conn": db["conn"]}

    @app.get("/u")
    def _u(user=Depends(get_user)):
        return user

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/u")
            assert r.status_code == 200
            assert r.json() == {"id": 1, "db_conn": 42}

    _run(go())


def test_async_dep_resolves_in_process():
    app = FastAPI()

    async def get_async_token():
        await asyncio.sleep(0)
        return "async-secret"

    @app.get("/a")
    async def _a(token: str = Depends(get_async_token)):
        return {"token": token}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/a")
            assert r.status_code == 200
            assert r.json() == {"token": "async-secret"}

    _run(go())


def test_yield_dep_teardown_runs_in_process():
    events: list[str] = []
    app = FastAPI()

    def db():
        events.append("open")
        try:
            yield {"conn": "live"}
        finally:
            events.append("close")

    @app.get("/q")
    def _q(conn=Depends(db)):
        events.append("handler")
        return {"got": conn["conn"]}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/q")
            assert r.status_code == 200

    _run(go())
    assert events == ["open", "handler", "close"], events


def test_dependency_overrides_apply_in_process():
    app = FastAPI()

    def real_token():
        return "real"

    @app.get("/t")
    def _t(tok: str = Depends(real_token)):
        return {"tok": tok}

    app.dependency_overrides[real_token] = lambda: "override"

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/t")
            assert r.status_code == 200
            assert r.json() == {"tok": "override"}

    _run(go())
