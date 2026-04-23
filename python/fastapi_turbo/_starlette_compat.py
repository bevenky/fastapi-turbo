"""Starlette-only compatibility classes that fastapi-turbo doesn't otherwise
expose as first-class features but needs for `isinstance()` checks and
`from starlette.* import ...` patterns used by third-party middleware.

None of these are the performance hot path — they're a thin layer to keep
drop-in compatibility with Starlette's public API surface.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping

# ── Type aliases used by Starlette-typed middleware ────────────────
# These match `starlette.types.*` exactly.
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


# ── Starlette routing primitives ────────────────────────────────────
# Re-export fastapi-turbo's APIRoute/APIRouter as starlette.routing.Route/etc.
# Additional classes: Mount, Host, WebSocketRoute.


class Route:
    """Starlette-compatible Route placeholder. Holds route metadata."""

    def __init__(
        self,
        path: str,
        endpoint: Callable,
        *,
        methods: list[str] | None = None,
        name: str | None = None,
        include_in_schema: bool = True,
    ):
        self.path = path
        self.endpoint = endpoint
        self.methods = methods or ["GET"]
        self.name = name or endpoint.__name__
        self.include_in_schema = include_in_schema


class WebSocketRoute:
    """Starlette-compatible WebSocketRoute placeholder."""

    def __init__(self, path: str, endpoint: Callable, *, name: str | None = None):
        self.path = path
        self.endpoint = endpoint
        self.name = name or endpoint.__name__


class Mount:
    """Starlette-compatible Mount — attach a sub-app or service at a prefix.

    Constructor shape matches Starlette: ``Mount(path, app=... | routes=...)``.
    When used with ``FastAPI.mount()`` we dispatch strip-prefix requests
    directly to the inner ASGI app.
    """

    def __init__(
        self,
        path: str,
        app=None,
        routes: list | None = None,
        name: str | None = None,
    ):
        self.path = path.rstrip("/")
        self.path_regex = self.path + "/{path:path}"
        self.app = app
        self.routes = routes or []
        self.name = name

    def matches(self, scope) -> bool:
        if scope["type"] not in ("http", "websocket"):
            return False
        req_path = scope.get("path", "")
        return req_path == self.path or req_path.startswith(self.path + "/")

    async def __call__(self, scope, receive, send):
        if self.app is None:
            raise RuntimeError("Mount has no inner app")
        # Strip the mount prefix before delegating, per Starlette convention.
        sub_scope = dict(scope)
        full_path = scope.get("path", "")
        sub_scope["path"] = full_path[len(self.path):] or "/"
        sub_scope["root_path"] = scope.get("root_path", "") + self.path
        await self.app(sub_scope, receive, send)


class Host:
    """Starlette-compatible Host — scope subrouter by Host header.

    fastapi-turbo doesn't route by host natively; this is a stub for
    code that does `isinstance(r, Host)` or imports the symbol.
    """

    def __init__(self, host: str, app=None, name: str | None = None):
        self.host = host
        self.app = app
        self.name = name


# ── Endpoint classes ────────────────────────────────────────────────


class HTTPEndpoint:
    """Starlette-compatible class-based endpoint.

    Subclass and define methods like ``get``, ``post``, ``put``, etc.
    The class is an ASGI app:

        class HomeEndpoint(HTTPEndpoint):
            async def get(self, request):
                return JSONResponse({"ok": True})

        app.add_route("/", HomeEndpoint)

    Starlette's pattern: calling the class with (scope, receive, send)
    constructs a new instance and dispatches to the method handler.
    """

    def __init__(self, scope=None, receive=None, send=None):
        self.scope = scope
        self.receive = receive
        self.send = send
        # Make the class itself callable as an ASGI factory:
        # Starlette treats the class object as the callable — when a
        # request comes in, it invokes ``cls(scope, receive, send)`` and
        # then awaits the returned instance's dispatch. Our `__await__`
        # below dispatches.

    def __await__(self):
        return self._dispatch().__await__()

    async def _dispatch(self):
        from fastapi_turbo.requests import Request
        from fastapi_turbo.exceptions import HTTPException
        request = Request(self.scope, self.receive, self.send)
        handler_name = (self.scope or {}).get("method", "GET").lower()
        handler = getattr(self, handler_name, None)
        if handler is None:
            # method_not_allowed default
            response = await _maybe_await(self.method_not_allowed(request))
        else:
            response = await _maybe_await(handler(request))
        if response is not None:
            await _send_response(response, self.send)

    async def method_not_allowed(self, request):
        from fastapi_turbo.responses import PlainTextResponse
        return PlainTextResponse("Method Not Allowed", status_code=405)


class WebSocketEndpoint:
    """Starlette-compatible class-based WebSocket endpoint.

    Subclass and define ``on_connect``, ``on_receive``, ``on_disconnect``.
    Set ``encoding`` to "text" / "bytes" / "json" to auto-decode frames.
    """

    encoding: str | None = None

    def __init__(self, scope=None, receive=None, send=None):
        self.scope = scope
        self.receive = receive
        self.send = send

    def __await__(self):
        return self._dispatch().__await__()

    async def _dispatch(self):
        # Build a WebSocket-like wrapper around the scope so the user's
        # methods can use the standard fastapi-turbo API.
        from fastapi_turbo.websockets import WebSocket
        websocket = WebSocket(scope=self.scope, receive=self.receive, send=self.send)
        close_code = 1000
        try:
            await self.on_connect(websocket)
            while True:
                if self.encoding == "text":
                    message = await websocket.receive_text()
                elif self.encoding == "bytes":
                    message = await websocket.receive_bytes()
                elif self.encoding == "json":
                    message = await websocket.receive_json()
                else:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        close_code = message.get("code", 1000)
                        break
                    # Extract body if present
                    if "text" in message:
                        message = message["text"]
                    elif "bytes" in message:
                        message = message["bytes"]
                await self.on_receive(websocket, message)
        except Exception:
            close_code = 1011
            raise
        finally:
            await self.on_disconnect(websocket, close_code)

    async def on_connect(self, websocket):
        await websocket.accept()

    async def on_receive(self, websocket, data):
        pass

    async def on_disconnect(self, websocket, close_code: int):
        pass


# ── Middleware wrapper ──────────────────────────────────────────────


class Middleware:
    """Starlette-compatible Middleware spec — tuples of (cls, kwargs) used
    when building an app with ``middleware=[Middleware(MyMw, arg=1), ...]``.
    """

    def __init__(self, cls, *args, **kwargs):
        self.cls = cls
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        name = getattr(self.cls, "__name__", str(self.cls))
        return f"Middleware({name}, ...)"


# ── Built-in error/exception middleware stubs ───────────────────────


class ServerErrorMiddleware:
    """Catch unhandled exceptions from the inner app, optionally call a
    custom handler, then return a 500 response. Matches
    ``starlette.middleware.errors.ServerErrorMiddleware`` semantics.
    """

    def __init__(self, app, handler=None, debug: bool = False):
        self.app = app
        self.handler = handler
        self.debug = debug

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            if not response_started:
                if self.handler is not None:
                    from fastapi_turbo.requests import Request
                    request = Request(scope, receive, send)
                    response = await _maybe_await(self.handler(request, exc))
                    await _send_response(response, send)
                else:
                    # Minimal plain-text 500
                    await send({
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    })
                    await send({
                        "type": "http.response.body",
                        "body": b"Internal Server Error",
                    })
            raise


class ExceptionMiddleware:
    """Map exception classes (or int status codes) to handlers — the
    handler receives ``(request, exc)`` and returns a Response. Matches
    ``starlette.middleware.exceptions.ExceptionMiddleware``.
    """

    def __init__(self, app, handlers=None, debug: bool = False):
        self.app = app
        self._status_handlers: dict[int, Callable] = {}
        self._exc_handlers: dict[type, Callable] = {}
        for key, handler in (handlers or {}).items():
            self.add_exception_handler(key, handler)
        self.debug = debug

    def add_exception_handler(self, key, handler) -> None:
        if isinstance(key, int):
            self._status_handlers[key] = handler
        elif isinstance(key, type) and issubclass(key, Exception):
            self._exc_handlers[key] = handler
        else:
            raise TypeError(f"Invalid exception handler key: {key!r}")

    def _lookup_handler(self, exc: BaseException):
        # Exact class → MRO walk
        for cls in type(exc).__mro__:
            if cls in self._exc_handlers:
                return self._exc_handlers[cls]
        # HTTPException-style: has status_code attr
        code = getattr(exc, "status_code", None)
        if isinstance(code, int) and code in self._status_handlers:
            return self._status_handlers[code]
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            handler = self._lookup_handler(exc)
            if handler is None or response_started:
                raise
            from fastapi_turbo.requests import Request
            request = Request(scope, receive, send)
            response = await _maybe_await(handler(request, exc))
            await _send_response(response, send)


async def _maybe_await(v):
    import inspect
    if inspect.isawaitable(v):
        return await v
    return v


async def _send_response(response, send) -> None:
    """Serialise a fastapi-turbo Response object over ASGI send."""
    if response is None:
        return
    status = getattr(response, "status_code", 200)
    headers_dict = getattr(response, "headers", {}) or {}
    body = getattr(response, "body", b"") or b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    raw_headers = []
    for k, v in headers_dict.items():
        raw_headers.append((
            k.encode("latin-1") if isinstance(k, str) else k,
            v.encode("latin-1") if isinstance(v, str) else v,
        ))
    await send({
        "type": "http.response.start",
        "status": int(status),
        "headers": raw_headers,
    })
    await send({"type": "http.response.body", "body": body})


class WSGIMiddleware:
    """Translate an ASGI scope into a WSGI environ, call the WSGI app in
    a threadpool, then stream its response back over ASGI send.

    Functionally equivalent to Starlette's `starlette.middleware.wsgi`.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            raise RuntimeError("WSGIMiddleware can only handle HTTP requests")
        await _WSGIResponder(self.app, scope, receive, send).__call__()


