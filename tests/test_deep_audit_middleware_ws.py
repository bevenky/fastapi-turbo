"""Deep audit of middleware + WebSocket behaviour under the compat shim.

Every test here is phrased as `import fastapi ...` (not
`import fastapi_turbo`) to confirm the sys.modules shim redirects all
symbols transparently — the same contract a real FastAPI app relies on.
"""
from __future__ import annotations

import time

import pytest

import fastapi_turbo  # noqa: F401 — installs the fastapi + starlette shim

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import WebSocketException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


# ── CORS ─────────────────────────────────────────────────────


def test_cors_allows_listed_origin():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://example.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["X-Custom"],
        allow_credentials=True,
    )

    @app.get("/c")
    def c():
        return {"ok": 1}

    with TestClient(app) as cli:
        r = cli.get("/c", headers={"Origin": "https://example.com"})
        assert r.headers.get("access-control-allow-origin") == "https://example.com"
        assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_preflight():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://example.com"],
        allow_methods=["GET", "POST"],
        allow_headers=["X-Custom"],
    )

    @app.get("/c")
    def c():
        return {}

    with TestClient(app) as cli:
        r = cli.options(
            "/c",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Custom",
            },
        )
        assert r.status_code == 200
        assert "POST" in (r.headers.get("access-control-allow-methods") or "")
        assert "x-custom" in (
            r.headers.get("access-control-allow-headers") or ""
        ).lower()


def test_cors_rejects_unlisted_origin():
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["https://example.com"])

    @app.get("/c")
    def c():
        return {}

    with TestClient(app) as cli:
        r = cli.get("/c", headers={"Origin": "https://evil.com"})
        assert r.headers.get("access-control-allow-origin") is None


# ── GZip ─────────────────────────────────────────────────────


def test_gzip_compresses_and_skips():
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=10)

    @app.get("/big")
    def big():
        return {"data": "x" * 500}

    with TestClient(app) as cli:
        r = cli.get("/big", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip"
        assert r.json() == {"data": "x" * 500}

        r = cli.get("/big", headers={"Accept-Encoding": "identity"})
        assert r.headers.get("content-encoding") is None


# ── TrustedHost ──────────────────────────────────────────────


def test_trusted_host_allowed_and_wildcard():
    app = FastAPI()
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["testserver", "*.testserver"],
    )

    @app.get("/h")
    def h():
        return {}

    with TestClient(app, base_url="http://testserver") as cli:
        assert cli.get("/h").status_code == 200
    with TestClient(app, base_url="http://api.testserver") as cli:
        assert cli.get("/h").status_code == 200


def test_trusted_host_rejects():
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["example.com"])

    @app.get("/h")
    def h():
        return {}

    with TestClient(app, base_url="http://evil.com") as cli:
        assert cli.get("/h").status_code == 400


# ── HTTPSRedirect ────────────────────────────────────────────


def test_https_redirect_returns_redirect():
    app = FastAPI()
    app.add_middleware(HTTPSRedirectMiddleware)

    @app.get("/r")
    def r_ep():
        return {}

    with TestClient(app) as cli:
        r = cli.get("/r", follow_redirects=False)
        assert r.status_code in (301, 302, 307, 308)


# ── BaseHTTPMiddleware ───────────────────────────────────────


def test_base_http_middleware_mutates_state_and_response():
    class Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.seen = True
            resp = await call_next(request)
            resp.headers["X-Stamp"] = "y"
            return resp

    app = FastAPI()
    app.add_middleware(Stamp)

    @app.get("/bh")
    def bh(request: Request):
        return {"seen": getattr(request.state, "seen", False)}

    with TestClient(app) as cli:
        r = cli.get("/bh")
        assert r.json() == {"seen": True}
        assert r.headers.get("x-stamp") == "y"


