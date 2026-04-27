"""R31 audit follow-ups — 413 oversized-body path produces an
expected client-side send-body failure (locked as accepted
behavior, not a regression), and an external-gate helper script
mirrors the CI / release force-reset sequence so auditors can run
the same gates locally."""
import os
import pathlib
import stat

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 413 path: client send-body failure is accepted behavior
# ────────────────────────────────────────────────────────────────────


@pytest.mark.requires_loopback
def test_413_oversized_body_returns_413_status_with_accepted_send_failure():
    """When ``max_request_size`` is set and the client streams a
    body larger than the cap, Tower's ``RequestBodyLimitLayer``
    rejects mid-stream. The TCP connection drops while the client
    is still writing — httpcore logs ``send_request_body.failed``
    with ``BrokenPipeError`` / ``ConnectionResetError`` for that
    iteration, but the SERVER side correctly emits 413 and the
    client's response object reflects it.

    This test locks the contract: the client receives 413, AND
    the underlying httpx exception (if any) is the expected
    early-reject family (``RemoteProtocolError`` /
    ``WriteError`` / ``ReadError``). Catching it here documents
    that the corresponding debug-log line is accepted behavior,
    not a regression.

    Most servers (nginx, axum) reject early too — reading the full
    body before rejecting would defeat the cap's purpose. The
    R31 audit flagged the noisy log line; this test makes the
    accepted behavior explicit instead of just filtering."""
    import httpx
    from fastapi_turbo import Body, FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI(max_request_size=1 * 1024 * 1024)  # 1 MiB cap

    @app.post("/upload")
    def upload(data: bytes = Body(..., media_type="application/octet-stream")):
        return {"size": len(data)}

    body = b"z" * (2 * 1024 * 1024)  # 2 MiB — over the cap
    # Narrow exception set: only the early-reject failure modes the
    # audit observed (BrokenPipe / ConnectionReset surfaced as
    # ``WriteError``, half-closed read surfaced as ``ReadError``,
    # and the catch-all RFC-compliant ``RemoteProtocolError`` for
    # the case where the server flushed 413 + RST before the
    # client finished streaming). DO NOT widen back to
    # ``httpx.HTTPError`` — that's the base class and would let
    # genuinely unrelated httpx failures (DNS, timeout, TLS) pass
    # through as "accepted".
    accepted_early_reject = (
        httpx.RemoteProtocolError,
        httpx.WriteError,
        httpx.ReadError,
    )
    with TestClient(app) as cli:
        # Either the server flushes the 413 before TCP drops (httpx
        # observes the response) or the connection is dropped before
        # the response lands. Both outcomes are accepted: the SERVER
        # behaviour is the same — early rejection.
        try:
            r = cli.post(
                "/upload",
                content=body,
                headers={"content-type": "application/octet-stream"},
            )
        except accepted_early_reject as exc:
            # Documented expected outcome: server rejected before
            # the client finished streaming. The exception MUST be
            # one of the early-reject family — anything else is a
            # real failure (DNS, TLS, hung socket, etc).
            #
            # Lock that the message references the body-write half
            # of the exchange so a post-handshake transport failure
            # (which would also be a ``RemoteProtocolError`` /
            # ``ReadError``) doesn't masquerade as the 413 path.
            msg = str(exc).lower()
            recognised_signature = any(
                s in msg
                for s in (
                    "broken pipe",
                    "connection reset",
                    "errno 32",
                    "errno 54",
                    "errno 104",  # Linux ECONNRESET
                    "send_request_body",
                    "remote protocol",
                    "without sending complete message body",
                    "premature",
                )
            )
            assert recognised_signature, (
                "413 early-reject test caught an httpx exception "
                f"that doesn't match the expected send-body-failure "
                f"signature: {type(exc).__name__}: {exc!r}"
            )
            return

        # Common case: server flushes 413 before the connection drops.
        assert r.status_code == 413, (r.status_code, r.text[:120])


@pytest.mark.requires_loopback
def test_413_small_body_no_send_failure_at_all(caplog):
    """Regression guard — the 413 path is only noisy when the body
    is large enough that the client streams it across multiple
    sendto() calls. A small over-cap body (slightly over the cap,
    well under TCP MSS) returns 413 cleanly with NO send-body
    failure logged. Locks the line: the audit's 'noisy log' is
    specifically about the streaming-body case, not 413 in
    general.

    Asserts:
      * status is 413 (server rejected via Tower's
        ``RequestBodyLimitLayer``).
      * No httpcore / httpx ``send_request_body.failed``,
        ``BrokenPipe``, or ``ConnectionReset`` log lines were
        emitted during the request — the small-body path is the
        clean reference for what "413 without noise" looks like."""
    import logging
    from fastapi_turbo import Body, FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI(max_request_size=512)  # 512 bytes

    @app.post("/upload")
    def upload(data: bytes = Body(..., media_type="application/octet-stream")):
        return {"size": len(data)}

    body = b"x" * 1024  # 1 KiB — over the 512 B cap, but still tiny
    with caplog.at_level(logging.DEBUG, logger="httpcore"):
        with caplog.at_level(logging.DEBUG, logger="httpx"):
            with TestClient(app) as cli:
                r = cli.post(
                    "/upload",
                    content=body,
                    headers={"content-type": "application/octet-stream"},
                )
                assert r.status_code == 413, r.status_code

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    forbidden = (
        "send_request_body.failed",
        "BrokenPipeError",
        "ConnectionResetError",
        "Errno 32",
        "Errno 54",
        "Errno 104",
    )
    leaks = [s for s in forbidden if s in log_text]
    assert not leaks, (
        f"small-body 413 path leaked send-failure log lines: {leaks}\n"
        f"captured: {log_text[:600]}"
    )


# ────────────────────────────────────────────────────────────────────
# #2 Local helper script mirrors CI external gates
# ────────────────────────────────────────────────────────────────────


def test_external_compat_gates_helper_script_exists():
    """``scripts/run_external_compat_gates.sh`` is the local mirror
    of the CI / release-workflow external gates. The audit caught
    that local ``/tmp/sentry-python`` was at 2.58.0 instead of the
    workflow-pinned 2.42.0 — auditors couldn't reproduce the gate
    locally. The script does the same force-fetch + reset --hard
    + conftest injection that CI does, so a local run is
    bit-identical to the CI gate."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "run_external_compat_gates.sh"
    assert script.exists(), "external compat helper script missing"

    text = script.read_text()
    # Must pin both upstream FastAPI and Sentry to the same tags
    # the workflows use.
    assert 'UPSTREAM_TAG="0.136.0"' in text, text
    assert 'SENTRY_TAG="2.42.0"' in text, text
    # Must force-reset (not lazy-skip) on both.
    assert "git -C /tmp/fastapi_upstream reset --hard" in text, text
    assert "git -C /tmp/sentry-python reset --hard" in text, text
    # Must inject the shim conftest into both.
    assert "import fastapi_turbo  # noqa: F401" in text, text
    # Must run BOTH Sentry trees (FastAPI integration + ASGI
    # integration), matching the workflows.
    assert "/tmp/sentry-python/tests/integrations/fastapi" in text, text
    assert "/tmp/sentry-python/tests/integrations/asgi" in text, text


def test_external_compat_gates_helper_script_is_executable():
    """The script must have the executable bit set so auditors
    can run it directly (``./scripts/run_external_compat_gates.sh``)
    without first ``chmod +x``'ing it."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "run_external_compat_gates.sh"
    mode = script.stat().st_mode
    assert mode & stat.S_IXUSR, f"not user-executable: {oct(mode)}"
