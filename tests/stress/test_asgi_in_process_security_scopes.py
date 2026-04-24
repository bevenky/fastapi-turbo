"""In-process ASGI must populate ``SecurityScopes`` with the scopes
accumulated along the dep chain's ``Security()`` markers.

FastAPI semantics: every ``Security(dep, scopes=[...])`` along the
resolution path contributes to the final ``SecurityScopes.scopes``
list seen by the innermost dep. This is how OAuth2 authorisation
is implemented against an endpoint's `required_scopes` set.

Before this fix, ``Security`` resolved the callable but left
``SecurityScopes`` empty, so an OAuth2 endpoint dispatched via
ASGITransport couldn't enforce its scope policy."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Security
from fastapi.security import SecurityScopes
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process fell back to loopback proxy — SecurityScopes test"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def test_security_scopes_populated_from_single_security_marker():
    app = FastAPI()

    def get_user(ss: SecurityScopes):
        return {"scopes": list(ss.scopes)}

    @app.get("/me")
    def _me(user=Security(get_user, scopes=["me", "items:read"])):
        return user

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/me")
            assert r.status_code == 200
            assert sorted(r.json()["scopes"]) == ["items:read", "me"]

    _run(go())


def test_security_scopes_accumulate_across_chain():
    """``Security`` markers at multiple levels contribute scopes."""
    app = FastAPI()

    def inner(ss: SecurityScopes):
        return {"scopes": list(ss.scopes)}

    def outer(v=Security(inner, scopes=["inner:read"])):
        return v

    @app.get("/deep")
    def _deep(v=Security(outer, scopes=["outer:admin"])):
        return v

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/deep")
            assert r.status_code == 200
            # Innermost dep sees scopes collected along the full path.
            collected = set(r.json()["scopes"])
            assert {"inner:read", "outer:admin"}.issubset(collected), collected

    _run(go())


def test_no_security_markers_gives_empty_scopes():
    app = FastAPI()

    def _user(ss: SecurityScopes):
        return {"scopes": list(ss.scopes)}

    @app.get("/plain")
    def _p(user=Security(_user)):
        return user

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/plain")
            assert r.status_code == 200
            assert r.json()["scopes"] == []

    _run(go())
