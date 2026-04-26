"""R12 audit follow-ups: regression locks for the 6 functional
findings the auditor flagged. Each test reproduces the bug AND
captures the upstream-parity expectation. Tests are split by
finding so a regression is unambiguous in CI output.

Findings covered:
  * #1 TestClient.websocket_connect after socket fallback.
  * #2 OPTIONS to ``{path:path}`` route redirects instead of 405.
  * #3 Body parser assumes JSON for every Body() param.
  * #4 _ASGISyncClientShim missing ``stream()``.
  * #5 worker_timeout not honoured in in-process dispatch.
  * #6 Non-finite Decimal serializes invalid JSON in fallback path.
"""
import asyncio
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_turbo_shim_after_each_test():
    yield
    _drop_fa_modules()
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _un()
    importlib.invalidate_caches()
    _in()


def _drop_fa_modules():
    for m in list(sys.modules):
        if (
            m == "fastapi"
            or m.startswith("fastapi.")
            or m == "starlette"
            or m.startswith("starlette.")
        ):
            del sys.modules[m]


def _import_upstream():
    from fastapi_turbo.compat import uninstall as _un
    _un()
    _drop_fa_modules()
    importlib.invalidate_caches()
    import fastapi as _fa  # noqa: F401
    return sys.modules["fastapi"], sys.modules.get("fastapi.responses")


def _import_turbo():
    from fastapi_turbo.compat import install as _in, uninstall as _un
    _drop_fa_modules()
    _un()
    importlib.invalidate_caches()
    _in()
    return sys.modules["fastapi"], sys.modules["fastapi.responses"]


def _run(coro):
    return asyncio.run(coro)


async def _drive(app, method, path, **kwargs):
    import httpx
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://t",
    ) as cli:
        return await cli.request(method, path, **kwargs)


def _build(builder):
    fa_up, resp_up = _import_upstream()
    up_app = builder(fa_up, resp_up)
    fa_tb, resp_tb = _import_turbo()
    tb_app = builder(fa_tb, resp_tb)
    return up_app, tb_app


# ────────────────────────────────────────────────────────────────────
# #2 OPTIONS to a {path:path} route returns 405, not redirect-loop.
# ────────────────────────────────────────────────────────────────────

def _path_route_app(fa_mod, _resp_mod):
    app = fa_mod.FastAPI()

    @app.get("/files/{full_path:path}")
    def _f(full_path: str):
        return {"p": full_path}

    return app


def test_options_on_path_route_returns_405_no_redirect_loop():
    """``@app.get('/files/{full_path:path}')`` — OPTIONS to a deep
    path must surface as 405 with ``Allow: GET``, not bounce
    through trailing-slash redirects until httpx raises
    ``TooManyRedirects``."""
    up, tb = _build(_path_route_app)

    async def go():
        ru = await _drive(up, "OPTIONS", "/files/a/b/c.png")
        rt = await _drive(tb, "OPTIONS", "/files/a/b/c.png")
        assert ru.status_code == rt.status_code, (
            f"OPTIONS path-route divergence: upstream={ru.status_code} "
            f"turbo={rt.status_code}"
        )
        # Both should advertise GET (and possibly HEAD) in Allow.
        up_allow = set(
            m.strip() for m in ru.headers.get("allow", "").split(",") if m.strip()
        )
        tb_allow = set(
            m.strip() for m in rt.headers.get("allow", "").split(",") if m.strip()
        )
        assert tb_allow == up_allow, (
            f"OPTIONS Allow divergence: upstream={up_allow} turbo={tb_allow}"
        )

    _run(go())


# ────────────────────────────────────────────────────────────────────
# #6 Non-finite Decimal serializes valid JSON.
# ────────────────────────────────────────────────────────────────────

def _decimal_nan_app(fa_mod, _resp_mod):
    from decimal import Decimal
    app = fa_mod.FastAPI()

    @app.get("/d")
    def _d():
        # Returning a non-finite Decimal must NOT produce literal
        # ``NaN`` in the response body — that's invalid JSON. The
        # response should either fail loudly (500) or coerce to a
        # serializable value, but it must NOT emit ``NaN``.
        return {"v": Decimal("NaN")}

    return app


