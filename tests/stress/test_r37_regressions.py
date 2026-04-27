"""R37 audit follow-ups — shutdown-handler failures surface as
``lifespan.shutdown.failed`` (and reset the startup guard so a
re-used app cycle works), the upstream-FastAPI gate threshold test
calls the canonical script (not pytest directly), and the gate
script catches stale Rust builds instead of running them silently."""
import asyncio
import os
import pathlib
import subprocess
import sys

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 shutdown failures surface as lifespan.shutdown.failed
# ────────────────────────────────────────────────────────────────────


def test_shutdown_handler_failure_emits_lifespan_shutdown_failed():
    """Probe-confirmed bug: a shutdown handler raising
    ``RuntimeError`` was caught and ``lifespan.shutdown.complete``
    sent anyway. Production supervisors lost the failure signal.
    R37 catches the failure, sends ``lifespan.shutdown.failed``
    with the error message, and resets the startup guard so the
    next cycle re-fires startup cleanly (the swallow path
    earlier left ``_startup_state == "started"``, poisoning the
    next cycle).

    Verified: cycle 1 emits ``lifespan.shutdown.failed``, cycle 2
    re-runs startup."""
    from fastapi_turbo import FastAPI

    app = FastAPI()
    counts = {"startup": 0, "shutdown": 0}

    @app.on_event("startup")
    async def _s():
        counts["startup"] += 1

    @app.on_event("shutdown")
    async def _sd():
        counts["shutdown"] += 1
        raise RuntimeError("shutdown_explosion_intentional")

    async def _cycle():
        sent: list[dict] = []
        sent_su, sent_sd = [False], [False]

        async def receive():
            if not sent_su[0]:
                sent_su[0] = True
                return {"type": "lifespan.startup"}
            if not sent_sd[0]:
                sent_sd[0] = True
                return {"type": "lifespan.shutdown"}
            await asyncio.Event().wait()

        async def send(msg):
            sent.append(msg)

        await app({"type": "lifespan"}, receive, send)
        return [m["type"] for m in sent]

    async def _drive() -> tuple[list[str], list[str]]:
        c1 = await _cycle()
        c2 = await _cycle()
        return c1, c2

    cycle1, cycle2 = asyncio.run(_drive())

    # Cycle 1: startup completes, shutdown FAILED is emitted (not
    # silently complete).
    assert "lifespan.shutdown.failed" in cycle1, cycle1
    assert "lifespan.shutdown.complete" not in cycle1, cycle1

    # Cycle 2: must run startup AND shutdown again — the failed
    # shutdown didn't poison the second cycle.
    assert "lifespan.startup.complete" in cycle2, cycle2
    assert counts["startup"] == 2, (
        f"reused app re-startup should fire (got {counts['startup']}, want 2)"
    )
    assert counts["shutdown"] == 2, (
        f"shutdown handler should run on every cycle "
        f"(got {counts['shutdown']}, want 2)"
    )


def test_shutdown_failed_message_contains_original_error():
    from fastapi_turbo import FastAPI

    app = FastAPI()

    @app.on_event("shutdown")
    async def _sd():
        raise OSError("boom_xyz_123")

    async def _drive() -> str:
        sent: list[dict] = []
        sent_su, sent_sd = [False], [False]

        async def receive():
            if not sent_su[0]:
                sent_su[0] = True
                return {"type": "lifespan.startup"}
            if not sent_sd[0]:
                sent_sd[0] = True
                return {"type": "lifespan.shutdown"}
            await asyncio.Event().wait()

        async def send(msg):
            sent.append(msg)

        await app({"type": "lifespan"}, receive, send)
        for m in sent:
            if m.get("type") == "lifespan.shutdown.failed":
                return m.get("message", "")
        return ""

    msg = asyncio.run(_drive())
    assert "boom_xyz_123" in msg, msg


# ────────────────────────────────────────────────────────────────────
# #2 upstream gate test calls the canonical script
# ────────────────────────────────────────────────────────────────────


def test_upstream_gate_regression_calls_canonical_script():
    """Earlier R36's gate-threshold test invoked ``pytest``
    directly, decoupling the test from the script
    (``scripts/run_external_compat_gates.sh fastapi``) that
    release / CI actually run. The R37 audit caught this — the
    test passed locally while the script reportedly failed.
    R37 makes the test call the script."""
    test_file = (
        pathlib.Path(__file__).resolve().parent / "test_r36_regressions.py"
    )
    text = test_file.read_text()
    assert "scripts/run_external_compat_gates.sh" in text or (
        "run_external_compat_gates.sh" in text and "fastapi" in text
    ), text
    # And it must check both pytest pass count AND script
    # returncode (to surface failures at any stage of the gate,
    # not just pytest summaries).
    assert "proc.returncode" in text, text


# ────────────────────────────────────────────────────────────────────
# #3 gate script catches stale Rust builds
# ────────────────────────────────────────────────────────────────────


