"""Frontend / low-priority route serving for the Rust door.

FastAPI 0.138 added ``app.frontend()`` / ``APIRouter.frontend()``: they register
low-priority routes (a ``_FrontendRouteGroup``) that serve a static SPA build
*only* when no normal route matched. FastAPI's custom ``Router.app`` checks
normal routes (full + partial) and the normal slash-redirect FIRST, and only
then falls through to ``_match_low_priority`` (frontend + any other
``_low_priority_routes``), and finally the default 404.

The Rust door dispatches normal routes, method-mismatch 405s, and slash
redirects itself; a genuine routing-miss lands in the door's 404 fallback
(``applications._rust_404_handler``). That fallback is therefore the exact
point FastAPI reaches ``_match_low_priority`` — so serving frontend there gives
byte-identical priority without re-implementing any of it. We reuse real
FastAPI/Starlette machinery (``Router._match_low_priority`` +
``_FrontendStaticFiles``), so fallback modes, Accept-header navigation
detection, longest-prefix matching, directory-index redirects, path-traversal
protection, symlink refusal, ETag/Last-Modified, and OpenAPI exclusion all
match upstream exactly.

The handler runs synchronously (it is called from the Rust 404 fallback); it
drives the matched route's ASGI ``handle`` on a private event loop and buffers
the response into ``(status, body, headers)``. ``HTTPException`` (404/405/401
from ``_FrontendStaticFiles``) is rendered through the app's registered
exception handlers, exactly like Starlette's ``ExceptionMiddleware``. A genuine
server error (``RuntimeError`` for a missing directory / fallback file, a
re-raised ``OSError``) is stashed on ``app._captured_server_exceptions`` so the
in-process door re-raises it to the caller (TestClient
``raise_server_exceptions=True``), matching upstream where such errors escape
to ``ServerErrorMiddleware``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.exceptions import HTTPException as _StarletteHTTPException
from starlette.routing import Match as _Match


def app_has_low_priority_routes(app: Any) -> bool:
    """True when ``app`` (or any router it includes, recursively) has any
    ``_low_priority_routes`` — i.e. ``frontend()`` or a hand-appended
    low-priority route. Used to decide whether to install the frontend-aware
    404 fallback at all (zero cost for apps that never call ``frontend()``).

    Note the two include lists: a FastAPI app tracks top-level includes on
    ``app._included_routers``, while a nested ``APIRouter`` tracks its own on
    ``router._included_routers``. ``frontend()`` on the root app populates
    ``app.router._low_priority_routes``."""

    def _router_check(router: Any) -> bool:
        if getattr(router, "_low_priority_routes", None):
            return True
        for entry in getattr(router, "_included_routers", []) or []:
            if _router_check(entry[0]):
                return True
        return False

    if getattr(app.router, "_low_priority_routes", None):
        return True
    for entry in getattr(app, "_included_routers", []) or []:
        if _router_check(entry[0]):
            return True
    return False


def _build_scope(app: Any, method: str, path: str, query: Any, headers: Any) -> dict:
    hdr_list: list[tuple[bytes, bytes]] = []
    for k, v in headers or []:
        if isinstance(k, str):
            k = k.encode("latin-1")
        if isinstance(v, str):
            v = v.encode("latin-1")
        hdr_list.append((bytes(k), bytes(v)))
    if isinstance(query, (bytes, bytearray)):
        qs = bytes(query)
    else:
        qs = (query or "").encode("latin-1")
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": qs,
        "root_path": "",
        "headers": hdr_list,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 0),
        "app": app,
        "path_params": {},
    }


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion on a private event loop. The frontend
    static-file path awaits ``run_in_threadpool`` (anyio) + streams a
    ``FileResponse`` — all asyncio-compatible, none of it needs the caller's
    loop, and this runs only on a routing-miss so a fresh loop is fine."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _dec(x: Any) -> str:
    return x.decode("latin-1") if isinstance(x, (bytes, bytearray)) else str(x)


class _Buffer:
    """Minimal ASGI receive/send that buffers one HTTP response."""

    def __init__(self) -> None:
        self.status = 500
        self.headers: list[tuple[Any, Any]] = []
        self.body = bytearray()

    async def receive(self) -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: dict) -> None:
        t = message["type"]
        if t == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers") or [])
        elif t == "http.response.body":
            self.body += message.get("body", b"") or b""

    def result(self) -> tuple[int, bytes, list[tuple[str, str]]]:
        return (
            self.status,
            bytes(self.body),
            [(_dec(k), _dec(v)) for k, v in self.headers],
        )


def _render_http_exception(
    app: Any, scope: dict, exc: _StarletteHTTPException
) -> tuple[int, bytes, list[tuple[str, str]]]:
    """Render an ``HTTPException`` raised by ``_FrontendStaticFiles`` the same
    way Starlette's ``ExceptionMiddleware`` would: a status-code handler wins
    over a class handler, else FastAPI's default ``{"detail": ...}`` body."""
    handlers = getattr(app, "exception_handlers", {}) or {}
    handler = handlers.get(exc.status_code)
    if handler is None:
        for klass in type(exc).__mro__:
            handler = handlers.get(klass)
            if handler is not None:
                break
    from fastapi_turbo.requests import Request as _Request

    request = _Request(scope)
    if handler is not None:
        result = handler(request, exc)
        if asyncio.iscoroutine(result):
            result = _run(result)
    else:
        from fastapi_turbo.responses import JSONResponse as _JSONResponse

        result = _JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )
    buf = _Buffer()
    _run(result(scope, buf.receive, buf.send))
    return buf.result()


