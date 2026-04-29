"""R29 audit follow-ups — in-process ASGI re-raises generic
``Exception`` after running a catch-all handler (matches Starlette's
``ServerErrorMiddleware``), release.yml runs the same release-required
gates as ci.yml, CI's upstream-FastAPI step runs from the upstream
root (no more --ignore=test_tutorial), Rust WebSocket close handshake
is graceful, and stale doc claims are updated."""
import asyncio
import pathlib

import httpx
import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 in-process ASGI re-raises generic Exception under
#    raise_app_exceptions=True / raise_server_exceptions=True
# ────────────────────────────────────────────────────────────────────


def test_inprocess_asgi_reraises_generic_exception_under_raise_app_exceptions():
    """Upstream FastAPI's ServerErrorMiddleware ALWAYS re-raises a
    generic ``Exception`` after the response is sent, so
    ``httpx.ASGITransport(raise_app_exceptions=True)`` propagates
    the original exception out of the test. Earlier turbo silently
    returned the catch-all handler's 500 response — sandbox /
    serverless / in-process tests masked real failures as
    successful 500s."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse

    app = FastAPI()

    @app.exception_handler(Exception)
    async def _eh(request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.get("/boom")
    async def _boom():
        raise ValueError("boom!")

    async def _drive() -> Exception:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            try:
                await c.get("/boom")
            except Exception as exc:
                return exc
        raise AssertionError("expected raise")

    raised = asyncio.run(_drive())
    assert isinstance(raised, ValueError), type(raised)
    assert "boom" in str(raised), raised


def test_inprocess_asgi_swallows_generic_exception_under_raise_app_false():
    """When ``raise_app_exceptions=False`` (the deliberate
    test-mode-as-real-server case), the catch-all handler's response
    is observed normally and the exception isn't re-raised out of
    httpx. Same behaviour as upstream FastAPI."""
    from fastapi_turbo import FastAPI, Request
    from fastapi_turbo.responses import JSONResponse

    app = FastAPI()

    @app.exception_handler(Exception)
    async def _eh(request: Request, exc: Exception):
        return JSONResponse(
            {"detail": str(exc), "handled": True}, status_code=500
        )

    @app.get("/boom")
    async def _boom():
        raise ValueError("oops")

    async def _drive() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            r = await c.get("/boom")
            return r.status_code, r.json()

    status, body = asyncio.run(_drive())
    assert status == 500, status
    assert body == {"detail": "oops", "handled": True}, body


def test_inprocess_asgi_doesnt_reraise_intentional_response_exceptions():
    """``HTTPException``, ``RequestValidationError``,
    ``WebSocketException``, and ``ResponseValidationError`` ARE the
    intended-response types FastAPI uses to encode 4xx/5xx outcomes.
    Their handlers run, the response goes out, and the exception
    is NOT re-raised — even under ``raise_app_exceptions=True``."""
    from fastapi_turbo import FastAPI, HTTPException

    app = FastAPI()

    @app.get("/x")
    async def _x():
        raise HTTPException(status_code=418, detail="teapot")

    async def _drive() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            r = await c.get("/x")
            return r.status_code, r.json()

    status, body = asyncio.run(_drive())
    assert status == 418, status
    assert body == {"detail": "teapot"}, body


# ────────────────────────────────────────────────────────────────────
# #2 release.yml runs the release-required gates
# ────────────────────────────────────────────────────────────────────


def test_release_workflow_runs_websocket_parity_upstream_sentry_gates():
    """The release workflow's gate job must run the same
    release-required steps as ci.yml — earlier release.yml only
    ran the fast subset + stress + import smoke, so a tag could
    publish wheels without parity / WS / upstream-FastAPI / Sentry
    coverage. R51 consolidated the gate runners into the canonical
    ``scripts/run_external_compat_gates.sh``; this test now
    enforces (a) release.yml still runs WS + parity + the canonical
    script, (b) the script keeps the pin contract."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    rel_text = (repo / ".github" / "workflows" / "release.yml").read_text()
    script_text = (
        repo / "scripts" / "run_external_compat_gates.sh"
    ).read_text()
    # WebSocket suite gate.
    assert "pytest tests/test_websocket.py" in rel_text, rel_text
    # Real-loopback parity gate.
    assert "pytest tests/parity" in rel_text, rel_text
    # Release workflow invokes the canonical script for both gates.
    assert "scripts/run_external_compat_gates.sh fastapi" in rel_text, rel_text
    assert "scripts/run_external_compat_gates.sh sentry" in rel_text, rel_text
    # Pin contract lives in the script (single source of truth).
    assert 'UPSTREAM_TAG="0.136.0"' in script_text, script_text
    assert "git -C /tmp/fastapi_upstream" in script_text and "reset --hard" in script_text, script_text
    assert 'SENTRY_TAG="2.42.0"' in script_text, script_text
    assert "/tmp/sentry-python/tests/integrations/fastapi" in script_text, script_text
    assert "/tmp/sentry-python/tests/integrations/asgi" in script_text, script_text


