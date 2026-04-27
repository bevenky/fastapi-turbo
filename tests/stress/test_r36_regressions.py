"""R36 audit follow-ups — failed startup poisons the app for its
lifetime, shutdown resets the startup guard so reused app instances
re-run startup, the bench-row parser handles ``set -euo pipefail``
correctly, and the upstream-FastAPI claim has a hardened gate
threshold."""
import asyncio
import pathlib
import subprocess

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 failed startup poisons the app — every subsequent request raises
# ────────────────────────────────────────────────────────────────────


def test_failed_startup_keeps_failing_on_every_subsequent_request():
    """Probe-confirmed bug: first ``/ok`` raised
    ``RuntimeError("startup exploded")``, but ``/ok`` #2 and #3
    returned ``{"ok": true, "calls": 1}`` — the failed app
    silently served traffic against an uninitialised state.
    R36 keeps the state machine in ``"failed"`` so every
    subsequent request RE-RAISES with the original error
    captured in the message."""
    from fastapi_turbo import FastAPI

    app = FastAPI()
    calls = {"count": 0}

    @app.on_event("startup")
    async def _s():
        calls["count"] += 1
        raise RuntimeError("startup_exploded_intentionally")

    @app.get("/ok")
    async def _ok():
        return {"ok": True, "calls": calls["count"]}

    async def _drive() -> tuple[int, int]:
        async def http_recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def http_send(_msg):
            return None

        scope = {
            "type": "http", "method": "GET", "path": "/ok",
            "raw_path": b"/ok", "query_string": b"", "headers": [],
            "asgi": {"version": "3.0"}, "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 1234),
        }
        raised = 0
        for _ in range(3):
            try:
                await app(scope, http_recv, http_send)
            except RuntimeError:
                raised += 1
        return raised, calls["count"]

    raised_count, hook_calls = asyncio.run(_drive())
    assert raised_count == 3, (
        f"poisoned app served traffic on {3 - raised_count}/3 requests "
        "after a failed startup"
    )
    # Startup hook fires exactly once even though we made 3 attempts —
    # the second + third attempts hit the cached failure.
    assert hook_calls == 1, f"startup hook fired {hook_calls}× (expected 1)"


def test_failed_startup_re_raises_with_original_error_in_message():
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.on_event("startup")
    async def _s():
        raise ValueError("kaboom_xyz")

    @app.get("/x")
    async def _x():
        return {"ok": True}

    async def _drive() -> str:
        async def http_recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def http_send(_msg):
            return None

        scope = {
            "type": "http", "method": "GET", "path": "/x",
            "raw_path": b"/x", "query_string": b"", "headers": [],
            "asgi": {"version": "3.0"}, "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 1234),
        }
        # First request raises the original ValueError; second
        # raises a RuntimeError describing the failed state.
        try:
            await app(scope, http_recv, http_send)
        except Exception:
            pass
        try:
            await app(scope, http_recv, http_send)
        except RuntimeError as exc:
            return str(exc)
        return ""

    msg = asyncio.run(_drive())
    assert "failed state" in msg or "cannot serve traffic" in msg, msg
    assert "kaboom_xyz" in msg, msg


# ────────────────────────────────────────────────────────────────────
# #2 shutdown resets startup guard — reused apps re-run startup
# ────────────────────────────────────────────────────────────────────


def test_shutdown_resets_startup_so_reused_app_re_runs_startup():
    """Probe-confirmed bug: two lifespan cycles produced startup=1
    / shutdown=2 (because the startup guard never reset). Apps
    re-used across TestClient context-manager invocations or
    across two ASGI lifespan cycles would run against
    closed pools / freed resources because startup didn't fire
    on the second cycle. R36 resets ``_startup_state`` to
    ``"not_started"`` in ``_run_shutdown_handlers`` so the next
    lifespan / first http re-fires startup."""
    from fastapi_turbo import FastAPI

    app = FastAPI()
    counts = {"startup": 0, "shutdown": 0}

    @app.on_event("startup")
    async def _s():
        counts["startup"] += 1

    @app.on_event("shutdown")
    async def _sd():
        counts["shutdown"] += 1

    async def _cycle():
        sent_startup = [False]
        sent_shutdown = [False]

        async def receive():
            if not sent_startup[0]:
                sent_startup[0] = True
                return {"type": "lifespan.startup"}
            if not sent_shutdown[0]:
                sent_shutdown[0] = True
                return {"type": "lifespan.shutdown"}
            await asyncio.Event().wait()

        async def send(_msg):
            return None

        await app({"type": "lifespan"}, receive, send)

    async def _drive() -> tuple[int, int]:
        await _cycle()
        await _cycle()
        return counts["startup"], counts["shutdown"]

    startup, shutdown = asyncio.run(_drive())
    assert startup == 2, f"startup fired {startup}× across 2 cycles (expected 2)"
    assert shutdown == 2, f"shutdown fired {shutdown}× across 2 cycles (expected 2)"


