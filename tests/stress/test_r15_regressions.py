"""R15 audit follow-ups: regression locks for the 6 findings.

  * #1 WS dependency failures must propagate (not silently let the
    endpoint accept the connection with the unresolved Depends).
  * #2 StaticFiles.lookup_path must reject sibling-prefix traversal.
  * #3 Nested include_router(..., dependencies=...) must propagate
    through the in-process dispatcher.
  * #4 WebSocket path/query params must be type-coerced.
  * #5 TestClient.stream() must stream lazily, not eager-drain.
"""
import asyncio
import os
import threading
import time

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #2 StaticFiles path traversal — sibling prefix
# ────────────────────────────────────────────────────────────────────

def test_staticfiles_rejects_sibling_prefix(tmp_path):
    """A configured directory ``/tmp/static`` must NOT also serve
    files from ``/tmp/static-secret/``. The previous
    ``startswith`` check matched the prefix as a string, letting
    ``../static-secret/leak.txt`` escape."""
    from fastapi_turbo.staticfiles import StaticFiles

    static_dir = tmp_path / "static"
    sibling = tmp_path / "static-secret"
    static_dir.mkdir()
    sibling.mkdir()
    (static_dir / "ok.txt").write_text("ok")
    leak_path = sibling / "leak.txt"
    leak_path.write_text("secret data")

    sf = StaticFiles(directory=str(static_dir))

    # Legitimate path resolves.
    full, _ = sf.lookup_path("ok.txt")
    assert full == str((static_dir / "ok.txt").resolve())

    # Sibling-prefix traversal must be rejected.
    full, _ = sf.lookup_path("../static-secret/leak.txt")
    assert full == "", (
        f"sibling-prefix traversal accepted: {full!r} (would have leaked "
        f"{leak_path!r})"
    )


def test_staticfiles_rejects_absolute_traversal(tmp_path):
    """Even unambiguous traversal attempts must be rejected."""
    from fastapi_turbo.staticfiles import StaticFiles

    static_dir = tmp_path / "static"
    static_dir.mkdir()

    sf = StaticFiles(directory=str(static_dir))
    full, _ = sf.lookup_path("../etc/passwd")
    assert full == ""


# ────────────────────────────────────────────────────────────────────
# #1 WS dependency failures must close the socket, NOT silently let
#    the endpoint run with an unresolved Depends marker.
# ────────────────────────────────────────────────────────────────────

def test_ws_dep_raising_websocketexception_closes_with_user_code():
    """A ``Depends(auth)`` that raises ``WebSocketException(1008)``
    must close the WS with code 1008 and prevent the endpoint
    body from running. Earlier code logged the error and let the
    endpoint accept the connection with the unresolved Depends
    marker — a real auth bypass."""
    from fastapi_turbo import FastAPI, Depends
    from fastapi_turbo.exceptions import (
        WebSocketDisconnect,
        WebSocketException,
    )
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    endpoint_ran = {"v": False}

    def auth(token: str = ""):
        if token != "secret":
            raise WebSocketException(code=1008, reason="bad token")
        return "user"

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket, who: str = Depends(auth)):
        endpoint_ran["v"] = True
        await websocket.accept()
        await websocket.send_text(f"hi {who}")
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert endpoint_ran["v"] is False, (
            "endpoint ran despite Depends raising WebSocketException — "
            "this is an auth bypass"
        )


# ────────────────────────────────────────────────────────────────────
# #3 Nested include_router deps cascade correctly
# ────────────────────────────────────────────────────────────────────

def test_nested_include_router_deps_chain():
    """Upstream FA runs deps in order: outer-include, parent-router,
    child-include, child-router. Each level adds its own deps to
    the chain. The in-process dispatcher must mirror that."""
    from fastapi_turbo import FastAPI, APIRouter, Depends
    from fastapi_turbo.testclient import TestClient

    order: list[str] = []

    def outer_dep():
        order.append("outer")

    def parent_dep():
        order.append("parent")

    def child_include_dep():
        order.append("child_include")

    def child_router_dep():
        order.append("child_router")

    parent = APIRouter(dependencies=[Depends(parent_dep)])
    child = APIRouter(dependencies=[Depends(child_router_dep)])

    @child.get("/leaf")
    def _leaf():
        return {"ok": True}

    parent.include_router(child, dependencies=[Depends(child_include_dep)])

    app = FastAPI()
    app.include_router(parent, dependencies=[Depends(outer_dep)])

    with TestClient(app, in_process=True) as c:
        order.clear()
        r = c.get("/leaf")
        assert r.status_code == 200, r.content
        # All four deps must have run.
        assert "outer" in order, f"outer dep never ran: {order}"
        assert "parent" in order, f"parent dep never ran: {order}"
        assert "child_include" in order, f"child_include never ran: {order}"
        assert "child_router" in order, f"child_router never ran: {order}"