def _build_wsgi_environ(scope, body: bytes) -> dict:
    import io

    headers_dict = {}
    for raw_k, raw_v in scope.get("headers", []):
        k = raw_k.decode("latin-1") if isinstance(raw_k, bytes) else raw_k
        v = raw_v.decode("latin-1") if isinstance(raw_v, bytes) else raw_v
        k_norm = "HTTP_" + k.upper().replace("-", "_")
        if k.lower() == "content-type":
            headers_dict["CONTENT_TYPE"] = v
        elif k.lower() == "content-length":
            headers_dict["CONTENT_LENGTH"] = v
        else:
            headers_dict[k_norm] = v

    server = scope.get("server") or ("localhost", 80)
    client = scope.get("client") or ("", 0)
    qs = scope.get("query_string", b"")
    if isinstance(qs, bytes):
        qs = qs.decode("latin-1")

    environ = {
        "REQUEST_METHOD": scope.get("method", "GET"),
        "SCRIPT_NAME": scope.get("root_path", ""),
        "PATH_INFO": scope.get("path", "/"),
        "QUERY_STRING": qs,
        "SERVER_NAME": server[0],
        "SERVER_PORT": str(server[1]),
        "SERVER_PROTOCOL": f"HTTP/{scope.get('http_version', '1.1')}",
        "REMOTE_ADDR": client[0] or "",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": io.BytesIO(body),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        **headers_dict,
    }
    return environ


