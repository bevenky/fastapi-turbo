"""R35 audit follow-ups — startup handlers run exactly once across
ASGI lifespan + first http request, startup failures propagate
out of ``__call__`` (don't get swallowed), bench-row parser is
shared across all matrix runners, and OFFLINE-mode external gate
verifies test deps are present."""
import asyncio
import pathlib

import httpx

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 startup handlers idempotent across lifespan + first http
# ────────────────────────────────────────────────────────────────────


def test_startup_runs_once_across_lifespan_then_http_request():
    """Probe-confirmed bug: ``_asgi_lifespan`` ran startup
    handlers, then the first http request fired
    ``_install_in_process_dynamic_routes`` which ran them again
    via ``_run_startup_handlers``. ``@app.on_event('startup')``
    fires twice — double-opens pools, schedules duplicate
    background jobs, reruns migrations.

    R35 adds a ``_startup_handlers_ran`` guard so whichever
    caller wins runs the handlers exactly once."""
    from fastapi_turbo import FastAPI

    app = FastAPI()
    n = {"count": 0}

    @app.on_event("startup")
    async def _s():
        n["count"] += 1

    @app.get("/ok")
    async def _ok():
        return {"n": n["count"]}

    async def _drive() -> int:
        # Drive ASGI lifespan startup explicitly.
        received = []

        async def receive():
            if not received:
                received.append("startup")
                return {"type": "lifespan.startup"}
            return {"type": "lifespan.shutdown"}

        async def send(_msg):
            return None

        await app({"type": "lifespan"}, receive, send)
        # Now drive an http request — the dynamic-routes installer
        # would re-run startup without the R35 guard.
        async def http_recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        msgs = []

        async def http_send(msg):
            msgs.append(msg)

        scope = {
            "type": "http", "method": "GET", "path": "/ok",
            "raw_path": b"/ok", "query_string": b"",
            "headers": [], "asgi": {"version": "3.0"},
            "scheme": "http", "server": ("testserver", 80),
            "client": ("test", 1234),
        }
        await app(scope, http_recv, http_send)
        return n["count"]

    final = asyncio.run(_drive())
    assert final == 1, f"startup ran {final} times (expected exactly 1)"


def test_startup_runs_once_across_two_http_requests():
    """A simpler shape: no explicit lifespan, just two http
    requests. The first installs dynamic routes (and runs
    startup); the second must NOT re-run startup. Idempotency
    on the dynamic-routes installer alone."""
    from fastapi_turbo import FastAPI

    app = FastAPI()
    n = {"count": 0}

    @app.on_event("startup")
    async def _s():
        n["count"] += 1

    @app.get("/h")
    async def _h():
        return {"ok": True}

    async def _drive() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            await c.get("/h")
            await c.get("/h")
        return n["count"]

    assert asyncio.run(_drive()) == 1


# ────────────────────────────────────────────────────────────────────
# #2 startup failures propagate out of __call__
# ────────────────────────────────────────────────────────────────────


def test_startup_failure_propagates_to_asgi_caller():
    """``_install_in_process_dynamic_routes`` runs the user's
    ``@app.on_event('startup')`` BEFORE registering the docs
    routes. Earlier ``__call__`` wrapped the install in
    ``try/except: pass``, so a startup hook raising
    ``RuntimeError`` was silently dropped — then ``/ok`` returned
    200 against a partially-initialised app. R35 catches the
    exception, marks the install as "tried" (so subsequent
    requests don't loop), and re-raises."""
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.on_event("startup")
    async def _s():
        raise RuntimeError("startup_failed_intentionally")

    @app.get("/ok")
    async def _ok():
        return {"ok": True}

    async def _drive() -> Exception | None:
        async def http_recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        msgs = []

        async def http_send(msg):
            msgs.append(msg)

        scope = {
            "type": "http", "method": "GET", "path": "/ok",
            "raw_path": b"/ok", "query_string": b"", "headers": [],
            "asgi": {"version": "3.0"}, "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 1234),
        }
        try:
            await app(scope, http_recv, http_send)
        except RuntimeError as exc:
            return exc
        return None

    raised = asyncio.run(_drive())
    assert raised is not None, "startup failure was swallowed"
    assert "startup_failed_intentionally" in str(raised), raised


