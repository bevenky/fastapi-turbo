"""In-process dispatch must recurse into ``app.mount('/sub', subapp)``
sub-applications without binding a loopback socket. Previously the
in-process path bailed and fell back to the proxy for anything that
didn't match a top-level route, so a mounted sub-app only worked
when loopback was available."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process path fell back to the loopback proxy — mount "
            "recursion didn't happen in-process"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_mounted_subapp_dispatches_in_process():
    sub = FastAPI()

    @sub.get("/hello")
    def _hello():
        return {"from": "sub"}

    app = FastAPI()
    app.mount("/v1", sub)

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/v1/hello")
            assert r.status_code == 200
            assert r.json() == {"from": "sub"}

    _run(go())


def test_mounted_subapp_with_path_param_dispatches_in_process():
    sub = FastAPI()

    @sub.get("/users/{uid}")
    def _u(uid: int):
        return {"uid": uid}

    app = FastAPI()
    app.mount("/api", sub)

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/api/users/42")
            assert r.status_code == 200
            assert r.json() == {"uid": 42}

    _run(go())


def test_top_level_route_still_wins_over_mount_prefix_match():
    """A top-level ``/status`` route must win even when a sub-app
    mounted at ``/`` also declares one (prefix-longer-first is wrong
    when top-level is a literal match)."""
    sub = FastAPI()

    @sub.get("/status")
    def _sub_status():
        return {"from": "sub"}

    app = FastAPI()

    @app.get("/status")
    def _top_status():
        return {"from": "top"}

    app.mount("/v1", sub)

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            # /status → top wins
            r = await cli.get("/status")
            assert r.json() == {"from": "top"}
            # /v1/status → sub wins
            r2 = await cli.get("/v1/status")
            assert r2.json() == {"from": "sub"}

    _run(go())