# ────────────────────────────────────────────────────────────────────
# #3 CI runs upstream FastAPI from the upstream root (no
#    --ignore=test_tutorial)
# ────────────────────────────────────────────────────────────────────


def test_ci_workflow_runs_upstream_fastapi_from_root_not_ignoring_tutorial():
    """The upstream-FastAPI step must ``cd`` into the upstream
    root and run ``pytest tests/`` so cwd-relative tutorial asset
    lookups work. Earlier the step ran ``pytest /tmp/fastapi_upstream/
    tests/`` with ``--ignore=test_tutorial``, which silently dropped
    a third of the suite — the canonical 3,125 / 3,129
    compatibility claim was never enforced. R51 moved the cwd
    + invocation into the canonical script — this test enforces
    the contract there now."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script_text = (
        repo / "scripts" / "run_external_compat_gates.sh"
    ).read_text()
    # The script must cd into the upstream root before running pytest.
    assert "cd /tmp/fastapi_upstream" in script_text, script_text
    # And must NOT --ignore the tutorial subtree.
    assert "--ignore=tests/test_tutorial" not in script_text, script_text
    assert "--ignore=/tmp/fastapi_upstream/tests/test_tutorial" not in script_text, script_text


# ────────────────────────────────────────────────────────────────────
# #4 Rust WebSocket close handshake doesn't leak ConnectionResetError
# ────────────────────────────────────────────────────────────────────


@pytest.mark.requires_loopback
def test_rust_ws_server_initiated_close_doesnt_emit_connection_reset():
    """When the WebSocket handler calls ``await ws.close()``, the
    Rust writer task now closes the underlying sink AFTER the
    Close frame so the client sees a clean WebSocket close
    handshake. Earlier the writer kept looping and the client's
    next read raced the TCP shutdown — manifested as
    ``ConnectionResetError: [Errno 54] Connection reset by peer``
    flooding test debug logs even on orderly shutdowns.

    This test drives a short server-initiated close over the real
    loopback Rust path and asserts the client observes the close
    frame WITHOUT a ConnectionResetError."""
    import threading

    from fastapi_turbo import FastAPI, WebSocket
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.websocket("/ws")
    async def _ws(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hello")
        await ws.close(code=1000)

    errors: list[Exception] = []

    def _run():
        try:
            with TestClient(app) as c:
                with c.websocket_connect("/ws") as conn:
                    msg = conn.receive_text()
                    assert msg == "hello", msg
                    # Wait for the server-initiated close; if the
                    # close handshake is graceful, this returns
                    # cleanly. If the server drops the TCP, this
                    # raises ConnectionResetError or
                    # WebSocketDisconnect.
                    try:
                        conn.receive_text()
                    except Exception as exc:
                        # WebSocketDisconnect IS the expected clean
                        # close signal; ConnectionReset is the bug.
                        if "Reset" in type(exc).__name__ or "Reset" in str(exc):
                            errors.append(exc)
        except Exception as exc:
            if "Reset" in type(exc).__name__ or "Reset" in str(exc):
                errors.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "ws test hung"
    assert not errors, f"server-initiated close leaked ConnectionReset: {errors}"


# ────────────────────────────────────────────────────────────────────
# #5 Stale-doc removal
# ────────────────────────────────────────────────────────────────────


def test_claudemd_test_count_is_not_stale_920():
    claude = pathlib.Path(__file__).resolve().parents[2] / "CLAUDE.md"
    text = claude.read_text()
    # The literal "920 tests" line was the stale watermark.
    assert "920 tests" not in text, text


def test_todosmd_no_longer_says_no_ci_or_510_tests():
    todos = pathlib.Path(__file__).resolve().parents[2] / "todos.md"
    text = todos.read_text()
    assert "No GitHub Actions / CI yet" not in text, text
    assert "510-test suite" not in text, text


def test_benchmarks_md_no_longer_recommends_pyo3_0_25():
    bench = pathlib.Path(__file__).resolve().parents[2] / "benchmarks.md"
    text = bench.read_text()
    # The earlier "Need PyO3 0.28+" recommendation pre-dated the
    # migration. The file should now state the migration is done.
    assert "Need PyO3 0.28+ for optimized free-threading support" not in text, text
