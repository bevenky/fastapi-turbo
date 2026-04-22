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


# ── Minimal awaitable wrappers ──────────────────────────────────────
#
# The Rust side exposes already-resolved awaitables for send ops and
# crossbeam-backed awaitables for receive ops. Wrapping them in
# `async def` adds ~3-5 μs of coroutine overhead PER call — significant
# for tight WebSocket loops that do 2 awaits per message (receive + send).
#
# These class-based awaitables skip the coroutine-alloc dance entirely
# while still letting Python translate low-level Rust RuntimeError into
# Starlette-compatible WebSocketDisconnect.


class _ImmediateNone:
    """Resolves synchronously to None on first __next__ — for fire-and-forget
    send ops where the Rust side queues the frame and returns immediately."""
    __slots__ = ()

    def __await__(self):
        return self
    def __iter__(self):
        return self
    def __next__(self):
        raise StopIteration(None)


_IMMEDIATE_NONE = _ImmediateNone()


class _RecvAwaitable:
    """Wraps a Rust awaitable; translates its RuntimeError into
    WebSocketDisconnect on the way out, and flips self._ws._app_state."""
    __slots__ = ("_inner", "_ws")

    def __init__(self, inner, ws):
        self._inner = inner
        self._ws = ws

    def __await__(self):
        try:
            return (yield from self._inner.__await__())
        except RuntimeError as e:
            self._ws._app_state = WebSocketState.DISCONNECTED
            code, reason = _extract_close_info_from_error(str(e))
            raise WebSocketDisconnect(code=code, reason=reason) from e


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
    def state(self):
        """Per-connection state namespace (Starlette-compatible)."""
        if not hasattr(self, '_state'):
            from fastapi_rs.datastructures import State
            self._state = State()
        return self._state

    async def send_denial_response(self, response) -> None:
        """Send an HTTP response to reject a WebSocket upgrade.

        Stub -- our Rust layer handles upgrade rejection before the
        Python handler is invoked, so this is a no-op for API compat.
        """
        pass

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

        Uses deferred-upgrade: the actual HTTP 101 upgrade happens when this
        method is called, not when the route handler starts. subprotocol is
        negotiated via axum's `WebSocketUpgrade.protocols(...)` — the chosen
        subprotocol appears in the response's Sec-WebSocket-Protocol header.

        headers: list of (bytes, bytes) or (str, str) — emitted on the
        handshake 101 response. Typical use is setting Set-Cookie during
        upgrade. Multiple Set-Cookie entries are preserved as duplicates.
        """
        if self._app_state != WebSocketState.CONNECTING:
            # Starlette tolerates double-accept; we mirror that.
            return
        if self._ws is not None:
            # Convert headers from [(bytes, bytes)] to [(str, str)] for PyO3.
            rust_headers: list[tuple[str, str]] = []
            if headers:
                for k, v in headers:
                    k_s = k.decode("latin-1") if isinstance(k, (bytes, bytearray)) else str(k)
                    v_s = v.decode("latin-1") if isinstance(v, (bytes, bytearray)) else str(v)
                    rust_headers.append((k_s, v_s))
            self._ws.accept(subprotocol, rust_headers if rust_headers else None)
        self._app_state = WebSocketState.CONNECTED

    def _reject(self, status: int = 403) -> None:
        """Abort the upgrade BEFORE any ``accept()``. Starlette's path for
        pre-accept ``WebSocketException``: the handshake HTTP response
        becomes an error status (defaults to 403) and no WS frames are
        sent. Safe to call at most once; subsequent calls no-op because
        the underlying oneshot channel has already fired.
        """
        self._app_state = WebSocketState.DISCONNECTED
        if self._ws is not None:
            try:
                self._ws.reject(status)
            except Exception:
                pass

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        """Close the WebSocket and wait for the frame to flush.

        Uses the Rust-side close_and_wait() so the caller is guaranteed the
        close frame has been handed to the underlying sink before returning.

        Starlette parity: ``close()`` called BEFORE ``accept()`` accepts
        the handshake first, then sends the close frame with the given
        code — otherwise the queued close message would never flush
        because the writer task isn't running yet.
        """
        if self._app_state == WebSocketState.DISCONNECTED:
            return
        # Remember the close code/reason so the exception-handler code
        # path can surface them to the TestClient capture queue.
        self._last_close_code = code
        self._last_close_reason = reason or ""
        pre_accept = self._app_state == WebSocketState.CONNECTING
        if pre_accept and self._ws is not None:
            # Accept the upgrade first so the writer task starts.
            try:
                self._ws.accept(None, None)
                self._app_state = WebSocketState.CONNECTED
            except Exception:  # noqa: BLE001
                pass
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

    # NOTE: send_* methods are sync but return an awaitable so
    # `await ws.send_text(x)` keeps working. Skipping the `async def`
    # wrapper saves ~3-5 μs of coroutine allocation per call.

    def send_text(self, data: str):
        if self._app_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                'Cannot call "send_text" before "accept" or after a close.'
            )
        self._ws.send_text(data)
        return _IMMEDIATE_NONE

    def send_bytes(self, data):
        if self._app_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                'Cannot call "send_bytes" before "accept" or after a close.'
            )
        if not isinstance(data, bytes):
            data = bytes(data)
        self._ws.send_bytes(data)
        return _IMMEDIATE_NONE

    def send_json(self, data, mode: str = "text"):
        if mode not in _VALID_SEND_JSON_MODES:
            raise RuntimeError('The "mode" argument should be "text" or "binary".')
        if self._app_state != WebSocketState.CONNECTED:
            raise RuntimeError(
                'Cannot call "send_json" before "accept" or after a close.'
            )
        text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if mode == "text":
            self._ws.send_text(text)
        else:
            self._ws.send_bytes(text.encode("utf-8"))
        return _IMMEDIATE_NONE

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

    def receive_text(self):
        """Returns the Rust TextAwaitable directly — zero Python wrapping.

        On close, the Rust side raises `WebSocketDisconnect(code, reason)`
        directly, so we don't need a Python try/except translation layer.
        """
        if self._app_state == WebSocketState.DISCONNECTED:
            raise WebSocketDisconnect(code=1000, reason="already disconnected")
        return self._ws.receive_text_async()

    def receive_bytes(self):
        """Returns the Rust BytesAwaitable directly."""
        if self._app_state == WebSocketState.DISCONNECTED:
            raise WebSocketDisconnect(code=1000, reason="already disconnected")
        return self._ws.receive_bytes_async()

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
