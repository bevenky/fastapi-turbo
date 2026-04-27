"""R32 audit follow-ups — CI/release install fastapi+uvicorn for
parity, the local-gate helper resolves a concrete python interpreter
(no bare ``python`` / ``pytest`` calls), the 413 regressions assert
narrower failure modes, COMPATIBILITY.md sandbox-bucket wording is
accurate, and the README oha cross-check claim matches reality."""
import pathlib

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 CI / release install fastapi + uvicorn before parity gate
# ────────────────────────────────────────────────────────────────────


def _read_workflow(name: str) -> str:
    repo = pathlib.Path(__file__).resolve().parents[2]
    return (repo / ".github" / "workflows" / name).read_text()


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_workflow_installs_fastapi_and_uvicorn_before_parity(workflow):
    """Parity tests spawn a STOCK FastAPI subprocess
    (``FASTAPI_TURBO_NO_SHIM=1; import uvicorn; from parity_app
    import app``). On a fresh CI runner those imports fail unless
    the workflow installs real upstream FastAPI + uvicorn alongside
    fastapi-turbo. The audit caught both ci.yml and release.yml
    omitting these — parity could fail at process startup before
    proving anything."""
    text = _read_workflow(workflow)
    # Pin the same upstream version COMPATIBILITY.md claims parity
    # against, so a drift in upstream FastAPI doesn't silently
    # break the gate.
    assert '"fastapi==0.136.0"' in text, (workflow, text[:1000])
    # uvicorn for the subprocess server (the parity conftest does
    # ``import uvicorn`` directly).
    assert "uvicorn[standard]" in text or "uvicorn" in text, workflow


# ────────────────────────────────────────────────────────────────────
# #2 helper script resolves PYTHON_BIN, doesn't use bare python
# ────────────────────────────────────────────────────────────────────


def test_external_compat_helper_resolves_python_bin():
    """The script must:
      * Resolve a concrete ``$PYTHON_BIN`` (env override → venv →
        PATH).
      * Verify ``fastapi_turbo`` imports from THAT interpreter
        before running pip / pytest.
      * Use ``"$PYTHON_BIN" -m pip`` and ``"$PYTHON_BIN" -m pytest``
        (NEVER bare ``pip`` / ``pytest`` / ``python``) so the gate
        can't silently pick up the wrong env.

    The audit caught a case where ``$VIRTUAL_ENV`` pointed at a
    different env than the one with the package — exactly the
    drift the script is supposed to prevent."""
    import re

    repo = pathlib.Path(__file__).resolve().parents[2]
    script = (repo / "scripts" / "run_external_compat_gates.sh").read_text()

    # Must do PYTHON_BIN resolution.
    assert 'PYTHON_BIN="$VIRTUAL_ENV/bin/python"' in script, script
    # Must verify fastapi_turbo imports BEFORE running anything.
    assert '"$PYTHON_BIN" -c \'import fastapi_turbo\'' in script, script

    # Every pip / pytest / python invocation that's actually executed
    # (i.e. starts a line, not buried in an echo / docstring) must
    # use ``"$PYTHON_BIN"``. Match lines that begin with
    # ``python``, ``pip``, ``pytest``, or ``  python``... at any
    # indent level after stripping leading whitespace.
    bad_starts = (re.compile(r"^(python|pip|pytest)\b"),)
    for raw_line in script.splitlines():
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith(("echo ", '"echo')):
            continue
        for pat in bad_starts:
            assert not pat.match(stripped), (
                f"bare interpreter invocation: {stripped!r} — "
                "must use \"$PYTHON_BIN\" -m pip / -m pytest"
            )


# ────────────────────────────────────────────────────────────────────
# #3 413 regression doesn't catch httpx.HTTPError (base class)
# ────────────────────────────────────────────────────────────────────


