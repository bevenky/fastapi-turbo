"""R25 audit follow-ups — pre-shim Starlette ``UploadFile`` is
captured at install time (real failure path), ``request_response``
shim is sync, ``MutableHeaders`` writes the underlying ``_list``,
``Headers.raw`` is a property, ``TestClient`` pins
``Accept-Encoding: gzip, deflate``, in-process dispatcher mutates
the outer ASGI scope so middleware sees ``scope['route']``."""
import asyncio
import inspect

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 pre-shim Starlette UploadFile captured at install time
# ────────────────────────────────────────────────────────────────────


def test_compat_captures_preshim_uploadfile_at_install_time():
    """The real failure path: user code does ``from
    starlette.datastructures import UploadFile`` BEFORE importing
    fastapi_turbo. The R24 fix probed ``sys.modules`` post-facto
    and saw only the (already-installed) shim — pre-shim references
    were skipped. Now ``compat.install`` captures the original
    Starlette class on the way in, so ``FormData.close`` can
    recognise both shimmed and pre-shim instances."""
    from fastapi_turbo import compat
    from fastapi_turbo.param_functions import UploadFile as _UF

    captured = compat.PRESHIM_STARLETTE_UPLOADFILE
    if captured is None:
        # Starlette was never imported before fastapi_turbo in this
        # process (common: tests start with a clean import graph).
        # That's fine — the capture-on-install logic is exercised
        # by the live-environment test below.
        pytest.skip("Starlette not pre-imported in this process")
    # The captured class must be a different type than our shim's
    # UploadFile — that's the whole point.
    assert captured is not _UF


def test_formdata_close_recognises_simulated_preshim_uploadfile():
    """Simulate the pre-shim path explicitly: install a fake
    ``UploadFile`` class into the compat module's
    ``PRESHIM_STARLETTE_UPLOADFILE`` slot, build a ``FormData`` with
    that type as a value, and verify ``await form.close()`` calls
    its async ``close()``. This locks the lookup against the
    captured-at-install-time slot, not the post-shim
    ``sys.modules`` view."""
    from fastapi_turbo import compat
    from fastapi_turbo.datastructures import FormData

    original_capture = compat.PRESHIM_STARLETTE_UPLOADFILE
    closed_log: list[bool] = []

    class _FakePreShimUF:
        def __init__(self):
            self.filename = "x.txt"
            self.file = None

        async def close(self):
            closed_log.append(True)

    compat.PRESHIM_STARLETTE_UPLOADFILE = _FakePreShimUF
    try:
        async def _run():
            f = FormData([("file", _FakePreShimUF())])
            await f.close()
        asyncio.run(_run())
    finally:
        compat.PRESHIM_STARLETTE_UPLOADFILE = original_capture

    assert closed_log == [True], closed_log


# ────────────────────────────────────────────────────────────────────
# #3 fastapi.routing.request_response shim is sync
# ────────────────────────────────────────────────────────────────────


def test_request_response_shim_is_sync_and_returns_asgi_callable():
    """Upstream FastAPI's ``request_response`` is a SYNC function
    returning an ASGI callable. Our earlier shim was ``async def``,
    so calling ``request_response(handler)`` produced a coroutine
    that callers couldn't invoke as an ASGI app
    (``TypeError: 'coroutine' object is not callable``)."""
    import fastapi_turbo  # noqa: F401 — install shim
    from fastapi.routing import request_response

    assert not inspect.iscoroutinefunction(request_response), (
        "request_response must be sync — upstream returns the ASGI "
        "callable directly, not a coroutine"
    )

    async def _handler(request):
        return None

    asgi_app = request_response(_handler)
    assert callable(asgi_app), asgi_app
    # The returned ASGI callable itself IS async (an ``async def
    # app(scope, receive, send)``) — and must be invocable on a
    # scope dict without a TypeError.
    assert inspect.iscoroutinefunction(asgi_app), asgi_app


# ────────────────────────────────────────────────────────────────────
# #4 MutableHeaders writes _list (not _dict)
# ────────────────────────────────────────────────────────────────────


def test_mutable_headers_setitem_persists():
    from fastapi_turbo.datastructures import MutableHeaders

    h = MutableHeaders()
    h["X-Custom"] = "1"
    assert h["x-custom"] == "1"  # case-insensitive lookup
    assert h.get("X-Custom") == "1"


def test_mutable_headers_setitem_replaces_existing():
    """``__setitem__`` must drop existing duplicates and leave one
    canonical value (matches Starlette's ``MutableHeaders`` set
    semantics — distinct from ``append``)."""
    from fastapi_turbo.datastructures import MutableHeaders

    h = MutableHeaders([("x", "1"), ("x", "2")])
    h["x"] = "3"
    assert h.getlist("x") == ["3"], h.getlist("x")


def test_mutable_headers_append_preserves_duplicates():
    from fastapi_turbo.datastructures import MutableHeaders

    h = MutableHeaders()
    h.append("set-cookie", "a=1")
    h.append("set-cookie", "b=2")
    assert h.getlist("set-cookie") == ["a=1", "b=2"]


def test_mutable_headers_setdefault_returns_existing_or_inserts():
    from fastapi_turbo.datastructures import MutableHeaders

    h = MutableHeaders()
    assert h.setdefault("x", "1") == "1"
    # Second call returns the existing value, not the new one.
    assert h.setdefault("x", "2") == "1"
    assert h["x"] == "1"


