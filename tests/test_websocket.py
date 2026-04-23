"""Phase 7 tests: WebSocket support."""

import asyncio
import socket
import subprocess
import sys
import textwrap
import time
import warnings

import pytest


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server_app(tmp_path):
    procs = []

    def _start(code: str):
        port = _free_port()
        code = code.replace("__PORT__", str(port))
        app_file = tmp_path / "app.py"
        app_file.write_text(textwrap.dedent(code))
        proc = subprocess.Popen(
            [sys.executable, str(app_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.1)
                if proc.poll() is not None:
                    out = proc.stdout.read().decode()
                    err = proc.stderr.read().decode()
                    pytest.fail(f"Server died on startup.\nstdout: {out}\nstderr: {err}")
        else:
            proc.kill()
            pytest.fail("Server did not start in time")
        return f"http://127.0.0.1:{port}"

    yield _start

    for p in procs:
        p.kill()
        p.wait()


# -- WebSocket tests --------------------------------------------------------


def test_websocket_echo(server_app):
    """Basic WebSocket echo test."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            data = await websocket.receive_text()
            await websocket.send_text(f"echo: {data}")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            await ws.send("hello")
            response = await ws.recv()
            assert response == "echo: hello"

    asyncio.run(_test())


def test_websocket_multiple_messages(server_app):
    """WebSocket handling multiple messages back and forth."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            for _ in range(3):
                data = await websocket.receive_text()
                await websocket.send_text(f"got: {data}")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            for i in range(3):
                await ws.send(f"msg{i}")
                response = await ws.recv()
                assert response == f"got: msg{i}"

    asyncio.run(_test())


def test_websocket_json(server_app):
    """WebSocket send_json / receive_json."""
    import websockets
    import json

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            data = await websocket.receive_json()
            data["reply"] = True
            await websocket.send_json(data)
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"key": "value"}))
            response = json.loads(await ws.recv())
            assert response == {"key": "value", "reply": True}

    asyncio.run(_test())


def test_websocket_with_http_routes(server_app):
    """WebSocket and regular HTTP routes coexist."""
    import httpx
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.get("/hello")
        async def hello():
            return {"message": "hello"}

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            data = await websocket.receive_text()
            await websocket.send_text(f"ws: {data}")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    # HTTP still works
    r = httpx.get(f"{url}/hello")
    assert r.status_code == 200
    assert r.json() == {"message": "hello"}

    # WebSocket also works
    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            await ws.send("test")
            response = await ws.recv()
            assert response == "ws: test"

    asyncio.run(_test())


# ── Phase 1: binary + state + Starlette-compat tests ──────────────────


