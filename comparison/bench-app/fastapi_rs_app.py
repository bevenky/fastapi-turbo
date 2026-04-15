"""Mini E-commerce API -- fastapi-rs implementation.

Exercises: CRUD, query params, path params, JSON body, Depends chain,
Bearer auth, CORS middleware, form data login, WebSocket echo with timestamps.
"""

import json
import os
import time

from fastapi_rs import FastAPI, Depends, Header, Query, HTTPException, WebSocket
from fastapi_rs.responses import JSONResponse
from fastapi_rs.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ---------------------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------------------

app = FastAPI(title="Mini E-commerce (fastapi-rs)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ItemCreate(BaseModel):
    name: str
    price: float
    description: Optional[str] = None


class ItemUpdate(BaseModel):
    name: str
    price: float
    description: Optional[str] = None

# ---------------------------------------------------------------------------
# In-memory database (pre-seeded)
# ---------------------------------------------------------------------------

_db: dict[int, dict] = {
    1: {"id": 1, "name": "Widget",    "price": 9.99,  "description": None},
    2: {"id": 2, "name": "Gadget",    "price": 19.99, "description": None},
    3: {"id": 3, "name": "Doohickey", "price": 29.99, "description": None},
}
_next_id = 4

SECRET_TOKEN = "secret-token-123"

# ---------------------------------------------------------------------------
# Dependency chain:  get_db -> verify_token -> get_current_user
# ---------------------------------------------------------------------------

def get_db():
    return _db


def verify_token(authorization: str = Header(None)):
    if not authorization or not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = authorization[7:]
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


def get_current_user(token: str = Depends(verify_token), db=Depends(get_db)):
    return {"username": "demo_user", "email": "demo@example.com"}

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/items")
def list_items(limit: int = Query(10), offset: int = Query(0), db=Depends(get_db)):
    items = list(db.values())
    return items[offset : offset + limit]


@app.get("/items/{item_id}")
def get_item(item_id: int, db=Depends(get_db)):
    if item_id not in db:
        raise HTTPException(status_code=404, detail="Item not found")
    return db[item_id]


@app.post("/items", status_code=201)
def create_item(item: ItemCreate, db=Depends(get_db)):
    global _next_id
    new_item = {"id": _next_id, "name": item.name, "price": item.price, "description": item.description}
    db[_next_id] = new_item
    _next_id += 1
    return new_item


@app.put("/items/{item_id}")
def update_item(item_id: int, item: ItemUpdate, db=Depends(get_db)):
    if item_id not in db:
        raise HTTPException(status_code=404, detail="Item not found")
    db[item_id] = {"id": item_id, "name": item.name, "price": item.price, "description": item.description}
    return db[item_id]


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int, db=Depends(get_db)):
    if item_id in db:
        del db[item_id]
    return None


@app.get("/users/me")
def get_me(current_user=Depends(get_current_user)):
    return current_user


@app.post("/login")
def login(username: str = "", password: str = ""):
    # Simple mock login -- accepts any credentials
    return {"access_token": SECRET_TOKEN, "token_type": "bearer"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            text = await websocket.receive_text()
            msg = json.loads(text)
            msg["server_ts"] = time.time()
            await websocket.send_text(json.dumps(msg))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "19001"))
    app.run(host="127.0.0.1", port=port)
