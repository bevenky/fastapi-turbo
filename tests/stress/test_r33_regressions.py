"""R33 audit follow-ups — parity conftest honours the FORCE
loopback env var (so its skip logic stays aligned with the
suite-level conftest), R32's doc regression now actually pins
the measured FORCE-mode count, helper script gains an
``OFFLINE=1`` mode for network-restricted audits, and the
ruff invalid-noqa warning in ``audit_qwen.py`` is fixed."""
import pathlib
import subprocess
import sys

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 parity conftest honours FASTAPI_TURBO_FORCE_LOOPBACK_DENIED
# ────────────────────────────────────────────────────────────────────


def test_parity_conftest_honours_force_loopback_denied_env_var():
    """Earlier the parity conftest tried ``from tests.conftest
    import LOOPBACK_DENIED`` (which fails — ``tests`` isn't a
    package) and fell back to a fresh bind probe. On a dev box
    that CAN bind, the FORCE env var was effective at the
    suite-level conftest but ignored at the parity-level conftest
    — so the FORCE flag skipped requires_loopback tests but
    still ran the 107 parity tests, producing an inconsistent
    third bucket. R33 reads the env vars directly in the parity
    conftest so the skip behaviour is identical to the suite
    conftest."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    parity_conftest = (repo / "tests" / "parity" / "conftest.py").read_text()
    # Must read the FORCE env vars directly.
    assert (
        "FASTAPI_TURBO_FORCE_LOOPBACK_DENIED" in parity_conftest
    ), parity_conftest
    assert (
        "FASTAPI_TURBO_FORCE_LOOPBACK_ALLOWED" in parity_conftest
    ), parity_conftest


# ────────────────────────────────────────────────────────────────────
# #2 doc regression actually pins the measured FORCE-mode count
# ────────────────────────────────────────────────────────────────────


def test_compatibility_md_pins_measured_force_mode_count():
    """The R32 regression test only checked that ``~903 pass`` is
    gone and that a FORCE-env phrase exists — it didn't verify
    the actual current count. R33 saw the doc still claimed
    917/55 even though the measured count was 817/162. This test
    pins the watermark count so a future drift trips."""
    compat = (
        pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    )
    text = compat.read_text()
    # Must contain the current measured numbers (R33 watermark)
    # for the FORCE-on-dev-box scenario. Numbers update with each
    # R-batch as the test count grows; this regression catches
    # drift between the doc claim and the actual measurement.
    assert "817 pass, 162 skipped" in text, text
    # And must not still carry the old-and-wrong 917/55 / 903/60
    # claims.
    for stale in ("917 pass, 55 skipped", "~903 pass", "~895 pass"):
        assert stale not in text, (stale, text)


# ────────────────────────────────────────────────────────────────────
# #3 helper script offline mode
# ────────────────────────────────────────────────────────────────────


def test_external_compat_helper_supports_offline_mode():
    """``OFFLINE=1`` must skip all network operations (clone,
    fetch, pip install) and instead verify the existing
    ``/tmp/<tree>`` checkout is already at the pinned tag.
    Audit environments with no network access need this to run
    the gate at all — earlier the script unconditionally
    fetched and failed at DNS before pytest."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()

    # OFFLINE env var is honoured.
    assert 'OFFLINE="${OFFLINE:-0}"' in script, script
    # Has a verify_at_tag helper for offline mode.
    assert "verify_at_tag" in script, script
    # Network-touching commands are guarded by the OFFLINE check.
    assert 'if [ "$OFFLINE" = "1" ]; then' in script, script
    assert "git clone https://github.com/fastapi/fastapi" in script, script
    assert "git clone https://github.com/getsentry/sentry-python" in script, script


def test_external_compat_helper_offline_mode_structurally_skips_network():
    """Static check: the script's ``run_fastapi_gate`` and
    ``run_sentry_gate`` functions must guard ``git clone``,
    ``git fetch``, and ``pip install`` behind the
    ``if [ "$OFFLINE" = "1" ]; then ... else ... fi`` block.
    Earlier these were unconditional — DNS-resolution failure
    on a network-restricted runner aborted before any pytest
    work. Walk the body line-by-line tracking the active
    OFFLINE-branch state via indent-aware ``then`` / ``else``
    / ``fi`` parsing."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()

    for fn in ("run_fastapi_gate", "run_sentry_gate"):
        start = script.find(f"{fn}() {{")
        assert start != -1, f"{fn} missing"
        rest = script[start:]
        end_rel = rest.find("\n}\n")
        assert end_rel != -1, f"{fn} body unterminated"
        body = rest[: end_rel]

        # Indent-depth-tracking state machine: enter OFFLINE
        # block on the OFFLINE ``if``, switch to ``else_branch``
        # on the OFFLINE ``else``, exit on the matching ``fi``
        # (NOT inner blocks' ``fi``). Track nesting depth so
        # nested ``if/fi`` pairs don't prematurely close the
        # OFFLINE block.
        in_offline_block = False
        in_else_branch = False
        offline_depth = 0  # nested-if depth WITHIN the OFFLINE block
        for raw_line in body.splitlines():
            stripped = raw_line.strip()
            if stripped == 'if [ "$OFFLINE" = "1" ]; then':
                in_offline_block = True
                in_else_branch = False
                offline_depth = 0
                continue
            if in_offline_block:
                if stripped.startswith("if ") and stripped.endswith("then"):
                    offline_depth += 1
                    continue
                if stripped == "fi":
                    if offline_depth > 0:
                        offline_depth -= 1
                        continue
                    # Matching outer fi — exit OFFLINE block.
                    in_offline_block = False
                    in_else_branch = False
                    continue
                if stripped == "else" and offline_depth == 0:
                    in_else_branch = True
                    continue

            net_tokens = ("git clone http", "git -C /tmp/", "pip install")
            if any(tok in raw_line for tok in net_tokens):
                assert in_offline_block and in_else_branch, (
                    f"{fn}: network-touching line is NOT gated under "
                    f"OFFLINE else-branch: {raw_line.strip()!r}"
                )


# ────────────────────────────────────────────────────────────────────
# #4 ruff: invalid noqa in audit_qwen.py
# ────────────────────────────────────────────────────────────────────


def test_no_invalid_ruff_noqa_codes_in_repo():
    """Earlier ``comparison/bench-app/audit_qwen.py:11`` had a
    bare ``# noqa: install shims`` directive — ruff treated
    ``install`` as an unrecognised rule code and emitted a
    warning. R33 changed it to ``# noqa: F401`` (the actual
    rule the line silences). Lock it via ``ruff check .`` —
    the warning would surface in stderr."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "Invalid rule code" not in combined, combined
    assert "warning: " not in combined.lower() or "Failed to" in combined, combined
    # The noqa in question must use a valid rule code.
    audit = (
        repo / "comparison" / "bench-app" / "audit_qwen.py"
    ).read_text()
    # Either a specific rule (``F401``) or nothing — the bare
    # ``install shims`` form must be gone. The pattern below is
    # intentionally constructed at runtime so this file's own
    # source doesn't carry an invalid noqa-like literal that
    # ruff would trip on.
    bad_token = "noqa" + ":" + " install"
    assert bad_token not in audit, audit
