"""Starlette-compatible Request class.

Wraps a dict-based scope. Many plugins and middleware check
``isinstance(request, Request)`` so this must exist.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi_turbo.datastructures import FormData

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
        # State must be SHARED across every Request/HTTPConnection
        # built from the same scope so middleware mutations
        # (``request.state.user = X``) propagate to the endpoint that
        # injects ``request: Request`` and reads ``request.state.user``
        # downstream. Storing per-instance (the previous
        # ``self._state``) made each builder produce its own state
        # object, breaking BaseHTTPMiddleware semantics.
        existing = self._scope.get("state")
        if isinstance(existing, State):
            return existing
        s = State()
        if existing:
            # Seed with lifespan-yielded state (a dict from a lifespan
            # context manager) or the owning app's ``_app_state``.
            for k, v in existing.items():
                setattr(s, k, v)
        else:
            app = self._scope.get("app")
            seed = getattr(app, "_app_state", None) if app is not None else None
            if seed:
                for k, v in seed.items():
                    setattr(s, k, v)
        self._scope["state"] = s
        return s


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
        # See ``HTTPConnection.state`` — state lives in scope, not on
        # the Request instance, so middleware mutations propagate.
        existing = self._scope.get("state")
        if isinstance(existing, State):
            return existing
        s = State()
        if existing:
            for k, v in existing.items():
                setattr(s, k, v)
        else:
            app = self._scope.get("app")
            seed = getattr(app, "_app_state", None) if app is not None else None
            if seed:
                for k, v in seed.items():
                    setattr(s, k, v)
        self._scope["state"] = s
        return s

    @state.setter
    def state(self, value: State) -> None:
        self._scope["state"] = value

    @property
    def user(self):
        """Authenticated user — populated by ``AuthenticationMiddleware``.

        Matches Starlette: ``request.user`` raises ``AssertionError``
        ("AuthenticationMiddleware must be installed to access
        request.user") when no auth middleware has set
        ``scope['user']``. The previous permissive fallback (return
        an ``UnauthenticatedUser`` sentinel) silently let auth-aware
        endpoints succeed without auth wired in — a real shipping
        risk for handlers that gate on ``if not request.user``."""
        if "user" not in self._scope:
            raise AssertionError(
                "AuthenticationMiddleware must be installed to access "
                "request.user"
            )
        return self._scope["user"]

    @property
    def auth(self):
        """AuthCredentials — populated by ``AuthenticationMiddleware``.

        Matches Starlette: raises ``AssertionError`` when the
        middleware hasn't run."""
        if "auth" not in self._scope:
            raise AssertionError(
                "AuthenticationMiddleware must be installed to access "
                "request.auth"
            )
        return self._scope["auth"]

    @property
    def session(self):
        """Session dict — populated by ``SessionMiddleware``.

        Matches Starlette: raises ``AssertionError`` when the
        middleware hasn't run."""
        if "session" not in self._scope:
            raise AssertionError(
                "SessionMiddleware must be installed to access "
                "request.session"
            )
        return self._scope["session"]

    @property
    def receive(self):
        """The ASGI receive callable (Starlette-compatible)."""
        return self._receive

    async def is_disconnected(self) -> bool:
        """Probe whether the client has disconnected.

        Schedules a single ``receive()`` task, lets the loop run
        one tick (``await asyncio.sleep(0)``), then either reads
        the result if it completed synchronously or cancels.
        ``asyncio.wait_for(receive(), 0)`` would NOT work — it
        cancels the task before it ever runs, so a receive that
        could return synchronously (e.g. a pre-stashed
        ``http.disconnect``) never gets a chance.

        Caveat (matches Starlette): if you call ``is_disconnected
        ()`` BEFORE draining the request body, a pending body
        chunk may be silently consumed by this peek. The standard
        advice is to call it inside long-running streaming
        handlers (SSE / long-poll) where the body is already
        drained.

        Idempotent — once a disconnect is observed, stays ``True``
        for the rest of the request lifetime so polling loops can
        check it cheaply each iteration. Returns ``False`` when no
        ``receive`` channel is available (Rust-bridged path with
        no Python receive bound)."""
        if getattr(self, "_disconnected", False):
            return True
        recv = self._receive
        if recv is None:
            return False
        import asyncio
        try:
            task = asyncio.ensure_future(recv())
        except Exception:  # noqa: BLE001
            return False
        # Yield one tick so the receive task can run. If receive
        # returns synchronously (no internal await), task.done()
        # will be True after the yield.
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return False
        try:
            msg = task.result()
        except Exception:  # noqa: BLE001
            return False
        if isinstance(msg, dict) and msg.get("type") == "http.disconnect":
            self._disconnected = True
            return True
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
        """Parse the request body as form data.

        Dispatches on ``Content-Type``:

          * ``application/x-www-form-urlencoded`` → ``parse_qsl``,
            multi-values preserved (``form.getlist("tag")`` works).
          * ``multipart/form-data`` → ``email.parser.BytesParser``
            applied to a synthetic ``Content-Type`` envelope, with
            file parts surfaced as ``UploadFile`` objects and text
            parts as plain strings. Same parser the in-process ASGI
            dispatcher uses (matches FastAPI parity for ``form: …
            = Form(...)`` / ``UploadFile`` endpoints).
          * Anything else → empty ``FormData``.

        ``max_files`` / ``max_fields`` cap the number of file and
        text parts respectively; exceeding either raises
        ``MultiPartException`` (becomes HTTP 400 in handlers), matching
        Starlette. Defaults match Starlette's 1000/1000.

        Earlier code unconditionally ran ``parse_qsl`` on the raw
        body, so ``await request.form()`` against a multipart
        upload returned the raw boundary text as a single garbled
        ``(key, value)`` pair instead of fields + ``UploadFile``."""
        if self._form is not None:
            return self._form
        from fastapi_turbo.datastructures import FormData

        # Don't lowercase the whole header — RFC 7578 boundaries are
        # case-sensitive (``boundary=AaB03x`` ≠ ``boundary=aab03x``).
        # Normalise only the media-type prefix for the dispatch check;
        # pass the original to the parser so it sees the real boundary.
        ct = self.headers.get("content-type") or ""
        ct_lower = ct.lower()
        raw = await self.body()
        if ct_lower.startswith("multipart/form-data"):
            self._form = self._parse_multipart_form(
                ct, raw, max_files=max_files, max_fields=max_fields
            )
        elif ct_lower.startswith("application/x-www-form-urlencoded") or not ct_lower:
            from urllib.parse import parse_qsl
            items = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
            self._form = FormData(items)
        else:
            self._form = FormData([])
        return self._form

    def _parse_multipart_form(
        self,
        content_type: str,
        raw_body: bytes,
        *,
        max_files: int = 1000,
        max_fields: int = 1000,
    ):
        """Parse a multipart body into a ``FormData`` with file parts
        materialised as ``UploadFile``. Mirrors the in-process ASGI
        dispatcher's parser so ``await request.form()`` and
        ``Form(...)`` / ``UploadFile`` injection see the same
        structure. ``max_files`` / ``max_fields`` cap the part counts —
        exceeding either raises ``MultiPartException`` (handled by
        FastAPI as a 400)."""
        import io
        import email.parser
        from fastapi_turbo.datastructures import FormData
        from fastapi_turbo.param_functions import UploadFile

        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip().strip('"')
                break
        if boundary is None:
            return FormData([])

        envelope = (
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n"
        ).encode("latin-1") + raw_body
        parser = email.parser.BytesParser()
        try:
            msg = parser.parsebytes(envelope)
        except Exception:  # noqa: BLE001
            return FormData([])

        items: list[tuple[str, Any]] = []
        files_seen = 0
        fields_seen = 0
        for part_msg in msg.walk():
            if part_msg.is_multipart():
                continue
            cd = part_msg.get("content-disposition", "")
            if not cd:
                continue
            params: dict[str, str] = {}
            for seg in cd.split(";"):
                seg = seg.strip()
                if "=" in seg:
                    k, v = seg.split("=", 1)
                    params[k.strip()] = v.strip().strip('"')
            fname = params.get("name")
            if fname is None:
                continue
            if "filename" in params:
                files_seen += 1
                if files_seen > max_files:
                    from fastapi_turbo.exceptions import MultiPartException
                    raise MultiPartException(
                        f"Too many files. Maximum number of files is {max_files}."
                    )
                payload = part_msg.get_payload(decode=True) or b""
                upload = UploadFile(
                    filename=params["filename"],
                    file=io.BytesIO(payload),
                    content_type=part_msg.get_content_type(),
                )
                items.append((fname, upload))
            else:
                fields_seen += 1
                if fields_seen > max_fields:
                    from fastapi_turbo.exceptions import MultiPartException
                    raise MultiPartException(
                        f"Too many fields. Maximum number of fields is {max_fields}."
                    )
                val = part_msg.get_payload(decode=True) or b""
                if isinstance(val, bytes):
                    val = val.decode("utf-8", errors="replace")
                items.append((fname, val))
        return FormData(items)

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
