"""Dataclass and ``TypedDict`` as ``response_model`` / return-type.

Both are accepted by upstream FastAPI via Pydantic's ``TypeAdapter``.
This suite locks in parity: dataclasses / TypedDicts serialise cleanly,
the response JSON has the declared fields only (filtering works), and
the generated OpenAPI schema is structurally sound.

``msgspec.Struct`` is intentionally NOT covered: upstream FastAPI
rejects it at decoration time with the same FastAPIError we raise, so
the parity outcome is "both frameworks say no". A regression there
would be an upstream-divergence bug, so we test the negative case too.
"""
from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclasses.dataclass
class UserDC:
    id: int
    name: str
    email: str


class UserTD(TypedDict):
    id: int
    name: str
    email: str


def test_dataclass_response_model_filters_and_serialises():
    app = FastAPI()

    @app.get("/by-model", response_model=UserDC)
    def _by_model():
        # Extra 'secret' key must be stripped by response_model filtering.
        return {"id": 1, "name": "alice", "email": "a@example.com", "secret": "leak"}

    c = TestClient(app)
    r = c.get("/by-model")
    assert r.status_code == 200
    body = r.json()
    assert body == {"id": 1, "name": "alice", "email": "a@example.com"}


def test_dataclass_instance_returned_directly_serialises():
    app = FastAPI()

    @app.get("/direct")
    def _direct():
        return UserDC(id=2, name="bob", email="b@example.com")

    c = TestClient(app)
    r = c.get("/direct")
    assert r.status_code == 200
    assert r.json() == {"id": 2, "name": "bob", "email": "b@example.com"}


def test_typeddict_response_model_filters():
    app = FastAPI()

    @app.get("/td", response_model=UserTD)
    def _td():
        return {"id": 3, "name": "carol", "email": "c@example.com", "extra": "leak"}

    c = TestClient(app)
    r = c.get("/td")
    assert r.status_code == 200
    assert r.json() == {"id": 3, "name": "carol", "email": "c@example.com"}


def test_typeddict_openapi_schema_has_properties():
    app = FastAPI()

    @app.get("/td", response_model=UserTD)
    def _td():
        return {"id": 1, "name": "x", "email": "y"}

    op = app.openapi()["paths"]["/td"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    # Inlined or $ref — follow $ref if present
    if "$ref" in schema:
        defs = app.openapi().get("components", {}).get("schemas", {})
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        schema = defs[ref_name]
    assert set(schema["properties"].keys()) == {"id", "name", "email"}
    assert set(schema["required"]) == {"id", "name", "email"}


def test_msgspec_struct_rejected_at_decoration_time_matches_upstream():
    msgspec = pytest.importorskip("msgspec")

    class UserS(msgspec.Struct):
        id: int
        name: str

    app = FastAPI()
    with pytest.raises(Exception) as excinfo:
        @app.get("/s", response_model=UserS)
        def _s():
            return UserS(id=1, name="x")
    # Upstream raises FastAPIError with the same message prefix.
    assert "Invalid args for response field" in str(excinfo.value)
