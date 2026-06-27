"""DB + Redis benchmark contract app (the deep matrix).

Postgres: select_one / select_list / insert / update / delete, each write with
commit=true (durable) AND commit=false (rollback — isolates commit/fsync cost),
across FOUR Python drivers:
  pg3sync  = psycopg3 sync   (psycopg_pool.ConnectionPool)
  pg3async = psycopg3 async  (psycopg_pool.AsyncConnectionPool)
  asyncpg  = asyncpg
  pg2sync  = psycopg2 sync   (psycopg2.pool.ThreadedConnectionPool)

Driver matrix paths (Python only):  /pgm/{driver}/{op}[?commit=true|false]
Cross-framework paths (all 5):      /pg/{op}/{sync|async}[?commit=]
  (Python: sync→pg3sync, async→asyncpg; Go/Node/Rust: their one native driver)

Redis (redis-py sync + asyncio): get / set / set_durable (server fsync) /
pipeline / multi, via /redis/{mode}/{op}  (mode = sync|async).

Pools are deliberately small (min 1, max 2 per driver) so that even with several
worker processes the total stays under Postgres max_connections=100. This makes
turbo's multiprocess (one pool PER worker) vs Go/Node's single shared pool an
explicit, documented asymmetry — see report notes.

Engine: BENCH_ENGINE=turbo → app.run() (Rust door); else uvicorn (real FastAPI).
"""
from __future__ import annotations

import os

if os.environ.get("BENCH_ENGINE") == "turbo":
    import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

app = FastAPI()

PGHOST, PGPORT, PGDB, PGUSER = "127.0.0.1", 5432, "fastapi_turbo_bench", "venky"
DSN = f"host={PGHOST} port={PGPORT} dbname={PGDB} user={PGUSER}"
REDIS_URL = "redis://127.0.0.1:6379"
PMIN, PMAX = 1, 2  # tiny pools — keep 100-conn budget across workers

SEL_ONE_3 = "SELECT id, sku, name, qty FROM items WHERE id = %s"
SEL_ONE_PG = "SELECT id, sku, name, qty FROM items WHERE id = $1"
SEL_LIST_3 = "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT %s"
SEL_LIST_PG = "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1"
INS_3 = "INSERT INTO bench_writes (val) VALUES (%s)"
INS_PG = "INSERT INTO bench_writes (val) VALUES ($1)"
UPD_3 = "UPDATE bench_writes SET val = %s WHERE id = 5"
UPD_PG = "UPDATE bench_writes SET val = $1 WHERE id = 5"
DEL_3 = "DELETE FROM bench_writes WHERE id = %s"        # non-existent id → 0 rows, repeatable
DEL_PG = "DELETE FROM bench_writes WHERE id = $1"
DEL_ID = 999_999_999

# ── lazy pools ────────────────────────────────────────────────────────
_pools: dict = {}


def pg3sync_pool():
    if "pg3sync" not in _pools:
        from psycopg_pool import ConnectionPool
        _pools["pg3sync"] = ConnectionPool(DSN, min_size=PMIN, max_size=PMAX, open=True)
    return _pools["pg3sync"]


def pg2_pool():
    if "pg2" not in _pools:
        import psycopg2.pool
        _pools["pg2"] = psycopg2.pool.ThreadedConnectionPool(PMIN, PMAX, dsn=DSN)
    return _pools["pg2"]


async def pg3async_pool():
    if "pg3async" not in _pools:
        from psycopg_pool import AsyncConnectionPool
        p = AsyncConnectionPool(DSN, min_size=PMIN, max_size=PMAX, open=False)
        await p.open()
        _pools["pg3async"] = p
    return _pools["pg3async"]


async def asyncpg_pool():
    if "asyncpg" not in _pools:
        import asyncpg
        _pools["asyncpg"] = await asyncpg.create_pool(
            host=PGHOST, port=PGPORT, database=PGDB, user=PGUSER,
            min_size=PMIN, max_size=PMAX)
    return _pools["asyncpg"]


def sync_redis():
    if "rs" not in _pools:
        import redis
        _pools["rs"] = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _pools["rs"]


async def async_redis():
    if "ra" not in _pools:
        import redis.asyncio as aioredis
        _pools["ra"] = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _pools["ra"]


def _row(r):
    return {"id": r[0], "sku": r[1], "name": r[2], "qty": r[3]}


def _wrote(commit):
    return {"ok": True, "committed": commit}


