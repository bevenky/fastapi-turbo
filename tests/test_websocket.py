"""Phase 7 tests: WebSocket support."""

import asyncio
import socket
import subprocess
import sys
import textwrap
import time

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
        from fastapi_rs import FastAPI, WebSocket
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
        from fastapi_rs import FastAPI, WebSocket
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
        from fastapi_rs import FastAPI, WebSocket
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
        from fastapi_rs import FastAPI, WebSocket
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


# ── Phase 1: binary + state + Pipecat-compat tests ──────────────────


def test_websocket_binary_preserved(server_app):
    """Binary frames must be preserved byte-exact (no UTF-8 coercion)."""
    import websockets

    url = server_app("""
        from fastapi_rs import FastAPI, WebSocket
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
    """ws.receive() returns ASGI-style dict (Pipecat's hot path)."""
    import websockets

    url = server_app("""
        from fastapi_rs import FastAPI, WebSocket
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
        from fastapi_rs import FastAPI, WebSocket, WebSocketState
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
    from fastapi_rs import WebSocketState

    assert int(WebSocketState.CONNECTING) == 0
    assert int(WebSocketState.CONNECTED) == 1
    assert int(WebSocketState.DISCONNECTED) == 2
    assert int(WebSocketState.RESPONSE) == 3


def test_starlette_websockets_import_shim():
    """`from starlette.websockets import WebSocketState` must work via the shim."""
    import fastapi_rs  # noqa: F401

    from starlette.websockets import WebSocket, WebSocketState, WebSocketDisconnect

    assert WebSocketState is not None
    assert int(WebSocketState.DISCONNECTED) == 2
    assert WebSocket is not None
    assert WebSocketDisconnect is not None
