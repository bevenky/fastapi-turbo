"""In-process ASGI must apply ``response_model`` filtering /
exclude-unset / by_alias / include / exclude the same way the
Rust hot path does.

Previously the in-process dispatcher fed the endpoint's return
value straight to ``jsonable_encoder`` — extra keys, unset
defaults, and non-aliased field names all leaked to the client."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import AsyncClient, ASGITransport
from pydantic import BaseModel, Field


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError("fell back to loopback — response_model test")

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


class PublicUser(BaseModel):
    id: int
    name: str


def test_response_model_filters_extra_keys():
    """Extra keys in the handler's dict return must be stripped."""
    app = FastAPI()

    @app.get("/u", response_model=PublicUser)
    def _u():
        return {"id": 1, "name": "alice", "password": "LEAKED"}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/u")
            assert r.status_code == 200
            assert r.json() == {"id": 1, "name": "alice"}

    _run(go())


def test_response_model_with_aliases_emits_alias_names():
    class UserWithAlias(BaseModel):
        user_id: int = Field(alias="userId")
        name: str

    app = FastAPI()

    @app.get("/a", response_model=UserWithAlias, response_model_by_alias=True)
    def _a():
        return {"userId": 7, "name": "bob"}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/a")
            assert r.status_code == 200
            body = r.json()
            assert "userId" in body
            assert body["userId"] == 7


    _run(go())


def test_response_model_exclude_unset_drops_defaulted_fields():
    class U(BaseModel):
        id: int
        nickname: str | None = None

    app = FastAPI()

    @app.get("/e", response_model=U, response_model_exclude_unset=True)
    def _e():
        # Only ``id`` set; ``nickname`` has a default and should be dropped.
        return U(id=42)

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/e")
            assert r.status_code == 200
            assert r.json() == {"id": 42}

    _run(go())
