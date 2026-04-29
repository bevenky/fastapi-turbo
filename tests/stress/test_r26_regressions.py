"""R26 audit follow-ups — ``request_response`` shim has working
behaviour (not just shape), Sentry's ``FastApiIntegration``
transaction-name update fires when the integration is loaded, CI
gates run real-loopback parity + upstream FastAPI + Sentry."""
import asyncio

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 request_response shim has working behaviour
# ────────────────────────────────────────────────────────────────────


def test_request_response_shim_dispatches_async_handler():
    """``request_response(handler)`` must build a Request, call the
    handler, and dispatch the Response. The R25 shape fix made the
    return callable, but the body was ``pass`` — calling the ASGI
    app produced ZERO messages and the test client hung. R26 wires
    real semantics."""
    from fastapi.routing import request_response
    from fastapi_turbo.responses import PlainTextResponse

    async def handler(request):
        return PlainTextResponse("ok")

    asgi_app = request_response(handler)

    async def _drive():
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "asgi": {"version": "3.0", "spec_version": "2.3"},
        }
        messages: list[dict] = []

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(msg):
            messages.append(msg)

        await asgi_app(scope, _receive, _send)
        return messages

    msgs = asyncio.run(_drive())
    # At minimum: one ``http.response.start`` and one or more
    # ``http.response.body`` events.
    types = [m.get("type") for m in msgs]
    assert "http.response.start" in types, msgs
    assert "http.response.body" in types, msgs

    start = next(m for m in msgs if m["type"] == "http.response.start")
    assert start["status"] == 200, start

    body = b"".join(
        m.get("body", b"") for m in msgs if m["type"] == "http.response.body"
    )
    assert body == b"ok", body


def test_request_response_shim_dispatches_sync_handler():
    """Sync handlers must run via ``run_in_executor`` (mirroring
    upstream's ``functools.partial(run_in_threadpool, func)``) so the
    event loop isn't blocked. The end result is the same: Response
    is dispatched as ASGI events."""
    from fastapi.routing import request_response
    from fastapi_turbo.responses import JSONResponse

    def handler(request):
        return JSONResponse({"ok": True})

    asgi_app = request_response(handler)

    async def _drive():
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "asgi": {"version": "3.0", "spec_version": "2.3"},
        }
        messages: list[dict] = []

        async def _receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(msg):
            messages.append(msg)

        await asgi_app(scope, _receive, _send)
        return messages

    msgs = asyncio.run(_drive())
    start = next(m for m in msgs if m["type"] == "http.response.start")
    assert start["status"] == 200, start
    body = b"".join(
        m.get("body", b"") for m in msgs if m["type"] == "http.response.body"
    )
    assert body == b'{"ok":true}' or body == b'{"ok": true}', body


# ────────────────────────────────────────────────────────────────────
# #2 Sentry FastApiIntegration sets transaction name
# ────────────────────────────────────────────────────────────────────


def test_sentry_fastapi_integration_sets_transaction_name_to_route_path():
    """Sentry's ``FastApiIntegration`` patches
    ``fastapi.routing.get_request_handler`` to set the transaction
    name from ``scope['route'].path``. Our dispatcher bypasses that
    handler, so the patch never fires and the transaction stays as
    the concrete URL. R26 calls Sentry's helper inline from the
    dispatcher when the integration is loaded — match upstream
    Sentry-FastAPI legacy-setup behaviour.

    Runs in a subprocess to keep ``sentry_sdk.init()`` global state
    out of the parent test process — Sentry monkey-patches several
    ASGI / asyncio surfaces, and leaving those patches active poisons
    Range / file-response tests that follow."""
    pytest.importorskip("sentry_sdk")
    pytest.importorskip("sentry_sdk.integrations.fastapi")

    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.transport import Transport

        captured = []

        class _Capture(Transport):
            def __init__(self):
                super().__init__({})

            def capture_envelope(self, envelope):
                for item in envelope.items:
                    if item.headers.get("type") == "transaction":
                        captured.append(item.payload.json.get("transaction"))

            def flush(self, timeout, callback=None):
                return None

        sentry_sdk.init(
            transport=_Capture(),
            integrations=[FastApiIntegration(transaction_style="url")],
            traces_sample_rate=1.0,
        )

        import fastapi_turbo
        from fastapi_turbo import FastAPI
        from fastapi_turbo.testclient import TestClient

        app = FastAPI()

        @app.get("/message/{message_id}")
        async def msg(message_id: int):
            return {"id": message_id}

        with TestClient(app, in_process=True) as c:
            r = c.get("/message/123456")
            assert r.status_code == 200, (r.status_code, r.content)

        sentry_sdk.flush(timeout=1.0)
        print("TX_NAMES:" + repr(captured))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    # Last "TX_NAMES:" line carries the captured transaction names.
    tx_line = next(
        (ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("TX_NAMES:")),
        None,
    )
    assert tx_line is not None, result.stdout
    tx_names = eval(tx_line[len("TX_NAMES:"):])
    assert "/message/{message_id}" in tx_names, tx_names


# ────────────────────────────────────────────────────────────────────
# #3 CI workflow runs the release-required gates
# ────────────────────────────────────────────────────────────────────


def test_ci_workflow_runs_parity_tests():
    """The release-readiness gate in COMPATIBILITY.md says
    real-loopback parity is required before shipping. This test
    locks the CI workflow against a regression where parity was
    skipped (which is what let the R26 Sentry findings ship in
    earlier audits). R51 consolidated the upstream-FastAPI +
    Sentry gates into ``scripts/run_external_compat_gates.sh``;
    this test now enforces (a) ci.yml runs parity + invokes the
    canonical script, (b) the script keeps the upstream + Sentry
    contract."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[2]
    ci_text = (repo / ".github" / "workflows" / "ci.yml").read_text()
    script_text = (
        repo / "scripts" / "run_external_compat_gates.sh"
    ).read_text()
    assert "tests/parity" in ci_text, "CI must run tests/parity"
    # CI delegates upstream + Sentry gates to the canonical script.
    assert "scripts/run_external_compat_gates.sh" in ci_text, ci_text
    # Canonical script must run BOTH external trees.
    assert "fastapi_upstream" in script_text or "fastapi/fastapi" in script_text, (
        "canonical script must run upstream FastAPI suite under the shim"
    )
    assert "sentry-python" in script_text or "sentry-fastapi" in script_text.lower(), (
        "canonical script must run Sentry FastAPI integration tests"
    )


# ────────────────────────────────────────────────────────────────────
# #4 README DB section caveats
# ────────────────────────────────────────────────────────────────────


def test_readme_db_section_doesnt_overstate_go_parity():
    """The README's CRUD/DB section previously said
    ``138us for 10 queries, beats Go goroutines at 148us`` and a
    'Winner' column favouring fastapi-turbo. Both are loopback-c=1
    micro-results that don't generalise. The release messaging in
    those sections must stay caveated (matches the top-of-README
    headline)."""
    import pathlib

    readme = pathlib.Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text()
    # The unqualified "beats Go" + "Winner" column phrasing must not
    # appear; the per-query inline comment must be caveated.
    assert "beats Go goroutines" not in text, (
        "DB-section inline comment must be caveated, not 'beats Go'"
    )
    # The DB performance table mustn't have a "Winner" column —
    # that framing was the headline-creep the audit flagged.
    assert "| Winner |" not in text, (
        "DB performance table 'Winner' column favours single-shot c=1 "
        "wins that don't survive concurrent / Linux runs"
    )
