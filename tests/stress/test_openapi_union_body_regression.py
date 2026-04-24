"""Regression: ``Union[int, str] = Body(...)`` (and other non-BaseModel
union arms) must not crash ``app.openapi()``.

Previously ``_openapi.py`` called an undefined ``_get_type_name`` helper
when a Body param was a union of primitive types, producing a NameError
at schema-generation time."""
from __future__ import annotations

from typing import Union
from uuid import UUID

import fastapi_turbo  # noqa: F401

from fastapi import Body, FastAPI


def test_openapi_with_primitive_union_body_does_not_crash():
    app = FastAPI()

    @app.post("/u")
    def _u(x: Union[int, str] = Body(...)):
        return {"x": x}

    schema = app.openapi()
    op = schema["paths"]["/u"]["post"]
    body_schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert "anyOf" in body_schema
    type_set = {frag.get("type") for frag in body_schema["anyOf"] if isinstance(frag, dict)}
    assert "integer" in type_set
    assert "string" in type_set


def test_openapi_with_uuid_in_union_keeps_format():
    app = FastAPI()

    @app.post("/u")
    def _u(x: Union[UUID, int] = Body(...)):
        return {}

    schema = app.openapi()
    body_schema = schema["paths"]["/u"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "anyOf" in body_schema
    # UUID arm should be ``{type: string, format: uuid}``
    uuid_arm = next(
        (f for f in body_schema["anyOf"] if isinstance(f, dict) and f.get("format") == "uuid"),
        None,
    )
    assert uuid_arm is not None, body_schema
