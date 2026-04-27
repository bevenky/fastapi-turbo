"""R38 audit follow-ups — lifespan ctx-manager ``__aexit__``
failures surface as ``lifespan.shutdown.failed`` (parity with the
event-handler path R37 fixed), the doc-drift detectors are
parent-FORCE-env-var-safe, and the R36 gate test checks
``returncode`` before parsing stdout for pass count."""
import asyncio
import contextlib
import pathlib

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 lifespan ctx-manager __aexit__ failures surface as failed
# ────────────────────────────────────────────────────────────────────


def test_lifespan_aexit_failure_emits_lifespan_shutdown_failed():
    """Probe-confirmed bug: a lifespan
    ``@asynccontextmanager`` whose ``__aexit__`` raised was
    silently caught and ``lifespan.shutdown.complete`` sent
    anyway. R37 fixed event-handler shutdown failures; R38
    extends the same contract to ctx-manager ``__aexit__``.
    Probe: turbo emits ``lifespan.shutdown.failed`` with the
    original message, matching upstream FastAPI."""
    from fastapi_turbo import FastAPI

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        raise RuntimeError("ctx_aexit_explosion_intentional")

    app = FastAPI(lifespan=lifespan)

    async def _drive() -> tuple[list[str], str]:
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
        types = [m["type"] for m in sent]
        failed = next(
            (m for m in sent if m.get("type") == "lifespan.shutdown.failed"),
            None,
        )
        return types, (failed or {}).get("message", "")

    types, fail_msg = asyncio.run(_drive())
    assert "lifespan.shutdown.failed" in types, types
    assert "lifespan.shutdown.complete" not in types, types
    assert "ctx_aexit_explosion_intentional" in fail_msg, fail_msg


def test_lifespan_multiple_ctxs_all_aexit_attempted_on_failure():
    """When two lifespan ctx-managers are stacked and the FIRST
    one's ``__aexit__`` raises, the second should still attempt
    cleanup. R38 keeps best-effort unwinding (at-most-once
    cleanup per resource) and re-raises only the first exception
    encountered."""
    from fastapi_turbo import FastAPI

    cleanups: list[str] = []

    @contextlib.asynccontextmanager
    async def cm1(_app):
        yield
        cleanups.append("cm1")
        raise RuntimeError("cm1_failed")

    @contextlib.asynccontextmanager
    async def cm2(_app):
        yield
        cleanups.append("cm2")

    @contextlib.asynccontextmanager
    async def combined(_app):
        async with cm1(_app):
            async with cm2(_app):
                yield

    app = FastAPI(lifespan=combined)

    async def _drive() -> bool:
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
        return any(
            m.get("type") == "lifespan.shutdown.failed" for m in sent
        )

    saw_failed = asyncio.run(_drive())
    assert saw_failed, "shutdown.failed should be emitted"
    # Inner cm2 always cleans up (it's nested inside cm1 in our
    # combined ctx, so its exit runs before cm1's). cm1 cleans up
    # too (best-effort across the chain).
    assert "cm2" in cleanups, cleanups
    assert "cm1" in cleanups, cleanups


# ────────────────────────────────────────────────────────────────────
# #2 R37 drift detector tolerates parent FORCE_LOOPBACK_DENIED
# ────────────────────────────────────────────────────────────────────


def test_r37_drift_detector_ignores_parent_force_loopback_env():
    """When parent pytest is invoked with
    ``FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1``, the R37 drift
    detector inherited the env var via ``**os.environ`` and
    measured the FORCE-mode count (~849) while comparing against
    the happy-path doc claim (~1009). R38 strips the FORCE env
    vars from the spawned subprocess so it always measures
    happy-path."""
    test_file = (
        pathlib.Path(__file__).resolve().parent / "test_r37_regressions.py"
    )
    text = test_file.read_text()
    # Must filter the FORCE env vars from the subprocess env.
    assert "FASTAPI_TURBO_FORCE_LOOPBACK_DENIED" in text, text
    assert "FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED" in text, text
    # The filter must be in a ``not in`` check (not an explicit
    # set with the var, which would propagate it).
    assert (
        'k not in (' in text
        or "FASTAPI_TURBO_FORCE_LOOPBACK_DENIED\",\n" in text
    ), text


# ────────────────────────────────────────────────────────────────────
# #3 R36 gate test checks returncode before parsing pass count
# ────────────────────────────────────────────────────────────────────


def test_r36_gate_test_checks_returncode_first():
    """Auditor reported the R36 test passing under pytest while
    the canonical script's underlying pytest clearly produced
    885 failures. The earlier ``passed >= 3000`` assertion
    could mask returncode-failures if the regex picked up a
    non-summary ``X passed`` substring earlier in the output.
    R38 makes the test:
      (a) check ``proc.returncode == 0`` BEFORE any text parsing;
      (b) use ``re.findall(...)[-1]`` so only the LAST ``X passed``
          (i.e. pytest's actual summary) counts toward the
          threshold."""
    test_file = (
        pathlib.Path(__file__).resolve().parent / "test_r36_regressions.py"
    )
    text = test_file.read_text()
    # The actual conditional check (an ``if proc.returncode != 0:``
    # statement, not the substring in a comment) must precede the
    # ``assert passed >= 3000`` line.
    rt_idx = text.find("if proc.returncode != 0:")
    pass_idx = text.find("assert passed >= 3000")
    assert rt_idx != -1, "if proc.returncode != 0 conditional missing"
    assert pass_idx != -1, "assert passed >= 3000 missing"
    assert rt_idx < pass_idx, (
        "returncode check must precede pass-count assertion"
    )
    # Must use re.findall + [-1] for the pass count (not re.search
    # which yields the FIRST match).
    assert "re.findall" in text, text
    assert "pass_matches[-1]" in text, text


# ────────────────────────────────────────────────────────────────────
# #4 subprocess-drift escape hatch wired everywhere
# ────────────────────────────────────────────────────────────────────


def test_full_suite_drift_skip_env_var_wired_in_all_three_detectors():
    """Each drift detector (R33 / R36 / R37) must check the
    ``FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT`` env var and skip when
    set. R38 wired this so the parent full-suite run can opt out
    of the nested-pytest contention while letting the dev /
    auditor run them on demand."""
    here = pathlib.Path(__file__).resolve().parent
    for fname in (
        "test_r33_regressions.py",
        "test_r36_regressions.py",
        "test_r37_regressions.py",
    ):
        text = (here / fname).read_text()
        assert "FASTAPI_TURBO_SKIP_SUBPROCESS_DRIFT" in text, fname
        assert "subprocess-drift detector skipped" in text, fname
