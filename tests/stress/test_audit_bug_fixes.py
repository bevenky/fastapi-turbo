"""Regression tests for two bugs found by the Rust-reuse audit (2026-06-03),
both on the in-process oneshot-door path (default-on).

BUG 1 — the direct PyDict->JSON writer (responses.rs write_any_json) extracted
ints as i64 then fell back to f64, silently rounding ints > i64::MAX to a lossy
float (Python json.dumps emits the exact integer).

BUG 2 — a custom (non-HTTP) exception raised INSIDE a dependency bypassed the
user's @app.exception_handler on the door path (the Rust dep-resolution error
branch rendered the default 500 directly). Handler-body raises already worked.
"""

import json

import fastapi_turbo  # noqa: F401  (installs the compat shim)
from fastapi import Depends, FastAPI
from fastapi_turbo.responses import JSONResponse
from fastapi_turbo.testclient import TestClient


def test_big_int_serializes_exactly_not_lossy_float():
    app = FastAPI()

    @app.get("/big")
    async def big():
        return {
            "over": 2**63 + 1,      # > i64::MAX
            "way_over": 2**128,     # far beyond f64 exact range
            "fit": 2**63 - 1,       # i64::MAX (fast path)
            "small": 2**62,
            "neg": -(2**63) - 1,    # < i64::MIN
        }

    with TestClient(app, in_process=True) as c:
        r = c.get("/big")
        assert r.status_code == 200, r.text
        # Byte-for-byte parity with Python json on the integers.
        assert r.json() == {
            "over": 2**63 + 1,
            "way_over": 2**128,
            "fit": 2**63 - 1,
            "small": 2**62,
            "neg": -(2**63) - 1,
        }
        # Explicit: the exact digits appear in the raw body (no rounding).
        assert str(2**128) in r.text
        assert "9223372036854775809" in r.text  # 2**63 + 1, not ...776000


def test_custom_exception_in_dependency_routes_to_user_handler():
    class MyErr(Exception):
        pass

    app = FastAPI()

    @app.exception_handler(MyErr)
    async def handle_myerr(_req, _exc):
        return JSONResponse({"handled": True}, status_code=418)

    def dep_raises():
        raise MyErr()

    @app.get("/in-dep")
    async def in_dep(_v=Depends(dep_raises)):
        return {"ok": True}

    # Control: a handler-body raise already routed to the handler.
    @app.get("/in-handler")
    async def in_handler():
        raise MyErr()

    with TestClient(app, in_process=True) as c:
        r = c.get("/in-dep")
        assert r.status_code == 418, r.text
        assert r.json() == {"handled": True}
        r2 = c.get("/in-handler")
        assert r2.status_code == 418, r2.text
        assert r2.json() == {"handled": True}


def test_http_exception_in_dependency_still_renders_detail():
    """HTTPException raised in a dep keeps the door's fast-path rendering."""
    from fastapi_turbo import HTTPException

    app = FastAPI()

    def dep_403():
        raise HTTPException(status_code=403, detail="nope")

    @app.get("/guard")
    async def guard(_v=Depends(dep_403)):
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.get("/guard")
        assert r.status_code == 403, r.text
        assert r.json() == {"detail": "nope"}