class _WSGIResponder:
    def __init__(self, app, scope, receive, send):
        self.app = app
        self.scope = scope
        self.receive = receive
        self.send = send
        self.status = None
        self.response_headers = None
        self.exc_info = None
        self.response_started = False

    async def __call__(self):
        import asyncio
        import anyio.to_thread as _to_thread  # reuse anyio if present

        # Collect the full request body
        body = b""
        more = True
        while more:
            msg = await self.receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)

        environ = _build_wsgi_environ(self.scope, body)

        def run_wsgi():
            chunks = list(self.app(environ, self._start_response))
            return chunks

        try:
            chunks = await _to_thread.run_sync(run_wsgi)
        except Exception:
            # Fallback to plain threadpool if anyio isn't available
            loop = asyncio.get_running_loop()
            chunks = await loop.run_in_executor(None, run_wsgi)

        # Normalize headers
        status_code = int(self.status.split(" ", 1)[0]) if self.status else 200
        raw_headers = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in (self.response_headers or [])
        ]
        await self.send({
            "type": "http.response.start",
            "status": status_code,
            "headers": raw_headers,
        })
        for chunk in chunks:
            await self.send({
                "type": "http.response.body",
                "body": chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8"),
                "more_body": True,
            })
        await self.send({"type": "http.response.body", "body": b"", "more_body": False})

    def _start_response(self, status, response_headers, exc_info=None):
        self.status = status
        self.response_headers = response_headers
        self.exc_info = exc_info
        return lambda chunk: None