# ────────────────────────────────────────────────────────────────────
# #4 WS path/query params type-coerce per annotation
# ────────────────────────────────────────────────────────────────────

def test_ws_path_param_int_coerced():
    """``room: int`` in a WS endpoint must arrive as ``int`` —
    not the raw ``str`` from the URL."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    captured: dict = {}

    app = FastAPI()

    @app.websocket("/ws/{room}")
    async def _ws(websocket: WebSocket, room: int):
        captured["room"] = room
        captured["type"] = type(room).__name__
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws/42") as ws:
            ws.receive_text()
    assert captured == {"room": 42, "type": "int"}, captured


def test_ws_query_param_int_coerced():
    """``q: int = Query(...)`` must arrive as ``int`` from
    ``?q=7``."""
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    captured: dict = {}

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket, q: int = Query(...)):
        captured["q"] = q
        captured["type"] = type(q).__name__
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws?q=7") as ws:
            ws.receive_text()
    assert captured == {"q": 7, "type": "int"}, captured


# ────────────────────────────────────────────────────────────────────
# R16 #1: WS required Query/Header must close 1008 when missing
# ────────────────────────────────────────────────────────────────────

def test_ws_required_query_missing_closes_1008():
    """``q: int = Query(...)`` (no default) on a WS endpoint must
    close with code 1008 when ``q`` is absent — NOT silently let
    the endpoint run with the ``Query(...)`` marker object as the
    parameter value."""
    from fastapi_turbo import FastAPI, Query
    from fastapi_turbo.exceptions import WebSocketDisconnect
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    endpoint_ran = {"v": False, "q": None}

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket, q: int = Query(...)):
        endpoint_ran["v"] = True
        endpoint_ran["q"] = q
        await websocket.accept()
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert endpoint_ran["v"] is False, (
            "endpoint ran despite missing required Query(...) — "
            f"got q={endpoint_ran['q']!r} instead of close 1008"
        )


def test_ws_required_header_missing_closes_1008():
    from fastapi_turbo import FastAPI, Header
    from fastapi_turbo.exceptions import WebSocketDisconnect
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    endpoint_ran = {"v": False}

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket, x_token: str = Header(...)):
        endpoint_ran["v"] = True
        await websocket.accept()
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert endpoint_ran["v"] is False


# ────────────────────────────────────────────────────────────────────
# R16 #2: WS dep params honour Query/Header markers + coercion
# ────────────────────────────────────────────────────────────────────

def test_ws_dep_query_marker_required_missing_closes_1008():
    """A dependency with ``def auth(token: str = Query(...))`` must
    close 1008 when ``token`` is absent — not pass the
    ``Query(...)`` marker object to the dep function."""
    from fastapi_turbo import FastAPI, Depends, Query
    from fastapi_turbo.exceptions import WebSocketDisconnect
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    captured = {"token": None}

    def auth(token: str = Query(...)):
        captured["token"] = token
        return token

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket, who: str = Depends(auth)):
        await websocket.accept()
        await websocket.send_text(who)
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/ws") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert captured["token"] is None, (
            f"auth dep ran with marker object: {captured['token']!r}"
        )


def test_ws_dep_query_marker_present_coerces_to_int():
    """``def dep(n: int = Query(...))`` with ``?n=7`` must receive
    ``int(7)``, not the str ``"7"``."""
    from fastapi_turbo import FastAPI, Depends, Query
    from fastapi_turbo.testclient import TestClient
    from fastapi_turbo.websockets import WebSocket

    captured = {}

    def dep(n: int = Query(...)):
        captured["n"] = n
        captured["type"] = type(n).__name__
        return n * 2

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(websocket: WebSocket, doubled: int = Depends(dep)):
        await websocket.accept()
        await websocket.send_text(str(doubled))
        await websocket.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws?n=7") as ws:
            reply = ws.receive_text()
    assert captured == {"n": 7, "type": "int"}, captured
    assert reply == "14"


# ────────────────────────────────────────────────────────────────────
# R16 #3: TestClient.stream() honours httpx kwargs
# ────────────────────────────────────────────────────────────────────

def test_testclient_stream_passes_params_kwarg():
    """``cli.stream("GET", "/s", params={"x": "1"})`` must reach
    the handler as ``?x=1``. Earlier impl built the URL from the
    ``url`` argument only and dropped ``params=`` entirely."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    seen = {"x": None}

    app = FastAPI()

    @app.get("/s")
    def _s(x: str = ""):
        seen["x"] = x

        def _gen():
            yield f"got:{x}".encode()

        return StreamingResponse(_gen(), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        with c.stream("GET", "/s", params={"x": "from_params"}) as r:
            body = b"".join(r.iter_bytes())
    assert seen["x"] == "from_params", seen
    assert body == b"got:from_params"


# ────────────────────────────────────────────────────────────────────
# R16 #4: TestClient.stream() cancels server task on early exit
# ────────────────────────────────────────────────────────────────────

def test_testclient_stream_early_exit_cancels_server():
    """If the test exits the ``with c.stream(...)`` block before
    the response is fully consumed, the in-process driver must
    cancel the underlying ASGI task. Otherwise an infinite stream
    keeps emitting forever and pytest reports
    ``Task was destroyed but it is pending``."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    cancellation_observed = {"v": False}
    chunks_after_close = {"v": 0}

    app = FastAPI()

    @app.get("/inf")
    def _inf():
        async def _gen():
            try:
                while True:
                    yield b"x" * 16
                    await asyncio.sleep(0.01)
            except (asyncio.CancelledError, GeneratorExit):
                cancellation_observed["v"] = True
                raise

        return StreamingResponse(_gen(), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        with c.stream("GET", "/inf") as r:
            it = r.iter_bytes()
            # Pull one chunk then exit early.
            next(it)
        # Give the server a beat to observe the disconnect / cancel.
        time.sleep(0.1)
    assert cancellation_observed["v"] is True, (
        "server-side generator did not see cancellation — "
        "early-exit leaks the ASGI task"
    )


# ────────────────────────────────────────────────────────────────────
# R17 #1: hot infinite stream early-exit must not hang
# ────────────────────────────────────────────────────────────────────

def test_testclient_stream_hot_infinite_early_exit_no_hang():
    """A no-sleep infinite generator floods the chunk queue. The
    earlier ``__exit__`` drained-then-cancelled — the drain loop
    never ran out of items, cancellation never fired, and exit
    hung forever. The fix drops the drain and goes straight to
    cancel."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/hot")
    def _hot():
        def _gen():
            # Sync generator with NO sleeps — the producer side
            # fills the queue as fast as the loop runs.
            while True:
                yield b"x" * 64

        return StreamingResponse(_gen(), media_type="text/plain")

    deadline = time.monotonic() + 5.0
    with TestClient(app, in_process=True) as c:
        with c.stream("GET", "/hot") as r:
            it = r.iter_bytes()
            next(it)
        # Exit must complete promptly — anything > 4s means we hung
        # in the drain loop and only escaped via the test's safety
        # margin.
    elapsed = time.monotonic() - (deadline - 5.0)
    assert elapsed < 4.0, (
        f"stream early-exit took {elapsed:.2f}s on a hot infinite "
        f"producer — drain loop is starving cancellation"
    )


