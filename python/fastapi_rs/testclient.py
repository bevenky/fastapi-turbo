"""TestClient for testing fastapi-rs/FastAPI applications.

Starts the Rust-backed server in a background thread and provides
an httpx-based client for making real HTTP requests.

Usage::

    from fastapi_rs import FastAPI
    from fastapi_rs.testclient import TestClient

    app = FastAPI()

    @app.get("/hello")
    def hello():
        return {"message": "hello"}

    with TestClient(app) as client:
        r = client.get("/hello")
        assert r.status_code == 200

    # Async usage:
    async with AsyncTestClient(app) as client:
        r = await client.get("/hello")
        assert r.status_code == 200
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx


class TestClient:
    """HTTP test client that launches a real fastapi-rs server in a background thread."""

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
    ):
        self.app = app
        self._base_url = base_url
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._raise_server_exceptions = raise_server_exceptions

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def __enter__(self) -> TestClient:
        self._port = self._find_free_port()
        self._thread = threading.Thread(
            target=self.app.run,
            kwargs={"host": "127.0.0.1", "port": self._port},
            daemon=True,
        )
        self._thread.start()

        # Wait for server to be ready
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError("TestClient: server did not start within 10 seconds")

        self._client = httpx.Client(base_url=f"http://127.0.0.1:{self._port}")
        return self

    def __exit__(self, *args: Any) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ── HTTP verb shortcuts ────────────────────────────────────────

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.put(url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.delete(url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.patch(url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.options(url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.head(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "TestClient must be used as a context manager"
        return self._client.request(method, url, **kwargs)

    # ── WebSocket ──────────────────────────────────────────────────
    #
    # NOTE: `TestClient.websocket_connect` runs the WS client in a thread
    # in the SAME Python process as the server, which hits a GIL/event-loop
    # deadlock in our current setup. For now, WebSocket tests should use
    # the subprocess pattern (see tests/test_websocket.py `server_app`
    # fixture) or the `WebSocketTestSession` helper below, which launches
    # the app in a subprocess.

    def websocket_connect(
        self,
        url: str,
        subprotocols: list[str] | None = None,
        **kwargs: Any,
    ) -> "_WebSocketTestSession":
        """Open a WebSocket connection to the running server.

        WARNING: in-process WS has a known deadlock under some conditions.
        For reliable WS testing, use the `WebSocketTestSession.from_code()`
        helper (launches a subprocess).
        """
        assert self._port is not None, "TestClient must be used as a context manager"
        ws_url = f"ws://127.0.0.1:{self._port}{url}"
        return _WebSocketTestSession(ws_url, subprotocols=subprotocols, **kwargs)


class _WebSocketTestSession:
    """Synchronous Starlette-compatible test WebSocket session."""

    def __init__(self, url: str, subprotocols: list[str] | None = None, **_: Any):
        self._url = url
        self._subprotocols = subprotocols
        self._ws = None
        from websockets.sync.client import connect as _connect  # noqa: E402
        self._connect = _connect

    def __enter__(self) -> "_WebSocketTestSession":
        kw: dict = {}
        if self._subprotocols:
            kw["subprotocols"] = self._subprotocols
        self._ws = self._connect(self._url, **kw)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # ── Send ───────────────────────────────────────────────────────

    def send_text(self, data: str) -> None:
        assert self._ws is not None
        self._ws.send(data)

    def send_bytes(self, data: bytes) -> None:
        assert self._ws is not None
        self._ws.send(data)

    def send_json(self, data: Any, mode: str = "text") -> None:
        import json as _json
        payload = _json.dumps(data)
        if mode == "binary":
            self.send_bytes(payload.encode("utf-8"))
        else:
            self.send_text(payload)

    # ── Receive ────────────────────────────────────────────────────

    def receive_text(self) -> str:
        assert self._ws is not None
        msg = self._ws.recv()
        if isinstance(msg, bytes):
            return msg.decode("utf-8")
        return msg

    def receive_bytes(self) -> bytes:
        assert self._ws is not None
        msg = self._ws.recv()
        if isinstance(msg, str):
            return msg.encode("utf-8")
        return msg

    def receive_json(self, mode: str = "text") -> Any:
        import json as _json
        if mode == "binary":
            return _json.loads(self.receive_bytes().decode("utf-8"))
        return _json.loads(self.receive_text())

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._ws is not None:
            self._ws.close(code=code, reason=reason)
            self._ws = None


class AsyncTestClient:
    """Async HTTP test client that launches a real fastapi-rs server in a background thread."""

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
    ):
        self.app = app
        self._base_url = base_url
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.AsyncClient | None = None
        self._raise_server_exceptions = raise_server_exceptions

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    async def __aenter__(self) -> AsyncTestClient:
        self._port = self._find_free_port()
        self._thread = threading.Thread(
            target=self.app.run,
            kwargs={"host": "127.0.0.1", "port": self._port},
            daemon=True,
        )
        self._thread.start()

        # Wait for server to be ready (run blocking wait in thread)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._wait_for_server)

        self._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self._port}")
        return self

    def _wait_for_server(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        raise RuntimeError("AsyncTestClient: server did not start within 10 seconds")

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- HTTP verb shortcuts --

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.post(url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.put(url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.delete(url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.patch(url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.options(url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.head(url, **kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None, "AsyncTestClient must be used as an async context manager"
        return await self._client.request(method, url, **kwargs)