# ════════════ psycopg3 SYNC ════════════
def _pg3sync(op, commit):
    pool = pg3sync_pool()
    with pool.connection() as conn:
        conn.autocommit = False
        cur = conn.cursor()
        if op == "select_one":
            cur.execute(SEL_ONE_3, (5,)); r = cur.fetchone(); conn.commit()
            return _row(r)
        if op == "select_list":
            cur.execute(SEL_LIST_3, (10,)); rs = cur.fetchall(); conn.commit()
            return {"items": [_row(x) for x in rs]}
        if op == "insert":
            cur.execute(INS_3, ("x",))
        elif op == "update":
            cur.execute(UPD_3, ("y",))
        elif op == "delete":
            cur.execute(DEL_3, (DEL_ID,))
        conn.commit() if commit else conn.rollback()
        return _wrote(commit)


# ════════════ psycopg2 SYNC ════════════
def _pg2sync(op, commit):
    pool = pg2_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        if op == "select_one":
            cur.execute(SEL_ONE_3, (5,)); r = cur.fetchone(); conn.commit()
            return _row(r)
        if op == "select_list":
            cur.execute(SEL_LIST_3, (10,)); rs = cur.fetchall(); conn.commit()
            return {"items": [_row(x) for x in rs]}
        if op == "insert":
            cur.execute(INS_3, ("x",))
        elif op == "update":
            cur.execute(UPD_3, ("y",))
        elif op == "delete":
            cur.execute(DEL_3, (DEL_ID,))
        conn.commit() if commit else conn.rollback()
        return _wrote(commit)
    finally:
        pool.putconn(conn)


# ════════════ psycopg3 ASYNC ════════════
async def _pg3async(op, commit):
    pool = await pg3async_pool()
    async with pool.connection() as conn:
        await conn.set_autocommit(False)
        cur = conn.cursor()
        if op == "select_one":
            await cur.execute(SEL_ONE_3, (5,)); r = await cur.fetchone(); await conn.commit()
            return _row(r)
        if op == "select_list":
            await cur.execute(SEL_LIST_3, (10,)); rs = await cur.fetchall(); await conn.commit()
            return {"items": [_row(x) for x in rs]}
        if op == "insert":
            await cur.execute(INS_3, ("x",))
        elif op == "update":
            await cur.execute(UPD_3, ("y",))
        elif op == "delete":
            await cur.execute(DEL_3, (DEL_ID,))
        await (conn.commit() if commit else conn.rollback())
        return _wrote(commit)


# ════════════ asyncpg ASYNC ════════════
async def _asyncpg(op, commit):
    pool = await asyncpg_pool()
    async with pool.acquire() as conn:
        if op == "select_one":
            r = await conn.fetchrow(SEL_ONE_PG, 5)
            return {"id": r["id"], "sku": r["sku"], "name": r["name"], "qty": r["qty"]}
        if op == "select_list":
            rs = await conn.fetch(SEL_LIST_PG, 10)
            return {"items": [{"id": x["id"], "sku": x["sku"], "name": x["name"], "qty": x["qty"]} for x in rs]}
        # writes: explicit txn so commit=false can roll back (asyncpg is autocommit otherwise)
        tr = conn.transaction()
        await tr.start()
        try:
            if op == "insert":
                await conn.execute(INS_PG, "x")
            elif op == "update":
                await conn.execute(UPD_PG, "y")
            elif op == "delete":
                await conn.execute(DEL_PG, DEL_ID)
            await (tr.commit() if commit else tr.rollback())
        except Exception:
            await tr.rollback()
            raise
        return _wrote(commit)


DRIVERS_SYNC = {"pg3sync": _pg3sync, "pg2sync": _pg2sync}
DRIVERS_ASYNC = {"pg3async": _pg3async, "asyncpg": _asyncpg}


# ── driver-matrix routes (Python only) ───────────────────────────────
def _make_routes():
    for drv, fn in DRIVERS_SYNC.items():
        for op in ("select_one", "select_list", "insert", "update", "delete"):
            def mk(fn=fn, op=op):
                def h(commit: bool = True):
                    return fn(op, commit)
                return h
            app.add_api_route(f"/pgm/{drv}/{op}", mk(),
                              methods=["POST"] if op in ("insert", "update", "delete") else ["GET"])
    for drv, fn in DRIVERS_ASYNC.items():
        for op in ("select_one", "select_list", "insert", "update", "delete"):
            def mka(fn=fn, op=op):
                async def h(commit: bool = True):
                    return await fn(op, commit)
                return h
            app.add_api_route(f"/pgm/{drv}/{op}", mka(),
                              methods=["POST"] if op in ("insert", "update", "delete") else ["GET"])


