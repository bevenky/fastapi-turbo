"""Starlette-compatible Request class.

Wraps a dict-based scope. Many plugins and middleware check
``isinstance(request, Request)`` so this must exist.
"""

from __future__ import annotations

import json as _json
from http.cookies import SimpleCookie
from typing import Any

from fastapi_turbo.datastructures import URL, Address, Headers, QueryParams, State


class ClientDisconnect(Exception):
    """Starlette-compatible: raised when a client drops the connection
    mid-request (e.g. while streaming a request body or long polling).

    Middleware and endpoints that await ``request.receive()`` / iterate
    ``request.stream()`` catch this to short-circuit cleanly. Starlette
    uses plain ``Exception`` as the base; we follow suit.
    """


class HTTPConnection:
    """Starlette-compatible HTTPConnection base class.

    Shared base for Request and WebSocket in Starlette — provides the
    URL/header/cookie/client/state scope-derived properties. Many
    third-party middlewares do ``isinstance(conn, HTTPConnection)``.
    """

    def __init__(self, scope: dict[str, Any] | None = None, receive=None, send=None):
        self._scope = scope or {}
        self._receive = receive
        self._send = send
        self._cookies: dict[str, str] | None = None
        self._state: State | None = None
        # Starlette/FastAPI: scope["root_path"] carries the app mount/prefix
        # (set by reverse-proxy ASGI middleware or TestClient(root_path=...)).
        # Our Rust-built scope doesn't populate it; mirror it from app.root_path
        # when available so handlers doing request.scope.get("root_path") work.
        if "root_path" not in self._scope:
            app = self._scope.get("app")
            rp = getattr(app, "root_path", None)
            if rp:
                self._scope["root_path"] = rp

    @property
    def scope(self) -> dict[str, Any]:
        return self._scope

    @property
    def app(self):
        return self._scope.get("app")

    @property
    def url(self) -> URL:
        return URL(self._scope)

    @property
    def base_url(self) -> URL:
        scheme = self._scope.get("scheme", "http")
        # Starlette parity: prefer the ``Host`` header over
        # ``scope["server"]`` for constructing base_url so an
        # ``Host: testserver`` header produces
        # ``http://testserver/`` (no port) rather than
        # ``http://testserver:50009/``. ``scope["server"]`` is a
        # fallback for raw ASGI dispatch where there's no host header.
        host_hdr = None
        for _k, _v in self._scope.get("headers", []):
            _k_bytes = _k if isinstance(_k, bytes) else str(_k).encode()
            if _k_bytes.lower() == b"host":
                host_hdr = _v.decode() if isinstance(_v, bytes) else str(_v)
                break
        if host_hdr:
            return URL(f"{scheme}://{host_hdr}/")
        server = self._scope.get("server")
        host = server[0] if server else "localhost"
        port = server[1] if server else None
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            return URL(f"{scheme}://{host}:{port}/")
        return URL(f"{scheme}://{host}/")

    @property
    def headers(self) -> Headers:
        raw = self._scope.get("headers", [])
        return Headers(raw)

    @property
    def query_params(self) -> QueryParams:
        qs = self._scope.get("query_string", b"")
        return QueryParams(qs)

    @property
    def path_params(self) -> dict:
        return dict(self._scope.get("path_params", {}))

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            cookies: dict[str, str] = {}
            cookie_header = self.headers.get("cookie")
            if cookie_header:
                sc = SimpleCookie()
                sc.load(cookie_header)
                for key, morsel in sc.items():
                    # Starlette strips surrounding double quotes from
                    # cookie values (matches RFC 6265's quoted-string
                    # encoding). Without this our dict exposes `"quoted"`
                    # where FastAPI exposes `quoted`.
                    v = morsel.value
                    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                        v = v[1:-1]
                    cookies[key] = v
            self._cookies = cookies
        return self._cookies

    @property
    def client(self):
        c = self._scope.get("client")
        if c:
            return Address(c)
        return None

    @property
    def session(self) -> dict:
        return self._scope.setdefault("session", {})

    @property
    def auth(self):
        return self._scope.get("auth")

    @property
    def user(self):
        return self._scope.get("user")

    @property
    def state(self) -> State:
        if self._state is None:
            s = State()
            # Seed with lifespan-yielded state from scope["state"] or the
            # owning app's ``_app_state`` so handlers can do
            # ``request.state.<key>`` after a lifespan yielded a dict.
            seed = self._scope.get("state")
            if not seed:
                app = self._scope.get("app")
                seed = getattr(app, "_app_state", None) if app is not None else None
            if seed:
                for k, v in seed.items():
                    setattr(s, k, v)
            self._state = s
        return self._state


