"""Canonical cross-framework benchmark app (the CONTRACT).

The SAME module runs under both uvicorn (real FastAPI) and fastapi-turbo
(``app.run()``), and the Fastify / Gin / raw-Axum servers mirror these
endpoints path-for-path with semantically-identical responses. JSON bodies
use only ints + strings (no floats) so responses are byte-comparable across
languages; key ORDER follows this module (other servers use ordered
structs/objects to match).

Dimensions covered: GET/POST/PUT/PATCH/DELETE x sync/async x
{simple JSON, large JSON, small/large XML} + streaming (sync/async/await) +
Redis (get/set x sync/async) + Postgres (item-by-id/list x sync/async) +
WebSocket echo at /ws (accept; loop receive text -> echo same text verbatim;
close cleanly on disconnect — byte-identical across all 5 servers).

Connection targets (shared by all 5 servers):
  Postgres: host=127.0.0.1 port=5432 dbname=fastapi_turbo_bench user=venky
  Redis:    redis://127.0.0.1:6379  (key ``bench:item`` pre-seeded)
"""
from __future__ import annotations

import asyncio
import os

# Engine selection: ``BENCH_ENGINE=turbo`` installs the fastapi-turbo shim
# BEFORE ``from fastapi import ...`` so ``app.run()`` (Rust door) is available;
# otherwise this is plain real FastAPI (run via uvicorn) — the "regular
# FastAPI" comparison point.
if os.environ.get("BENCH_ENGINE") == "turbo":
    import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

app = FastAPI()

PG_DSN = "host=127.0.0.1 port=5432 dbname=fastapi_turbo_bench user=venky"
# Pool bounds per WORKER PROCESS. The runner must keep workers x PG_POOL_MAX
# under Postgres max_connections (100): at workers=18 the old fixed 4..8 meant
# a 144-connection growth storm -> "too many clients" -> pool checkouts stall
# (measured: turbo pg-sync collapsed to 23 rps at w18 while fine at w1/w8).
PG_POOL_MIN = int(os.environ.get("BENCH_PG_POOL_MIN", "2"))
PG_POOL_MAX = int(os.environ.get("BENCH_PG_POOL_MAX", "4"))
REDIS_URL = "redis://127.0.0.1:6379"


# ── models ───────────────────────────────────────────────────────────
class Item(BaseModel):
    sku: str
    qty: int
    tags: list[str] = []


class PatchItem(BaseModel):
    qty: int


# ── shared payload builders (byte-identical across languages) ─────────
def _large_list() -> dict:
    return {"items": [{"id": i, "name": f"item-{i}"} for i in range(1000)]}


_XML_SMALL = b'<?xml version="1.0" encoding="UTF-8"?><item><id>1</id><name>item-1</name></item>'


def _xml_large() -> bytes:
    parts = [b'<?xml version="1.0" encoding="UTF-8"?><items>']
    for i in range(1000):
        parts.append(f"<item><id>{i}</id><name>item-{i}</name></item>".encode())
    parts.append(b"</items>")
    return b"".join(parts)


_XML_LARGE = _xml_large()

# ── streaming contract: 10 chunks, each a fixed 64-byte line ──────────
_STREAM_N = 10
_STREAM_CT = "text/plain; charset=utf-8"


def _chunk(i: int) -> bytes:
    return (f"chunk-{i}: " + "=" * 54 + "\n").encode()


# ── lazy connection pools (bind to the running loop on first use) ─────
_sync_pg = None
_async_pg = None
_async_pg3 = None
_sync_redis = None
_async_redis = None


def _get_sync_pg():
    global _sync_pg
    if _sync_pg is None:
        from psycopg_pool import ConnectionPool
        # autocommit: this app's PG endpoints are read-only SELECTs. Without it
        # psycopg3 wraps every read in BEGIN..COMMIT = 3 wire round trips where
        # Gin/pgx and node-postgres do 1 (audited) — an unfair 2-RTT handicap.
        _sync_pg = ConnectionPool(PG_DSN, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX, open=True,
                                  kwargs={"autocommit": True})
    return _sync_pg