def test_two_testclient_in_process_contexts_both_run_startup():
    """End-to-end: two ``TestClient(app, in_process=True)``
    context managers on the SAME app must each run startup +
    shutdown. Earlier the second context found the startup
    guard already pinned and silently skipped — apps re-used
    in test fixtures would hit closed pools."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()
    counts = {"startup": 0}

    @app.on_event("startup")
    async def _s():
        counts["startup"] += 1

    @app.get("/x")
    async def _x():
        return {"ok": True}

    with TestClient(app, in_process=True) as c:
        c.get("/x")
    with TestClient(app, in_process=True) as c:
        c.get("/x")

    # Each context manager's exit should run shutdown which
    # resets state; the next entry's first request fires startup
    # again. Total: 2 startup invocations.
    assert counts["startup"] == 2, (
        f"reused app's TestClient context-manager pair fired startup "
        f"{counts['startup']}× (expected 2)"
    )


# ────────────────────────────────────────────────────────────────────
# #3 _bench_row.sh handles set -euo pipefail correctly
# ────────────────────────────────────────────────────────────────────


def test_bench_row_handles_set_euo_pipefail_with_unparsable_input():
    """Earlier bug: under ``set -euo pipefail``, ``grep``'s
    no-match exit (1) propagated out of the command substitution
    and aborted the parent script BEFORE reaching the
    ``BENCH_ALLOW_UNPARSABLE`` / diagnostic branch. R36 fix
    appends ``|| true`` to each grep substitution so the parser
    actually reaches the soft-fail / fail-loud decision."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    bench_row = repo / "comparison" / "bench-app" / "_bench_row.sh"

    # Soft-fail path: opt-in via env var, must succeed and emit a
    # placeholder row.
    soft = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{bench_row}"; '
         'BENCH_ALLOW_UNPARSABLE=1 bench_row "fw" "/x" "garbage"'],
        capture_output=True, text=True, timeout=10,
    )
    assert soft.returncode == 0, (soft.returncode, soft.stdout, soft.stderr)
    assert "fw\t/x\t?\t?\t?" in soft.stdout, soft.stdout

    # Fail-loud path: default; must return 1 with a diagnostic on
    # stderr (NOT silently abort the parent).
    loud = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{bench_row}"; '
         'bench_row "fw" "/x" "garbage" || echo "row-failed:$?"'],
        capture_output=True, text=True, timeout=10,
    )
    assert "row-failed:1" in loud.stdout, (loud.stdout, loud.stderr)
    assert "produced unparsable output" in loud.stderr, loud.stderr


def test_bench_row_parses_well_formed_input_correctly():
    repo = pathlib.Path(__file__).resolve().parents[2]
    bench_row = repo / "comparison" / "bench-app" / "_bench_row.sh"
    sample = "frame: 12345 req/s p50=42 p99=99 min=10"
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{bench_row}"; '
         f'bench_row "fw" "/x" "{sample}"'],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == "fw\t/x\t12345\t42\t99", proc.stdout


# ────────────────────────────────────────────────────────────────────
# #4 upstream FastAPI gate — pinned-pass-count threshold
# ────────────────────────────────────────────────────────────────────


@pytest.mark.requires_loopback
def test_upstream_fastapi_gate_passes_canonical_threshold():
    """Run the upstream FastAPI 0.136.0 suite under the shim and
    assert the pass count is ≥ 3000 (well above the auditor's
    reported 2237 in the failing case but well below the
    healthy 3125 — leaves room for upstream test churn within
    a single tag without re-pinning). Catches:

      * a regression in our shim that breaks ~30%+ of upstream
        tests (the R36 reported failure mode);
      * a stale ``/tmp/fastapi_upstream`` checkout pointing at
        the wrong tag.

    Skipped in sandbox (subprocess pytest needs loopback) and
    when ``/tmp/fastapi_upstream`` isn't available."""
    upstream = pathlib.Path("/tmp/fastapi_upstream")
    if not (upstream / "tests").is_dir():
        pytest.skip("/tmp/fastapi_upstream not available; run "
                    "scripts/run_external_compat_gates.sh fastapi first")
    # Make sure the shim conftest is in place (the helper script
    # would write it, but a hand-test may have wiped it).
    conftest = upstream / "conftest.py"
    if not conftest.exists():
        conftest.write_text("import fastapi_turbo  # noqa: F401\n")
    import sys
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
        cwd=str(upstream), capture_output=True, text=True, timeout=600,
    )
    import re
    m = re.search(r"(\d+)\s+passed", proc.stdout or "")
    assert m is not None, (proc.stdout, proc.stderr)
    passed = int(m.group(1))
    assert passed >= 3000, (
        f"upstream FastAPI gate passed {passed} tests; "
        "below the 3000 threshold means a real shim regression — "
        "investigate before shipping. (Healthy state is ~3125.)"
    )