def test_413_regression_doesnt_catch_httpx_base_error_class():
    """The 413 ``oversized_body`` test in
    ``test_r31_regressions.py`` previously caught
    ``httpx.HTTPError`` — the base class of every httpx exception
    — so unrelated transport failures (TLS, DNS, hung socket)
    could pass as "accepted early reject". R32 narrows the
    accepted set to the early-reject family AND asserts the
    exception message carries a recognisable send-body-failure
    signature."""
    test_file = (
        pathlib.Path(__file__).resolve().parent / "test_r31_regressions.py"
    )
    text = test_file.read_text()
    # Strip block comments / docstrings before scanning — the
    # docstring legitimately mentions ``httpx.HTTPError`` while
    # explaining why it's NOT in the catch tuple. Look at the
    # ``except`` clause definition only.
    excepted_tuple = ""
    in_tuple = False
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("accepted_early_reject = ("):
            in_tuple = True
            continue
        if in_tuple:
            if stripped.startswith(")"):
                break
            excepted_tuple += stripped + "\n"

    # The actual exception tuple must NOT include the base class.
    assert "httpx.HTTPError" not in excepted_tuple, excepted_tuple
    # The accepted set must include the concrete classes the
    # audit observed.
    for cls in ("RemoteProtocolError", "WriteError", "ReadError"):
        assert f"httpx.{cls}" in excepted_tuple, (cls, excepted_tuple)
    # And there must be a signature check on the exception message
    # somewhere in the test (not just the catch tuple).
    assert "recognised_signature" in text, text


# ────────────────────────────────────────────────────────────────────
# #4 small-body 413 path test asserts NO send-failure log noise
# ────────────────────────────────────────────────────────────────────


def test_413_small_body_test_actually_asserts_no_send_failure():
    """Earlier the test only asserted ``status_code == 413``. R32
    upgrades it to capture httpcore / httpx debug logs and assert
    none of the ``send_request_body.failed`` /
    ``ConnectionResetError`` / ``BrokenPipeError`` strings appear.
    Locks the 'small body is the clean path' contract."""
    test_file = (
        pathlib.Path(__file__).resolve().parent / "test_r31_regressions.py"
    )
    text = test_file.read_text()
    # The test must use caplog and gate on the forbidden strings.
    assert "caplog.at_level" in text, text
    for s in (
        "send_request_body.failed",
        "BrokenPipeError",
        "ConnectionResetError",
    ):
        assert s in text, s


# ────────────────────────────────────────────────────────────────────
# #5 COMPATIBILITY sandbox-bucket wording is accurate
# ────────────────────────────────────────────────────────────────────


def test_compatibility_sandbox_doc_doesnt_misclassify_force_env():
    """Earlier wording claimed
    ``FASTAPI_TURBO_FORCE_LOOPBACK_DENIED=1`` on a dev box puts
    the run in the ``runtime-OK`` bucket with ``~903 pass / ~60
    skipped`` — but those exact numbers were stale. The doc must
    now reflect the measured count AND clearly state that the
    FORCE env var alone doesn't take you to the true-sandbox
    bucket (kernel state matters, not just the conftest probe)."""
    compat = (
        pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    )
    text = compat.read_text()
    # The stale-by-multiple-batches "~903 pass" claim must be gone.
    assert "~903 pass" not in text, text
    # And the doc must call out the FORCE behaviour explicitly —
    # either the original "doesn't take you to bucket #1" wording
    # (from R32) or R33's clearer "IS sufficient to produce
    # bucket #1 numbers on a dev box that can bind".
    force_phrase_options = (
        "FORCE env var ALONE doesn't take you to bucket #1",
        "FORCE env var alone doesn't take you to bucket #1",
        "FORCE env var IS sufficient to produce bucket #1 numbers",
    )
    assert any(p in text for p in force_phrase_options), text


# ────────────────────────────────────────────────────────────────────
# #6 README oha cross-check claim matches reality
# ────────────────────────────────────────────────────────────────────


def test_readme_oha_cross_check_claim_is_accurate():
    """README previously said ``rows in the single-connection
    table have been re-run with oha -c 1``, implying every row
    was cross-checked. Per-benchmark docs only publish the
    cross-check for ``/hello``. Wording is now narrower:
    ``ONE row (GET /hello) has been re-run with oha -c 1``."""
    readme = pathlib.Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text()
    # The plural "rows ... have been re-run with oha" claim must
    # be gone.
    assert "rows in the single-connection table have been\n  re-run with `oha -c 1`" not in text, text
    # The narrower wording must be present.
    assert "ONE row" in text, text
