"""WebSocket support matching FastAPI/Starlette's interface.

Architecture:
  Sync receive: crossbeam channel (blocking recv with GIL released)
  Async receive: ChannelAwaitable (custom awaitable backed by crossbeam — zero asyncio overhead)
  Send (both): direct tokio mpsc push (~100ns)
"""

from __future__ import annotations

import json
from typing import Any

from fastapi_rs.exceptions import WebSocketDisconnect


class WebSocket:
    """Wraps the Rust PyWebSocket for FastAPI/Starlette compatibility."""

    def __init__(self, _rust_ws=None, scope=None, receive=None, send=None):
        self._ws = _rust_ws
        self._accepted = False
        self._scope = scope or {}

    @property
    def scope(self) -> dict[str, Any]:
        return self._scope

    async def accept(self, subprotocol: str | None = None) -> None:
        self._accepted = True
        if self._ws is not None:
            self._ws.accept()

    async def send_text(self, data: str) -> None:
        self._ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        self._ws.send_bytes(data)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        text = json.dumps(data)
        if mode == "text":
            self._ws.send_text(text)
        else:
            self._ws.send_bytes(text.encode("utf-8"))

    async def receive_text(self) -> str:
        # Use ChannelAwaitable — blocks directly on crossbeam channel,
        # zero asyncio scheduling, zero pipe syscalls.
        # The __next__ method releases the GIL and blocks on the channel.
        try:
            return await self._ws.receive_text_async()
        except RuntimeError as e:
            raise WebSocketDisconnect(code=1000, reason=str(e)) from e

    async def receive_bytes(self) -> bytes:
        text = await self.receive_text()
        return text.encode("utf-8") if isinstance(text, str) else text

    async def receive_json(self, mode: str = "text") -> Any:
        if mode == "text":
            return json.loads(await self.receive_text())
        return json.loads(await self.receive_bytes())

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self._ws is not None:
            self._ws.close(code)

    # -- Async iterators --

    async def iter_text(self):
        """Async generator that yields text messages until disconnect."""
        while True:
            try:
                yield await self.receive_text()
            except (RuntimeError, WebSocketDisconnect):
                return

    async def iter_bytes(self):
        """Async generator that yields bytes messages until disconnect."""
        while True:
            try:
                yield await self.receive_bytes()
            except (RuntimeError, WebSocketDisconnect):
                return

    async def iter_json(self, mode: str = "text"):
        """Async generator that yields parsed JSON messages until disconnect."""
        while True:
            try:
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
