"""Per-route ``servers`` and ``externalDocs`` in the generated OpenAPI.

Two paths must both populate the operation object:
  * ``openapi_extra={'servers': ..., 'externalDocs': ...}`` — the
    upstream-compatible path (works in vanilla FastAPI too).
  * ``servers=...`` / ``external_docs=...`` kwargs on route decorators —
    our beyond-parity convenience for OpenAPI 3.1 operation-level metadata.
"""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import APIRouter, FastAPI


def test_openapi_extra_servers_and_external_docs():
    app = FastAPI()

    @app.get(
        "/x",
        openapi_extra={
            "servers": [{"url": "https://x.example.com"}],
            "externalDocs": {"url": "https://docs.example.com"},
        },
    )
    def _x():
        return {}

    op = app.openapi()["paths"]["/x"]["get"]
    assert op["servers"] == [{"url": "https://x.example.com"}]
    assert op["externalDocs"] == {"url": "https://docs.example.com"}


def test_route_decorator_kwargs_servers_and_external_docs():
    app = FastAPI()

    @app.get(
        "/a",
        servers=[{"url": "https://a.example.com", "description": "Primary"}],
        external_docs={"url": "https://docs.example.com", "description": "See docs"},
    )
    def _a():
        return {}

    op = app.openapi()["paths"]["/a"]["get"]
    assert op["servers"] == [
        {"url": "https://a.example.com", "description": "Primary"}
    ]
    assert op["externalDocs"] == {
        "url": "https://docs.example.com",
        "description": "See docs",
    }


def test_per_route_kwargs_through_apirouter_include():
    app = FastAPI()
    r = APIRouter()

    @r.get("/b", servers=[{"url": "https://b.example.com"}])
    def _b():
        return {}

    app.include_router(r)
    op = app.openapi()["paths"]["/b"]["get"]
    assert op["servers"] == [{"url": "https://b.example.com"}]


def test_absent_kwargs_do_not_populate_operation():
    app = FastAPI()

    @app.get("/c")
    def _c():
        return {}

    op = app.openapi()["paths"]["/c"]["get"]
    assert "servers" not in op
    assert "externalDocs" not in op