# Async PG driver: each engine runs its BEST driver (measured, benchmarks/
# DEEPDIVE async deep-dive): under the turbo door psycopg3-async is healthy
# (~25k rps) while asyncpg pays a pathological penalty; under uvicorn the
# inverse holds (asyncpg healthy, psycopg3-async can deadlock). Overridable
# via BENCH_PG_ASYNC_DRIVER=asyncpg|psycopg3 for driver-vs-driver runs.
PG_ASYNC_DRIVER = os.environ.get("BENCH_PG_ASYNC_DRIVER") or (
    "psycopg3" if os.environ.get("BENCH_ENGINE") == "turbo" else "asyncpg"
)


async def _get_async_pg():
    global _async_pg
    if _async_pg is None:
        import asyncpg

        async def _no_reset(conn):
            # skip the default 4-statement pool-release reset script (+1 RTT
            # per request; audited) — bench conns hold no session state.
            return None

        _async_pg = await asyncpg.create_pool(
            host="127.0.0.1", port=5432, database="fastapi_turbo_bench",
            user="venky", min_size=PG_POOL_MIN, max_size=PG_POOL_MAX, reset=_no_reset,
        )
    return _async_pg


async def _get_async_pg3():
    global _async_pg3
    if _async_pg3 is None:
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(PG_DSN, min_size=PG_POOL_MIN, max_size=PG_POOL_MAX, open=False,
                                   kwargs={"autocommit": True})
        await pool.open()
        _async_pg3 = pool
    return _async_pg3


def _get_sync_redis():
    global _sync_redis
    if _sync_redis is None:
        import redis
        _sync_redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _sync_redis


async def _get_async_redis():
    global _async_redis
    if _async_redis is None:
        import redis.asyncio as aioredis
        _async_redis = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _async_redis


# ── plain JSON: simple ───────────────────────────────────────────────
# (turbo serves a built-in pure-Rust /_ping baseline; vanilla FastAPI has
# no static fast-path, so its baseline is /hello — the runner skips /_ping
# on any framework that 404s it.)
@app.get("/hello")
def hello():
    return {"message": "hello"}


@app.get("/async/hello")
async def async_hello():
    return {"message": "hello"}


# ── plain JSON: large ────────────────────────────────────────────────
@app.get("/json/large")
def json_large():
    return _large_list()


@app.get("/async/json/large")
async def async_json_large():
    return _large_list()


# ── body methods: POST / PUT / PATCH / DELETE ────────────────────────
@app.post("/items")
def create_item(item: Item):
    return {"ok": True, "sku": item.sku, "qty": item.qty, "tag_count": len(item.tags)}


@app.post("/async/items")
async def async_create_item(item: Item):
    return {"ok": True, "sku": item.sku, "qty": item.qty, "tag_count": len(item.tags)}


@app.put("/items/{item_id}")
def put_item(item_id: int, item: Item):
    return {"ok": True, "id": item_id, "sku": item.sku, "qty": item.qty,
            "tag_count": len(item.tags)}


@app.patch("/items/{item_id}")
def patch_item(item_id: int, patch: PatchItem):
    return {"ok": True, "id": item_id, "qty": patch.qty}


@app.delete("/items/{item_id}")
def delete_item(item_id: int):
    return {"ok": True, "deleted": item_id}


# ── XML ──────────────────────────────────────────────────────────────
@app.get("/xml/small")
def xml_small():
    return Response(content=_XML_SMALL, media_type="application/xml")


@app.get("/xml/large")
def xml_large():
    return Response(content=_XML_LARGE, media_type="application/xml")


# ── streaming ────────────────────────────────────────────────────────
@app.get("/stream-sync")
def stream_sync():
    def gen():
        for i in range(_STREAM_N):
            yield _chunk(i)
    return StreamingResponse(gen(), media_type=_STREAM_CT)