def test_startup_failure_doesnt_loop_on_subsequent_requests():
    """After a startup failure, a second request must not RE-RUN
    the failing startup hook. The install is marked as "tried"
    even on failure so the dispatcher doesn't loop."""
    from fastapi_turbo import FastAPI

    app = FastAPI()
    calls = {"count": 0}

    @app.on_event("startup")
    async def _s():
        calls["count"] += 1
        raise RuntimeError("boom")

    @app.get("/ok")
    async def _ok():
        return {"ok": True}

    async def _drive() -> int:
        async def http_recv():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def http_send(_msg): return None

        scope = {
            "type": "http", "method": "GET", "path": "/ok",
            "raw_path": b"/ok", "query_string": b"", "headers": [],
            "asgi": {"version": "3.0"}, "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 1234),
        }
        for _ in range(3):
            try:
                await app(scope, http_recv, http_send)
            except RuntimeError:
                pass
        return calls["count"]

    assert asyncio.run(_drive()) == 1, (
        "startup hook was re-tried on subsequent requests"
    )


# ────────────────────────────────────────────────────────────────────
# #3 shared bench-row parser
# ────────────────────────────────────────────────────────────────────


def test_all_bench_runners_use_shared_bench_row_helper():
    """Every benchmark runner must source ``_bench_row.sh`` so
    they inherit the same fail-on-unparsable contract. The R34
    fix was scoped to v3 only; R35 audit caught DB / Redis /
    SQLA still using their own grep+``${rps:-?}`` parsers."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    bench_dir = repo / "comparison" / "bench-app"

    helper = bench_dir / "_bench_row.sh"
    assert helper.exists(), "shared bench-row helper missing"
    helper_text = helper.read_text()
    assert "BENCH_ALLOW_UNPARSABLE" in helper_text, helper_text
    assert "return 1" in helper_text, helper_text

    runners = [
        "run_benchmark_v3.sh",
        "run_db_matrix.sh",
        "run_db_matrix_v2.sh",
        "run_redis_matrix.sh",
        "run_redis_matrix_v2.sh",
        "run_sqla_matrix.sh",
    ]
    for name in runners:
        text = (bench_dir / name).read_text()
        assert "_bench_row.sh" in text, name
        # The local grep+``${rps:-?}`` parser pattern must be gone
        # from each runner — they delegate to ``bench_row``.
        assert "${rps:-?}" not in text, (
            f"{name}: legacy ``${{rps:-?}}`` placeholder still present"
        )


# ────────────────────────────────────────────────────────────────────
# #4 OFFLINE-mode external gate verifies test deps
# ────────────────────────────────────────────────────────────────────


def test_offline_compat_gate_verifies_test_deps_present():
    """OFFLINE mode skips the ``pip install pytest-asyncio
    pyyaml dirty-equals sqlmodel inline-snapshot`` step. If the
    env doesn't have those, hundreds of upstream FastAPI tests
    fail at collection / fixture setup — looks like a compat
    regression but is actually a missing-dep error. R35 audit
    saw 888 reported failures that turned out to be this. The
    helper now verifies each dep imports BEFORE running pytest
    in OFFLINE mode."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()

    # Must check each dep import in OFFLINE mode. The script
    # does ``for dep in pytest_asyncio yaml dirty_equals sqlmodel
    # inline_snapshot; do "$PYTHON_BIN" -c "import $dep"; done``,
    # so we look for the dep token in the for-loop list AND the
    # shell-interpolated ``import $dep`` invocation.
    assert 'pytest_asyncio yaml dirty_equals sqlmodel inline_snapshot' in script, (
        "OFFLINE-mode dep check loop body not found"
    )
    assert '"$PYTHON_BIN" -c "import $dep"' in script, (
        "OFFLINE-mode dep import probe not found"
    )
