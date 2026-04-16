"""Starlette-compatible Request class.

Wraps a dict-based scope. Many plugins and middleware check
``isinstance(request, Request)`` so this must exist.
"""

from __future__ import annotations

import json as _json
from http.cookies import SimpleCookie
from typing import Any

from fastapi_rs.datastructures import URL, Address, Headers, QueryParams, State


class Request:
    """Starlette-compatible Request wrapper.

    For now, wraps a simple dict-based scope since the Rust side
    does not yet pass a Request object.
    """

    def __init__(self, scope: dict[str, Any] | None = None, receive=None, send=None):
        self._scope = scope or {}
        self._receive = receive
        self._send = send
        self._body: bytes | None = None
        self._json: Any = None
        self._form: dict[str, Any] | None = None
        self._cookies: dict[str, str] | None = None
        self._state: State | None = None

    @property
    def scope(self) -> dict[str, Any]:
        return self._scope

    @property
    def method(self) -> str:
        return self._scope.get("method", "GET")

    @property
    def url(self) -> URL:
        return URL(self._scope)

    @property
    def base_url(self) -> URL:
        scope = dict(self._scope)
        scope["path"] = "/"
        scope["query_string"] = ""
        return URL(scope)

    @property
    def headers(self) -> Headers:
        return Headers(self._scope.get("headers", {}))

    @property
    def query_params(self) -> QueryParams:
        qs = self._scope.get("query_string", "")
        return QueryParams(qs)

    @property
    def path_params(self) -> dict[str, Any]:
        return self._scope.get("path_params", {})

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            self._cookies = {}
            headers = self.headers
            cookie_header = headers.get("cookie", "")
            if cookie_header:
                sc = SimpleCookie()
                sc.load(cookie_header)
                self._cookies = {key: morsel.value for key, morsel in sc.items()}
        return self._cookies

    @property
    def client(self) -> Address:
        return Address(self._scope.get("client", ("0.0.0.0", 0)))

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = State()
        return self._state

    @state.setter
    def state(self, value: State) -> None:
        self._state = value

    @property
    def app(self):
        return self._scope.get("app")

    @property
    def user(self):
        """Authenticated user (populated by AuthenticationMiddleware).

        Matches Starlette's Request.user: returns None until an auth backend
        sets scope['user']. When no user is authenticated, returns an
        UnauthenticatedUser-like sentinel that evaluates falsy.
        """
        from fastapi_rs.authentication import UnauthenticatedUser

        return self._scope.get("user") or UnauthenticatedUser()

    @property
    def auth(self):
        """AuthCredentials (populated by AuthenticationMiddleware).

        Matches Starlette's Request.auth: returns an object exposing .scopes
        (list[str]). Returns empty AuthCredentials when no auth middleware ran.
        """
        from fastapi_rs.authentication import AuthCredentials

        return self._scope.get("auth") or AuthCredentials()

    @property
    def session(self):
        """Session dict (populated by SessionMiddleware).

        Returns {} if SessionMiddleware isn't installed — matches Starlette
        if session cookie missing; real Starlette raises AssertionError if
        SessionMiddleware is fully absent. We're permissive.
        """
        return self._scope.setdefault("session", {})

    def url_for(self, name: str, /, **path_params: Any) -> URL:
        """Return the full URL for a named route (includes scheme and host)."""
        app = self.app
        if app is None or not hasattr(app, "url_path_for"):
            raise RuntimeError("Request.url_for requires request.app with url_path_for")
        path = app.url_path_for(name, **path_params)
        base = str(self.base_url).rstrip("/")
        return URL(base + path)

    async def body(self) -> bytes:
        if self._body is not None:
            return self._body
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
        if self._body is not None:
            # Body already buffered — yield once and done
            yield self._body
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

    async def form(self) -> dict[str, Any]:
        if self._form is not None:
            return self._form
        # Basic form parsing — assumes application/x-www-form-urlencoded
        raw = await self.body()
        from urllib.parse import parse_qs
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        self._form = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
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
