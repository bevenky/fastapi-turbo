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


class _BufferedStreamFacade:
    """Stream-facade backed by a fully-buffered body. Returned by
    ``_ASGISyncClientShim.stream()`` when an auth challenge-response
    loop forced us to drain intermediate responses to feed back into
    ``flow.send(resp)``. The user-facing surface (``iter_bytes`` /
    ``iter_text`` / ``read`` / ``content`` / ``text`` / ``status_code``
    / ``headers`` / ``url`` / context-manager protocol) is identical
    to the live-streaming facade — only the chunking is lost (the
    body comes out as a single chunk, since by the time we know it's
    the final response we've already consumed it). For Basic auth the
    flow terminates after one ``StopIteration`` so this still wraps a
    correct response; for Digest auth the same applies after the
    second round."""

    def __init__(self, *, status_code: int, headers, body: bytes, url: str):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def iter_bytes(self, chunk_size: int | None = None):
        if not self._body:
            return
        if chunk_size is None or len(self._body) <= chunk_size:
            yield self._body
            return
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def _charset(self) -> str:
        ct = self.headers.get("content-type", "")
        if "charset=" in ct:
            return ct.split("charset=", 1)[1].strip().split(";", 1)[0]
        return "utf-8"

    def iter_text(self, chunk_size: int | None = None):
        charset = self._charset()
        for c in self.iter_bytes(chunk_size):
            yield c.decode(charset, errors="replace")

    def read(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode(self._charset(), errors="replace")

    @property
    def content(self) -> bytes:
        return self._body


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
        raise_app_exceptions: bool = True,
    ):
        self._app = app
        self._base_url = base_url
        self._follow_redirects = follow_redirects
        # Pin ``Accept-Encoding: gzip, deflate`` as a session default
        # (matches Starlette's TestClient — see comment in
        # ``TestClient.__init__`` for the upstream-FastAPI Tutorial
        # snapshot mismatch this fixes). User-supplied headers
        # override; the default fills in if absent.
        merged_headers: dict = {"accept-encoding": "gzip, deflate"}
        for k, v in (headers or {}).items():
            merged_headers[str(k).lower()] = v
        self._headers = merged_headers
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

        # ``raise_app_exceptions=True`` (httpx default): unhandled
        # exceptions propagate as-is. ``=False``: httpx converts them
        # to synthetic 500 responses so the test can assert on them.
        # Starlette's TestClient threads this via
        # ``raise_server_exceptions``; we match that semantic.
        transport = httpx.ASGITransport(
            app=app, raise_app_exceptions=raise_app_exceptions,
        )

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

    def stream(self, method: str, url: str, **kwargs: Any):
        """Sync streaming-response context manager (httpx parity).

        Drives the underlying ASGI app DIRECTLY (bypassing
        ``httpx.ASGITransport``, which buffers all body parts before
        returning the response — see httpx ``_transports/asgi.py``
        ``body_parts.append(body)`` followed by
        ``ASGIResponseStream(body_parts)`` only after the app fully
        completes). The custom driver here pushes each
        ``http.response.body`` frame into a thread-safe queue as it
        arrives, so ``iter_bytes()`` yields chunk-by-chunk in real
        time.

        Suitable for SSE, long-poll, and infinite streams. First
        chunk arrives as soon as the handler ``yields`` it, not
        when the whole response completes.

        Honours ``follow_redirects`` — when True (or the session
        default is True), 301/302/303/307/308 responses with a
        ``Location`` header trigger an automatic re-issue at the
        new target. Capped at 10 redirects to mirror httpx.

        Honours ``auth`` — accepts ``(user, pass)`` (treated as
        ``httpx.BasicAuth``) or any ``httpx.Auth`` instance. The
        full challenge-response loop runs: each yielded request goes
        through the in-process app, the resulting response is fed
        back via ``flow.send(resp)``, and the loop terminates on
        ``StopIteration``. This makes ``httpx.DigestAuth`` work
        end-to-end (initial 401 → server's nonce parsed by the auth
        flow → second request stamped with ``Authorization: Digest …``
        → 200 returned to the caller). Earlier code only used the
        first yielded request, so digest auth always returned 401."""
        # Pull redirect / timeout / auth knobs out of kwargs BEFORE
        # we forward to the per-request driver — the rest stay as
        # httpx Request kwargs.
        follow_redirects = kwargs.pop(
            "follow_redirects", self._follow_redirects
        )
        # ``timeout`` isn't enforced strictly here (the in-process
        # path is bounded by the user's handler, not by network
        # latency); we accept and discard for kwarg parity so
        # ``c.stream(..., timeout=...)`` doesn't TypeError.
        kwargs.pop("timeout", None)

        auth = kwargs.pop("auth", None)
        auth_inst = self._normalise_auth(auth)

        if auth_inst is None:
            return self._stream_with_redirects(
                method, url, follow_redirects, **kwargs
            )

        # Auth challenge-response loop. Build a fresh ``httpx.Request``
        # to seed the flow, then for each yielded request run the
        # in-process app, materialise an ``httpx.Response``, and feed
        # it back. ``StopIteration`` means the last request/response
        # pair is the final answer.
        from urllib.parse import urlsplit
        full_url = url
        if "://" not in full_url:
            full_url = self._base_url.rstrip("/") + "/" + full_url.lstrip("/")
        merged_headers = dict(self._headers or {})
        for k, v in (kwargs.get("headers") or {}).items():
            merged_headers[k] = v
        seed_req = httpx.Request(
            method=method.upper(),
            url=full_url,
            params=kwargs.get("params"),
            content=kwargs.get("content"),
            data=kwargs.get("data"),
            files=kwargs.get("files"),
            json=kwargs.get("json"),
            headers=merged_headers,
            cookies=kwargs.get("cookies"),
        )
        flow = auth_inst.sync_auth_flow(seed_req)
        try:
            cur_req = next(flow)
        except StopIteration:
            return self._stream_with_redirects(
                method, url, follow_redirects, **kwargs
            )

        from httpx import Response as _Resp
        max_auth_rounds = 4  # mirrors httpx's auth-loop guard
        for _round in range(max_auth_rounds + 1):
            # Translate the auth-flow's ``httpx.Request`` back into
            # kwargs for ``_stream_with_redirects``. The flow may have
            # mutated headers (e.g. added ``Authorization``) — pass
            # the request's headers + URL + body wholesale; drop
            # original encoder kwargs (``json`` / ``files`` / ``data``
            # / ``params``) since the body is already serialised.
            round_kwargs: dict[str, Any] = {}
            round_kwargs["headers"] = dict(cur_req.headers)
            round_kwargs["content"] = cur_req.read() or b""
            facade = self._stream_with_redirects(
                cur_req.method, str(cur_req.url), follow_redirects,
                **round_kwargs,
            )
            body = facade.read()
            facade.__exit__(None, None, None)
            resp = _Resp(
                status_code=facade.status_code,
                headers=facade.headers,
                content=body,
                request=cur_req,
            )
            try:
                cur_req = flow.send(resp)
            except StopIteration:
                return _BufferedStreamFacade(
                    status_code=facade.status_code,
                    headers=facade.headers,
                    body=body,
                    url=str(cur_req.url),
                )
        raise RuntimeError(
            f"auth flow did not terminate after {max_auth_rounds} rounds"
        )

    def _normalise_auth(self, auth: Any) -> Any:
        """Normalise ``auth=`` kwarg into an ``httpx.Auth`` instance,
        or ``None`` if not provided / unsupported."""
        if auth is None:
            return None
        from httpx import Auth as _Auth, BasicAuth as _BasicAuth
        if isinstance(auth, _Auth):
            return auth
        if isinstance(auth, tuple) and len(auth) == 2:
            return _BasicAuth(auth[0], auth[1])
        return None

    def _stream_with_redirects(
        self,
        method: str,
        url: str,
        follow_redirects: bool,
        **kwargs: Any,
    ):
        """Per-request driver wrapped with the follow-redirects loop.
        Auth is handled one level up in ``stream()``; this just runs
        the request, and on a 3xx with a Location header re-issues at
        the new target up to ``max_redirects`` times."""
        max_redirects = 10
        for _hop in range(max_redirects + 1):
            facade = self._stream_one_shot(method, url, **kwargs)
            if not (
                follow_redirects
                and facade.status_code in (301, 302, 303, 307, 308)
                and facade.headers.get("location")
            ):
                return facade
            # Close the current stream cleanly before re-issuing.
            location = facade.headers["location"]
            status = facade.status_code
            facade.__exit__(None, None, None)
            # Method-rewrite rules — match httpx (and historical
            # browser behaviour, which RFC 7231 §6.4 acknowledges
            # via the §6.4.2 / §6.4.3 "should not change ... but
            # historical clients did" notes):
            #
            #   * 303: any non-GET/HEAD method becomes GET (mandatory
            #     per RFC).
            #   * 301 / 302: POST becomes GET (historical browsers
            #     always rewrote — httpx mirrors that). Other
            #     methods are preserved.
            #   * 307 / 308: method is preserved (RFC mandate).
            method_upper = method.upper()
            if status == 303 and method_upper not in ("GET", "HEAD"):
                method = "GET"
            elif status in (301, 302) and method_upper == "POST":
                method = "GET"
            # Body / json / form / files are dropped whenever the
            # method changes — preserving them across a method
            # rewrite would replay a POST body as a GET.
            if method != method_upper:
                kwargs.pop("content", None)
                kwargs.pop("json", None)
                kwargs.pop("data", None)
                kwargs.pop("files", None)
            url = location
        # Too many redirects — raise so the caller sees the loop.
        raise RuntimeError(
            f"too many redirects (>{max_redirects}) starting from {url!r}"
        )

    def _stream_one_shot(self, method: str, url: str, **kwargs: Any):
        """Drive a single non-redirected request through the in-process
        ASGI app. Returns the streaming facade. ``stream()`` wraps
        this with the follow-redirects loop."""
        from urllib.parse import urlsplit, urlencode
        import queue as _queue

        # Capture loop reference up-front for ``__exit__`` cancellation.
        loop = self._loop

        # Honour httpx-style request kwargs by building an
        # ``httpx.Request`` first — that handles ``params``, ``data``,
        # ``files``, ``json``, ``content``, ``headers``, ``cookies``,
        # auth, and url joining the same way ``httpx.AsyncClient.
        # request`` would. The constructed request is then translated
        # into an ASGI scope + body. This gives us drop-in httpx
        # parity for the kwargs surface without re-implementing each
        # encoder by hand.
        full_url = url
        if "://" not in full_url:
            full_url = self._base_url.rstrip("/") + "/" + full_url.lstrip("/")
        merged_headers = dict(self._headers or {})
        for k, v in (kwargs.pop("headers", None) or {}).items():
            merged_headers[k] = v
        # Per-call cookies stack on top of the session jar; httpx
        # merges them into the Cookie header for us.
        merged_cookies = httpx.Cookies()
        if self._cookies is not None:
            try:
                merged_cookies.update(self._cookies)
            except Exception:  # noqa: BLE001
                pass
        for k, v in (kwargs.pop("cookies", None) or {}).items():
            merged_cookies.set(k, v)
        # ``httpx.Request`` accepts the per-request kwargs natively.
        # ``auth=`` is handled at the ``stream()`` level above (full
        # challenge-response loop), not here — by this point the
        # auth-flow's mutations (added ``Authorization`` header etc.)
        # have already landed in ``kwargs["headers"]``.
        req = httpx.Request(
            method=method.upper(),
            url=full_url,
            params=kwargs.pop("params", None),
            content=kwargs.pop("content", None),
            data=kwargs.pop("data", None),
            files=kwargs.pop("files", None),
            json=kwargs.pop("json", None),
            headers=merged_headers,
            cookies=merged_cookies if merged_cookies else None,
        )
        parts = urlsplit(str(req.url))
        path = parts.path or "/"
        query = parts.query.encode("latin-1") if parts.query else b""
        host = parts.hostname or "testserver"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        # ``httpx.Request`` already serialised the body and built
        # the final header list (Content-Type, Content-Length,
        # Cookie, etc.) — translate directly.
        req_body = req.read() or b""
        raw_headers: list[tuple[bytes, bytes]] = []
        for k, v in req.headers.raw:
            raw_headers.append(
                (k.lower() if isinstance(k, bytes) else k.lower().encode("latin-1"),
                 v if isinstance(v, bytes) else v.encode("latin-1"))
            )
        if not any(k == b"host" for k, _ in raw_headers):
            raw_headers.append(
                (b"host", (
                    f"{host}:{port}" if port not in (80, 443) else host
                ).encode("latin-1"))
            )

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": parts.scheme or "http",
            "server": (host, port),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": query,
            "headers": raw_headers,
            "app": self._app,
        }

        # Thread-safe queue for body chunks. Status + headers
        # captured separately and signalled via an Event. Early
        # client exit is signalled via ``client_disconnected`` —
        # the next ``receive()`` call will return an
        # ``http.disconnect`` message so the app's generator can
        # observe ``CancelledError`` / ``GeneratorExit`` and unwind
        # cleanly. Without this, an infinite stream would keep
        # producing chunks after the test exited the ``with`` block
        # and pytest would warn ``Task was destroyed but it is
        # pending``.
        chunk_q: _queue.Queue = _queue.Queue()
        SENTINEL = object()
        status_holder: dict = {}
        ready = threading.Event()
        done = threading.Event()
        client_disconnected = threading.Event()
        request_consumed = threading.Event()
        # ``app_task_holder`` lets ``__exit__`` cancel the running
        # asyncio task — necessary for infinite generators that
        # don't observe ``http.disconnect`` (they ignore receive).
        app_task_holder: dict = {}

        async def _receive():
            # First call delivers the request body. Subsequent calls
            # are non-blocking probes used by ``request.is_disconnected
            # ()`` (which wraps receive in ``wait_for(..., timeout=0)``)
            # AND by handlers that ``await receive()`` directly. The
            # short-circuit before any ``await`` matters: it makes
            # disconnect-detection deterministic under
            # ``wait_for(0)`` — if we awaited even
            # ``asyncio.sleep(0.01)`` first, ``wait_for`` would
            # cancel the await before we ever reached the disconnect
            # check.
            if not request_consumed.is_set():
                request_consumed.set()
                return {
                    "type": "http.request",
                    "body": req_body,
                    "more_body": False,
                }
            if client_disconnected.is_set():
                return {"type": "http.disconnect"}
            # Not disconnected yet — block until it happens (long-
            # poll-style) so handlers that ``await receive()``
            # directly stay parked.
            while not client_disconnected.is_set():
                await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        async def _send(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["headers"] = message.get("headers", [])
                ready.set()
            elif mtype == "http.response.body":
                body = message.get("body", b"")
                more = message.get("more_body", False)
                if body:
                    chunk_q.put(body)
                if not more:
                    chunk_q.put(SENTINEL)
                    done.set()

        async def _run_app():
            try:
                # Capture the running task so ``__exit__`` can
                # cancel it explicitly when the client exits early.
                app_task_holder["task"] = asyncio.current_task()
                await self._app(scope, _receive, _send)
            except asyncio.CancelledError:
                # Client cancelled — propagate so the loop unwinds.
                raise
            finally:
                if not done.is_set():
                    chunk_q.put(SENTINEL)
                    done.set()

        # Schedule the app on the background loop.
        future = asyncio.run_coroutine_threadsafe(_run_app(), self._loop)

        # Wait for headers (or completion if the app errored).
        if not ready.wait(timeout=10):
            future.cancel()
            raise RuntimeError("ASGI app did not send response headers within 10s")

        status_code = status_holder["status"]
        # Build httpx-style Headers from raw list.
        from httpx import Headers as _Hdr
        decoded = [
            (
                k.decode("latin-1") if isinstance(k, bytes) else str(k),
                v.decode("latin-1") if isinstance(v, bytes) else str(v),
            )
            for k, v in status_holder["headers"]
        ]
        response_headers = _Hdr(decoded)

        class _StreamFacade:
            status_code = None  # set below
            headers = None
            url = full_url

            def __enter__(_self):
                return _self

            def __exit__(_self, *exc):
                # Signal the receive loop that the client has gone.
                # The next ``await receive()`` returns
                # ``http.disconnect`` so the app's body generator
                # observes ``GeneratorExit`` and unwinds.
                client_disconnected.set()
                # Do NOT drain ``chunk_q`` here — with an unbounded
                # queue the producer never blocks on ``put``, so
                # there's no deadlock to break. A drain loop would
                # spin forever against a no-sleep infinite generator
                # because new chunks land as fast as we pull them,
                # and we'd never reach the cancellation code below.
                # Give the app a brief beat to honour the disconnect
                # naturally (handlers that ``await ws.receive()``
                # observe the ``http.disconnect`` reply); then
                # cancel explicitly for handlers that ignore
                # ``receive()`` entirely (the common
                # ``async for chunk in source: yield chunk`` shape).
                try:
                    future.result(timeout=0.1)
                except Exception:  # noqa: BLE001
                    pass
                if not future.done():
                    task = app_task_holder.get("task")
                    if task is not None:
                        loop.call_soon_threadsafe(task.cancel)
                    try:
                        future.result(timeout=2)
                    except Exception:  # noqa: BLE001
                        pass
                return None

            def iter_bytes(_self, chunk_size: int | None = None):
                while True:
                    item = chunk_q.get()
                    if item is SENTINEL:
                        return
                    if chunk_size is None or len(item) <= chunk_size:
                        yield item
                    else:
                        for i in range(0, len(item), chunk_size):
                            yield item[i : i + chunk_size]

            def _charset(_self) -> str:
                ct = response_headers.get("content-type", "")
                if "charset=" in ct:
                    return (
                        ct.split("charset=", 1)[1].strip().split(";", 1)[0]
                    )
                return "utf-8"

            def iter_text(_self, chunk_size: int | None = None):
                charset = _self._charset()
                for c in _self.iter_bytes(chunk_size):
                    yield c.decode(charset, errors="replace")

            def read(_self) -> bytes:
                return b"".join(_self.iter_bytes())

            @property
            def text(_self) -> str:
                return _self.read().decode(_self._charset(), errors="replace")

            @property
            def content(_self) -> bytes:
                return _self.read()

        _StreamFacade.status_code = status_code
        _StreamFacade.headers = response_headers
        return _StreamFacade()

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

        # Three-state flag:
        #   ``in_process=None`` (default): auto-fallback allowed. If the
        #     loopback bind fails we transparently switch to ASGI.
        #   ``in_process=True``: force ASGI path now.
        #   ``in_process=False``: force real-HTTP path. Bind failures
        #     must SURFACE (user is testing the Rust/Tower path).
        # ``_auto_fallback`` captures the "didn't explicitly opt out"
        # bit so ``_ensure_started`` can honour it.
        if in_process is None:
            import os as _os
            env = _os.environ.get("FASTAPI_TURBO_TESTCLIENT_IN_PROCESS")
            self._in_process = env is not None and env.lower() in ("1", "true", "yes")
            self._auto_fallback = True
        else:
            self._in_process = bool(in_process)
            self._auto_fallback = False
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

    def _switch_to_in_process(self) -> None:
        """Auto-fallback path: the user didn't explicitly ask for
        in-process, but the loopback bind failed (sandboxed env,
        serverless, a network namespace without loopback access). Flip
        to ASGI dispatch so the TestClient stays usable without
        requiring every call site to pass ``in_process=True``."""
        self._client = _ASGISyncClientShim(
            app=self.app,
            base_url=self._base_url,
            follow_redirects=self._follow_redirects,
            headers=dict(self._seed_headers or {}) or None,
            cookies=self._seed_cookies,
            raise_app_exceptions=self._raise_server_exceptions,
        )
        self._started = True
        self._port = None
        self._thread = None
        self._in_process = True
        self._follow_state = threading.local()
        self._follow_state.follow = self._follow_redirects

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
            self._switch_to_in_process()
            return
        # Auto-fallback: if bind() fails (sandboxed env, serverless,
        # CAP_NET_BIND_SERVICE missing), flip to in-process — BUT only
        # when ``in_process=None`` was used (default path). An explicit
        # ``in_process=False`` means the user is specifically testing
        # the real-HTTP path; surface the bind error instead of
        # silently degrading.
        try:
            _probe_port = self._find_free_port()
        except (OSError, PermissionError):
            if self._auto_fallback:
                self._switch_to_in_process()
                return
            raise
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

        # Starlette's TestClient pins ``Accept-Encoding: gzip,
        # deflate`` as a default so request snapshots are stable
        # regardless of which compression libraries the test
        # environment has installed. httpx's default includes ``br``
        # when ``brotli`` is on sys.path, which leaks into upstream
        # FastAPI tutorial test snapshots (``test_tutorial001`` and
        # similar) and produces a 6-test failure that has nothing to
        # do with our code. Match Starlette by injecting the same
        # default; user ``headers={...}`` overrides win.
        default_headers = {
            "host": host_header,
            "user-agent": "testclient",
            "accept-encoding": "gzip, deflate",
        }
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

    def stream(self, method: str, url: str, **kwargs: Any):
        """Streaming-response context manager (httpx parity).

        Routes through ``self._client.stream`` which exists on both
        backing clients: real ``httpx.Client`` (when bound to a real
        loopback server) and ``_ASGISyncClientShim`` (in-process
        fallback). Both return a context manager that yields a
        response with ``status_code`` / ``headers`` /
        ``iter_bytes`` / ``iter_text`` etc.
        """
        self._ensure_started()
        self._track_follow(kwargs)
        return self._client.stream(method, url, **kwargs)

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

        # In-process / fallback path: there is no real loopback port,
        # so build an ASGI WebSocket scope and drive ``self.app`` via
        # asyncio queues. Without this we'd construct
        # ``ws://127.0.0.1:None{url}`` and ``websockets.sync.client``
        # would crash with ``ValueError: Port could not be cast to
        # integer value as 'None'``.
        if getattr(self, "_in_process", False) or self._port is None:
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
            hdrs_list: list[tuple[str, str]] = []
            if headers:
                for k, v in dict(headers).items():
                    hdrs_list.append((k, v))
            ws_path = url
            if not ws_path.startswith("/"):
                # Allow callers to pass a full ``ws://host/path`` URL.
                from urllib.parse import urlparse as _up
                parsed = _up(url)
                ws_path = parsed.path + (
                    f"?{parsed.query}" if parsed.query else ""
                )
            if not ws_path.startswith("/"):
                ws_path = "/" + ws_path
            inproc_url = f"ws://testserver{ws_path}"
            return _InProcessWebSocketSession(
                app=self.app,
                url=inproc_url,
                subprotocols=subprotocols,
                headers=hdrs_list,
                cookies=cookie_jar,
            )

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


class _InProcessWebSocketSession:
    """ASGI-driven WebSocket session for the in-process / fallback
    ``TestClient`` path.

    No real socket — drives the app's ``websocket`` scope through
    asyncio queues so ``ws://127.0.0.1:None`` (the broken loopback
    URL when ``_port=None``) is never built. Implements the same
    surface as ``_WebSocketTestSession`` for the methods Starlette
    tests commonly use: ``send_text`` / ``send_bytes`` /
    ``send_json``, the matching ``receive_*``, ``close``,
    ``accepted_subprotocol``."""

    def __init__(
        self,
        app: Any,
        url: str,
        subprotocols: list[str] | None = None,
        headers: list[tuple[str, str]] | None = None,
        cookies: dict[str, str] | None = None,
    ):
        self._app = app
        self._url = url
        self._subprotocols = subprotocols or []
        self._headers = list(headers or [])
        self._cookies = dict(cookies or {})
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="fastapi-turbo-ws-inproc",
        )
        self._thread.start()
        self._client_to_server: Any = None
        self._server_to_client: Any = None
        self._app_task: Any = None
        self._accepted = False
        self._accepted_subprotocol: str | None = None
        self._closed = False

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def __enter__(self) -> "_InProcessWebSocketSession":
        from urllib.parse import urlparse

        u = urlparse(self._url)
        path = u.path or "/"
        query = u.query.encode("latin-1") if u.query else b""

        hdrs: list[tuple[bytes, bytes]] = []
        for k, v in self._headers:
            hdrs.append(
                (
                    k.lower().encode("latin-1"),
                    v.encode("latin-1"),
                )
            )
        if self._cookies:
            cookie = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
            hdrs.append((b"cookie", cookie.encode("latin-1")))
        if not any(h[0] == b"host" for h in hdrs):
            hdrs.append((b"host", b"testserver"))

        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": path,
            "raw_path": path.encode("latin-1"),
            "query_string": query,
            "headers": hdrs,
            "subprotocols": list(self._subprotocols),
            "app": self._app,
        }

        async def _setup():
            self._client_to_server = asyncio.Queue()
            self._server_to_client = asyncio.Queue()
            await self._client_to_server.put({"type": "websocket.connect"})

            async def _receive():
                return await self._client_to_server.get()

            async def _send(msg):
                await self._server_to_client.put(msg)

            self._app_task = asyncio.create_task(
                self._app(scope, _receive, _send)
            )
            return await self._server_to_client.get()

        first_msg = self._run(_setup())
        if first_msg["type"] == "websocket.accept":
            self._accepted = True
            self._accepted_subprotocol = first_msg.get("subprotocol")
        elif first_msg["type"] == "websocket.close":
            from fastapi_turbo.exceptions import WebSocketDisconnect
            raise WebSocketDisconnect(code=first_msg.get("code", 1000))
        else:
            raise RuntimeError(
                f"unexpected first WS server message: {first_msg!r}"
            )
        return self

    def __exit__(self, *exc) -> None:
        self.close()
        self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def accepted_subprotocol(self) -> str | None:
        return self._accepted_subprotocol

    def send_text(self, text: str) -> None:
        async def _put():
            await self._client_to_server.put(
                {"type": "websocket.receive", "text": text}
            )
        self._run(_put())

    def send_bytes(self, data: bytes) -> None:
        async def _put():
            await self._client_to_server.put(
                {"type": "websocket.receive", "bytes": bytes(data)}
            )
        self._run(_put())

    def send_json(self, data: Any, mode: str = "text") -> None:
        import json
        encoded = json.dumps(data)
        if mode == "binary":
            self.send_bytes(encoded.encode("utf-8"))
        else:
            self.send_text(encoded)

    def _next_message(self) -> dict[str, Any]:
        async def _get():
            return await self._server_to_client.get()
        msg = self._run(_get())
        if msg["type"] == "websocket.close":
            self._closed = True
            from fastapi_turbo.exceptions import WebSocketDisconnect
            raise WebSocketDisconnect(
                code=msg.get("code", 1000),
                reason=msg.get("reason", "") or "",
            )
        return msg

    def receive_text(self) -> str:
        msg = self._next_message()
        return msg.get("text", "")

    def receive_bytes(self) -> bytes:
        msg = self._next_message()
        return msg.get("bytes", b"")

    def receive_json(self, mode: str = "text") -> Any:
        import json
        if mode == "binary":
            return json.loads(self.receive_bytes())
        return json.loads(self.receive_text())

    def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True

        async def _disconnect():
            try:
                await self._client_to_server.put(
                    {"type": "websocket.disconnect", "code": code}
                )
            except Exception:  # noqa: BLE001
                pass
            if self._app_task is not None:
                try:
                    await asyncio.wait_for(self._app_task, timeout=1.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass

        try:
            self._run(_disconnect())
        except Exception:  # noqa: BLE001
            pass


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