def _drive_route(
    app: Any, route: Any, scope: dict
) -> tuple[int, bytes, list[tuple[str, str]]]:
    """Drive one matched route's ASGI ``handle`` and buffer its response.

    ``HTTPException`` is rendered through the app's exception handlers; a
    genuine server error (RuntimeError / OSError) is stashed on
    ``_captured_server_exceptions`` so the in-process door re-raises it to the
    caller, and a 500 is returned for the socket path."""
    buf = _Buffer()

    async def _drive() -> None:
        # FastAPI's ``APIRoute.app`` asserts an ``AsyncExitStack`` under
        # ``fastapi_middleware_astack`` (normally injected by
        # ``AsyncExitStackMiddleware``); it holds yield-dependency / UploadFile
        # cleanup. ``_FrontendRoute`` (a plain ASGI app) ignores it, so setting
        # it unconditionally is harmless.
        import contextlib

        async with contextlib.AsyncExitStack() as stack:
            scope["fastapi_middleware_astack"] = stack
            await route.handle(scope, buf.receive, buf.send)

    try:
        _run(_drive())
    except _StarletteHTTPException as exc:
        return _render_http_exception(app, scope, exc)
    except Exception as exc:  # noqa: BLE001 — server error: propagate to caller
        caps = getattr(app, "_captured_server_exceptions", None)
        if caps is not None:
            caps.append(exc)
        return (
            500,
            b"Internal Server Error",
            [("content-type", "text/plain; charset=utf-8")],
        )
    return buf.result()


def _match_normal_routes(
    app: Any, scope: dict
) -> tuple[int, bytes, list[tuple[str, str]]] | None:
    """Match the app's CURRENT normal routes at the 404 fallback, mirroring
    ``Router.app``'s first pass (full match wins immediately; first partial
    match is used otherwise). Two kinds of route reach here:

    * a custom ``BaseRoute`` the Rust door can't dispatch (no ``.path`` /
      bespoke ``matches`` — e.g. a synthetic partial-matching route), and
    * a normal route added AFTER the socket server started (turbo's TestClient
      binds a real ``app.run()`` server whose Rust router is fixed at startup),
      including one added to an already-included router — matched through the
      ``_IncludedRouter`` marker.

    Both are NORMAL routes, so they outrank frontend / low-priority routes and
    are matched before ``_match_low_priority``. Startup routes the door already
    serves never reach here (the door would have matched them), so re-matching
    the full route list is safe — only door-unknown routes can hit."""
    from starlette.routing import BaseRoute as _BaseRoute

    partial: tuple[Any, dict] | None = None
    for route in list(getattr(app.router, "routes", []) or []):
        if getattr(route, "_is_included_shadow", False):
            continue
        if not isinstance(route, _BaseRoute):
            continue
        try:
            match, child_scope = route.matches(scope)
        except Exception:  # noqa: BLE001
            continue
        if match == _Match.FULL:
            scope.update(child_scope)
            return _drive_route(app, route, scope)
        if match == _Match.PARTIAL and partial is None:
            partial = (route, child_scope)
    if partial is not None:
        route, child_scope = partial
        scope.update(child_scope)
        return _drive_route(app, route, scope)
    return None


def serve_low_priority_request(
    app: Any,
    method: str,
    path: str,
    query: Any = b"",
    headers: Any = None,
) -> tuple[int, bytes, list[tuple[str, str]]] | None:
    """Serve a routing-miss through the app's custom normal routes and then its
    low-priority routes (frontend + any hand-appended ``_low_priority_routes``).
    Returns ``(status, body, headers)`` when something matched (a file, a
    redirect, a rendered 404/405/401, a custom route's response), or ``None``
    when nothing matched — the caller then falls through to normal 404 handling.

    Mirrors FastAPI's ``Router.app`` tail exactly: any normal route (including a
    partial/method-mismatch) outranks frontend, and ``_match_low_priority``
    (longest-prefix, navigation detection, fallback modes) runs only after.
    """
    from fastapi.routing import (
        APIRoute,
        _FASTAPI_EFFECTIVE_ROUTE_CONTEXT_KEY,
        _get_fastapi_scope,
        _update_scope,
    )

    router = app.router
    scope = _build_scope(app, method, path, query, headers)

    # Ensure every APIRoute/router in the tree has a usable
    # ``generate_unique_id_function`` so FastAPI's effective-route builders
    # (used by ``_IncludedRouter.matches`` and ``_match_low_priority``) don't
    # trip over turbo's ``None`` default. Idempotent; catches routes added after
    # the socket server started.
    from fastapi_turbo._route_helpers import normalize_app_gen_ids

    normalize_app_gen_ids(app)

    # Normal routes (door-unknown or added after the server started) outrank
    # frontend / low-priority routes — match them first, like Router.app.
    out = _match_normal_routes(app, scope)
    if out is not None:
        return out

    match, low_scope, route, context = router._match_low_priority(scope)
    if match == _Match.NONE or route is None:
        return None

    _update_scope(scope, low_scope)
    handler_route = route
    if context is not None:
        _get_fastapi_scope(scope)[_FASTAPI_EFFECTIVE_ROUTE_CONTEXT_KEY] = context
        original_route = context.original_route
        if isinstance(original_route, APIRoute):
            scope["route"] = original_route
            handler_route = original_route

    return _drive_route(app, handler_route, scope)