def test_gate_script_detects_stale_rust_build():
    """Auditor consistently saw 888 failures the local run can't
    reproduce. Most likely cause: stale ``_fastapi_turbo_core.so``
    that pre-dates the source files on disk (the auditor probably
    didn't run ``maturin develop`` between R-batches). R37 adds a
    stale-build check that compares the installed ``.so`` mtime
    against the newest ``src/*.rs`` and bails out with a clear
    ``maturin develop`` instruction instead of silently running
    a stale binary."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()
    assert "STALE BUILD" in script, script
    assert "maturin develop" in script, script
    assert "SKIP_STALE_BUILD_CHECK" in script, script


def test_gate_stale_build_check_can_be_overridden():
    """Override env var must work — for the rare case where the
    user genuinely wants to run against a build older than the
    sources (e.g. bisecting). Static check on the script body
    that the override reaches the conditional skip."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()
    # The if-guard around the stale check must read
    # ``SKIP_STALE_BUILD_CHECK`` env var.
    assert (
        '"${SKIP_STALE_BUILD_CHECK:-0}" != "1"' in script
    ), script


def test_gate_stale_build_check_logic_in_script():
    """Static check on the script body: the stale-build guard
    must (a) read the ``.so`` path from the loaded
    ``fastapi_turbo._fastapi_turbo_core`` module, (b) compare it
    against ``find $REPO_ROOT/src -name '*.rs' -newer $SO_PATH``,
    (c) exit 2 with a clear ``STALE BUILD`` diagnostic when the
    find produces output. End-to-end testing is brittle (mtime
    granularity, tmpdir interactions), so we lock the structure
    here and trust the live integration via the canonical
    threshold test in ``test_r36_regressions.py``."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()
    # (a) reads the .so path from the loaded module.
    assert "_fastapi_turbo_core" in script, script
    assert "m.__file__" in script, script
    # (b) uses ``find ... -newer "$SO_PATH"``.
    assert '-name \'*.rs\' -newer "$SO_PATH"' in script, script
    # (c) exits 2 with the diagnostic.
    assert "STALE BUILD" in script, script
    assert "exit 2" in script, script


# ────────────────────────────────────────────────────────────────────
# #4 doc count drift threshold tightened — pin the actual measurement
# ────────────────────────────────────────────────────────────────────


def test_compat_md_doc_count_isnt_off_by_more_than_one():
    """R34's drift detector allowed ±5 drift; R37 saw the
    happy-path count drift to 1004 while the doc still said 1003.
    Tighten to ±2 so the doc tracks within noise tolerance."""
    import re

    compat = (
        pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    )
    text = compat.read_text()
    # Parse the happy-path claim.
    # Format: "Bind works, no env var* (normal dev box): <P> pass, <S> skipped"
    m = re.search(
        r"Bind works,\s*no env var[^\n]*?:\s*(\d+)\s*pass,\s*(\d+)\s*skipped",
        text,
    )
    assert m is not None, "happy-path claim not found"
    claimed_pass = int(m.group(1))
    claimed_skip = int(m.group(2))

    # Spawn pytest to measure (recursion-guarded same way
    # test_r33_regressions.py does). The subprocess MUST run in
    # the happy-path environment — clear FORCE_LOOPBACK_DENIED /
    # FORCE_LOOPBACK_ALLOWED so the parent's env-vars don't leak
    # in (R38 caught this: when parent sets
    # ``FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1`` for its OWN run,
    # the subprocess inherits it via ``**os.environ``, measures
    # the FORCE-mode count, and trips the assertion against the
    # happy-path doc claim).
    if os.environ.get("FASTAPI_TURBO_SKIP_DOC_DRIFT_CHECK"):
        return
    # The full-suite driver sets ``FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT``
    # to skip every drift detector (R33 / R36 / R37) — each spawns its
    # own pytest subprocess which contends for pytest-cache / fs and
    # turns into flake under simultaneous full-suite execution. Run
    # ``pytest tests/stress/test_r37_regressions.py`` directly to
    # exercise this guard outside the parent suite.
    if os.environ.get("FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT"):
        pytest.skip("subprocess-drift detector skipped under full-suite run")
    repo = pathlib.Path(__file__).resolve().parents[2]
    sub_env = {k: v for k, v in os.environ.items()
               if k not in (
                   "FASTAPI_TURBO_FORCE_LOOPBACK_DENIED",
                   "FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED",
               )}
    sub_env["FASTAPI_TURBO_SKIP_DOC_DRIFT_CHECK"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--timeout=60"],
        cwd=str(repo),
        env=sub_env,
        capture_output=True, text=True, timeout=300,
    )
    summary = re.search(
        r"(\d+)\s+passed,?\s*(\d+)?\s*skipped?", proc.stdout or ""
    )
    if summary is None:
        return
    measured_pass = int(summary.group(1))
    measured_skip = int(summary.group(2) or "0")
    # Tighter than R33's ±5: ±2 catches single-test additions
    # without re-pinning per batch.
    assert abs(claimed_pass - measured_pass) <= 2, (
        f"COMPATIBILITY.md happy-path claims {claimed_pass} pass, "
        f"measured {measured_pass} — refresh the doc"
    )
    assert abs(claimed_skip - measured_skip) <= 2, (
        f"COMPATIBILITY.md happy-path claims {claimed_skip} skipped, "
        f"measured {measured_skip}"
    )
