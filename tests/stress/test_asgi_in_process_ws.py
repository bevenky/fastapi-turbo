"""In-process WebSocket dispatch via httpx-style ASGITransport.

The ASGI contract for WebSockets is:
  * client sends ``{"type": "websocket.connect"}``
  * server responds with ``{"type": "websocket.accept"}`` or ``.close``
  * bidirectional exchange of ``.send`` / ``.receive`` text/bytes messages
  * client ``.disconnect`` terminates the scope

Our in-process dispatcher must route ``scope["type"] == "websocket"``
to the matching user WS endpoint, constructing a WebSocket object
that bridges the ASGI receive/send channels — no loopback socket
needed, so it works in sandboxed / serverless environments."""
from __future__ import annotations

import asyncio

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, WebSocket


@pytest.fixture(autouse=True)
def _block_loopback(monkeypatch):
    async def _boom(self):
        raise RuntimeError(
            "in-process fell back to the loopback proxy — WS dispatch "
            "didn't run in-process"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


def _ws_scope(path="/ws"):
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": [(b"host", b"t")],
        "subprotocols": [],
        "client": ("127.0.0.1", 9999),
        "server": ("t", None),
        "root_path": "",
    }


async def _drive_ws(app, *, client_messages):
    """Drive one WS connection against ``app`` via the ASGI contract,
    collecting every message the server sends. ``client_messages``
    is a list of ``receive``-side messages fed in order."""
    received: list[dict] = []
    queue = list(client_messages)

    async def receive():
        if not queue:
            return {"type": "websocket.disconnect", "code": 1000}
        return queue.pop(0)

    async def send(msg):
        received.append(msg)

    await app(_ws_scope(), receive, send)
    return received


def test_in_process_ws_echo():
    app = FastAPI()

    @app.websocket("/ws")
    async def _echo(ws: WebSocket):
        await ws.accept()
        for _ in range(3):
            msg = await ws.receive_text()
            await ws.send_text(msg.upper())

    client = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "hello"},
        {"type": "websocket.receive", "text": "world"},
        {"type": "websocket.receive", "text": "ws"},
    ]
    msgs = _run(_drive_ws(app, client_messages=client))
    # Expect: accept, then 3 text responses.
    types = [m.get("type") for m in msgs]
    assert "websocket.accept" in types, types
    texts = [m.get("text") for m in msgs if m.get("type") == "websocket.send"]
    assert texts == ["HELLO", "WORLD", "WS"], texts


def test_in_process_ws_with_path_param():
    app = FastAPI()

    @app.websocket("/rooms/{room_id}")
    async def _ws(ws: WebSocket, room_id: str):
        await ws.accept()
        await ws.send_text(f"room:{room_id}")

    # Drive against /rooms/lobby; path param should flow through.
    received: list[dict] = []
    queue = [
        {"type": "websocket.connect"},
        {"type": "websocket.disconnect", "code": 1000},
    ]

    async def receive():
        return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1000}

    async def send(msg):
        received.append(msg)

    scope = dict(_ws_scope("/rooms/lobby"))
    _run(app(scope, receive, send))
    types = [m.get("type") for m in received]
    assert "websocket.accept" in types
    texts = [m.get("text") for m in received if m.get("type") == "websocket.send"]
    assert texts == ["room:lobby"], texts
