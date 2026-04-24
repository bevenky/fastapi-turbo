"""HEAD/OPTIONS on a GET-only route must return 405 with an ``Allow``
header listing ONLY the declared methods — matching upstream FastAPI
exactly (not a generic catch-all list)."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app():
    app = FastAPI()

    @app.get("/only_get")
    def _g():
        return {"ok": 1}

    @app.post("/only_post")
    def _p():
        return {"ok": 1}

    @app.api_route("/get_post", methods=["GET", "POST"])
    def _gp():
        return {"ok": 1}

    return app


def test_head_on_get_only_returns_405_with_allow_get():
    c = TestClient(_app())
    r = c.request("HEAD", "/only_get")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET"
    assert r.content == b""


def test_options_on_get_only_returns_405_with_allow_get():
    c = TestClient(_app())
    r = c.request("OPTIONS", "/only_get")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET"


def test_options_on_post_only_returns_405_with_allow_post():
    c = TestClient(_app())
    r = c.request("OPTIONS", "/only_post")
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_disallowed_method_lists_all_declared_methods():
    c = TestClient(_app())
    r = c.request("DELETE", "/get_post")
    assert r.status_code == 405
    methods = {m.strip() for m in r.headers["allow"].split(",")}
    assert methods == {"GET", "POST"}
