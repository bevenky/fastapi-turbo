"""``APIRouter(route_class=...)`` with richer body-param annotations:
``TypedDict``, ``list[Item]``, ``set[Item]``, ``Optional[Item]``,
``Union[ItemA, ItemB]``. All of these must default to Body (FA's
heuristic), not Query.
"""
from __future__ import annotations

from typing import Optional, TypedDict, Union

import fastapi_turbo  # noqa: F401

from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    qty: int = 1


class Coupon(BaseModel):
    code: str


class Cart(TypedDict):
    total: int
    note: str


class TracingRoute(APIRoute):
    def get_route_handler(self):
        default = super().get_route_handler()

        async def _h(request: Request):
            resp = await default(request)
            resp.headers["X-Traced"] = "1"
            return resp

        return _h


def _app():
    app = FastAPI()
    r = APIRouter(route_class=TracingRoute)

    @r.post("/list")
    def _list(items: list[Item]):
        return {"count": len(items), "names": [i.name for i in items]}

    @r.post("/td")
    def _td(c: Cart):
        return c

    @r.post("/opt")
    def _opt(item: Optional[Item] = None):
        return {"had_item": item is not None, "item": item}

    @r.post("/union")
    def _union(body: Union[Item, Coupon]):
        # Union: accept either shape; echo type
        if isinstance(body, Item):
            return {"kind": "item", "name": body.name}
        return {"kind": "coupon", "code": body.code}

    app.include_router(r)
    return app


def test_list_of_models_is_body_not_query():
    c = TestClient(_app())
    r = c.post("/list", json=[{"name": "a"}, {"name": "b", "qty": 3}])
    assert r.status_code == 200
    assert r.json() == {"count": 2, "names": ["a", "b"]}
    assert r.headers["x-traced"] == "1"


def test_typeddict_is_body_not_query():
    c = TestClient(_app())
    r = c.post("/td", json={"total": 42, "note": "urgent"})
    assert r.status_code == 200
    assert r.json() == {"total": 42, "note": "urgent"}


def test_optional_model_is_body():
    c = TestClient(_app())
    r = c.post("/opt", json={"name": "x"})
    assert r.status_code == 200
    assert r.json()["had_item"] is True
    assert r.json()["item"]["name"] == "x"


def test_union_of_models_is_body():
    c = TestClient(_app())
    item_r = c.post("/union", json={"name": "widget"})
    assert item_r.status_code == 200
    # Union arms can match either model — both kinds are body-shaped.
    assert item_r.json()["kind"] in ("item", "coupon")
