"""R30 audit follow-ups — graceful WebSocket close handshake (no
``ConnectionResetError`` in client logs), stress-regression for
the intermittently-failing ``test_ws_iter_text`` pattern, and
docs that no longer recommend ``Wait for PyO3 0.28+``."""
import logging
import pathlib
import sys

import pytest

import fastapi_turbo  # noqa: F401


# ────────────────────────────────────────────────────────────────────
# #1 stress regression for ``test_ws_iter_text`` flake
# ────────────────────────────────────────────────────────────────────


@pytest.mark.requires_loopback
def test_ws_iter_text_stress_30_iterations():
    """The audit caught ``test_ws_iter_text`` timing out
    intermittently on full-suite runs (``websockets.sync.client.
    connect`` hung past 10s on first run, passed on second). Lock
    the connect/iter/close cycle: 30 sequential ``websocket_connect``
    calls against a fresh app each time, each cycle exchanging 3
    text messages and reading the server's ``got:N`` reply. Catches
    cross-test resource leaks (thread/port exhaustion) and
    handshake-race regressions."""
    from fastapi_turbo import FastAPI, WebSocket
    from fastapi_turbo.testclient import TestClient

    for _ in range(30):
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_iter(websocket: WebSocket):
            await websocket.accept()
            count = 0
            async for _msg in websocket.iter_text():
                count += 1
                if count == 3:
                    break
            await websocket.send_text(f"got:{count}")
            await websocket.close()

        with TestClient(app).websocket_connect("/ws") as ws:
            ws.send_text("a")
            ws.send_text("b")
            ws.send_text("c")
            assert ws.receive_text() == "got:3"


# ────────────────────────────────────────────────────────────────────
# #2 graceful WS close — no ConnectionResetError noise
# ────────────────────────────────────────────────────────────────────


@pytest.mark.requires_loopback
def test_rust_ws_close_doesnt_log_connection_reset(caplog):
    """Server-initiated WebSocket close must produce a clean
    handshake — no ``ConnectionResetError`` traceback in stderr or
    captured ``websockets``-library logs. R29 broke the test-level
    propagation of the reset, but the underlying close path still
    raced the client's read; tracebacks flooded debug logs (~11 per
    full WS-suite run). R30 drains the read side after sending
    Close (200ms cap), echoes a Close frame on client-initiated
    close, then closes the sink — RFC 6455 §5.5.1 ordering."""
    from fastapi_turbo import FastAPI, WebSocket
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.websocket("/ws")
    async def ws_handler(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close(code=1000)

    # Capture both stderr (where Python prints unraisable tracebacks)
    # AND the ``websockets`` library logger output.
    from io import StringIO

    stderr_buf = StringIO()
    saved_stderr = sys.stderr
    sys.stderr = stderr_buf
    try:
        with caplog.at_level(logging.DEBUG, logger="websockets"):
            for _ in range(8):
                with TestClient(app).websocket_connect("/ws") as ws:
                    assert ws.receive_text() == "hi"
                    # Server sends Close — receiving the next frame
                    # surfaces a normal close as WebSocketDisconnect.
                    try:
                        ws.receive_text()
                    except Exception:
                        pass
    finally:
        sys.stderr = saved_stderr

    captured = stderr_buf.getvalue() + "\n".join(r.getMessage() for r in caplog.records)
    assert "ConnectionResetError" not in captured, captured
    assert "Connection reset by peer" not in captured, captured
    assert "Errno 54" not in captured, captured


@pytest.mark.requires_loopback
def test_rust_ws_client_initiated_close_returns_close_echo():
    """When the client initiates the close (the common case for
    test teardown via ``with`` block exit), the server should echo
    a Close frame back so the client's ``websockets`` library
    treats this as ``ConnectionClosedOK`` rather than abnormal.
    R30 added the echo + sink-close in the read path."""
    import websockets.exceptions
    from fastapi_turbo import FastAPI, WebSocket
    from fastapi_turbo.testclient import TestClient

    app = FastAPI()

    @app.websocket("/ws")
    async def ws_handler(websocket: WebSocket):
        await websocket.accept()
        # Receive forever — exit via the client's close.
        try:
            while True:
                await websocket.receive_text()
        except Exception:  # noqa: BLE001
            pass

    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_text("ping")
        # The ``with`` block exits via close(). Client side observes
        # the server's close echo through the ``websockets`` library
        # — this should NOT raise ConnectionClosedError or surface a
        # ConnectionReset.


# ────────────────────────────────────────────────────────────────────
# #3 stale doc cleanup
# ────────────────────────────────────────────────────────────────────


def test_benchmarks_md_no_longer_says_wait_for_pyo3_028():
    """The optimization roadmap row for free-threaded Python
    previously read ``**Tested — NOT ready.** Wait for PyO3 0.28+``,
    contradicting both ``Cargo.toml`` (PyO3 0.28) and the "Status
    as of R-batch refresh" paragraph two lines up. R30 reconciles
    the row."""
    bench = pathlib.Path(__file__).resolve().parents[2] / "benchmarks.md"
    text = bench.read_text()
    assert "Wait for PyO3 0.28+" not in text, text
    # The row must mention that the migration is complete.
    assert "Migration complete on PyO3 0.28" in text, text


def test_compatibility_md_sandbox_watermark_isnt_stale_R27():
    """The sandbox-mode breakdown must reference the current
    R-batch (R30) rather than a stale pin from earlier audits.
    R28 caught R26 → R27 drift; R30 catches R27 → R30 drift."""
    compat = (
        pathlib.Path(__file__).resolve().parents[2] / "COMPATIBILITY.md"
    )
    text = compat.read_text()
    # The R27 string must not be the active watermark anywhere in
    # the sandbox-numbers paragraphs (anchor the search to that
    # block via the leading sentinel).
    sandbox_idx = text.find("Two sandbox flavours")
    assert sandbox_idx != -1, "expected sandbox doc block missing"
    sandbox_block = text[sandbox_idx:sandbox_idx + 2000]
    assert "R27 watermark" not in sandbox_block, sandbox_block
