"""In-process ASGI dispatch through the Rust oneshot door — consolidated.

The app must be usable as a plain ASGI callable without binding a loopback
socket (httpx ``ASGITransport`` — the path serverless runtimes and sandboxed
test harnesses use). The autouse fixture monkeypatches ``_asgi_ensure_server``
to raise, so any silent fall-through to the loopback proxy is a loud failure.

CONSOLIDATION (coverage-differential, round-9 follow-up): this file replaces
ten ``test_asgi_in_process_*`` files. Every test deleted had (a) an EMPTY
unique-arc differential over ``python/fastapi_turbo`` against the retained
suite + upstream FastAPI suite (coverage.py, branch, per-test contexts), and
(b) a retained test pinning the same endpoint shape through the same door:

  * basic GET/path/query/body/404/422 → tests/test_oneshot_inprocess_door.py
  * Request injection               → tests/stress/test_asgi_adapter_fidelity.py
  * urlencoded form / file upload   → tests/test_pivot_adapter.py (A/B oracle)
  * http-MW header inject + order   → tests/test_door_late_registration.py,
                                       tests/test_new_features.py (in_process)
  * raw-ASGI MW inject + LIFO order → tests/stress/test_broad_starlette_parity.py
                                       (A/B oracle: state, 3-MW order, mixed
                                       Tower+raw order)
  * response_model filter/unset     → tests/test_pivot_adapter.py (/rm, /rm2)
  * response_model aliases          → tests/test_p0_parity.py (both directions)
  * yield-dep teardown ordering     → tests/stress/test_r42_regressions.py
                                       (finally-on-raise, bg-before-teardown)
  * dependency_overrides            → upstream suite (TestClient auto-switches
                                       to in-process on overrides) + r47
  * simple/inner-query deps         → test_asgi_in_process_parity_contract.py
  * streaming sync-gen + media_type → tests/stress/test_broad_starlette_parity.py
  * WS echo / path params via door  → tests/stress/test_r12_regressions.py,
                                       test_r15_regressions.py, r47/r48, r53

What remains here are the tests that DO carry unique arcs (async-dep override
wrapper, nested-dep introspection, mount recursion + top-level-wins, the
SecurityScopes special-param emitter, async-generator streaming) plus three
shape pins with no exact retained door twin (mixed Form+File, user-MW
short-circuits)."""
from __future__ import annotations

import asyncio
import io

import pytest

import fastapi_turbo  # noqa: F401

from fastapi import Depends, FastAPI, File, Form, Request, Security, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import SecurityScopes
from fastapi.testclient import AsyncClient, ASGITransport


@pytest.fixture(autouse=True)
def _block_loopback_server(monkeypatch):
    """Any fall-through to ``_asgi_ensure_server`` becomes a loud failure
    so we catch the "silently went back to the loopback proxy" case."""

    async def _boom(self):
        raise RuntimeError(
            "in-process ASGI path silently fell back to the loopback "
            "proxy — the oneshot door didn't handle this request"
        )

    monkeypatch.setattr(FastAPI, "_asgi_ensure_server", _boom)


def _run(coro):
    return asyncio.run(coro)


async def _get(app, path, **kw):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as cli:
        return await cli.get(path, **kw)


# ── Dependencies (unique arcs: async override wrapper, nested-dep wiring) ──


def test_async_dep_resolves_in_process():
    app = FastAPI()

    async def get_async_token():
        await asyncio.sleep(0)
        return "async-secret"

    @app.get("/a")
    async def _a(token: str = Depends(get_async_token)):
        return {"token": token}

    async def go():
        r = await _get(app, "/a")
        assert r.status_code == 200
        assert r.json() == {"token": "async-secret"}

    _run(go())


def test_nested_dep_resolves_in_process():
    app = FastAPI()

    def get_db():
        return {"conn": 42}

    def get_user(db=Depends(get_db)):
        return {"id": 1, "db_conn": db["conn"]}

    @app.get("/u")
    def _u(user=Depends(get_user)):
        return user

    async def go():
        r = await _get(app, "/u")
        assert r.status_code == 200
        assert r.json() == {"id": 1, "db_conn": 42}

    _run(go())


# ── Mounts (unique arcs: _asgi_try_http_mount recursion + precedence) ──


def test_mounted_subapp_with_path_param_dispatches_in_process():
    sub = FastAPI()

    @sub.get("/users/{uid}")
    def _u(uid: int):
        return {"uid": uid}

    app = FastAPI()
    app.mount("/api", sub)

    async def go():
        r = await _get(app, "/api/users/42")
        assert r.status_code == 200
        assert r.json() == {"uid": 42}

    _run(go())