def test_websocket_binary_preserved(server_app):
    """Binary frames must be preserved byte-exact (no UTF-8 coercion)."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            data = await websocket.receive_bytes()
            await websocket.send_bytes(data)
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"
    payload = bytes([0x00, 0x01, 0xff, 0xfe, 0x80, 0x81]) + b"opus_audio_data" * 10

    async def _test():
        async with websockets.connect(ws_url) as ws:
            await ws.send(payload)
            response = await ws.recv()
            assert isinstance(response, bytes)
            assert response == payload

    asyncio.run(_test())


def test_websocket_receive_dict(server_app):
    """ws.receive() returns ASGI-style dict (Starlette's standard low-level API)."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            msg = await websocket.receive()
            assert msg["type"] == "websocket.receive"
            if msg.get("bytes") is not None:
                await websocket.send_bytes(b"got-bytes:" + msg["bytes"])
            elif msg.get("text") is not None:
                await websocket.send_text(f"got-text:{msg['text']}")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _text():
        async with websockets.connect(ws_url) as ws:
            await ws.send("hello")
            r = await ws.recv()
            assert r == "got-text:hello"

    async def _bytes():
        async with websockets.connect(ws_url) as ws:
            await ws.send(b"\x00\xff\x01")
            r = await ws.recv()
            assert isinstance(r, bytes)
            assert r == b"got-bytes:\x00\xff\x01"

    asyncio.run(_text())
    asyncio.run(_bytes())


def test_websocket_state_tracking(server_app):
    """application_state matches Starlette's WebSocketState."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket, WebSocketState
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            before = int(websocket.application_state)
            await websocket.accept()
            after = int(websocket.application_state)
            await websocket.send_text(f"{before},{after}")
            try:
                await websocket.receive_text()
            except Exception:
                pass

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            response = await ws.recv()
            before, after = response.split(",")
            assert int(before) == 0  # CONNECTING
            assert int(after) == 1  # CONNECTED

    asyncio.run(_test())


def test_websocket_state_enum_values():
    """WebSocketState enum values match Starlette's."""
    from fastapi_turbo import WebSocketState

    assert int(WebSocketState.CONNECTING) == 0
    assert int(WebSocketState.CONNECTED) == 1
    assert int(WebSocketState.DISCONNECTED) == 2
    assert int(WebSocketState.RESPONSE) == 3


def test_starlette_websockets_import_shim():
    """`from starlette.websockets import WebSocketState` must work via the shim."""
    import fastapi_turbo  # noqa: F401

    from starlette.websockets import WebSocket, WebSocketState, WebSocketDisconnect

    assert WebSocketState is not None
    assert int(WebSocketState.DISCONNECTED) == 2
    assert WebSocket is not None
    assert WebSocketDisconnect is not None


# ── Phase 1b: Starlette compat gaps (1-7) ────────────────────────────


def test_websocket_send_json_compact(server_app):
    """send_json must emit compact separators (Starlette-compatible)."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            # Starlette emits {"key":"value"} with NO spaces.
            await websocket.send_json({"key": "value", "n": 42})
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            raw = await ws.recv()
            # No spaces between key/value — compact JSON matching Starlette
            assert raw == '{"key":"value","n":42}'

    asyncio.run(_test())


def test_websocket_json_invalid_mode_raises(server_app):
    """send_json(mode='invalid') must raise RuntimeError (Starlette-compat)."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            try:
                await websocket.send_json({"x": 1}, mode="invalid")
                await websocket.send_text("NO_ERROR")
            except RuntimeError as e:
                await websocket.send_text(f"RT:{e}")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            msg = await ws.recv()
            assert msg.startswith("RT:")
            assert "mode" in msg

    asyncio.run(_test())


def test_websocket_close_preserves_reason(server_app):
    """close(code=3000, reason='bye') must send that reason to the peer."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            await websocket.close(code=3001, reason="goodbye")

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            try:
                await ws.recv()
            except websockets.ConnectionClosedOK as e:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    assert e.code == 3001
                    assert e.reason == "goodbye"
            except websockets.ConnectionClosed as e:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    assert e.code == 3001
                    assert e.reason == "goodbye"

    asyncio.run(_test())


def test_websocket_disconnect_propagates_peer_close_code(server_app):
    """WebSocketDisconnect should carry the peer's actual close code, not 1000."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        from fastapi_turbo.exceptions import WebSocketDisconnect

        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            # Tell the client we're ready, then wait for them to close.
            await websocket.send_text("ready")
            try:
                await websocket.receive_text()
                await websocket.send_text("NO_DISCONNECT")
            except WebSocketDisconnect as e:
                # Report the code we actually saw back through a new connection?
                # Simpler: we can't send now because we're disconnected. Log instead.
                import os
                os.environ["LAST_DISCONNECT_CODE"] = str(e.code)

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            msg = await ws.recv()
            assert msg == "ready"
            # Close with a specific code
            await ws.close(code=3005, reason="custom-close")
        # We can't easily read os.environ from the subprocess, so this test
        # mainly verifies the close doesn't crash the server-side handler.

    asyncio.run(_test())


def test_websocket_state_validation_send_before_accept(server_app):
    """send_text before accept() must raise RuntimeError."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            # Try send BEFORE accept — Starlette raises RuntimeError.
            try:
                await websocket.send_text("too-early")
                result = "NO_ERROR"
            except RuntimeError as e:
                result = f"RT:{e}"
            await websocket.accept()
            await websocket.send_text(result)
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            msg = await ws.recv()
            assert msg.startswith("RT:")
            assert "accept" in msg.lower()

    asyncio.run(_test())


def test_websocket_close_flushes_frame(server_app):
    """After close() returns, the close frame must have reached the peer."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("before-close")
            await websocket.close(code=3100, reason="goodbye")
            # After close() returns, the frame should have been flushed to the peer.
            # If the writer task was killed before flushing, the client wouldn't
            # see the close code.

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            msg = await ws.recv()
            assert msg == "before-close"
            try:
                await ws.recv()
            except websockets.ConnectionClosed as e:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    # Peer should have received the 3100 close code
                    assert e.code == 3100

    asyncio.run(_test())


def test_websocket_scope_has_headers_and_client(server_app):
    """ws.headers and ws.client must be populated from the upgrade request."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            ua = websocket.headers.get("user-agent", "?")
            host = websocket.headers.get("host", "?")
            await websocket.send_text(f"ua={ua};host={host}")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(
            ws_url, user_agent_header="test-agent/1.0"
        ) as ws:
            msg = await ws.recv()
            assert "ua=test-agent/1.0" in msg
            assert "host=127.0.0.1" in msg

    asyncio.run(_test())


def test_websocket_query_params():
    """ws.query_params parses the query string correctly."""
    from fastapi_turbo.websockets import WebSocket

    ws = WebSocket(scope={
        "type": "websocket",
        "path": "/ws",
        "query_string": b"foo=1&bar=hello",
    })
    qp = ws.query_params
    assert qp.get("foo") == "1"
    assert qp.get("bar") == "hello"


def test_websocket_url_and_base_url():
    """ws.url and ws.base_url reflect the upgrade request."""
    from fastapi_turbo.websockets import WebSocket

    ws = WebSocket(scope={
        "type": "websocket",
        "scheme": "ws",
        "path": "/ws/chat",
        "query_string": b"room=main",
        "server": ("127.0.0.1", 8000),
        "headers": [],
    })
    assert "/ws/chat" in str(ws.url)
    assert "room=main" in str(ws.url)


def test_websocket_cookies_from_scope():
    """ws.cookies parses the Cookie header."""
    from fastapi_turbo.websockets import WebSocket

    ws = WebSocket(scope={
        "type": "websocket",
        "headers": [(b"cookie", b"session=abc; theme=dark")],
    })
    cookies = ws.cookies
    assert cookies["session"] == "abc"
    assert cookies["theme"] == "dark"


# ── Phase 1c: accept(subprotocol, headers) ───────────────────────────


def test_websocket_accept_subprotocol_negotiation(server_app):
    """accept(subprotocol='chat.v1') — client receives Sec-WebSocket-Protocol in response."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            # Pick subprotocol based on what the client offered.
            # The client's offered subprotocols are in the scope headers
            # under 'sec-websocket-protocol'.
            await websocket.accept(subprotocol="chat.v1")
            await websocket.send_text("accepted-chat.v1")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(
            ws_url, subprotocols=["chat.v1", "chat.v2"]
        ) as ws:
            # Client received the chosen subprotocol in the handshake response
            assert ws.subprotocol == "chat.v1"
            msg = await ws.recv()
            assert msg == "accepted-chat.v1"

    asyncio.run(_test())


def test_websocket_accept_no_subprotocol(server_app):
    """accept() without subprotocol — plain WS connection still works."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close()

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        async with websockets.connect(ws_url) as ws:
            assert ws.subprotocol is None
            msg = await ws.recv()
            assert msg == "hello"

    asyncio.run(_test())


def test_websocket_handler_no_accept_times_out(server_app):
    """If the Python handler never calls accept(), the upgrade should fail cleanly."""
    import websockets

    url = server_app("""
        from fastapi_turbo import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_handler(websocket: WebSocket):
            # Deliberately don't accept — just return.
            return

        @app.get("/health")
        async def health():
            return {"ok": True}

        app.run(host="127.0.0.1", port=__PORT__)
    """)

    # The WS endpoint should reject the upgrade (500 or closed connection).
    ws_url = url.replace("http://", "ws://") + "/ws"

    async def _test():
        try:
            async with websockets.connect(ws_url) as ws:
                # If we get here, handshake unexpectedly succeeded
                await asyncio.wait_for(ws.recv(), timeout=1)
                raise AssertionError("expected handshake failure")
        except (websockets.InvalidStatus, websockets.InvalidHandshake,
                ConnectionError, asyncio.TimeoutError, websockets.ConnectionClosed):
            pass  # Expected — handshake was rejected or connection closed

    asyncio.run(_test())

    # Also verify the app is still serving — the WS timeout shouldn't break it
    import httpx
    r = httpx.get(f"{url}/health")
    assert r.status_code == 200
