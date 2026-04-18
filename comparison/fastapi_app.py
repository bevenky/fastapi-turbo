from fastapi import FastAPI, Depends, Header, WebSocket
from pydantic import BaseModel
import os

app = FastAPI()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(data)
    except Exception:
        pass

class Item(BaseModel):
    name: str
    price: float

async def get_db():
    return {"connected": True}

async def get_user(db=Depends(get_db), authorization: str = Header("t")):
    return {"name": "alice"}

@app.get("/_ping")
def ping():
    return {"ping": "pong"}

@app.get("/hello")
def hello():
    return {"message": "hello"}

@app.get("/with-deps")
async def with_deps(user=Depends(get_user), db=Depends(get_db)):
    return {"user": user["name"]}

@app.post("/items")
def create_item(item: Item):
    return {"name": item.name, "price": item.price, "created": True}
