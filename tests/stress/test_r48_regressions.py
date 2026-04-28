"""R48 audit follow-ups — WS ``Depends(use_cache=False)`` honoured,
in-process TestClient + WS session don't leak event loops / sockets,
canonical gate scrubs ``OFFLINE`` from the pytest child env, and
``verify_at_tag`` validates a real git work tree rather than just
the presence of ``.git``.
"""
import os
import pathlib
import warnings

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 WS Depends(use_cache=False) re-runs the dep
# ────────────────────────────────────────────────────────────────────


def test_ws_depends_use_cache_false_reruns_for_each_param():
    """Two ``Depends(dep, use_cache=False)`` params on the same WS
    handler must produce two distinct calls. Earlier the WS dep
    resolver unconditionally read/wrote ``dep_cache``, so the
    second param got the first call's value back. Probe-confirmed:
    turbo returned ``a=1, b=1, calls=1`` where upstream returns
    ``a=1, b=2, calls=2``."""
    import json

    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    state = {"calls": 0}

    def dep():
        state["calls"] += 1
        return state["calls"]

    app = FastAPI()

    @app.websocket("/ws")
    async def h(
        ws,
        a: int = Depends(dep, use_cache=False),
        b: int = Depends(dep, use_cache=False),
    ):
        await ws.accept()
        await ws.send_text(json.dumps({"a": a, "b": b, "calls": state["calls"]}))
        await ws.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            assert json.loads(ws.receive_text()) == {
                "a": 1, "b": 2, "calls": 2,
            }


def test_ws_depends_use_cache_default_caches():
    """Sanity guard: with ``use_cache=True`` (the default), the
    same dep yields the same value across two params in the same
    handler invocation."""
    import json

    from fastapi_turbo import Depends, FastAPI
    from fastapi_turbo.testclient import TestClient

    state = {"calls": 0}

    def dep():
        state["calls"] += 1
        return state["calls"]

    app = FastAPI()

    @app.websocket("/ws")
    async def h(ws, a: int = Depends(dep), b: int = Depends(dep)):
        await ws.accept()
        await ws.send_text(json.dumps({"a": a, "b": b}))
        await ws.close()

    with TestClient(app, in_process=True) as c:
        with c.websocket_connect("/ws") as ws:
            assert json.loads(ws.receive_text()) == {"a": 1, "b": 1}


# ────────────────────────────────────────────────────────────────────
# #2 in-process TestClient cleanup doesn't leak event loops/sockets
# ────────────────────────────────────────────────────────────────────


def test_in_process_testclient_close_no_resource_warning():
    """20 close-after-use cycles must not emit any
    ``ResourceWarning`` from ``BaseEventLoop.__del__`` ("unclosed
    event loop") or ``socket`` ("unclosed socket fd=…"). Earlier
    ``_ASGISyncClientShim.close`` only stopped the loop without
    joining the thread or closing the loop — the deallocator then
    warned loudly under ``-W error::ResourceWarning``."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.get("/x")
    async def _h():
        return {"ok": True}

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        for _ in range(20):
            c = TestClient(app, in_process=True)
            c.get("/x")
            c.close()


def test_in_process_websocket_session_close_no_resource_warning():
    """Same guarantee for the in-process WebSocket session. The
    earlier ``__exit__`` only stopped the loop, leaving the worker
    thread + transport sockets dangling."""
    from fastapi_turbo import FastAPI
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.websocket("/ws")
    async def _h(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        with TestClient(app, in_process=True) as c:
            for _ in range(20):
                with c.websocket_connect("/ws") as ws:
                    assert ws.receive_text() == "hi"


# ────────────────────────────────────────────────────────────────────
# #3 OFFLINE is scrubbed from the pytest child env
# ────────────────────────────────────────────────────────────────────


def test_external_gate_script_scrubs_offline_from_pytest_env():
    """``OFFLINE`` is a script-mode switch (clone vs verify-existing-
    checkout) — it has no defined meaning inside pytest. Leaking
    it would let any test branch on the value and create a hidden
    coupling between gate-mode and test-result that's easy to
    mistake for a compat regression. Static check: every pytest
    invocation in the gate script wraps with ``env -u OFFLINE``."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script_text = (repo / "scripts" / "run_external_compat_gates.sh").read_text()
    # Join continuation lines (``\`` at EOL) so a multi-line pytest
    # invocation is one logical command for the check below.
    joined = []
    buf = ""
    for ln in script_text.splitlines():
        if ln.endswith("\\"):
            buf += ln[:-1] + " "
        else:
            buf += ln
            joined.append(buf)
            buf = ""
    if buf:
        joined.append(buf)
    pytest_cmds = [
        cmd for cmd in joined
        if "$PYTHON_BIN" in cmd and "-m pytest" in cmd
    ]
    assert pytest_cmds, script_text
    for cmd in pytest_cmds:
        assert "env -u OFFLINE" in cmd, (
            f"pytest invocation does not scrub OFFLINE: {cmd!r}\n"
            f"All pytest invocations in the gate script must wrap "
            f"with `env -u OFFLINE` so the script-helper variable "
            f"can never reach the test process."
        )


# ────────────────────────────────────────────────────────────────────
# #4 verify_at_tag uses git rev-parse, not -d .git
# ────────────────────────────────────────────────────────────────────


def test_verify_at_tag_uses_git_rev_parse():
    """A partial / corrupted clone passes ``-d .git`` but blows up
    on ``git rev-parse``. The audit caught this exact state on
    /tmp/sentry-python — the helper exited 128 with no useful
    remediation path. The fix is to validate via
    ``git rev-parse --is-inside-work-tree`` and tell the user
    they can ``rm -rf <tree>`` to let the script reclone."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    script_text = (repo / "scripts" / "run_external_compat_gates.sh").read_text()
    # The validation is now ``git rev-parse --is-inside-work-tree``.
    assert "rev-parse --is-inside-work-tree" in script_text, script_text
    # Both the OFFLINE branch (verify_at_tag) and the online
    # branches must be guarded by the same check, so a corrupted
    # checkout in either mode triggers a fresh reclone (online)
    # or a clear remediation message (OFFLINE).
    assert script_text.count("rev-parse --is-inside-work-tree") >= 3, (
        "expected the rev-parse check in verify_at_tag plus the two "
        "online clone-reset branches (FastAPI + Sentry); count was "
        f"{script_text.count('rev-parse --is-inside-work-tree')}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-v"])
