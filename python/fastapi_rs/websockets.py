"""WebSocket support matching FastAPI/Starlette's interface.

Architecture:
  Sync receive: crossbeam channel (blocking recv with GIL released)
  Async receive: ChannelAwaitable (custom awaitable backed by crossbeam — zero asyncio overhead)
  Binary: preserved end-to-end (no UTF-8 coercion, zero-copy via Bytes)
  State: tracked via atomic u8 (matches Starlette's WebSocketState enum)
"""

from __future__ import annotations

import enum
import json
from typing import Any

from fastapi_rs.exceptions import WebSocketDisconnect


class WebSocketState(enum.IntEnum):
    """WebSocket connection state (matches Starlette exactly).

    Values must match the u8 constants in src/websocket.rs.
    """

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    RESPONSE = 3


class WebSocket:
    """Wraps the Rust PyWebSocket for FastAPI/Starlette compatibility.

    API matches Starlette's WebSocket class:
      - await ws.accept() / ws.close()
      - await ws.receive() → ASGI dict {"type": ..., "text"/"bytes": ...}
      - await ws.receive_text() / receive_bytes() / receive_json()
      - await ws.send_text() / send_bytes() / send_json()
      - ws.application_state / ws.client_state → WebSocketState
      - async for msg in ws.iter_text() / iter_bytes() / iter_json()
    """

    def __init__(self, _rust_ws=None, scope=None, receive=None, send=None):
        self._ws = _rust_ws
        self._scope = scope or {}
        # Track our own state additionally; Rust also tracks in _ws for auth across PyO3.
        self._app_state = WebSocketState.CONNECTING

    # ── Properties ─────────────────────────────────────────────────

    @property
    def scope(self) -> dict[str, Any]:
        return self._scope

    @property
    def application_state(self) -> WebSocketState:
        """Server-side WebSocket state (matches Starlette)."""
        if self._ws is not None:
            try:
                return WebSocketState(self._ws.get_application_state())
            except Exception:
                pass
        return self._app_state

    @property
    def client_state(self) -> WebSocketState:
        """Client-side WebSocket state (matches Starlette)."""
        if self._ws is not None:
            try:
                return WebSocketState(self._ws.get_client_state())
            except Exception:
                pass
        return self._app_state

    # ── Lifecycle ─────────────────────────────────────────────────

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: list | None = None,
    ) -> None:
        self._app_state = WebSocketState.CONNECTED
        if self._ws is not None:
            self._ws.accept()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self._app_state = WebSocketState.DISCONNECTED
        if self._ws is not None:
            self._ws.close(code)

    # ── Send ──────────────────────────────────────────────────────

    async def send(self, message: dict) -> None:
        """Low-level ASGI send. Matches Starlette's WebSocket.send()."""
        msg_type = message.get("type", "")
        if msg_type == "websocket.accept":
            await self.accept(
                subprotocol=message.get("subprotocol"),
                headers=message.get("headers"),
            )
        elif msg_type == "websocket.send":
            if message.get("text") is not None:
                await self.send_text(message["text"])
            elif message.get("bytes") is not None:
                await self.send_bytes(message["bytes"])
        elif msg_type == "websocket.close":
            await self.close(code=message.get("code", 1000), reason=message.get("reason"))

    async def send_text(self, data: str) -> None:
        self._ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        # Accept bytes/bytearray/memoryview — Rust side expects PyBytes.
        if not isinstance(data, bytes):
            data = bytes(data)
        self._ws.send_bytes(data)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        text = json.dumps(data)
        if mode == "text":
            self._ws.send_text(text)
        else:
            self._ws.send_bytes(text.encode("utf-8"))

    # ── Receive ───────────────────────────────────────────────────

    async def receive(self) -> dict:
        """Low-level ASGI receive (Starlette-compatible).

        Returns:
            {"type": "websocket.receive", "text": str} for text frames
            {"type": "websocket.receive", "bytes": bytes} for binary frames
            {"type": "websocket.disconnect", "code": int, "reason": str} on close
        """
        try:
            msg = await self._ws.receive_async()
        except RuntimeError as e:
            self._app_state = WebSocketState.DISCONNECTED
            return {"type": "websocket.disconnect", "code": 1000, "reason": str(e)}
        if msg.get("type") == "websocket.disconnect":
            self._app_state = WebSocketState.DISCONNECTED
        return msg

    async def receive_text(self) -> str:
        """Fast path: returns str directly, no dict allocation."""
        try:
            return await self._ws.receive_text_async()
        except RuntimeError as e:
            self._app_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(code=1000, reason=str(e)) from e

    async def receive_bytes(self) -> bytes:
        """Fast path: returns bytes directly, no dict allocation."""
        try:
            return await self._ws.receive_bytes_async()
        except RuntimeError as e:
            self._app_state = WebSocketState.DISCONNECTED
            raise WebSocketDisconnect(code=1000, reason=str(e)) from e

    async def receive_json(self, mode: str = "text") -> Any:
        if mode == "text":
            return json.loads(await self.receive_text())
        return json.loads(await self.receive_bytes())

    # ── Async iterators ───────────────────────────────────────────

    async def iter_text(self):
        """Async generator yielding text messages until disconnect."""
        try:
            while True:
                yield await self.receive_text()
        except (RuntimeError, WebSocketDisconnect):
            return

    async def iter_bytes(self):
        """Async generator yielding bytes messages until disconnect."""
        try:
            while True:
                yield await self.receive_bytes()
        except (RuntimeError, WebSocketDisconnect):
            return

    async def iter_json(self, mode: str = "text"):
        """Async generator yielding parsed JSON messages until disconnect."""
        try:
            while True:
                yield await self.receive_json(mode=mode)
        except (RuntimeError, WebSocketDisconnect):
            return

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await self.receive_text()
        except (RuntimeError, WebSocketDisconnect):
            raise StopAsyncIteration
