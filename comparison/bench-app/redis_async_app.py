"""fastapi-turbo + redis.asyncio (ASYNC) — pure Redis GET/SET benchmark."""
import os
from fastapi_turbo import FastAPI
import redis.asyncio as aredis

app = FastAPI()
ar: aredis.Redis | None = None

async def _get_client():
    global ar
    if ar is None:
        ar = aredis.Redis(host="localhost", port=6379, decode_responses=True)
        await ar.set("bench:key", "hello-world")
    return ar

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/cache/get")
async def cache_get():
    c = await _get_client()
    v = await c.get("bench:key")
    return {"v": v}

@app.post("/cache/set")
async def cache_set():
    c = await _get_client()
    await c.set("bench:key", "updated")
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 19041)))