# ── URL convertors ──────────────────────────────────────────────────


class Convertor:
    """Base class for path-segment convertors — matches Starlette's interface."""

    regex: str = ".*"

    def convert(self, value: str):
        return value

    def to_string(self, value) -> str:
        return str(value)


class StringConvertor(Convertor):
    regex = "[^/]+"


class PathConvertor(Convertor):
    regex = ".*"


class IntegerConvertor(Convertor):
    regex = "[0-9]+"

    def convert(self, value: str) -> int:
        return int(value)


class FloatConvertor(Convertor):
    regex = r"[0-9]+(\.[0-9]+)?"

    def convert(self, value: str) -> float:
        return float(value)


class UUIDConvertor(Convertor):
    regex = (
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )


CONVERTOR_TYPES = {
    "str": StringConvertor(),
    "path": PathConvertor(),
    "int": IntegerConvertor(),
    "float": FloatConvertor(),
    "uuid": UUIDConvertor(),
}


# ── Form parsers ────────────────────────────────────────────────────
# Thin stubs — the actual parsing happens on the Rust side.


class FormParser:
    """Stub for ``starlette.formparsers.FormParser``.

    Only kept for import-compat; fastapi-turbo parses forms in Rust.
    """

    def __init__(self, headers=None, stream=None):
        self.headers = headers
        self.stream = stream


class MultiPartParser:
    """Stub for ``starlette.formparsers.MultiPartParser``.

    Kept for import-compat; real parsing happens in Rust.
    """

    def __init__(self, headers=None, stream=None):
        self.headers = headers
        self.stream = stream


# ── Schema generator ────────────────────────────────────────────────


class SchemaGenerator:
    """Starlette-compatible OpenAPI generator.

    Parses docstrings from endpoint functions in YAML form (Starlette's
    convention) and merges them into an OpenAPI base. fastapi-turbo's native
    OpenAPI generation is richer but this class is kept for users who
    explicitly construct ``SchemaGenerator(...)``.
    """

    def __init__(self, base_schema=None):
        self.base_schema = dict(base_schema or {})

    def get_schema(self, routes=None) -> dict:
        schema = dict(self.base_schema)
        schema.setdefault("paths", {})
        for route in (routes or []):
            path = getattr(route, "path", None)
            endpoint = getattr(route, "endpoint", None)
            if not path or endpoint is None:
                continue
            doc = getattr(endpoint, "__doc__", None)
            if not doc:
                continue
            import yaml  # soft dependency; Starlette also requires PyYAML
            try:
                parsed = yaml.safe_load(doc)
                if isinstance(parsed, dict):
                    schema["paths"].setdefault(path, {}).update(parsed)
            except Exception:
                continue
        return schema

    def OpenAPIResponse(self, request):
        from fastapi_turbo.responses import Response
        import yaml
        body = yaml.safe_dump(self.get_schema())
        return Response(content=body, media_type="application/yaml")


# ── has_required_scope helper ───────────────────────────────────────


def has_required_scope(request, scopes) -> bool:
    """Matches ``starlette.authentication.has_required_scope``.

    Returns True if the request's auth scopes include every scope in
    ``scopes`` (list of strings).
    """
    auth = getattr(request, "auth", None)
    auth_scopes = set()
    if auth is not None:
        auth_scopes = set(getattr(auth, "scopes", []))
    return all(scope in auth_scopes for scope in scopes)
