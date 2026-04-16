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
from http.cookies import SimpleCookie
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


_VALID_SEND_JSON_MODES = ("text", "binary")


class WebSocket:
    """Wraps the Rust PyWebSocket for FastAPI/Starlette compatibility.

    API matches Starlette's WebSocket (which extends HTTPConnection):
      - await ws.accept() / ws.close()
      - await ws.receive() → ASGI dict
      - await ws.receive_text() / receive_bytes() / receive_json()
      - await ws.send_text() / send_bytes() / send_json()
      - ws.application_state / ws.client_state → WebSocketState
      - ws.scope / ws.headers / ws.url / ws.query_params / ws.path_params / ws.cookies / ws.client / ws.app
      - async for msg in ws.iter_text() / iter_bytes() / iter_json()
    """

    def __init__(self, _rust_ws=None, scope=None, receive=None, send=None):
        self._ws = _rust_ws
        # Lazily materialize the full scope dict on first access.
        # If user passed one explicitly (e.g., tests), prefer theirs.
        self._scope_override = scope
        self._scope_cache: dict | None = None
        self._receive = receive
        self._send = send
        # Track our own state additionally; Rust tracks the definitive state.
        self._app_state = WebSocketState.CONNECTING
        # Cached reference to the owning FastAPI app; not always set.
        self._app = None

    # ── Scope + HTTPConnection-like properties ─────────────────────

    @property
    def scope(self) -> dict[str, Any]:
        """ASGI scope dict. Lazily materialized from the Rust-side data."""
        if self._scope_cache is not None:
            return self._scope_cache
        if self._scope_override is not None:
            self._scope_cache = dict(self._scope_override)
            return self._scope_cache
        if self._ws is not None:
            try:
                self._scope_cache = dict(self._ws.get_scope_dict())
                return self._scope_cache
            except Exception:
                pass
        self._scope_cache = {"type": "websocket"}
        return self._scope_cache

    @property
    def headers(self):
        """Case-insensitive headers view (Starlette-compatible Headers)."""
        from fastapi_rs.datastructures import Headers

        raw = self.scope.get("headers", [])
        # ASGI headers are list[tuple[bytes, bytes]]; Headers class handles it.
        if raw and isinstance(raw, list) and raw and isinstance(raw[0], tuple):
            if isinstance(raw[0][0], (bytes, bytearray)):
                # Convert to str pairs for the Headers class
                raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in raw]
        return Headers(raw)

    @property
    def url(self):
        """WebSocket URL (Starlette-compatible URL)."""
        from fastapi_rs.datastructures import URL

        return URL(self.scope)

    @property
    def base_url(self):
        """Base URL — scheme + host only."""
        from fastapi_rs.datastructures import URL

        scope = dict(self.scope)
        scope["path"] = "/"
        scope["query_string"] = b""
        return URL(scope)

    @property
    def query_params(self):
        """QueryParams view of the query string."""
        from fastapi_rs.datastructures import QueryParams

        qs = self.scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs = qs.decode("latin-1")
        return QueryParams(qs)

    @property
    def path_params(self) -> dict[str, Any]:
        """Path parameters extracted from the URL pattern."""
        return self.scope.get("path_params", {}) or {}

    @property
    def cookies(self) -> dict[str, str]:
        """Parsed cookie dict from the Cookie header."""
        headers = self.headers
        cookie_header = headers.get("cookie", "") if hasattr(headers, "get") else ""
        if not cookie_header:
            return {}
        sc = SimpleCookie()
        sc.load(cookie_header)
        return {k: morsel.value for k, morsel in sc.items()}

    @property
    def client(self):
        """Client address as an (host, port) tuple-like Address."""
        from fastapi_rs.datastructures import Address

        c = self.scope.get("client")
        if c is None:
            return Address(("0.0.0.0", 0))
        return Address(c)

    @property
    def app(self):
        """Owning FastAPI app (if set — populated when routing dispatches)."""
        return self._app or self.scope.get("app")

    @app.setter
    def app(self, value):
        self._app = value

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
        """Accept the WebSocket upgrade.

        Note: subprotocol / headers negotiation happens at the Axum upgrade
        layer BEFORE this Python method runs, so passing them here is
        accepted for API compatibility but has limited effect today. A future
        phase will plumb these through the upgrade handshake.
        """
        if self._app_state != WebSocketState.CONNECTING:
            # Starlette tolerates double-accept; we mirror that to avoid breaking apps.
            pass
        self._app_state = WebSocketState.CONNECTED
        if self._ws is not None:
            self._ws.accept()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        """Close the WebSocket and wait for the frame to flush.

        Uses the Rust-side close_and_wait() so the caller is guaranteed the
        close frame has been handed to the underlying sink before returning.
        """
        if self._app_state == WebSocketState.DISCONNECTED:
            return
        self._app_state = WebSocketState.DISCONNECTED
        if self._ws is not None:
            try:
                # close_and_wait returns a CloseAwaitable — await it for flush.
                await self._ws.close_and_wait(code, reason or "")
            except Exception:
                # Fall back to non-waiting close if close_and_wait fails
                try:
                    self._ws.close(code, reason or "")
                except Exception:
                    pass

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
            if self._app_state != WebSocketState.CONNECTED:
                raise RuntimeError(
                    'Cannot call "send" once a close message has been sent or before accept.'
                )
            if message.get("text") is not None:
                self._ws.send_text(message["text"])
            elif message.get("bytes") is not None:
                self._ws.send_bytes(bytes(message["bytes"]))
        elif msg_type == "websocket.close":
            await self.close(code=message.get("code", 1000), reason=message.get("reason"))

    async def send_text(self, data: str) -> None:
        if self._app_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                'Cannot call "send_text" before "accept" or after a close.'
            )
        self._ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        if self._app_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                'Cannot call "send_bytes" before "accept" or after a close.'
            )
        # Accept bytes/bytearray/memoryview — Rust side expects PyBytes.
        if not isinstance(data, bytes):
            data = bytes(data)
        self._ws.send_bytes(data)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        if mode not in _VALID_SEND_JSON_MODES:
            raise RuntimeError('The "mode" argument should be "text" or "binary".')
        if self._app_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                'Cannot call "send_json" before "accept" or after a close.'
            )
        # Starlette-compatible serialization: no whitespace, no ASCII escapes.
        # Matters for HMAC-signed payloads and byte-count-sensitive consumers.
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
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
        if self._app_state == WebSocketState.DISCONNECTED:
            raise RuntimeError(
                'Cannot call "receive" once a disconnect has been received.'
            )
        try:
            msg = await self._ws.receive_async()
        except RuntimeError as e:
            self._app_state = WebSocketState.DISCONNECTED
            return {"type": "websocket.disconnect", "code": 1000, "reason": str(e)}
        if msg.get("type") == "websocket.disconnect":
            self._app_state = WebSocketState.DISCONNECTED
        return msg

    async def receive_text(self) -> str:
        """Fast path: returns str directly, no dict allocation.

        Propagates the actual Close code and reason from the peer as fields on
        WebSocketDisconnect — matches Starlette.
        """
        if self._app_state == WebSocketState.DISCONNECTED:
            raise WebSocketDisconnect(code=1000, reason="already disconnected")
        try:
            return await self._ws.receive_text_async()
        except RuntimeError as e:
            # The Rust side lost track of the real close code when it
            # converted it to a RuntimeError. Re-check by peeking at receive()
            # for the actual disconnect dict — but that would consume another
            # message. For now propagate 1000, and use receive() if you need
            # the exact code.
            self._app_state = WebSocketState.DISCONNECTED
            code, reason = _extract_close_info_from_error(str(e))
            raise WebSocketDisconnect(code=code, reason=reason) from e

    async def receive_bytes(self) -> bytes:
        """Fast path: returns bytes directly, no dict allocation."""
        if self._app_state == WebSocketState.DISCONNECTED:
            raise WebSocketDisconnect(code=1000, reason="already disconnected")
        try:
            return await self._ws.receive_bytes_async()
        except RuntimeError as e:
            self._app_state = WebSocketState.DISCONNECTED
            code, reason = _extract_close_info_from_error(str(e))
            raise WebSocketDisconnect(code=code, reason=reason) from e

    async def receive_json(self, mode: str = "text") -> Any:
        if mode not in _VALID_SEND_JSON_MODES:
            raise RuntimeError('The "mode" argument should be "text" or "binary".')
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


def _extract_close_info_from_error(msg: str) -> tuple[int, str]:
    """Parse the Rust-side RuntimeError to recover close code + reason.

    The fast-path awaitables format close errors as 'WS_CLOSED:<code>:<reason>'
    so we can preserve the peer's actual close code in WebSocketDisconnect.
    """
    if msg.startswith("WS_CLOSED:"):
        rest = msg[len("WS_CLOSED:"):]
        code_str, _, reason = rest.partition(":")
        try:
            return int(code_str), reason
        except ValueError:
            pass
    return 1000, msg
