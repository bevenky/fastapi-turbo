"""R46 audit follow-ups — ResponseValidationError carries
endpoint_ctx; non-JSON body with model param hands raw to Pydantic.
Net sandboxed-gate change: 112 → 111 failed.
"""
from typing import Annotated

import pytest
from pydantic import BaseModel

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 ResponseValidationError carries endpoint context
# ────────────────────────────────────────────────────────────────────


def test_response_validation_error_carries_endpoint_function():
    """When ``response_model`` validation fails, the
    ``ResponseValidationError`` should include the endpoint
    function / file / line / path so user
    ``@app.exception_handler(ResponseValidationError)`` impls can
    log them. Earlier our in-process dispatcher passed
    ``endpoint_ctx=None`` to ``_apply_response_model``."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.exceptions import ResponseValidationError
    from fastapi_turbo.testclient import TestClient

    captured: dict = {}

    class _Out(BaseModel):
        id: int
        name: str

    app = FastAPI()

    @app.exception_handler(ResponseValidationError)
    async def _h(_req, exc):
        captured["fn"] = exc.endpoint_function
        captured["str"] = str(exc)
        from fastapi_turbo.responses import JSONResponse
        return JSONResponse({"detail": exc.errors()}, status_code=500)

    @app.get("/items/", response_model=_Out)
    async def get_item():
        return {"name": "Widget"}  # Missing required ``id`` field.

    with TestClient(app, in_process=True) as c:
        c.get("/items/")

    assert captured.get("fn") == "get_item", captured
    assert "get_item" in captured.get("str", ""), captured


# ────────────────────────────────────────────────────────────────────
# #2 form-encoded body to JSON-model endpoint hands raw to Pydantic
# ────────────────────────────────────────────────────────────────────


def test_form_encoded_body_to_json_model_emits_pydantic_error():
    """When the wire ``content-type`` is form-encoded but the
    endpoint declares a model body, FA hands the raw body to
    Pydantic so its ``model_*_type`` error surfaces with the raw
    string in ``input``. Earlier we always emitted ``json_invalid``
    even when the body wasn't claimed to be JSON, hiding the real
    type-mismatch from the client."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Item(BaseModel):
        name: str
        price: float

    app = FastAPI()

    @app.post("/items/")
    async def _h(item: _Item):
        return item

    with TestClient(app, in_process=True) as c:
        # Form-encoded body to JSON endpoint.
        r = c.post("/items/", data={"name": "Foo", "price": 50.5})
        assert r.status_code == 422, r.text
        body = r.json()
        # Pydantic surfaced the type mismatch — input is the raw
        # body string, NOT the json_invalid empty-dict shape.
        e = body["detail"][0]
        assert e["input"] == "name=Foo&price=50.5", e
        # Error type is one of Pydantic's model-shape rejections —
        # ``model_attributes_type`` (FA's snapshot) or
        # ``model_type`` (Pydantic v2 with the synthetic combined-
        # body wrapper). Either is acceptable parity since both
        # surface the raw body.
        assert e["type"] in ("model_attributes_type", "model_type"), e


# ────────────────────────────────────────────────────────────────────
# #3 valid JSON with broken JSON body still emits json_invalid
# ────────────────────────────────────────────────────────────────────


def test_broken_json_with_json_content_type_emits_json_invalid():
    """Sanity guard: when content-type IS JSON, broken JSON still
    emits ``json_invalid`` (not the form-fallback path). The R46
    fix only kicks in when content-type isn't JSON."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    class _Item(BaseModel):
        name: str

    app = FastAPI()

    @app.post("/items/")
    async def _h(item: _Item):
        return item

    with TestClient(app, in_process=True) as c:
        r = c.post(
            "/items/",
            content="{not-json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["detail"][0]["type"] == "json_invalid", body


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