def test_non_finite_decimal_default_path_does_not_emit_invalid_json():
    """A handler that returns ``{"v": Decimal("NaN")}`` should NOT
    surface as a 200 with literal ``NaN`` in the body. Upstream
    routes this through ``jsonable_encoder`` (Decimal → float) and
    then ``json.dumps``; we add ``allow_nan=False`` so the result
    raises rather than emitting invalid JSON. Either way the
    response body must be valid JSON or the response must be a
    non-200 error."""
    _, tb = _build(_decimal_nan_app)

    async def go():
        rt = await _drive(tb, "GET", "/d")
        body = rt.content
        if rt.status_code == 200:
            import json
            assert b"NaN" not in body, (
                f"turbo emitted invalid JSON with NaN literal: {body!r}"
            )
            json.loads(body)

    _run(go())


def test_explicit_jsonresponse_decimal_raises_typeerror():
    """Explicit ``fastapi_turbo.responses.JSONResponse({"v":
    Decimal("NaN")})`` must raise ``TypeError`` on construction —
    matching upstream ``starlette.responses.JSONResponse`` exactly.
    The previous behavior (silent ``"NaN"`` string coercion) was a
    drop-in parity break for users who explicitly construct a
    JSONResponse with raw Python objects."""
    from decimal import Decimal
    import pytest as _pytest
    from fastapi_turbo.responses import JSONResponse

    with _pytest.raises(TypeError, match="Decimal"):
        JSONResponse({"v": Decimal("NaN")})

    # Finite Decimal also raises (encoder handles them; explicit
    # JSONResponse path is the same as upstream).
    with _pytest.raises(TypeError, match="Decimal"):
        JSONResponse({"v": Decimal("1.5")})


# ────────────────────────────────────────────────────────────────────
# #3 Body parser accepts non-JSON content-types.
# ────────────────────────────────────────────────────────────────────

def _bytes_body_app(fa_mod, _resp_mod):
    Body = fa_mod.Body
    app = fa_mod.FastAPI()

    @app.post("/upload")
    def _u(payload: bytes = Body(..., media_type="application/octet-stream")):
        return {"len": len(payload)}

    return app


def test_octet_stream_body_passes_raw_bytes():
    """``Body(..., media_type='application/octet-stream')`` with a
    binary payload must reach the handler as raw ``bytes`` — not
    422 ``Input should be a valid…``. The in-process body parser
    was unconditionally JSON-decoding bodies, breaking binary
    upload handlers."""
    _, tb = _build(_bytes_body_app)

    async def go():
        payload = b"\x00\x01\x02hello binary\xff"
        rt = await _drive(
            tb, "POST", "/upload",
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        assert rt.status_code == 200, (
            f"binary body rejected: status={rt.status_code} body={rt.content!r}"
        )
        assert rt.json() == {"len": len(payload)}

    _run(go())


# ────────────────────────────────────────────────────────────────────
# #5 worker_timeout enforced in in-process dispatch.
# ────────────────────────────────────────────────────────────────────

def test_worker_timeout_cancels_slow_handler():
    """``FastAPI(worker_timeout=…)`` bounds how long a single async
    handler may run. The in-process dispatcher must enforce that
    bound and turn timeouts into a non-200 response — previously
    the dispatcher just awaited the endpoint forever, returning a
    spurious 200 with the slow handler's eventual output."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI(worker_timeout=0.1)

    @app.get("/slow")
    async def _slow():
        await asyncio.sleep(2.0)
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.get("/slow")
        assert r.status_code != 200, (
            f"worker_timeout not enforced — slow handler returned 200 "
            f"(expected timeout error)"
        )


# ────────────────────────────────────────────────────────────────────
# #4 _ASGISyncClientShim has stream() method.
# ────────────────────────────────────────────────────────────────────

def test_testclient_in_process_stream_method_works():
    """``TestClient(app, in_process=True).stream(...)`` must yield a
    streamed response context manager, matching httpx /
    starlette.testclient.TestClient. The shim previously only had
    ``request``/``get``/``post``/etc., so ``stream`` raised
    ``AttributeError``."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/s")
    def _s():
        def _gen():
            yield b"chunk1\n"
            yield b"chunk2\n"
            yield b"chunk3\n"

        return StreamingResponse(_gen(), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        # ``stream`` is a context manager that yields a response;
        # iterating ``iter_bytes()`` (or ``iter_text()``) drains.
        with c.stream("GET", "/s") as r:
            assert r.status_code == 200
            chunks = list(r.iter_bytes())
            joined = b"".join(chunks)
            assert joined == b"chunk1\nchunk2\nchunk3\n", joined


# ────────────────────────────────────────────────────────────────────
# #1 TestClient.websocket_connect in fallback mode.
# ────────────────────────────────────────────────────────────────────

def test_max_request_size_enforced_in_in_process_fallback():
    """``FastAPI(max_request_size=…)`` must reject oversized
    bodies on the in-process / TestClient fallback path with 413,
    matching the Tower layer's behaviour on the Rust server.
    Without this, fallback tests pass for bodies that production
    rejects."""
    from fastapi_turbo import FastAPI, Body
    from fastapi_turbo.testclient import TestClient

    app = FastAPI(max_request_size=1024)

    @app.post("/u")
    def _u(payload: bytes = Body(..., media_type="application/octet-stream")):
        return {"len": len(payload)}

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/u",
            content=b"x" * 4096,
            headers={"content-type": "application/octet-stream"},
        )
        assert r.status_code == 413, (r.status_code, r.content)