def test_base_http_middleware_ordering_lifo():
    class A(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            resp.headers["X-Stack"] = (resp.headers.get("X-Stack") or "") + "A"
            return resp

    class B(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            resp = await call_next(request)
            resp.headers["X-Stack"] = (resp.headers.get("X-Stack") or "") + "B"
            return resp

    app = FastAPI()
    app.add_middleware(A)
    app.add_middleware(B)

    @app.get("/s")
    def s():
        return {}

    with TestClient(app) as cli:
        r = cli.get("/s")
        assert r.headers.get("x-stack") == "AB"


# ── Raw ASGI middleware ──────────────────────────────────────


def test_raw_asgi_middleware():
    class RawASGI:
        def __init__(self, app, marker):
            self.app = app
            self.marker = marker.encode()

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                async def wrap_send(message):
                    if message["type"] == "http.response.start":
                        hdrs = list(message.get("headers", []))
                        hdrs.append((b"x-raw", self.marker))
                        message["headers"] = hdrs
                    await send(message)

                return await self.app(scope, receive, wrap_send)
            return await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(RawASGI, marker="asgi-hit")

    @app.get("/r")
    def r_ep():
        return {"ok": 1}

    with TestClient(app) as cli:
        r = cli.get("/r")
        assert r.headers.get("x-raw") == "asgi-hit"


def test_http_exception_raised_inside_middleware():
    from fastapi import HTTPException

    class ThrowMid(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/boom":
                raise HTTPException(status_code=418, detail="teapot")
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(ThrowMid)

    @app.get("/ok")
    def ok_ep():
        return {}

    @app.get("/boom")
    def boom_ep():
        return {}

    with TestClient(app) as cli:
        assert cli.get("/ok").status_code == 200
        r = cli.get("/boom")
        assert r.status_code == 418
        assert r.json() == {"detail": "teapot"}


# ── WebSocket surface ────────────────────────────────────────


def test_ws_text_bytes_json_roundtrip():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_ep(websocket: WebSocket):
        await websocket.accept()
        t = await websocket.receive_text()
        await websocket.send_text(f"text:{t}")
        b = await websocket.receive_bytes()
        await websocket.send_bytes(b"bytes:" + b)
        j = await websocket.receive_json()
        await websocket.send_json({"echo": j})
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "text:hello"
        ws.send_bytes(b"raw")
        assert ws.receive_bytes() == b"bytes:raw"
        ws.send_json({"k": 1})
        assert ws.receive_json() == {"echo": {"k": 1}}


def test_ws_subprotocol_negotiation():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_ep(websocket: WebSocket):
        await websocket.accept(subprotocol="chat")
        await websocket.send_text("ok")
        await websocket.close()

    cli = TestClient(app)
    with cli.websocket_connect("/ws", subprotocols=["chat", "other"]) as ws:
        assert ws.accepted_subprotocol == "chat"
        assert ws.receive_text() == "ok"


def test_ws_custom_close_code():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_close(websocket: WebSocket):
        await websocket.accept()
        await websocket.close(code=4321, reason="custom")

    cli = TestClient(app)
    with cli.websocket_connect("/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
        assert exc_info.value.code == 4321


def test_ws_client_disconnect_surfaces_on_server():
    app = FastAPI()
    seen = {"v": False}

    @app.websocket("/ws")
    async def ws_dc(websocket: WebSocket):
        await websocket.accept()
        try:
            await websocket.receive_text()
        except WebSocketDisconnect:
            seen["v"] = True

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.close()
    time.sleep(0.1)
    assert seen["v"] is True


def test_ws_depends_and_query_param():
    def get_token(token: str = ""):
        return f"tok:{token}"

    app = FastAPI()

    @app.websocket("/ws/{room}")
    async def ws_pp(
        websocket: WebSocket,
        room: str,
        token_val: str = Depends(get_token),
    ):
        await websocket.accept()
        await websocket.send_json({"room": room, "token_val": token_val})
        await websocket.close()

    with TestClient(app).websocket_connect("/ws/42?token=secret") as ws:
        got = ws.receive_json()
        assert got == {"room": "42", "token_val": "tok:secret"}


def test_ws_large_frames():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_big(websocket: WebSocket):
        await websocket.accept()
        m = await websocket.receive_bytes()
        await websocket.send_bytes(m)
        await websocket.close()

    payload = b"X" * (64 * 1024)
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_bytes(payload)
        assert ws.receive_bytes() == payload


def test_ws_iter_text():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_iter(websocket: WebSocket):
        await websocket.accept()
        count = 0
        async for _ in websocket.iter_text():
            count += 1
            if count == 3:
                break
        await websocket.send_text(f"got:{count}")
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("a")
        ws.send_text("b")
        ws.send_text("c")
        assert ws.receive_text() == "got:3"


def test_ws_mounted_via_apirouter():
    app = FastAPI()
    r = APIRouter()

    @r.websocket("/ws2")
    async def ws_via_router(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("router-ws")
        await websocket.close()

    app.include_router(r, prefix="/v1")

    with TestClient(app).websocket_connect("/v1/ws2") as ws:
        assert ws.receive_text() == "router-ws"


def test_ws_exception_post_accept():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_exc(websocket: WebSocket):
        await websocket.accept()
        raise WebSocketException(code=1008, reason="policy")

    cli = TestClient(app)
    # Starlette convention: WebSocketDisconnect may escape the ``with``
    # block as session.__exit__ re-raises the captured server exception.
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with cli.websocket_connect("/ws") as ws:
            ws.receive_text()
    assert exc_info.value.code == 1008


def test_ws_state_persists_across_messages():
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_state(websocket: WebSocket):
        await websocket.accept()
        websocket.state.counter = 0
        for _ in range(3):
            text = await websocket.receive_text()
            websocket.state.counter += 1
            await websocket.send_text(f"{text}-{websocket.state.counter}")
        await websocket.close()

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("x")
        r1 = ws.receive_text()
        ws.send_text("x")
        r2 = ws.receive_text()
        ws.send_text("x")
        r3 = ws.receive_text()
        assert (r1, r2, r3) == ("x-1", "x-2", "x-3")
