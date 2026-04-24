"""``APIRouter(route_class=<MyAPIRoute>)`` must be honoured end-to-end
through Rust: the custom ``get_route_handler`` runs per request, bodies
are parsed, Pydantic validation fires, and ``HTTPException`` raised
from a wrapped handler reaches the user's wrapper in ``get_route_handler``."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Pair(BaseModel):
    x: int
    y: int


class HeaderInjectingRoute(APIRoute):
    def get_route_handler(self):
        default = super().get_route_handler()

        async def _handler(request: Request):
            resp = await default(request)
            resp.headers["X-Route-Class"] = "injected"
            return resp

        return _handler


class BodyCountingRoute(APIRoute):
    def get_route_handler(self):
        default = super().get_route_handler()

        async def _handler(request: Request):
            body = await request.body()
            # request.body() caches — default handler reads from the same cache
            resp = await default(request)
            resp.headers["X-Body-Len"] = str(len(body))
            return resp

        return _handler


class ExceptionCatchingRoute(APIRoute):
    def get_route_handler(self):
        default = super().get_route_handler()

        async def _handler(request: Request):
            try:
                return await default(request)
            except HTTPException as e:
                return JSONResponse(
                    {"error": e.detail, "wrapped_by": "ExceptionCatchingRoute"},
                    status_code=e.status_code,
                )

        return _handler


def test_route_class_header_injection_on_simple_get():
    app = FastAPI()
    r = APIRouter(route_class=HeaderInjectingRoute)

    @r.get("/p")
    def _p():
        return {"ok": 1}

    app.include_router(r)
    c = TestClient(app)
    resp = c.get("/p")
    assert resp.status_code == 200
    assert resp.headers["x-route-class"] == "injected"
    assert resp.json() == {"ok": 1}


def test_route_class_body_param_validates_pydantic():
    app = FastAPI()
    r = APIRouter(route_class=HeaderInjectingRoute)

    @r.post("/add")
    def _add(b: Pair):
        return {"sum": b.x + b.y}

    app.include_router(r)
    c = TestClient(app)
    # Valid
    resp = c.post("/add", json={"x": 3, "y": 4})
    assert resp.status_code == 200
    assert resp.json() == {"sum": 7}
    assert resp.headers["x-route-class"] == "injected"
    # 422 on invalid body
    bad = c.post("/add", json={"x": "nope", "y": 4})
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["loc"] == ["body", "x"]


def test_route_class_pre_reads_body_and_default_handler_sees_same_body():
    app = FastAPI()
    r = APIRouter(route_class=BodyCountingRoute)

    @r.post("/echo")
    async def _echo(request: Request):
        data = await request.body()
        return {"got": len(data)}

    app.include_router(r)
    c = TestClient(app)
    resp = c.post("/echo", content=b"hello world")
    assert resp.status_code == 200
    assert resp.json() == {"got": 11}
    assert resp.headers["x-body-len"] == "11"


def test_route_class_catches_http_exception_from_handler():
    app = FastAPI()
    r = APIRouter(route_class=ExceptionCatchingRoute)

    @r.post("/may-fail")
    def _mf(b: Pair):
        if b.x == 0:
            raise HTTPException(status_code=400, detail="zero")
        return {"sum": b.x + b.y}

    app.include_router(r)
    c = TestClient(app)
    # Happy path
    ok = c.post("/may-fail", json={"x": 1, "y": 2})
    assert ok.status_code == 200
    assert ok.json() == {"sum": 3}
    # Caught and re-shaped by the custom route class
    bad = c.post("/may-fail", json={"x": 0, "y": 2})
    assert bad.status_code == 400
    assert bad.json() == {"error": "zero", "wrapped_by": "ExceptionCatchingRoute"}