def test_testclient_websocket_connect_in_fallback_mode():
    """When ``TestClient`` falls back to in-process mode (loopback
    bind denied or ``in_process=True``), ``websocket_connect`` must
    NOT raise ``ValueError: Port could not be cast to integer value
    as 'None'``. It should drive an ASGI websocket scope through
    the app directly."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await websocket.accept()
        msg = await websocket.receive_text()
        await websocket.send_text(f"echo:{msg}")
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("hello")
            reply = ws.receive_text()
            assert reply == "echo:hello"


def test_inprocess_ws_query_param_and_depends():
    """In-process WS dispatch must resolve ``Query``-style scalar
    params and ``Depends(...)`` markers from the endpoint signature
    — not just path params. Reproduces the
    ``test_ws_depends_and_query_param`` divergence the auditor
    flagged."""
    from fastapi_turbo import FastAPI, Depends
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    def get_token(token: str = ""):
        return f"tok:{token}"

    app = FastAPI()

    @app.websocket("/ws/{room}")
    async def _ws(
        websocket: WebSocket,
        room: str,
        token_val: str = Depends(get_token),
    ):
        await websocket.accept()
        await websocket.send_json({"room": room, "token_val": token_val})
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws/42?token=secret") as ws:
            assert ws.receive_json() == {"room": "42", "token_val": "tok:secret"}


def test_inprocess_ws_iter_text_async_iterator():
    """``async for chunk in websocket.iter_text():`` must yield
    until the client disconnects — and not throw on the disconnect
    boundary."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await websocket.accept()
        count = 0
        async for _ in websocket.iter_text():
            count += 1
            if count == 3:
                break
        await websocket.send_text(f"got:{count}")
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("a")
            ws.send_text("b")
            ws.send_text("c")
            assert ws.receive_text() == "got:3"


def test_inprocess_ws_post_accept_exception_uses_user_code():
    """``raise WebSocketException(code=1008)`` after accept must
    close the WS with code 1008 — not the generic 1011 the previous
    catch-all dispatcher emitted."""
    import pytest as _pytest
    from fastapi_turbo import FastAPI
    from fastapi_turbo.exceptions import (
        WebSocketDisconnect,
        WebSocketException,
    )
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await websocket.accept()
        raise WebSocketException(code=1008, reason="policy")

    with TestClient(app, in_process=True) as c:
        with _pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008


def test_inprocess_ws_state_persists_across_messages():
    """``websocket.state`` must be shared across messages within a
    single WS session — backed by ``scope['state']`` so middleware
    and endpoint share the same object."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await websocket.accept()
        websocket.state.counter = 0
        for _ in range(3):
            text = await websocket.receive_text()
            websocket.state.counter += 1
            await websocket.send_text(f"{text}-{websocket.state.counter}")
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("x")
            r1 = ws.receive_text()
            ws.send_text("x")
            r2 = ws.receive_text()
            ws.send_text("x")
            r3 = ws.receive_text()
            assert (r1, r2, r3) == ("x-1", "x-2", "x-3")


def test_inprocess_ws_custom_close_code_propagates():
    """``await websocket.close(code=4321)`` must surface to the
    client as ``WebSocketDisconnect(code=4321)``."""
    import pytest as _pytest
    from fastapi_turbo import FastAPI
    from fastapi_turbo.exceptions import WebSocketDisconnect
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.close(code=4321, reason="custom")

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            with _pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 4321
