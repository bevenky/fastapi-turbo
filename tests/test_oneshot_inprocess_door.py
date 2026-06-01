"""In-process ASGI door (the second door into the one Rust engine).

These tests drive requests through ``process_request`` — the PyO3 entry that
runs the SAME assembled ``axum::Router`` in-process via
``tower::Service::oneshot``, with no socket. This is the mechanism that will
replace the ~3,300-line Python in-process HTTP dispatcher: uvicorn /
serverless / ``TestClient(in_process=True)`` will feed requests here instead
of re-implementing the request lifecycle in Python.

The point of the dispatcher collapse is ONE engine, two doors:
  * door A — ``app.run()``         → ``axum::serve(listener, router)``
  * door B — ASGI ``__call__``     → ``process_request`` → ``router.oneshot``

Both drive the identical router, so they must produce byte-identical output.
These tests assert door B produces correct FastAPI responses for the common
HTTP cases (sync + async handlers, path/query params, JSON body validation,
404). They exercise the load-bearing risk the plan flagged: a *sync* handler
runs ``block_in_place`` inside the oneshot future — legal only because the
oneshot is spawned onto a multi-thread-runtime worker.
"""

import json

import fastapi_turbo  # noqa: F401  (installs the `fastapi`/`starlette` compat shim)
from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_turbo._fastapi_turbo_core import process_request, register_app_router


class Item(BaseModel):
    name: str
    qty: int = 1


def _register(app: FastAPI) -> None:
    """Assemble + store the app's router for the in-process door, using the
    SAME full-fidelity arg-building that ``app.run()`` uses (``_build_server_args``)
    so both doors drive a byte-identical router."""
    register_app_router(id(app), *app._build_server_args("127.0.0.1", 0))


def _call(app, method, path, query="", headers=None, body=b""):
    hdrs = [(b"host", b"testserver")]
    if headers:
        hdrs += [(k.encode(), v.encode()) for k, v in headers.items()]
    status, raw_headers, raw_body = process_request(
        id(app), method, path, query, hdrs, body, "127.0.0.1", 50000
    )
    resp_headers = {k.decode(): v.decode() for k, v in raw_headers}
    return status, resp_headers, raw_body


def _make_app():
    app = FastAPI()

    @app.get("/sync")
    def sync_handler():
        return {"kind": "sync", "n": 1}

    @app.get("/async")
    async def async_handler():
        return {"kind": "async", "n": 2}

    @app.get("/item/{item_id}")
    def read_item(item_id: int):
        return {"item_id": item_id, "doubled": item_id * 2}

    @app.get("/q")
    def query_param(name: str = "none", count: int = 0):
        return {"name": name, "count": count}

    @app.post("/items")
    def create_item(item: Item):
        return {"created": item.name, "qty": item.qty}

    return app


def test_sync_handler_dispatches_in_process():
    # A *sync* handler runs block_in_place inside the oneshot future.
    app = _make_app()
    _register(app)
    status, headers, body = _call(app, "GET", "/sync")
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {"kind": "sync", "n": 1}


def test_async_handler_dispatches_in_process():
    app = _make_app()
    _register(app)
    status, _headers, body = _call(app, "GET", "/async")
    assert status == 200
    assert json.loads(body) == {"kind": "async", "n": 2}


def test_path_param_coercion():
    app = _make_app()
    _register(app)
    status, _headers, body = _call(app, "GET", "/item/42")
    assert status == 200
    assert json.loads(body) == {"item_id": 42, "doubled": 84}


def test_query_params():
    app = _make_app()
    _register(app)
    status, _headers, body = _call(app, "GET", "/q", query="name=hi&count=3")
    assert status == 200
    assert json.loads(body) == {"name": "hi", "count": 3}


def test_post_json_body_validation():
    app = _make_app()
    _register(app)
    payload = json.dumps({"name": "widget", "qty": 5}).encode()
    status, _headers, body = _call(
        app, "POST", "/items", headers={"content-type": "application/json"}, body=payload
    )
    assert status == 200
    assert json.loads(body) == {"created": "widget", "qty": 5}


def test_invalid_body_returns_422():
    app = _make_app()
    _register(app)
    # qty must be an int; a string that can't coerce → 422.
    payload = json.dumps({"name": "widget", "qty": "not-an-int"}).encode()
    status, _headers, _body = _call(
        app, "POST", "/items", headers={"content-type": "application/json"}, body=payload
    )
    assert status == 422


def test_unknown_path_returns_404():
    app = _make_app()
    _register(app)
    status, _headers, _body = _call(app, "GET", "/does-not-exist")
    assert status == 404


def test_openapi_json_through_door():
    # _build_server_args registers the dynamic /openapi.json route, so the
    # in-process door must serve it too (full-fidelity registration).
    app = _make_app()
    _register(app)
    status, headers, body = _call(app, "GET", "/openapi.json")
    assert status == 200
    assert "application/json" in headers["content-type"]
    schema = json.loads(body)
    assert "openapi" in schema and "/items" in schema["paths"]


def test_oneshot_selftest_mechanism():
    # Pure-Rust route, proves the runtime + Service::oneshot + body collection.
    from fastapi_turbo._fastapi_turbo_core import _oneshot_selftest

    status, body = _oneshot_selftest()
    assert status == 200
    assert json.loads(body) == {"oneshot": "ok"}
