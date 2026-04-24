"""TestClient for testing fastapi-turbo/FastAPI applications.

Starts the Rust-backed server in a background thread and provides
an httpx-based client for making real HTTP requests.

Usage::

    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

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

# Convenience re-export — mirrors FastAPI's recommended async testing
# recipe (``httpx.AsyncClient(transport=ASGITransport(app=app))``) so
# users can write ``from fastapi.testclient import AsyncClient`` with
# no extra import. Same class that httpx ships; no wrapping.
AsyncClient = httpx.AsyncClient
ASGITransport = httpx.ASGITransport


class _ASGISyncClientShim:
    """Sync httpx-like facade over an async ``httpx.AsyncClient`` backed
    by ``ASGITransport``. Used when the wrapped app is a bare ASGI
    callable (e.g. ``SentryAsgiMiddleware(fastapi_app)``) — Sentry's
    own tests use this form.
    """

    def __init__(
        self,
        app,
        base_url: str,
        follow_redirects: bool,
        headers=None,
        cookies=None,
    ):
        self._app = app
        self._base_url = base_url
        self._follow_redirects = follow_redirects
        self._headers = dict(headers or {})
        self._cookies = cookies

        # Persistent event loop on a background thread so sync code can
        # issue many requests without re-initializing each time.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="fastapi-turbo-asgi-client",
        )
        self._thread.start()

        transport = httpx.ASGITransport(app=app)

        async def _mk_client():
            return httpx.AsyncClient(
                transport=transport,
                base_url=base_url,
                follow_redirects=follow_redirects,
                headers=self._headers or None,
                cookies=self._cookies,
            )

        self._client = asyncio.run_coroutine_threadsafe(
            _mk_client(), self._loop,
        ).result()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.get(url, **kwargs))

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.post(url, **kwargs))

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.put(url, **kwargs))

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.patch(url, **kwargs))

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.delete(url, **kwargs))

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.head(url, **kwargs))

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.options(url, **kwargs))

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._run(self._client.request(method, url, **kwargs))

    @property
    def headers(self):
        return self._client.headers

    @property
    def cookies(self):
        return self._client.cookies

    def close(self) -> None:
        try:
            self._run(self._client.aclose())
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


class TestClient:
    """HTTP test client that launches a real fastapi-turbo server in a background thread."""

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
        in_process: bool | None = None,
        **_ignored: Any,
    ):
        """TestClient for fastapi-turbo apps.

        ``in_process``:
          * ``None`` (default): start the Rust/Axum server on a free
            loopback port — "real HTTP" path. Exercises the production
            code path end-to-end including every Tower layer.
          * ``True``: dispatch through our in-process ASGI adapter via
            ``httpx.ASGITransport`` — no port bound, works in sandboxed
            environments. Matches Starlette's TestClient semantics.
            Middleware registered via ``add_middleware`` still fires;
            Rust-only Tower layers (native CompressionLayer, etc.) do
            NOT — that's the trade-off for avoiding a socket.
          * ``False``: force the real-HTTP path even if loopback bind
            fails (useful when a test specifically targets the Rust
            hot path).

        ``FASTAPI_TURBO_TESTCLIENT_IN_PROCESS=1`` in the environment
        flips the default so an entire suite can run sandboxed without
        touching every ``TestClient(...)`` call.
        """
        self.app = app
        self._base_url = base_url
        self._port: int | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._raise_server_exceptions = raise_server_exceptions
        self._started = False

        if in_process is None:
            import os as _os
            env = _os.environ.get("FASTAPI_TURBO_TESTCLIENT_IN_PROCESS")
            self._in_process = env is not None and env.lower() in ("1", "true", "yes")
        else:
            self._in_process = bool(in_process)
        # Optional defaults to seed into the httpx client once it's lazy-
        # created — matches Starlette's TestClient kwargs surface.
        self._seed_cookies = cookies
        self._seed_headers = headers
        self._follow_redirects = follow_redirects
        # Starlette's TestClient accepts root_path to simulate reverse-proxy
        # mounting; it populates scope["root_path"] so handlers and the
        # openapi schema see it.
        #
        # We track the app's "original" root_path (set via
        # ``FastAPI(root_path=...)``) in a private slot the FIRST time
        # a TestClient wraps the app. Subsequent clients with their own
        # ``root_path=`` override, and clients without one REVERT to
        # the original (fixes ``test_openapi_cache_root_path`` — a
        # spoofed prefix from one client must not leak to the next).
        if hasattr(app, "root_path"):
            try:
                if not hasattr(app, "_fastapi_turbo_original_root_path"):
                    app._fastapi_turbo_original_root_path = app.root_path or ""
                effective = root_path if root_path else app._fastapi_turbo_original_root_path
                if app.root_path != effective:
                    app.root_path = effective
                    if hasattr(app, "openapi_schema"):
                        app.openapi_schema = None
            except Exception:
                pass
        self._root_path = root_path

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

        When the app is a bare ASGI callable (e.g. ``SentryAsgiMiddleware
        (fastapi_app)``) rather than a FastAPI instance with ``.run()``,
        falls back to ``httpx.ASGITransport`` for an in-process
        Starlette-style transport — this mirrors how Starlette's own
        TestClient wraps arbitrary ASGI apps.
        """
        if self._started:
            return
        # in_process=True (or env var) → same ASGI-transport path we
        # already use for wrapped-ASGI apps. Bypasses the loopback
        # server entirely so sandboxed / serverless environments work
        # out of the box.
        if self._in_process or not hasattr(self.app, "run"):
            import httpx as _httpx
            self._client = _ASGISyncClientShim(
                app=self.app,
                base_url=self._base_url,
                follow_redirects=self._follow_redirects,
                headers=dict(self._seed_headers or {}) or None,
                cookies=self._seed_cookies,
            )
            self._started = True
            self._port = None
            self._thread = None
            self._follow_state = threading.local()
            self._follow_state.follow = self._follow_redirects
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
                    name=f"fastapi-turbo-testclient-{port}",
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
        # Starlette's TestClient passes scope["scheme"] derived from
        # base_url. Our server listens on plain http; when base_url
        # specifies https we forward the scheme via X-Forwarded-Proto
        # so middleware like HTTPSRedirectMiddleware and code reading
        # request.url.scheme behave as Starlette does.
        _forced_scheme = (parsed.scheme or "http").lower()

        # The URL-derived Host httpx sets automatically (connection
        # netloc, e.g. ``127.0.0.1:61234``). Any value other than this
        # or our synthesized one must have been caller-supplied via
        # ``headers={"Host": ...}`` — preserve in that case so tests
        # exercising Host-based routing / TrustedHost see the literal
        # value they asked for.
        _auto_host_url = httpx.URL(f"http://127.0.0.1:{self._port}").host
        _auto_host_with_port = f"127.0.0.1:{self._port}"

        def _force_host(request: httpx.Request) -> None:
            current_host = request.headers.get("host", "")
            is_auto = (
                not current_host
                or current_host == _forced_host
                or current_host == _auto_host_url
                or current_host == _auto_host_with_port
            )
            if is_auto:
                request.headers["host"] = _forced_host
            if _forced_scheme == "https" and "x-forwarded-proto" not in request.headers:
                request.headers["x-forwarded-proto"] = "https"
            # Starlette's TestClient uses ``user-agent: testclient`` — FA
            # tests assert on this exact value in 422 error ``input``
            # dicts. httpx defaults to ``python-httpx/X.Y.Z``.
            if request.headers.get("user-agent", "").startswith("python-httpx"):
                request.headers["user-agent"] = "testclient"
            # Starlette's TestClient talks to the ASGI app in-process,
            # bypassing h11's strict header-value validation. Real-HTTP
            # tests that send e.g. ``Authorization: "Other  foobar "``
            # would be rejected by h11 (leading/trailing whitespace),
            # so we strip outer whitespace from every header value to
            # mirror the ASGI transport's leniency. The server-side
            # security code already handles whitespace-collapsed
            # credentials via ``.split(None, 1)``.
            for k in list(request.headers.keys()):
                v = request.headers[k]
                stripped = v.strip() if isinstance(v, str) else v
                if stripped != v:
                    request.headers[k] = stripped

        default_headers = {"host": host_header, "user-agent": "testclient"}
        if self._seed_headers:
            # Let user-supplied defaults win over our injected Host.
            for k, v in dict(self._seed_headers).items():
                default_headers[k] = v
        # Our trailing-slash redirect middleware emits an ABSOLUTE URL
        # built from the request's Host header ("testserver" or the
        # user's base_url hostname). Starlette's in-memory TestClient
        # transport happily follows that, but real httpx does a DNS
        # lookup on the hostname and fails (nodename not known). When
        # a redirect is about to be FOLLOWED we rewrite the Location
        # header to a relative path so httpx routes it back to our
        # 127.0.0.1 base_url. When the response will be handed to the
        # caller as-is (follow_redirects=False) we must leave it alone
        # so ``response.headers["location"]`` reflects what the server
        # sent.
        _base_host_name = urlparse(self._base_url).hostname or "testserver"
        self._follow_state = threading.local()
        _follow_state = self._follow_state
        def _rewrite_redirect_location(response: httpx.Response) -> None:
            if not (300 <= response.status_code < 400):
                return
            will_follow = getattr(_follow_state, "follow", self._follow_redirects)
            if not will_follow:
                return
            loc = response.headers.get("location")
            if not loc:
                return
            parsed_loc = urlparse(loc)
            if not parsed_loc.netloc:
                return
            host_only = parsed_loc.hostname or ""
            if host_only != _base_host_name and parsed_loc.netloc != _forced_host:
                return
            new_path = parsed_loc.path or "/"
            if parsed_loc.query:
                new_path = f"{new_path}?{parsed_loc.query}"
            if parsed_loc.fragment:
                new_path = f"{new_path}#{parsed_loc.fragment}"
            response.headers["location"] = new_path

        # FA's test suite runs with ``filterwarnings = ["error"]`` —
        # any ``ResourceWarning`` (including unclosed sockets) becomes
        # a test failure. Disable keep-alive so each httpx call closes
        # its socket synchronously instead of leaving it in the pool
        # for GC to finalize.
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{self._port}",
            follow_redirects=self._follow_redirects,
            headers=default_headers,
            cookies=self._seed_cookies,
            event_hooks={
                "request": [_force_host],
                "response": [_rewrite_redirect_location],
            },
            limits=httpx.Limits(
                max_keepalive_connections=0,
                max_connections=10,
            ),
        )
        self._started = True

    def __enter__(self) -> TestClient:
        # FA parity: ``with TestClient(app)`` must complete the
        # lifespan STARTUP phase before ``__enter__`` returns — the
        # tutorial ``app_testing/tutorial004`` pattern asserts the
        # lifespan populated ``items`` inside the ``with`` block.
        # If the server-backed ensure_started wouldn't fire startup
        # soon enough, run it explicitly in the current thread.
        if (
            hasattr(self.app, "_collect_lifespans")
            and self.app._collect_lifespans()
            and not getattr(self.app, "_lifespan_cms", None)
        ):
            try:
                self.app._run_lifespan_startup()
            except Exception:  # noqa: BLE001
                pass
        self._ensure_started()
        return self

    def __exit__(self, *args: Any) -> None:
        # FA tests use ``with TestClient(app) as client:`` as a signal
        # that the app's lifespan (startup → shutdown) should complete
        # AND the background server thread should be torn down before
        # the block ends. Starlette's TestClient does this inherently;
        # we need to:
        #   1. Run shutdown handlers / lifespan teardown.
        #   2. Trigger a graceful Rust-server shutdown on the port we
        #      launched (via ``_fastapi_turbo_core.request_server_shutdown``).
        #   3. Drop the ``_app_servers`` cache entry so the app ref is
        #      GC-eligible.
        try:
            stop_chain = getattr(self.app, "_stop_lifespan_mw_chain", None)
            if stop_chain is not None and stop_chain():
                pass
            else:
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
        # Ask the Rust server on our port to shut down gracefully, then
        # drop the cache entry. The daemon thread will exit once axum's
        # ``with_graceful_shutdown`` resolves — typically within tens of
        # ms for idle servers. We best-effort join with a short timeout
        # so stragglers don't block test teardown, but the cache is
        # cleared regardless.
        try:
            from fastapi_turbo._fastapi_turbo_core import request_server_shutdown
            request_server_shutdown(self._port)
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "_thread", None) is not None:
            try:
                self._thread.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        key = id(self.app)
        with TestClient._app_lock:
            cached = TestClient._app_servers.get(key)
            if cached is not None and cached[1] == self._port:
                TestClient._app_servers.pop(key, None)
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

    _FORWARD_TO_HTTPX = frozenset({"cookies", "headers", "params", "base_url"})

    def __setattr__(self, name: str, value: Any) -> None:
        # ``client.cookies = [...]`` / ``client.headers = {...}`` is a
        # common Starlette-TestClient idiom to seed request state. Forward
        # to the underlying httpx.Client so it actually reaches the wire.
        # Private attrs and unknown names fall through to the default
        # object __setattr__.
        if name in TestClient._FORWARD_TO_HTTPX and not name.startswith("_"):
            client = self.__dict__.get("_client")
            if client is None:
                # Before the server starts, stash on self and replay in
                # _ensure_started (cookies → _seed_cookies, headers →
                # _seed_headers). Cookies path goes through _seed_cookies.
                if name == "cookies":
                    object.__setattr__(self, "_seed_cookies", value)
                    return
                if name == "headers":
                    object.__setattr__(self, "_seed_headers", value)
                    return
                # params/base_url aren't used before start, stash normally
                object.__setattr__(self, name, value)
                return
            setattr(client, name, value)
            return
        object.__setattr__(self, name, value)

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

    def _track_follow(self, kwargs: dict) -> None:
        """Record the effective follow_redirects flag for this call so
        the response hook can decide whether to rewrite Location headers
        pointing at our synthetic base_url host."""
        if hasattr(self, "_follow_state"):
            self._follow_state.follow = kwargs.get(
                "follow_redirects", self._follow_redirects
            )

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.get(url, **kwargs)
        self._check_raised()
        return r

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.post(url, **kwargs)
        self._check_raised()
        return r

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.put(url, **kwargs)
        self._check_raised()
        return r

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.delete(url, **kwargs)
        self._check_raised()
        return r

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.patch(url, **kwargs)
        self._check_raised()
        return r

    def options(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.options(url, **kwargs)
        self._check_raised()
        return r

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
        r = self._client.head(url, **kwargs)
        self._check_raised()
        return r

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._ensure_started()
        self._track_follow(kwargs)
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
        headers: dict | None = None,
        cookies: dict | None = None,
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
        # Carry forward cookies set on the client (``client.cookies``
        # or ``cookies=`` in the TestClient ctor) as a ``Cookie``
        # header; ``websockets.sync.client.connect`` doesn't expose a
        # cookies arg.
        extra_hdrs: list[tuple[str, str]] = []
        if headers:
            for k, v in dict(headers).items():
                extra_hdrs.append((k, v))
        # Merge cookies from client + per-call + seed
        cookie_jar: dict[str, str] = {}
        if self._client is not None:
            try:
                for c in self._client.cookies.jar:
                    cookie_jar[c.name] = c.value
            except Exception:  # noqa: BLE001
                pass
        if self._seed_cookies:
            cookie_jar.update(dict(self._seed_cookies))
        if cookies:
            cookie_jar.update(cookies)
        if cookie_jar:
            cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_jar.items())
            extra_hdrs.append(("Cookie", cookie_header))
        # Drain any stale server exceptions from prior sessions BEFORE
        # opening this one so we don't surface them here.
        try:
            getattr(self.app, "_ws_server_exceptions", []).clear()
        except Exception:  # noqa: BLE001
            pass
        # Capture the TEST thread's contextvars context and queue it on
        # the app so the server-side WS handler can replay it before
        # running the user's coroutine. This makes tests that set a
        # ``ContextVar`` in the test thread observe mutations performed
        # by yield-dep teardowns that run on the server's async worker
        # thread — the teardown replays the captured vars, so they
        # mutate the SAME underlying objects the test holds.
        try:
            import contextvars as _cv
            ctx = _cv.copy_context()
            q = getattr(self.app, "_ws_pending_test_contexts", None)
            if q is None:
                q = []
                try:
                    self.app._ws_pending_test_contexts = q
                except Exception:  # noqa: BLE001
                    q = None
            if q is not None:
                q.append(ctx)
        except Exception:  # noqa: BLE001
            pass
        return _WebSocketTestSession(
            ws_url,
            subprotocols=subprotocols,
            additional_headers=extra_hdrs or None,
            app=self.app,
            **kwargs,
        )


