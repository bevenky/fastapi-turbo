"""``operation_id`` plumbing in the generated OpenAPI schema:

* Duplicate ``operation_id`` across routes emits a ``UserWarning``
  (matching upstream FastAPI's behaviour).
* ``generate_unique_id_function`` is honoured at three levels:
  app-wide, per-router, and per-route decorator kwarg.
"""
from __future__ import annotations

import warnings

import fastapi_turbo  # noqa: F401

from fastapi import APIRouter, FastAPI


def test_duplicate_operation_id_emits_user_warning():
    app = FastAPI()

    @app.get("/a", operation_id="dupe")
    def _a():
        return {}

    @app.get("/b", operation_id="dupe")
    def _b():
        return {}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        app.openapi()
    msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("Duplicate Operation ID dupe" in m for m in msgs), msgs


def test_generate_unique_id_function_app_level():
    def slug(route):
        return route.path.strip("/").replace("/", "_") + "_" + list(route.methods)[0].lower()

    app = FastAPI(generate_unique_id_function=slug)

    @app.get("/users/{id}")
    def _get_user(id: int):
        return {}

    op = app.openapi()["paths"]["/users/{id}"]["get"]
    assert op["operationId"] == "users_{id}_get"


def test_generate_unique_id_function_router_level():
    def slug(route):
        return route.path.strip("/").replace("/", "_") + "_" + list(route.methods)[0].lower()

    app = FastAPI()
    r = APIRouter(generate_unique_id_function=slug)

    @r.get("/posts/{id}")
    def _get_post(id: int):
        return {}

    app.include_router(r)
    op = app.openapi()["paths"]["/posts/{id}"]["get"]
    assert op["operationId"] == "posts_{id}_get"


def test_generate_unique_id_function_per_route():
    app = FastAPI()

    @app.get("/t", generate_unique_id_function=lambda r: "route_scoped")
    def _t():
        return {}

    op = app.openapi()["paths"]["/t"]["get"]
    assert op["operationId"] == "route_scoped"
