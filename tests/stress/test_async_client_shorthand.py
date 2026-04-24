"""Regression: AsyncClient + ASGITransport must be importable from
``fastapi.testclient`` — matches FastAPI's recommended async-testing
recipe."""
from __future__ import annotations

import asyncio

import fastapi_turbo  # noqa: F401


def test_async_client_shorthand_from_fastapi_testclient():
    from fastapi.testclient import AsyncClient, ASGITransport
    import httpx

    assert AsyncClient is httpx.AsyncClient
    assert ASGITransport is httpx.ASGITransport


def test_async_client_end_to_end():
    from fastapi import FastAPI
    from fastapi.testclient import AsyncClient, ASGITransport

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    async def _run():
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://t",
        ) as cli:
            r = await cli.get("/ping")
            assert r.status_code == 200
            assert r.json() == {"ok": True}

    asyncio.run(_run())
