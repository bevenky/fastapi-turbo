"""Request classes — re-exported from REAL starlette.

The clone Request/HTTPConnection reimplementation (which subclassed real
Starlette for isinstance but overrode every property to read the minimal
Rust-built scope) is retired. The door builds requests via
``_door_make_request``, which:
  * enriches the minimal Rust-built scope with the keys real Starlette reads
    (request-line defaults, ``root_path`` from the app, a seeded ``state``);
  * sets the buffered body as the ``_body`` instance attr — real Starlette
    ``body()``/``stream()``/``form()`` short-circuit on it, so no synthetic
    body ``receive()`` is needed;
  * binds a ``receive()`` that bridges the door's disconnect ``threading.Event``
    to ``Request.is_disconnected()`` (the door has no live ASGI receive).

Imported during fastapi_turbo package init BEFORE the compat shim rebinds
sys.modules, so ``starlette.requests`` resolves to the REAL package; the bound
references stay real after the shim runs.
"""
from __future__ import annotations

import anyio
from starlette.requests import ClientDisconnect, HTTPConnection, Request

__all__ = ["Request", "HTTPConnection", "ClientDisconnect", "_door_make_request"]


def _make_disconnect_receive(disconnect_event):
    """Build the ``receive()`` for a door-built Request.

    ``body()``/``stream()``/``form()`` use the pre-set ``_body`` attr, so this
    is driven ONLY by ``Request.is_disconnected()`` — which peeks one tick via
    an immediately-cancelled ``anyio.CancelScope``. So: return
    ``http.disconnect`` SYNCHRONOUSLY when the door's Event is set (the peek
    sees it → disconnected), else await a checkpoint so the cancelled scope
    aborts the peek and ``is_disconnected()`` returns False (matches the clone's
    "False when no live receive" leniency; also covers the no-Event case)."""

    async def receive():
        if disconnect_event is not None and disconnect_event.is_set():
            return {"type": "http.disconnect"}
        await anyio.sleep_forever()

    return receive


def _door_make_request(scope):
    """Build a real Starlette ``Request`` for the in-process / ``app.run()``
    door from the minimal Rust-built scope. The scope is mutated in place (not
    copied) so ``state`` / headers propagate across every Request built off it
    (BaseHTTPMiddleware semantics). See the module docstring."""
    scope.setdefault("type", "http")
    scope.setdefault("method", "GET")
    scope.setdefault("path", "/")
    scope.setdefault("query_string", b"")
    scope.setdefault("scheme", "http")
    # real Starlette URL(scope) iterates scope["headers"] (KeyError otherwise) —
    # the contextvar-built exception-handler scope may omit it.
    scope.setdefault("headers", [])
    app = scope.get("app")
    if "root_path" not in scope:
        scope["root_path"] = getattr(app, "root_path", "") or ""
    if "state" not in scope:
        # Real Starlette only does ``scope.setdefault("state", {})``; the door
        # additionally seeds the app's lifespan/startup state so a request built
        # off a bare scope still sees it (zero-behavior-change insurance).
        seed = getattr(app, "_app_state", None) if app is not None else None
        scope["state"] = dict(seed) if seed else {}
    req = Request(scope, _make_disconnect_receive(scope.get("_fastapi_turbo_disconnect")))
    # Pre-set the buffered body so body()/stream()/form() resolve synchronously
    # without a body-delivering receive().
    req._body = scope.get("_body", b"")
    return req
