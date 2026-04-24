"""Regression: ``decimal.Decimal`` response serialization must match
upstream FastAPI byte-for-byte.

Upstream FA's default ``jsonable_encoder`` encodes ``Decimal`` as a
JSON **number** (int when integral, float when fractional). We
previously encoded as a quoted string in both the Rust fast path and
the Python ``JSONResponse`` fallback — breaking any client parsing the
response with a strict numeric schema (e.g. the Stripe SDK, Go encoding/
json's ``Decimal`` types, pgx rowbuild types)."""
from __future__ import annotations

import decimal

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient


def test_rust_path_emits_decimal_as_json_number():
    app = FastAPI()

    @app.get("/d")
    def _d():
        return {
            "whole": decimal.Decimal("5"),
            "frac": decimal.Decimal("1.23"),
            "neg": decimal.Decimal("-100"),
        }

    c = TestClient(app)
    r = c.get("/d")
    assert r.status_code == 200
    # Byte-for-byte upstream parity (same key order + same value shape).
    assert r.content == b'{"whole":5,"frac":1.23,"neg":-100}'


def test_rust_path_nonfinite_decimal_becomes_null():
    # Upstream crashes on NaN Decimal (json.dumps ValueError); we
    # emit JSON null to keep the response valid.
    app = FastAPI()

    @app.get("/n")
    def _n():
        return {"v": decimal.Decimal("NaN")}

    c = TestClient(app)
    r = c.get("/n")
    assert r.status_code == 200
    assert r.content == b'{"v":null}'


def test_python_jsonresponse_path_emits_decimal_as_number():
    """``JSONResponse(content=...)`` uses our Python fallback
    ``_json_default`` — must agree with the Rust path."""
    app = FastAPI()

    @app.get("/p")
    def _p():
        return JSONResponse({"amount": decimal.Decimal("12.99")})

    c = TestClient(app)
    r = c.get("/p")
    assert r.status_code == 200
    assert r.content == b'{"amount":12.99}'


def test_nested_decimal_in_list_and_dict():
    app = FastAPI()

    @app.get("/nested")
    def _nested():
        return {
            "prices": [decimal.Decimal("9.99"), decimal.Decimal("19.95")],
            "map": {"tax": decimal.Decimal("0.08")},
        }

    r = TestClient(app).get("/nested")
    assert r.status_code == 200
    body = r.json()
    assert body == {"prices": [9.99, 19.95], "map": {"tax": 0.08}}
