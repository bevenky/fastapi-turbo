"""R34 audit follow-ups — in-process ASGI dispatch installs the
dynamic OpenAPI / docs / redoc routes (so ``httpx.ASGITransport``
and ``TestClient(app, in_process=True)`` see them as 200, not 404),
and the benchmark runners use the shared PY_RS resolver / fail
loudly on unparsable rows."""
import asyncio
import json
import pathlib

import httpx

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 in-process ASGI dispatch installs OpenAPI / docs / redoc
# ────────────────────────────────────────────────────────────────────


def test_inprocess_asgi_serves_openapi_json():
    """``GET /openapi.json`` must return 200 with a valid OpenAPI 3.x
    schema when the app is driven through ``httpx.ASGITransport``
    — the same path TestClient(app, in_process=True) and Sentry's
    ASGI tests use. Earlier the in-process dispatcher skipped the
    dynamic-route installer (it's only called by ``run()`` for the
    Rust path), so /openapi.json was a 404. ~1273 upstream FastAPI
    tests in the offline gate failed because of this."""
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.get("/hello")
    async def _h():
        return {"ok": True}

    async def _drive() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            r = await c.get("/openapi.json")
            return r.status_code, r.json()

    status, body = asyncio.run(_drive())
    assert status == 200, status
    assert body.get("openapi", "").startswith("3."), body
    assert "/hello" in body.get("paths", {}), body


def test_inprocess_asgi_serves_docs_html():
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.get("/h")
    async def _h():
        return {"ok": True}

    async def _drive() -> tuple[int, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            r = await c.get("/docs")
            return r.status_code, r.text

    status, body = asyncio.run(_drive())
    assert status == 200, status
    # Swagger UI HTML always references swagger-ui assets.
    assert "swagger-ui" in body.lower() or "swagger" in body.lower(), body[:300]


def test_inprocess_asgi_serves_redoc_html():
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.get("/h")
    async def _h():
        return {"ok": True}

    async def _drive() -> tuple[int, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            r = await c.get("/redoc")
            return r.status_code, r.text

    status, body = asyncio.run(_drive())
    assert status == 200, status
    assert "redoc" in body.lower(), body[:300]


def test_testclient_in_process_serves_openapi_json():
    """Same contract via ``TestClient(app, in_process=True)`` —
    the path that wraps ``httpx.ASGITransport``. CI sandbox runs
    use this, so the /openapi.json regression had to be in-process
    too (the earlier R-batch CI smoke that hit /openapi.json was
    going through the Rust loopback path, which was always green)."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/h")
    async def _h():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200, (r.status_code, r.content)
        body = r.json()
        assert body.get("openapi", "").startswith("3."), body


def test_inprocess_asgi_dynamic_route_installer_is_idempotent():
    """The installer is guarded by
    ``_in_process_dynamic_routes_installed``. Repeated requests
    (or repeated TestClient instances on the same app) must not
    re-install or duplicate routes."""
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.get("/h")
    async def _h():
        return {"ok": True}

    async def _drive() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            for _ in range(5):
                r = await c.get("/openapi.json")
                assert r.status_code == 200
            return r.status_code

    asyncio.run(_drive())

    # The number of registered ``/openapi.json`` routes must be
    # exactly one — re-installation is suppressed by the guard
    # flag. Earlier without the guard, multiple ASGI requests
    # would register the route N times.
    matching = [
        r for r in app.routes
        if getattr(r, "path", None) == "/openapi.json"
    ]
    assert len(matching) == 1, [r.path for r in matching]


# ────────────────────────────────────────────────────────────────────
# #2 doc count drift — assertions for live numbers
#    (the actual drift check lives in test_r33_regressions.py;
#    here we just lock the layout: the doc must contain a
#    bucket-#1 claim parseable as ``<P> pass, <S> skipped``).
# ────────────────────────────────────────────────────────────────────


def test_compatibility_md_has_parseable_force_mode_count():
    """COMPATIBILITY.md must carry a parseable claim for the
    FORCE-on-dev-box pass/skip count so the R33 drift check
    (which runs pytest in a subprocess and compares) can extract
    the claimed numbers. Without this anchor, the drift check
    silently passes when the claim format is unrecognised."""
    import re

    compat = (
        pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    )
    text = compat.read_text()
    m = re.search(
        r"FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1[^\n]*?:\s*(\d+)\s*pass,\s*(\d+)\s*skipped",
        text,
    )
    assert m is not None, "FORCE-bucket count anchor missing or unparseable"


# ────────────────────────────────────────────────────────────────────
# #3 benchmark runners use the shared PY_RS resolver
# ────────────────────────────────────────────────────────────────────


def test_bench_runners_source_shared_py_rs_resolver():
    """All five benchmark runners must source the shared
    ``_resolve_py_rs.sh`` helper that verifies the active Python
    can ``import fastapi_turbo`` BEFORE spawning subprocess
    apps. Earlier each runner had its own bare
    ``PY_RS="python3"`` default — a wrong $PATH could silently
    measure an unrelated stack."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    bench_dir = repo / "comparison" / "bench-app"

    helper = bench_dir / "_resolve_py_rs.sh"
    assert helper.exists(), "shared PY_RS resolver missing"

    helper_text = helper.read_text()
    assert 'import fastapi_turbo' in helper_text, helper_text

    runners = [
        "run_benchmark_v3.sh",
        "run_db_matrix_v2.sh",
        "run_redis_matrix_v2.sh",
        "run_sqla_matrix.sh",
        "run_db_matrix.sh",
        "run_redis_matrix.sh",
    ]
    for name in runners:
        text = (bench_dir / name).read_text()
        assert "_resolve_py_rs.sh" in text, name
        # The bare default must be gone.
        assert 'PY_RS="python3"' not in text, name


def test_bench_v3_row_fails_loudly_on_unparsable_output():
    """The v3 runner's ``row()`` (and every matrix runner's
    ``bench_one``) must fail when rps / p50 / p99 can't be
    parsed, NOT silently emit ``?`` fields. R34 introduced this
    in v3 inline; R35 extracted it into a shared
    ``_bench_row.sh`` so DB / Redis / SQLA runners get the same
    contract. Test now looks at the shared helper."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    bench_dir = repo / "comparison" / "bench-app"
    helper = (bench_dir / "_bench_row.sh").read_text()
    assert "BENCH_ALLOW_UNPARSABLE" in helper, (
        "shared bench-row helper must support an opt-in soft-fail mode"
    )
    assert "return 1" in helper, (
        "shared bench-row helper must fail loudly by default"
    )
    # The v3 runner must source the helper (so its ``row()``
    # function inherits the contract).
    v3 = (bench_dir / "run_benchmark_v3.sh").read_text()
    assert "_bench_row.sh" in v3, "v3 runner must source the shared helper"
