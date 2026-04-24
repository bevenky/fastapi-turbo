"""Regression: async WebSocket handler that yields BEFORE any websocket
operation must still run correctly.

``drive_coroutine_on_local_loop`` previously probed the coroutine with
``send(None)``, and when the coro suspended (e.g. on ``asyncio.sleep(0)``)
it called ``coro.close()`` then submitted the SAME (now-closed) coro
to the async worker. That produced a 500 even though the handler was
otherwise valid.
"""
from __future__ import annotations

import asyncio

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient


def test_ws_handler_suspends_before_accept():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        # Force a real asyncio suspension BEFORE the first websocket op —
        # previously triggered the close-then-resubmit bug.
        await asyncio.sleep(0)
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        msg = ws.receive_text()
        assert msg == "hello"


def test_ws_handler_multiple_awaits_before_accept():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        for _ in range(3):
            await asyncio.sleep(0)
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_text() == "ok"


def test_ws_handler_with_event_wait():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        # asyncio.sleep with non-zero delay forces a real timer suspension
        # (different code path in asyncio than sleep(0)'s yield).
        await asyncio.sleep(0.001)
        await websocket.accept()
        await websocket.send_text("after-event")
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_text() == "after-event"
