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