# ────────────────────────────────────────────────────────────────────
# R17 #2: TestClient.stream(..., follow_redirects=True) follows
# ────────────────────────────────────────────────────────────────────

def test_testclient_stream_follow_redirects():
    """``c.stream("GET", "/redirect", follow_redirects=True)`` must
    end on the final 200 response, not the intermediate 307."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import RedirectResponse, StreamingResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/redirect")
    def _r():
        return RedirectResponse(url="/final", status_code=307)

    @app.get("/final")
    def _final():
        def _gen():
            yield b"final body\n"

        return StreamingResponse(_gen(), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        # Explicit follow_redirects=False stops at the 307.
        with c.stream("GET", "/redirect", follow_redirects=False) as r:
            assert r.status_code == 307
            assert r.headers["location"] == "/final"

        # follow_redirects=True ends on /final with the streamed body.
        with c.stream("GET", "/redirect", follow_redirects=True) as r:
            assert r.status_code == 200, (
                f"follow_redirects=True returned {r.status_code} "
                f"instead of 200 — redirect chain not followed"
            )
            body = b"".join(r.iter_bytes())
            assert body == b"final body\n"

    # The session-default (TestClient(follow_redirects=True) — the
    # httpx default) also follows automatically.
    with TestClient(app, in_process=True) as c:
        with c.stream("GET", "/redirect") as r:
            assert r.status_code == 200, (
                f"session-default follow_redirects=True did not follow: "
                f"got {r.status_code}"
            )


# ────────────────────────────────────────────────────────────────────
# R18 #1: redirect method rewrite parity (RFC 7231 + httpx semantics)
# ────────────────────────────────────────────────────────────────────

def test_testclient_stream_redirect_method_rewrite_parity():
    """Per httpx's redirect rules (RFC 7231 §6.4 + historical
    browser behaviour):

      * 301 / 302: POST → GET, others preserve method.
      * 303:       any non-GET/HEAD → GET (mandatory).
      * 307 / 308: method preserved (mandatory).

    Earlier turbo only rewrote 303, so a POST that hit a 301 or
    302 redirected as POST — diverging from upstream FastAPI's
    httpx-backed TestClient and from real browsers."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import RedirectResponse, JSONResponse
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    for code in (301, 302, 303, 307, 308):
        @app.api_route(f"/r{code}", methods=["GET", "POST"])
        def _r(request: Request, _code=code):
            return RedirectResponse(url="/landing", status_code=_code)

    @app.api_route("/landing", methods=["GET", "POST"])
    def _landing(request: Request):
        return JSONResponse({"method": request.method})

    expectations = {
        301: "GET",   # POST → GET
        302: "GET",   # POST → GET
        303: "GET",   # any non-GET/HEAD → GET
        307: "POST",  # preserved
        308: "POST",  # preserved
    }

    with TestClient(app, in_process=True) as c:
        for code, want_method in expectations.items():
            with c.stream(
                "POST", f"/r{code}", follow_redirects=True
            ) as r:
                body = b"".join(r.iter_bytes())
                assert r.status_code == 200, (code, r.status_code)
                import json
                got = json.loads(body)["method"]
                assert got == want_method, (
                    f"redirect {code}: POST → expected method "
                    f"{want_method!r} on landing, got {got!r}"
                )


