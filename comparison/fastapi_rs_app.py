"""fastapi-rs benchmark app with HTTP and WebSocket endpoints."""
from fastapi_rs import FastAPI, WebSocket
import os

app = FastAPI()


@app.get("/hello")
def hello():
    return {"message": "hello"}


@app.websocket("/ws")
async def ws_echo(websocket: WebSocket):
    await websocket.accept()
    for _ in range(50000):
        data = await websocket.receive_text()
        await websocket.send_text(data)
    await websocket.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "18005"))
    app.run(host="127.0.0.1", port=port)
