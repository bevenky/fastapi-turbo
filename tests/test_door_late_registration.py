"""The in-process Rust door registers its router (and caches its WS route
table) lazily on the first request. Routes / middleware added AFTER that first
request must still be served — real FastAPI/Starlette match routes live, so a
drop-in replacement must too. Before the fingerprint-invalidation fix the door
served a stale router (HTTP 404) and the WS table 1000-closed freshly-added WS
routes."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, WebSocket
from fastapi_turbo.testclient import TestClient


def test_http_route_added_after_first_request_is_served():
    app = FastAPI()

    @app.get("/first")
    def first():
        return {"r": "first"}

    with TestClient(app, in_process=True) as c:
        assert c.get("/first").json() == {"r": "first"}

        # Add a route AFTER the door has already registered its router.
        @app.get("/late")
        def late():
            return {"r": "late"}

        r = c.get("/late")
        assert r.status_code == 200, r.text
        assert r.json() == {"r": "late"}
        # The original route still works after re-registration.
        assert c.get("/first").json() == {"r": "first"}


def test_ws_route_added_after_first_ws_is_served():
    app = FastAPI()

    @app.websocket("/ws-first")
    async def ws_first(ws: WebSocket):
        await ws.accept()
        await ws.send_text("first")
        await ws.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws-first") as w:
            assert w.receive_text() == "first"

        # Add a WS route AFTER the WS table was first built/cached.
        @app.websocket("/ws-late")
        async def ws_late(ws: WebSocket):
            await ws.accept()
            await ws.send_text("late")
            await ws.close()

        with c.websocket_connect("/ws-late") as w:
            assert w.receive_text() == "late"


def test_middleware_added_after_first_request_takes_effect():
    app = FastAPI()

    @app.get("/m")
    def m():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        assert "x-late-mw" not in c.get("/m").headers

        @app.middleware("http")
        async def add_header(request, call_next):
            resp = await call_next(request)
            resp.headers["x-late-mw"] = "yes"
            return resp

        r = c.get("/m")
        assert r.headers.get("x-late-mw") == "yes", dict(r.headers)
