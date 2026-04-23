"""fastapi-rs + redis-py (SYNC) — pure Redis GET/SET benchmark."""
import os
from fastapi_rs import FastAPI
import redis

app = FastAPI()
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

# Pre-seed key
r.set("bench:key", "hello-world")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/cache/get")
def cache_get():
    return {"v": r.get("bench:key")}

@app.post("/cache/set")
def cache_set():
    r.set("bench:key", "updated")
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 19040)))