@app.get("/stream-async")
async def stream_async():
    async def gen():
        for i in range(_STREAM_N):
            yield _chunk(i)
    return StreamingResponse(gen(), media_type=_STREAM_CT)


@app.get("/stream-await")
async def stream_await():
    async def gen():
        for i in range(_STREAM_N):
            await asyncio.sleep(0)
            yield _chunk(i)
    return StreamingResponse(gen(), media_type=_STREAM_CT)


# ── Redis ────────────────────────────────────────────────────────────
@app.get("/redis/get/sync")
def redis_get_sync():
    val = _get_sync_redis().get("bench:item")
    return Response(content=val, media_type="application/json")


@app.get("/redis/get/async")
async def redis_get_async():
    r = await _get_async_redis()
    val = await r.get("bench:item")
    return Response(content=val, media_type="application/json")


@app.post("/redis/set/sync")
def redis_set_sync():
    _get_sync_redis().set("bench:wkey", "bench-value")
    return {"ok": True}


@app.post("/redis/set/async")
async def redis_set_async():
    r = await _get_async_redis()
    await r.set("bench:wkey", "bench-value")
    return {"ok": True}


# ── Postgres ─────────────────────────────────────────────────────────
@app.get("/pg/item/{item_id}/sync")
def pg_item_sync(item_id: int):
    pool = _get_sync_pg()
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, sku, name, qty FROM items WHERE id = %s", (item_id,)
        ).fetchone()
    if row is None:
        return Response(status_code=404)
    return {"id": row[0], "sku": row[1], "name": row[2], "qty": row[3]}


if PG_ASYNC_DRIVER == "asyncpg":
    @app.get("/pg/item/{item_id}/async")
    async def pg_item_async(item_id: int):
        pool = await _get_async_pg()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, sku, name, qty FROM items WHERE id = $1", item_id
            )
        if row is None:
            return Response(status_code=404)
        return {"id": row["id"], "sku": row["sku"], "name": row["name"], "qty": row["qty"]}
else:
    @app.get("/pg/item/{item_id}/async")
    async def pg_item_async(item_id: int):
        pool = await _get_async_pg3()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, sku, name, qty FROM items WHERE id = %s", (item_id,)
            )
            row = await cur.fetchone()
        if row is None:
            return Response(status_code=404)
        return {"id": row[0], "sku": row[1], "name": row[2], "qty": row[3]}


@app.get("/pg/items/sync")
def pg_items_sync(limit: int = 10):
    pool = _get_sync_pg()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT %s", (limit,)
        ).fetchall()
    return {"items": [{"id": r[0], "sku": r[1], "name": r[2], "qty": r[3]} for r in rows]}


if PG_ASYNC_DRIVER == "asyncpg":
    @app.get("/pg/items/async")
    async def pg_items_async(limit: int = 10):
        pool = await _get_async_pg()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1", limit
            )
        return {"items": [{"id": r["id"], "sku": r["sku"], "name": r["name"], "qty": r["qty"]}
                          for r in rows]}
else:
    @app.get("/pg/items/async")
    async def pg_items_async(limit: int = 10):
        pool = await _get_async_pg3()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT %s", (limit,)
            )
            rows = await cur.fetchall()
        return {"items": [{"id": r[0], "sku": r[1], "name": r[2], "qty": r[3]}
                          for r in rows]}


# ── WebSocket echo (contract: receive text -> echo SAME text verbatim) ─
@app.websocket("/ws")
async def ws_echo(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    if os.environ.get("BENCH_ENGINE") == "turbo":
        # FASTAPI_TURBO_WORKERS env (read inside app.run) overrides workers=1.
        app.run(host="127.0.0.1", port=port, workers=1)
    else:
        import uvicorn
        workers = int(os.environ.get("BENCH_WORKERS", "1"))
        # uvicorn needs an import string (not an app object) for workers > 1.
        uvicorn.run("app:app", host="127.0.0.1", port=port, workers=workers,
                    log_level="warning")