class _WebSocketTestSession:
    """Synchronous Starlette-compatible test WebSocket session."""

    def __init__(
        self,
        url: str,
        subprotocols: list[str] | None = None,
        additional_headers: list[tuple[str, str]] | None = None,
        app: Any = None,
        **_: Any,
    ):
        self._url = url
        self._subprotocols = subprotocols
        self._additional_headers = additional_headers
        self._ws = None
        self._app = app
        from websockets.sync.client import connect as _connect  # noqa: E402
        self._connect = _connect

    def __enter__(self) -> "_WebSocketTestSession":
        kw: dict = {}
        if self._subprotocols:
            kw["subprotocols"] = self._subprotocols
        if self._additional_headers:
            kw["additional_headers"] = self._additional_headers
        try:
            self._ws = self._connect(self._url, **kw)
        except Exception as e:
            # FA's TestClient converts handshake rejection to
            # ``WebSocketDisconnect`` — tests wrap in
            # ``pytest.raises(WebSocketDisconnect)`` for both pre- and
            # post-accept closures.
            from fastapi_turbo.exceptions import WebSocketDisconnect
            try:
                from websockets.exceptions import InvalidStatus
                if isinstance(e, InvalidStatus):
                    code = getattr(e.response, "status_code", None) or 1008
                    # If the server-side handler raised a
                    # ``WebSocketException`` pre-accept, the Starlette-
                    # spec HTTP response is 403; but the WS close code
                    # attached to the exception (e.g. 1008 for
                    # POLICY_VIOLATION) is what FA tests assert on. Poll
                    # the app's capture queue for the original exc.
                    srv_exc = self._pop_server_exception()
                    if srv_exc is not None and isinstance(srv_exc, WebSocketDisconnect):
                        raise srv_exc from None
                    # FA maps HTTP 404 (no route) to WS normal-closure 1000.
                    if code == 404:
                        code = 1000
                    raise WebSocketDisconnect(code=code) from None
            except ImportError:
                pass
            raise
        return self

    def _pop_server_exception(self):
        """Pop the most recent captured server-side WS exception.
        Brief poll in case the server thread is still finalising."""
        import time as _t
        if self._app is None:
            return None
        try:
            q = getattr(self._app, "_ws_server_exceptions", None)
            if q is None:
                return None
            deadline = _t.monotonic() + 0.25
            while _t.monotonic() < deadline:
                if q:
                    return q.pop(0)
                _t.sleep(0.01)
        except Exception:  # noqa: BLE001
            return None
        return None

    def __exit__(self, *args: Any) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        # Starlette's TestClient re-raises server-side exceptions on exit.
        # Our server thread doesn't share a future, so we poll the
        # capture queue: when the handler raised ``WebSocketDisconnect``
        # or another exception, surface it here so ``pytest.raises(WS
        # Disconnect)`` around the ``with`` block fires.
        srv_exc = self._pop_server_exception()
        if srv_exc is not None:
            raise srv_exc

    @property
    def accepted_subprotocol(self) -> str | None:
        """Subprotocol the server picked in its ``accept(subprotocol=…)``.

        Mirrors Starlette's ``WebSocketTestSession.accepted_subprotocol`` —
        tests read this after ``__enter__`` to assert that the server
        negotiated the expected subprotocol.
        """
        if self._ws is None:
            return None
        sp = getattr(self._ws, "subprotocol", None)
        return sp if sp else None

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

    def _translate_ws_error(self, exc):
        """Convert underlying ``websockets`` library errors into the
        ``WebSocketDisconnect`` that FA test suites expect.
        """
        from fastapi_turbo.exceptions import WebSocketDisconnect
        try:
            from websockets.exceptions import (
                ConnectionClosed, ConnectionClosedOK, ConnectionClosedError,
            )
            if isinstance(exc, (ConnectionClosedOK, ConnectionClosedError, ConnectionClosed)):
                code = getattr(exc, "code", 1000) or 1000
                reason = getattr(exc, "reason", "") or ""
                return WebSocketDisconnect(code=code, reason=reason)
        except ImportError:
            pass
        return None

    def receive_text(self) -> str:
        assert self._ws is not None
        try:
            msg = self._ws.recv()
        except Exception as exc:
            translated = self._translate_ws_error(exc)
            if translated is not None:
                raise translated from None
            raise
        if isinstance(msg, bytes):
            return msg.decode("utf-8")
        return msg

    def receive_bytes(self) -> bytes:
        assert self._ws is not None
        try:
            msg = self._ws.recv()
        except Exception as exc:
            translated = self._translate_ws_error(exc)
            if translated is not None:
                raise translated from None
            raise
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
    """Async HTTP test client that launches a real fastapi-turbo server in a background thread."""

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