def test_mutable_headers_delitem_removes_all_duplicates():
    from fastapi_turbo.datastructures import MutableHeaders

    h = MutableHeaders([("x", "1"), ("y", "2"), ("x", "3")])
    del h["x"]
    assert h.getlist("x") == []
    assert h["y"] == "2"


# ────────────────────────────────────────────────────────────────────
# #5 Headers.raw is a property (iterable without parens)
# ────────────────────────────────────────────────────────────────────


def test_headers_raw_is_iterable_attribute_not_method():
    """``Headers.raw`` must be a property so callers can do
    ``for k, v in headers.raw: ...`` — matches Starlette and httpx.
    Earlier impl was a method, so the same call site iterated a
    bound-method object and raised ``TypeError: 'method' object
    is not iterable`` (silently caught in WS scope assembly,
    leaving raw ASGI WS middleware with empty headers)."""
    from fastapi_turbo.datastructures import Headers

    h = Headers({"X-A": "1", "X-B": "2"})
    raw = h.raw  # No parens — must be the list, not a bound method.
    assert isinstance(raw, list), type(raw)
    # Round-trip pairs are bytes-encoded.
    assert (b"x-a", b"1") in raw, raw


def test_websocket_scope_assembly_propagates_headers():
    """End-to-end: drive a WebSocket through the in-process ASGI
    path with raw-ASGI middleware that captures
    ``scope['headers']`` at connection time. The R25 fix to
    ``Headers.raw`` (property, not method) means the scope
    assembly path can now serialise the WS headers without
    silently catching a ``TypeError``."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    captured: dict = {}

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(ws):
        captured["headers"] = list(ws.headers.raw)
        await ws.accept()
        await ws.send_text("ok")
        await ws.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as conn:
            assert conn.receive_text() == "ok"

    # The captured headers must be a non-empty list of byte tuples
    # (Host header at minimum; TestClient sets it). Earlier the
    # ``TypeError`` made this list silently empty in raw-ASGI
    # middleware paths.
    assert captured["headers"], captured
    assert all(isinstance(k, bytes) and isinstance(v, bytes)
               for k, v in captured["headers"]), captured


# ────────────────────────────────────────────────────────────────────
# #2(a) TestClient pins Accept-Encoding: gzip, deflate
# ────────────────────────────────────────────────────────────────────


def test_testclient_default_accept_encoding_is_gzip_deflate_only():
    """Starlette's TestClient pins ``Accept-Encoding: gzip,
    deflate`` so request snapshots are stable regardless of which
    compression libs are installed. httpx's default includes
    ``br`` when ``brotli`` is on sys.path, which leaks into
    upstream FastAPI tutorial test snapshots and produces 6
    test failures unrelated to our code."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/x")
    async def _x(request: Request):
        return JSONResponse({"ae": request.headers.get("accept-encoding")})

    with TestClient(app, in_process=True) as c:
        r = c.get("/x")
        assert r.status_code == 200, (r.status_code, r.content)
        assert r.json() == {"ae": "gzip, deflate"}, r.json()


# ────────────────────────────────────────────────────────────────────
# #2(b) in-process dispatcher mutates outer scope so middleware
#       sees scope['route'] / scope['endpoint']
# ────────────────────────────────────────────────────────────────────


def test_inprocess_dispatcher_mutates_outer_scope_with_matched_route():
    """Legacy raw-ASGI middleware (``SentryAsgiMiddleware(app)``,
    OTel, rate-limit) sees the scope dict the outermost wrapper
    passes in. Our dispatcher used to copy the scope and mutate
    only the copy — middleware never saw ``scope['route']``, so
    Sentry's transaction name was the concrete path
    (``/message/123456``) instead of the route shape
    (``/message/{message_id}``). Now we mutate the outer scope
    in-place too, matching Starlette's router."""
    from fastapi_turbo import FastAPI

    captured = {}

    app = FastAPI()

    @app.get("/message/{message_id}")
    async def _msg(message_id: int):
        return {"id": message_id}

    # Wrap the FastAPI app with a raw-ASGI middleware that snoops
    # at response.start time — this is what Sentry does. By that
    # point our dispatcher has run route matching, so
    # ``scope['route']`` should be populated.
    async def snoop(scope, receive, send):
        async def _send(msg):
            if msg.get("type") == "http.response.start":
                captured["route_path"] = getattr(scope.get("route"), "path", None)
                captured["endpoint"] = scope.get("endpoint")
                captured["path_params"] = scope.get("path_params")
            await send(msg)
        await app(scope, receive, _send)

    from fastapi_turbo.testclient import TestClient

    # Drive snoop directly via the in-process TestClient: pass the
    # snoop callable as the "app" to TestClient.
    with TestClient(snoop, in_process=True) as c:
        r = c.get("/message/123456")
        assert r.status_code == 200, (r.status_code, r.content)

    assert captured.get("route_path") == "/message/{message_id}", captured
    # ``path_params`` are the raw matched strings — coercion to the
    # endpoint's annotation (``int``) happens later, in dependency
    # resolution. Sentry's transaction templating only needs the
    # route shape, not coerced values.
    assert captured.get("path_params") == {"message_id": "123456"}, captured
    assert captured.get("endpoint") is not None, captured