# ────────────────────────────────────────────────────────────────────
# #5 TestClient.stream is lazy
# ────────────────────────────────────────────────────────────────────

def test_testclient_stream_is_lazy_not_eager_drained():
    """The streaming endpoint sleeps between chunks. If the client
    is eager-draining, ``cli.stream(...)`` blocks until the whole
    body is flushed and the time-to-first-byte is the sum of all
    chunk sleeps. A lazy implementation returns the response
    immediately and yields chunks as they arrive — so
    ``next(iter_bytes())`` should land in well under the
    cumulative sleep."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.responses import StreamingResponse
    from fastapi_turbo.testclient import TestClient

    chunks_emitted: list[float] = []

    app = FastAPI()

    @app.get("/s")
    def _s():
        async def _gen():
            for i in range(5):
                chunks_emitted.append(time.monotonic())
                yield f"chunk{i}\n".encode()
                await asyncio.sleep(0.05)

        return StreamingResponse(_gen(), media_type="text/plain")

    with TestClient(app, in_process=True) as c:
        t0 = time.monotonic()
        with c.stream("GET", "/s") as r:
            t_open = time.monotonic() - t0
            assert r.status_code == 200
            it = r.iter_bytes()
            first = next(it)
            t_first = time.monotonic() - t0
            assert first.startswith(b"chunk")
            # Pull the rest.
            rest = list(it)
            t_total = time.monotonic() - t0

        # Sanity: all 5 chunks delivered.
        assert len(rest) >= 4, f"missing chunks: rest={rest}"
        # Time-to-first-byte should be much less than full drain
        # (5 * 0.05 = 0.25s). On a lazy implementation we expect
        # well under the total. Allow generous slack for CI.
        assert t_first < 0.20, (
            f"stream not lazy: TTFB {t_first:.3f}s ≥ 0.20s "
            f"(open={t_open:.3f}s, total={t_total:.3f}s)"
        )
