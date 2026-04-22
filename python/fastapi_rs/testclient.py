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

    # pytest collects any ``class Test*`` by default — mark ourselves as
    # not a test class so projects using ``TestClient`` inside a test
    # module don't get PytestCollectionWarning noise.
    __test__ = False

    # Cache of running servers keyed by ``id(app)`` → (app_ref, port, thread).
    # We keep a STRONG reference to the app alongside the id so that the
    # Python GC cannot collect an app whose thread is still running and
    # reassign its id() to a different app — a subtle but real source of
    # cross-test bleed where one app's port starts serving a later app's
    # requests (routes from the "wrong" server, `exception="http-exception"`
    # body on an unrelated client.get). FA's own TestClient is
    # synchronous so doesn't hit this; ours does because the server runs
    # in a daemon thread.
    _app_servers: dict[int, tuple[Any, int, threading.Thread]] = {}
    _app_lock = threading.Lock()

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        root_path: str = "",
        backend: str | None = None,
        backend_options: dict | None = None,
        cookies: dict | None = None,
        headers: dict | None = None,
        follow_redirects: bool = True,
        client: tuple[str, int] | None = None,
        **_ignored: Any,
    ):
        self.app = app
        self._base_url = base_url
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._raise_server_exceptions = raise_server_exceptions
        self._started = False
        # Optional defaults to seed into the httpx client once it's lazy-
        # created — matches Starlette's TestClient kwargs surface.
        self._seed_cookies = cookies
        self._seed_headers = headers
        self._follow_redirects = follow_redirects

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _ensure_started(self) -> None:
        """Lazily boot the server on first request. Starlette's TestClient
        is usable without ``with``; we match that so FastAPI-style tests
        (``client = TestClient(app); client.get(...)``) work verbatim.
        Reuses a single background server per ``app`` instance across
        TestClient invocations (see ``_app_servers`` cache above).
        """
        if self._started:
            return
        key = id(self.app)
        with TestClient._app_lock:
            cached = TestClient._app_servers.get(key)
            # Require that the cached entry points to the SAME app object
            # (identity check), not just a matching id — after a prior
            # app is GC'd its id can be reassigned to an unrelated app,
            # and reusing that thread would silently route the new app's
            # requests to the old server.
            if (
                cached is not None
                and cached[0] is self.app
                and cached[2].is_alive()
            ):
                self._port, self._thread = cached[1], cached[2]
            else:
                port = self._find_free_port()
                thread = threading.Thread(
                    target=self.app.run,
                    kwargs={"host": "127.0.0.1", "port": port},
                    daemon=True,
                    name=f"fastapi-rs-testclient-{port}",
                )
                thread.start()
                self._port, self._thread = port, thread
                TestClient._app_servers[key] = (self.app, port, thread)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError("TestClient: server did not start within 10 seconds")

        # Starlette's TestClient follows redirects by default — FastAPI
        # tests hit ``/foo/`` vs ``/foo`` expecting a 200 on the
        # canonicalised URL, not the intermediate 307. Mirror that so
        # third-party test suites work verbatim.
        # Starlette's TestClient reports Host as ``testserver`` (or the
        # user-supplied ``base_url``'s hostname) regardless of where the
        # client actually connects. Our httpx is connecting to 127.0.0.1,
        # so force the outgoing Host header at request-build time via an
        # event hook — this survives ``client.headers.clear()`` (FA's
        # own header-param-models fixtures do this).
        from urllib.parse import urlparse
        parsed = urlparse(self._base_url)
        host_header = parsed.hostname or "testserver"
        if parsed.port:
            host_header = f"{host_header}:{parsed.port}"
        _forced_host = host_header

        def _force_host(request: httpx.Request) -> None:
            request.headers["host"] = _forced_host
            # Starlette's TestClient uses ``user-agent: testclient`` — FA
            # tests assert on this exact value in 422 error ``input``
            # dicts. httpx defaults to ``python-httpx/X.Y.Z``.
            if request.headers.get("user-agent", "").startswith("python-httpx"):
                request.headers["user-agent"] = "testclient"

        default_headers = {"host": host_header}
        if self._seed_headers:
            # Let user-supplied defaults win over our injected Host.
            for k, v in dict(self._seed_headers).items():
                default_headers[k] = v
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{self._port}",
            follow_redirects=self._follow_redirects,
            headers=default_headers,
            cookies=self._seed_cookies,
            event_hooks={"request": [_force_host]},
        )
        self._started = True

    def __enter__(self) -> TestClient:
        self._ensure_started()
        return self

    def __exit__(self, *args: Any) -> None:
        # FA tests use ``with TestClient(app) as client:`` as a signal
        # that the app's lifespan (startup → shutdown) should complete
        # before the block ends. Starlette's TestClient does this
        # inherently; ours caches the background server to keep thread
        # counts bounded, so we fire shutdown handlers explicitly here.
        try:
            shutdown = getattr(self.app, "_run_shutdown_handlers", None)
            if shutdown is not None:
                shutdown()
            lifespan_stop = getattr(self.app, "_run_lifespan_shutdown", None)
            if lifespan_stop is not None and (
                getattr(self.app, "lifespan", None)
                or getattr(self.app, "_lifespan_cms", None)
            ):
                lifespan_stop()
        except Exception:  # noqa: BLE001
            pass
        if self._client:
            self._client.close()
            self._client = None
        self._started = False

    # ── Lifespan state ────────────────────────────────────────────────
    @property
    def app_state(self) -> dict:
        """Merged state yielded by app + router lifespans.

        Starlette exposes this so tests can assert on state after
        lifespan startup. We mirror the dict the app collects during
        ``_run_lifespan_startup``.
        """
        return getattr(self.app, "_app_state", {}) or {}

    # ── Attribute forwarding ──────────────────────────────────────────
    # Starlette's TestClient inherits from ``httpx.Client``, so user
    # code commonly reads ``client.cookies``, ``client.headers``,
    # ``client.params`` directly to seed default values for every
    # request. We proxy through to the underlying httpx client so those
    # patterns work verbatim.
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # Trigger lazy start if something like ``client.cookies`` is
        # accessed before any request.
        self._ensure_started()
        if self._client is not None and hasattr(self._client, name):
            return getattr(self._client, name)
        raise AttributeError(name)

    # ── HTTP verb shortcuts ────────────────────────────────────────

    def _check_raised(self) -> None:
        """Drain server-side exceptions captured during the last request
        and re-raise the first one in the test thread. FA's TestClient
        does this automatically via the ASGI protocol; we do it
        manually via ``app._captured_server_exceptions``. Always drain
        the list so captured exceptions from a ``raise_server_exceptions
        =False`` client don't leak into a subsequent
        ``raise_server_exceptions=True`` request.
        """
        captured = getattr(self.app, "_captured_server_exceptions", None)
        if not captured:
            return
        exc = captured.pop(0)
        captured.clear()
        if not self._raise_server_exceptions:
            return
        raise exc

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.get(url, **kwargs)
        self._check_raised()
        return r

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.post(url, **kwargs)
        self._check_raised()
        return r

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.put(url, **kwargs)
        self._check_raised()
        return r

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.delete(url, **kwargs)
        self._check_raised()
        return r

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.patch(url, **kwargs)
        self._check_raised()
        return r

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.options(url, **kwargs)
        self._check_raised()
        return r

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.head(url, **kwargs)
        self._check_raised()
        return r

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        r = self._client.request(method, url, **kwargs)
        self._check_raised()
        return r

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
        # Lazy-start the server so bare ``client.websocket_connect(...)``
        # (without ``with TestClient(...)``) works — matches Starlette's
        # TestClient, which FA's tests use.
        self._ensure_started()
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