class Request(HTTPConnection):
    """Starlette-compatible Request wrapper.

    For now, wraps a simple dict-based scope since the Rust side
    does not yet pass a Request object.
    """

    def __init__(self, scope: dict[str, Any] | None = None, receive=None, send=None):
        super().__init__(scope, receive, send)
        # NOTE: ``_body`` is NOT initialised here — Starlette-parity.
        # Subclasses like FA's docs ``GzipRequest`` override ``body()``
        # with an ``if not hasattr(self, "_body"): ...`` guard so the
        # decompression hook fires exactly once. Initialising
        # ``self._body = None`` here would short-circuit that pattern.
        self._json: Any = None
        self._form: dict[str, Any] | None = None

    # Most properties (url, headers, query_params, path_params, cookies,
    # client, state, app, auth) are inherited from HTTPConnection.
    # Request-only: method + body/json/form access.

    @property
    def method(self) -> str:
        return self._scope.get("method", "GET")

    @property
    def state(self) -> State:
        if self._state is None:
            s = State()
            seed = self._scope.get("state")
            if not seed:
                app = self._scope.get("app")
                seed = getattr(app, "_app_state", None) if app is not None else None
            if seed:
                for k, v in seed.items():
                    setattr(s, k, v)
            self._state = s
        return self._state

    @state.setter
    def state(self, value: State) -> None:
        self._state = value

    @property
    def user(self):
        """Authenticated user (populated by AuthenticationMiddleware).

        Matches Starlette's Request.user: returns None until an auth backend
        sets scope['user']. When no user is authenticated, returns an
        UnauthenticatedUser-like sentinel that evaluates falsy.
        """
        from fastapi_turbo.authentication import UnauthenticatedUser

        return self._scope.get("user") or UnauthenticatedUser()

    @property
    def auth(self):
        """AuthCredentials (populated by AuthenticationMiddleware).

        Matches Starlette's Request.auth: returns an object exposing .scopes
        (list[str]). Returns empty AuthCredentials when no auth middleware ran.
        """
        from fastapi_turbo.authentication import AuthCredentials

        return self._scope.get("auth") or AuthCredentials()

    @property
    def session(self):
        """Session dict (populated by SessionMiddleware).

        Returns {} if SessionMiddleware isn't installed — matches Starlette
        if session cookie missing; real Starlette raises AssertionError if
        SessionMiddleware is fully absent. We're permissive.
        """
        return self._scope.setdefault("session", {})

    @property
    def receive(self):
        """The ASGI receive callable (Starlette-compatible)."""
        return self._receive

    async def is_disconnected(self) -> bool:
        """Check if the client has disconnected.

        Cannot reliably detect in our Rust-bridged architecture, so
        this always returns False. Matches the Starlette API surface.
        """
        return False

    def url_for(self, name: str, /, **path_params: Any) -> URL:
        """Return the full URL for a named route (includes scheme and host)."""
        app = self.app
        if app is None or not hasattr(app, "url_path_for"):
            raise RuntimeError("Request.url_for requires request.app with url_path_for")
        path = app.url_path_for(name, **path_params)
        base = str(self.base_url).rstrip("/")
        return URL(base + path)

    async def body(self) -> bytes:
        cached = getattr(self, "_body", None)
        if cached is not None:
            return cached
        body = self._scope.get("_body", b"")
        if body:
            self._body = body
            return body
        if self._receive is not None:
            chunks: list[bytes] = []
            while True:
                message = await self._receive()
                body_chunk = message.get("body", b"")
                if body_chunk:
                    chunks.append(body_chunk)
                if not message.get("more_body", False):
                    break
            self._body = b"".join(chunks)
        else:
            self._body = b""
        return self._body

    async def stream(self):
        """Async iterator yielding request body in chunks.

        Matches Starlette's Request.stream() — useful for large bodies where
        you don't want to buffer the whole thing in memory. Each chunk is a
        bytes object; the iterator ends when the body is fully read.

        Note: calling stream() consumes the body. Subsequent body()/json()/
        form() calls will return what stream already yielded.
        """
        cached = getattr(self, "_body", None)
        if cached is not None:
            # Body already buffered — yield once and done
            yield cached
            yield b""
            return
        body = self._scope.get("_body", b"")
        if body:
            self._body = body
            yield body
            yield b""
            return
        if self._receive is None:
            yield b""
            return
        chunks: list[bytes] = []
        while True:
            message = await self._receive()
            body_chunk = message.get("body", b"")
            if body_chunk:
                chunks.append(body_chunk)
                yield body_chunk
            if not message.get("more_body", False):
                break
        self._body = b"".join(chunks)
        yield b""  # Sentinel: Starlette ends streams with an empty chunk

    async def json(self) -> Any:
        if self._json is not None:
            return self._json
        raw = await self.body()
        self._json = _json.loads(raw)
        return self._json

    async def form(self, *, max_files: int = 1000, max_fields: int = 1000) -> "FormData":
        if self._form is not None:
            return self._form
        # Basic form parsing — assumes application/x-www-form-urlencoded.
        # Preserve multi-values as separate `(key, value)` entries so
        # `form.getlist("tag")` surfaces each occurrence individually.
        raw = await self.body()
        from urllib.parse import parse_qsl
        from fastapi_turbo.datastructures import FormData
        items = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
        self._form = FormData(items)
        return self._form

    async def close(self) -> None:
        pass

    def __getitem__(self, key: str) -> Any:
        return self._scope[key]

    def __iter__(self):
        return iter(self._scope)

    def __len__(self) -> int:
        return len(self._scope)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Request):
            return self._scope == other._scope
        return NotImplemented

    def __repr__(self) -> str:
        return f"Request(scope={self._scope!r})"
