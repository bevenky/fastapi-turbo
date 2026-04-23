"""fastapi-turbo WebSocket echo benchmark app — text and binary."""
import sys
from fastapi_turbo import FastAPI, WebSocket
from fastapi_turbo.exceptions import WebSocketDisconnect

app = FastAPI()


@app.websocket("/ws-text")
async def ws_text(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws-bytes")
async def ws_bytes(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_bytes()
            await websocket.send_bytes(data)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws-sync")
def ws_sync(websocket: WebSocket):
    # Sync handler — simulates no asyncio overhead
    websocket._ws.accept()
    while True:
        try:
            data = websocket._ws.receive_text(__import__("sys").modules[__name__])
        except Exception:
            break


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    app.run("127.0.0.1", port)
