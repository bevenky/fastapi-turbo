"""Regression tests for two latent bugs in the Response-object JSON
fallback (responses.rs ``pyobj_to_serde``), found by the Rust-reuse audit.

A previous fix (db017cd) repaired big-int corruption ONLY in the direct
PyDict->JSON writer (``write_any_json``). The parallel serde_json-based
converter ``pyobj_to_serde`` kept both bugs:

BUG 1 — ints were extracted as i64 with an f64 fallback, so a bare scalar
return > i64::MAX (or < i64::MIN) silently corrupted to a lossy float
(Python json.dumps emits the exact integer).

BUG 2 — tuples (and other non-dict/list containers) fell through to a
``str()`` repr fallback, serializing as the Python repr string instead of
a JSON array (which ``write_any_json`` already emits correctly).

Reachable from Python via:
  * a handler returning a bare scalar int (py_to_response int branch), and
  * a Response subclass whose ``render`` defers serialization to the
    server, so ``.body`` reaches the door's dict-like body fallback
    (response_object_to_response).
"""

import fastapi_turbo  # noqa: F401  (installs the compat shim)
from fastapi import FastAPI
from fastapi_turbo.responses import Response
from fastapi_turbo.testclient import TestClient


def test_bare_bigint_scalar_return_serializes_exactly():
    app = FastAPI()

    @app.get("/fit")
    async def fit():
        return 2**63 - 1  # i64::MAX — control, fast path

    @app.get("/over")
    async def over():
        return 2**63 + 1  # > i64::MAX

    @app.get("/way-over")
    async def way_over():
        return 2**128  # far beyond f64 exact range

    @app.get("/neg")
    async def neg():
        return -(2**63) - 1  # < i64::MIN

    with TestClient(app, in_process=True) as c:
        r = c.get("/fit")
        assert r.status_code == 200, r.text
        assert r.text == str(2**63 - 1)

        r = c.get("/over")
        assert r.status_code == 200, r.text
        # Exact digits, not ...776000 (the f64 rounding artifact).
        assert r.text == "9223372036854775809", r.text

        r = c.get("/way-over")
        assert r.text == str(2**128), r.text

        r = c.get("/neg")
        assert r.text == str(-(2**63) - 1), r.text


class DeferredJSONResponse(Response):
    """User-style subclass that hands the raw Python object through
    ``render``, deferring JSON encoding to the server. The door's
    Response-object body fallback then serializes ``.body`` itself."""

    media_type = "application/json"

    def render(self, content):
        return content


def _deferred(content) -> DeferredJSONResponse:
    resp = DeferredJSONResponse(content)
    # init_headers computed content-length from len(<python object>);
    # drop it so the transport frames the real serialized body.
    del resp.headers["content-length"]
    return resp


def test_response_object_dict_body_bigints_and_tuples():
    app = FastAPI()

    @app.get("/deferred")
    async def deferred():
        return _deferred(
            {
                "big": 2**63 + 1,
                "way_big": 2**128,
                "pair": (1, 2),
                "nested": {"t": (3, "x")},
            }
        )

    with TestClient(app, in_process=True) as c:
        r = c.get("/deferred")
        assert r.status_code == 200, r.text
        data = r.json()
        # BUG 1 — exact big-int digits (no float rounding).
        assert data["big"] == 2**63 + 1, r.text
        assert data["way_big"] == 2**128, r.text
        assert "9223372036854775809" in r.text
        # BUG 2 — tuples are JSON arrays, not repr strings.
        assert data["pair"] == [1, 2], r.text
        assert data["nested"]["t"] == [3, "x"], r.text


def test_response_object_tuple_body_is_json_array():
    app = FastAPI()

    @app.get("/tuple-body")
    async def tuple_body():
        # Mixed types so the body cannot accidentally extract as bytes.
        return _deferred(("a", 1, 2**70))

    with TestClient(app, in_process=True) as c:
        r = c.get("/tuple-body")
        assert r.status_code == 200, r.text
        assert r.json() == ["a", 1, 2**70], r.text