def test_top_level_route_still_wins_over_mount_prefix_match():
    """A top-level literal route must win over a mount-prefix match."""
    sub = FastAPI()

    @sub.get("/status")
    def _sub_status():
        return {"from": "sub"}

    app = FastAPI()

    @app.get("/status")
    def _top_status():
        return {"from": "top"}

    app.mount("/v1", sub)

    async def go():
        r = await _get(app, "/status")
        assert r.json() == {"from": "top"}
        r2 = await _get(app, "/v1/status")
        assert r2.json() == {"from": "sub"}

    _run(go())


# ── SecurityScopes (unique arcs: _emit_dep_special_params emitter) ──


def test_security_scopes_populated_from_single_security_marker():
    app = FastAPI()

    def get_user(ss: SecurityScopes):
        return {"scopes": list(ss.scopes)}

    @app.get("/me")
    def _me(user=Security(get_user, scopes=["me", "items:read"])):
        return user

    async def go():
        r = await _get(app, "/me")
        assert r.status_code == 200
        assert sorted(r.json()["scopes"]) == ["items:read", "me"]

    _run(go())


def test_security_scopes_accumulate_across_chain():
    """``Security`` markers at multiple levels contribute scopes."""
    app = FastAPI()

    def inner(ss: SecurityScopes):
        return {"scopes": list(ss.scopes)}

    def outer(v=Security(inner, scopes=["inner:read"])):
        return v

    @app.get("/deep")
    def _deep(v=Security(outer, scopes=["outer:admin"])):
        return v

    async def go():
        r = await _get(app, "/deep")
        assert r.status_code == 200
        collected = set(r.json()["scopes"])
        assert {"inner:read", "outer:admin"}.issubset(collected), collected

    _run(go())


def test_no_security_markers_gives_empty_scopes():
    app = FastAPI()

    def _user(ss: SecurityScopes):
        return {"scopes": list(ss.scopes)}

    @app.get("/plain")
    def _p(user=Security(_user)):
        return user

    async def go():
        r = await _get(app, "/plain")
        assert r.status_code == 200
        assert r.json()["scopes"] == []

    _run(go())


# ── Streaming (unique arcs: async-generator inline-coop drive path) ──


def test_streaming_response_async_generator():
    app = FastAPI()

    @app.get("/s2")
    def _s2():
        async def gen():
            for i in range(3):
                await asyncio.sleep(0)
                yield f"a{i}|".encode()
        return StreamingResponse(gen(), media_type="text/plain")

    async def go():
        r = await _get(app, "/s2")
        assert r.status_code == 200
        assert r.content == b"a0|a1|a2|"

    _run(go())


# ── Shape pins with no exact retained door twin ──


def test_mixed_form_and_file():
    """Form field + UploadFile in ONE endpoint (multipart mixed parts)."""
    app = FastAPI()

    @app.post("/submit")
    async def _submit(
        title: str = Form(...),
        f: UploadFile = File(...),
    ):
        content = await f.read()
        return {"title": title, "upload_len": len(content)}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.post(
                "/submit",
                data={"title": "cool"},
                files={"f": ("a.bin", io.BytesIO(b"xxx"), "application/octet-stream")},
            )
            assert r.status_code == 200
            assert r.json() == {"title": "cool", "upload_len": 3}

    _run(go())


def test_http_middleware_can_short_circuit_with_own_response():
    """``@app.middleware('http')`` returning its own response must skip
    the endpoint entirely (and still run it when it doesn't gate)."""
    app = FastAPI()

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if request.headers.get("x-forbid"):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        return await call_next(request)

    ran = []

    @app.get("/p")
    def _p():
        ran.append(True)
        return {}

    async def go():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as cli:
            r = await cli.get("/p", headers={"x-forbid": "1"})
            assert r.status_code == 403
            assert r.json() == {"detail": "forbidden"}
            assert ran == []
            r2 = await cli.get("/p")
            assert r2.status_code == 200
            assert ran == [True]

    _run(go())


def test_raw_asgi_middleware_can_short_circuit():
    """Raw ASGI MW that responds without calling ``self.app`` must
    prevent the endpoint from running."""
    endpoint_ran = []

    class BlockingMW:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 418,
                    "headers": [(b"content-type", b"text/plain")],
                })
                await send({"type": "http.response.body", "body": b"blocked"})
                return
            await self.app(scope, receive, send)

    app = FastAPI()
    app.add_middleware(BlockingMW)

    @app.get("/ping")
    def _p():
        endpoint_ran.append(True)
        return {"ok": True}

    async def go():
        r = await _get(app, "/ping")
        assert r.status_code == 418
        assert r.content == b"blocked"

    _run(go())
    assert endpoint_ran == []
