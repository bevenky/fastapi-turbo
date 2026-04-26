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


def test_nonfinite_decimal_response_does_not_emit_invalid_json():
    """Returning ``{"v": Decimal("NaN")}`` from a handler must NOT
    surface as a 200 response with the invalid literal ``NaN`` /
    ``Infinity`` token in the body. Both the Rust hot path and the
    Python ``JSONResponse.render`` (with ``allow_nan=False``) reject
    non-finite floats — the response is either a 5xx error (matches
    upstream FastAPI's ``json.dumps`` ValueError → 500) OR valid
    JSON without the literal token (e.g. quoted ``"NaN"`` for paths
    that coerce non-finite Decimals to strings).

    The earlier assertion ``r.content == b'{"v":null}'`` baked in
    the Rust path's specific null-coercion as a contract; that's
    not parity with upstream and creates an inconsistency with the
    Python path, which raises. Drop the literal-bytes assertion in
    favour of the semantic guarantee."""
    app = FastAPI()

    @app.get("/n")
    def _n():
        return {"v": decimal.Decimal("NaN")}

    # ``raise_server_exceptions=False`` so a server-side ValueError
    # surfaces as a 500 to the client (Starlette TestClient parity).
    # Without it, the exception escapes the TestClient and the test
    # crashes instead of asserting on the response.
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/n")
    if r.status_code == 200:
        assert b"NaN" not in r.content, (
            f"emitted invalid JSON with NaN literal: {r.content!r}"
        )
        assert b"Infinity" not in r.content, (
            f"emitted invalid JSON with Infinity literal: {r.content!r}"
        )
        # If the path emitted a successful 200, the body must be valid JSON.
        import json
        json.loads(r.content)
    else:
        # Non-200 (typically 500) is the upstream-parity outcome.
        assert r.status_code >= 500, (
            f"non-finite Decimal expected 200-with-coercion or "
            f"5xx error; got {r.status_code} {r.content!r}"
        )


def test_python_jsonresponse_path_with_decimal_raises_typeerror():
    """Explicit ``fastapi_turbo.responses.JSONResponse({...Decimal...})``
    raises ``TypeError`` on construction — matching upstream
    ``starlette.responses.JSONResponse`` exactly. Users who want
    Decimal coercion should return the dict from a handler (so
    ``jsonable_encoder`` runs) rather than constructing the
    JSONResponse directly with raw Decimal values.

    The previous behavior (silent coercion via ``_json_default``)
    was a drop-in parity break — upstream wraps stdlib
    ``json.dumps`` with no ``default=``, so any non-serializable
    type raises."""
    import pytest as _pytest

    with _pytest.raises(TypeError, match="Decimal"):
        JSONResponse({"amount": decimal.Decimal("12.99")})

    with _pytest.raises(TypeError, match="Decimal"):
        JSONResponse({"v": decimal.Decimal("NaN")})


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