_make_routes()


# ── cross-framework routes (representative drivers) ──────────────────
@app.get("/pg/select_one/sync")
def x_so_s():
    return _pg3sync("select_one", True)


@app.get("/pg/select_one/async")
async def x_so_a():
    return await _asyncpg("select_one", True)


@app.get("/pg/select_list/sync")
def x_sl_s():
    return _pg3sync("select_list", True)


@app.get("/pg/select_list/async")
async def x_sl_a():
    return await _asyncpg("select_list", True)


@app.post("/pg/insert/sync")
def x_i_s(commit: bool = True):
    return _pg3sync("insert", commit)


@app.post("/pg/insert/async")
async def x_i_a(commit: bool = True):
    return await _asyncpg("insert", commit)


@app.post("/pg/update/sync")
def x_u_s(commit: bool = True):
    return _pg3sync("update", commit)


@app.post("/pg/update/async")
async def x_u_a(commit: bool = True):
    return await _asyncpg("update", commit)


@app.post("/pg/delete/sync")
def x_d_s(commit: bool = True):
    return _pg3sync("delete", commit)


@app.post("/pg/delete/async")
async def x_d_a(commit: bool = True):
    return await _asyncpg("delete", commit)


# ── Redis ────────────────────────────────────────────────────────────
RKEY = "bench:item"
RWKEY = "bench:wkey"
RVAL = "bench-value"


@app.get("/redis/sync/get")
def r_get_s():
    return Response(content=sync_redis().get(RKEY), media_type="application/json")


@app.get("/redis/async/get")
async def r_get_a():
    r = await async_redis()
    return Response(content=await r.get(RKEY), media_type="application/json")


@app.post("/redis/sync/set")
def r_set_s():
    sync_redis().set(RWKEY, RVAL)
    return {"ok": True}


@app.post("/redis/async/set")
async def r_set_a():
    r = await async_redis()
    await r.set(RWKEY, RVAL)
    return {"ok": True}


@app.post("/redis/sync/set_durable")
def r_setd_s():
    # durability: SET then force an AOF fsync via WAITAOF (local fsync, 0 replicas).
    c = sync_redis()
    c.set(RWKEY, RVAL)
    try:
        c.execute_command("WAITAOF", 1, 0, 100)
    except Exception:
        pass
    return {"ok": True}


@app.post("/redis/async/set_durable")
async def r_setd_a():
    r = await async_redis()
    await r.set(RWKEY, RVAL)
    try:
        await r.execute_command("WAITAOF", 1, 0, 100)
    except Exception:
        pass
    return {"ok": True}


@app.post("/redis/sync/pipeline")
def r_pipe_s():
    pipe = sync_redis().pipeline(transaction=False)
    for i in range(10):
        pipe.set(f"{RWKEY}:{i}", RVAL)
    pipe.execute()
    return {"ok": True, "n": 10}


@app.post("/redis/async/pipeline")
async def r_pipe_a():
    r = await async_redis()
    pipe = r.pipeline(transaction=False)
    for i in range(10):
        pipe.set(f"{RWKEY}:{i}", RVAL)
    await pipe.execute()
    return {"ok": True, "n": 10}


@app.post("/redis/sync/multi")
def r_multi_s():
    pipe = sync_redis().pipeline(transaction=True)  # MULTI/EXEC
    for i in range(10):
        pipe.set(f"{RWKEY}:{i}", RVAL)
    pipe.execute()
    return {"ok": True, "n": 10}


@app.post("/redis/async/multi")
async def r_multi_a():
    r = await async_redis()
    pipe = r.pipeline(transaction=True)
    for i in range(10):
        pipe.set(f"{RWKEY}:{i}", RVAL)
    await pipe.execute()
    return {"ok": True, "n": 10}


@app.get("/_health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    if os.environ.get("BENCH_ENGINE") == "turbo":
        app.run(host="127.0.0.1", port=port, workers=int(os.environ.get("FASTAPI_TURBO_WORKERS", "8")))
    else:
        import uvicorn
        uvicorn.run("app_db:app", host="127.0.0.1", port=port,
                    workers=int(os.environ.get("BENCH_WORKERS", "8")), log_level="warning")
