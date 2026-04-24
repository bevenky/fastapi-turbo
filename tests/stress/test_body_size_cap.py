"""Regression: the router must not impose a hidden body-size cap
beyond what the app's ``max_request_size`` specifies.

Previously ``src/router.rs`` called ``to_bytes(body, 10 * 1024 * 1024)``
regardless of the ``max_request_size`` configured on the ``FastAPI(...)``
app. An app that set ``max_request_size=50 * 1024 * 1024`` would still
reject uploads > 10 MiB with 400 Bad Request — surprising.
"""
from __future__ import annotations

import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Body
from fastapi.testclient import TestClient


def test_20mb_upload_succeeds_with_50mb_limit():
    app = FastAPI(max_request_size=50 * 1024 * 1024)

    @app.post("/upload")
    def upload(data: bytes = Body(..., media_type="application/octet-stream")):
        return {"size": len(data)}

    body = b"x" * (20 * 1024 * 1024)
    with TestClient(app) as cli:
        r = cli.post("/upload", content=body, headers={"content-type": "application/octet-stream"})
        assert r.status_code == 200, r.text
        assert r.json() == {"size": len(body)}


def test_default_no_max_accepts_large_body():
    """When max_request_size is unset, uploads should be bounded only
    by realistic memory. Previously the internal 10 MiB cap fired."""
    app = FastAPI()

    @app.post("/upload")
    def upload(data: bytes = Body(..., media_type="application/octet-stream")):
        return {"size": len(data)}

    body = b"y" * (12 * 1024 * 1024)  # 12 MiB — would fail under the old 10 MiB cap
    with TestClient(app) as cli:
        r = cli.post("/upload", content=body, headers={"content-type": "application/octet-stream"})
        assert r.status_code == 200, r.text
        assert r.json() == {"size": len(body)}


def test_max_request_size_still_rejects_oversized():
    """Setting max_request_size must still enforce 413 on oversize."""
    app = FastAPI(max_request_size=1 * 1024 * 1024)  # 1 MiB

    @app.post("/upload")
    def upload(data: bytes = Body(..., media_type="application/octet-stream")):
        return {"size": len(data)}

    body = b"z" * (2 * 1024 * 1024)  # 2 MiB — over the 1 MiB limit
    with TestClient(app) as cli:
        r = cli.post("/upload", content=body, headers={"content-type": "application/octet-stream"})
        # Tower's RequestBodyLimitLayer → 413
        assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text[:120]}"
