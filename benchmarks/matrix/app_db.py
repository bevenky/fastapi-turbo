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

import asyncio
import os
import threading

if os.environ.get("BENCH_ENGINE") == "turbo":
    import fastapi_turbo  # noqa: F401

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

app = FastAPI()

PGHOST, PGPORT, PGDB, PGUSER = "127.0.0.1", 5432, "fastapi_turbo_bench", "venky"
DSN = f"host={PGHOST} port={PGPORT} dbname={PGDB} user={PGUSER}"
REDIS_URL = "redis://127.0.0.1:6379"
PMIN = int(os.environ.get("BENCH_POOL_MIN", "1"))
PMAX = int(os.environ.get("BENCH_POOL_MAX", "2"))  # tiny default — 100-conn budget across workers

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
# First-touch guards. Without these the check-then-create races: sync
# creators race across request threads; async creators AWAIT mid-create,
# so a warmup burst of concurrent first requests each builds its own pool
# and the losers leak their connections for the process lifetime →
# Postgres max_connections exhaustion (TooManyConnectionsError 500s)
# during warmup / after a few boots. Double-checked: steady state stays
# lock-free.
_pool_lock = threading.Lock()
_apool_lock = asyncio.Lock()


def pg3sync_pool():
    if "pg3sync" not in _pools:
        from psycopg_pool import ConnectionPool
        with _pool_lock:
            if "pg3sync" not in _pools:
                _pools["pg3sync"] = ConnectionPool(DSN, min_size=PMIN, max_size=PMAX, open=True)
    return _pools["pg3sync"]


def pg2_pool():
    if "pg2" not in _pools:
        import psycopg2.pool
        with _pool_lock:
            if "pg2" not in _pools:
                # Two psycopg2 pool quirks the other drivers don't have:
                # 1) getconn() RAISES PoolError when exhausted — psycopg3
                #    sync/async and asyncpg all BLOCK. Unguarded at w8/c64
                #    (~8 concurrent per worker vs max_size 2) 55-64% of
                #    pg2sync responses were fast 500s and the rows were
                #    error-storm artifacts (5-15k, erratic, once 0.8c).
                #    The semaphore restores blocking-checkout parity.
                # 2) putconn() CLOSES any connection beyond minconn idle
                #    (`len(_pool) < minconn` retention) — minconn=1 meant
                #    constant reconnect churn (PMAX=4 measured 2.5k rps of
                #    pure connect handshakes). minconn=maxconn disables the
                #    churn, matching the retention of every other pool.
                # Raw psycopg2 is only ~8-11% slower per op than psycopg3.
                _pools["pg2"] = (
                    psycopg2.pool.ThreadedConnectionPool(PMAX, PMAX, dsn=DSN),
                    threading.BoundedSemaphore(PMAX),
                )
    return _pools["pg2"]


async def pg3async_pool():
    if "pg3async" not in _pools:
        from psycopg_pool import AsyncConnectionPool
        async with _apool_lock:
            if "pg3async" not in _pools:
                p = AsyncConnectionPool(DSN, min_size=PMIN, max_size=PMAX, open=False)
                await p.open()
                _pools["pg3async"] = p
    return _pools["pg3async"]


async def asyncpg_pool():
    if "asyncpg" not in _pools:
        import asyncpg

        async def _no_reset(conn):
            # Default pool release runs a 4-statement reset script = +1 wire
            # round trip per request (audited). Bench conns hold no session
            # state (reads autocommit; writes always commit/rollback), so a
            # no-op reset gives asyncpg its true 1-RTT read cost.
            return None

        async with _apool_lock:
            if "asyncpg" not in _pools:
                _pools["asyncpg"] = await asyncpg.create_pool(
                    host=PGHOST, port=PGPORT, database=PGDB, user=PGUSER,
                    min_size=PMIN, max_size=PMAX, reset=_no_reset)
    return _pools["asyncpg"]


def sync_redis():
    if "rs" not in _pools:
        import redis
        with _pool_lock:
            if "rs" not in _pools:
                _pools["rs"] = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _pools["rs"]


async def async_redis():
    if "ra" not in _pools:
        import redis.asyncio as aioredis
        async with _apool_lock:
            if "ra" not in _pools:
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
        # Reads run autocommit = 1 wire round trip (parity with pgx/node-postgres,
        # audited); writes keep the implicit txn so commit=false can roll back.
        # Set unconditionally per checkout so the flag can't leak between ops.
        conn.autocommit = op in ("select_one", "select_list")
        cur = conn.cursor()
        if op == "select_one":
            cur.execute(SEL_ONE_3, (5,)); r = cur.fetchone()
            return _row(r)
        if op == "select_list":
            cur.execute(SEL_LIST_3, (10,)); rs = cur.fetchall()
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
    pool, gate = pg2_pool()
    with gate:  # blocking checkout, same semantics as the other 3 pools
        conn = pool.getconn()
        try:
            # Same read-autocommit fairness (1 RTT reads, txn writes).
            # Safe per-checkout: putconn rolls back any non-IDLE conn, and
            # this path always commits/rollbacks, so autocommit is never
            # toggled mid-transaction (psycopg2 forbids that).
            conn.autocommit = op in ("select_one", "select_list")
            cur = conn.cursor()
            if op == "select_one":
                cur.execute(SEL_ONE_3, (5,)); r = cur.fetchone()
                return _row(r)
            if op == "select_list":
                cur.execute(SEL_LIST_3, (10,)); rs = cur.fetchall()
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
        # Same read-autocommit fairness as _pg3sync (1 RTT reads, txn writes).
        await conn.set_autocommit(op in ("select_one", "select_list"))
        cur = conn.cursor()
        if op == "select_one":
            await cur.execute(SEL_ONE_3, (5,)); r = await cur.fetchone()
            return _row(r)
        if op == "select_list":
            await cur.execute(SEL_LIST_3, (10,)); rs = await cur.fetchall()
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

# Cross-framework /pg/*/async representative: asyncpg for BOTH engines.
# History: asyncpg looked pathological under the turbo door; root cause was an
# async-worker loop init race (fixed) — post-fix asyncpg beats psycopg3-async
# under the door as well (w8 45.3k vs ~40k rps; 26µs vs 45µs loop CPU per op).
# Override with BENCH_PG_ASYNC_DRIVER=pg3async|asyncpg for driver-vs-driver runs.
_CROSS_ASYNC_NAME = os.environ.get("BENCH_PG_ASYNC_DRIVER", "asyncpg")
_cross_async = DRIVERS_ASYNC[_CROSS_ASYNC_NAME]


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
    return await _cross_async("select_one", True)


@app.get("/pg/select_list/sync")
def x_sl_s():
    return _pg3sync("select_list", True)


@app.get("/pg/select_list/async")
async def x_sl_a():
    return await _cross_async("select_list", True)


@app.post("/pg/insert/sync")
def x_i_s(commit: bool = True):
    return _pg3sync("insert", commit)


@app.post("/pg/insert/async")
async def x_i_a(commit: bool = True):
    return await _cross_async("insert", commit)


@app.post("/pg/update/sync")
def x_u_s(commit: bool = True):
    return _pg3sync("update", commit)


@app.post("/pg/update/async")
async def x_u_a(commit: bool = True):
    return await _cross_async("update", commit)


@app.post("/pg/delete/sync")
def x_d_s(commit: bool = True):
    return _pg3sync("delete", commit)


@app.post("/pg/delete/async")
async def x_d_a(commit: bool = True):
    return await _cross_async("delete", commit)


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
