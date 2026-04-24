"""OpenAPI 3.1 ``webhooks`` section.

FastAPI exposes webhook definitions through ``app.webhooks`` (an
``APIRouter``). Each operation registered there appears under the
top-level ``webhooks`` key of the generated schema, rather than
``paths``."""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel


class PaymentEvent(BaseModel):
    # NB: module-level because `from __future__ import annotations` +
    # a class defined inside a function body leaves the annotation as
    # a string that Pydantic can't resolve (matches upstream behaviour).
    amount: float
    currency: str


def test_webhooks_appear_under_webhooks_key_not_paths():
    app = FastAPI()

    @app.webhooks.post("new-subscription")
    def _new_sub():
        """Notify subscribers when a new entity appears."""
        return {}

    schema = app.openapi()
    assert schema["paths"] == {}
    assert "new-subscription" in schema["webhooks"]
    op = schema["webhooks"]["new-subscription"]["post"]
    assert "Notify subscribers" in op["description"]


def test_webhooks_with_body_model_generates_schema():
    app = FastAPI()

    @app.webhooks.post("payment.succeeded")
    def _pay(event: PaymentEvent):
        return {}

    schema = app.openapi()
    wh = schema["webhooks"]["payment.succeeded"]["post"]
    body = wh["requestBody"]["content"]["application/json"]["schema"]
    # Either inlined or $ref — accept both
    assert body.get("$ref") or body.get("properties")


def test_webhooks_kwarg_accepts_prebuilt_router():
    r = APIRouter()

    @r.post("order.shipped")
    def _o():
        return {}

    app = FastAPI(webhooks=r)
    schema = app.openapi()
    assert "order.shipped" in schema["webhooks"]


def test_empty_webhooks_omits_or_empties_section():
    app = FastAPI()
    schema = app.openapi()
    # If present, must be empty
    assert schema.get("webhooks", {}) == {}
