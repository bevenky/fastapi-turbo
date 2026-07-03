"""Raw driver micro: psqlpy (Rust PyO3 + tokio-postgres) vs asyncpg on ONE uvloop.

Evidence bench for the README "Driver guidance" psqlpy verdict. psqlpy is the
same architecture as the parked in-house Rust-PG fast path (side tokio runtime
bridged into the Python loop), which lost to asyncpg under the door (40.2k vs
50.1k w8c64, 92 vs 73µs conn=1) because every query pays 2 cross-thread wakes
+ 1 GIL attach where asyncpg's socket lives ON the loop. This micro tests
whether psqlpy escapes that; it does not.

Method (mirrors the parked-rustpg "pure client" micro): single process, single
uvloop; select-by-id -> endpoint-shaped dict; per metric REPS in-process
repetitions, median printed; whole script re-run 3x for medians of 3.
  - serial acquire:  checkout-per-op through each pool's own acquire path
                     (endpoint parity with app_db.py handlers)
  - serial HELD:     one connection held for the whole loop — pool checkout
                     removed; isolates the per-query driver cost
  - gather-64:       64 concurrent tasks, ops/s (the worker-loop c64 shape)
  - pool sizes 2 (matrix PMAX) and 8
asyncpg pool uses the no-op reset (same as app_db.py — audited 1-RTT reads).

Measured 2026-07-03 (psqlpy 0.12.1, asyncpg pool, medians of 3 runs):
  serial acquire  pool=8: asyncpg  83.1 µs/op   psqlpy 106.4  (1.28x slower)
  serial HELD conn:       asyncpg  31.2 µs/op   psqlpy  79.6  (2.55x slower)
  gather-64       pool=2: asyncpg 25.8k ops/s   psqlpy 14.4k  (0.56x)
  gather-64       pool=8: asyncpg 71.0k ops/s   psqlpy 22.2k  (0.31x)
psqlpy barely scales 2->8 conns (14.6k->22k vs asyncpg 25.8k->72k): it is
wake/GIL-bound, not connection-bound — exactly the parked-rustpg root cause.
"""
import asyncio
import statistics
import time

import uvloop

DSN_KW = dict(host="127.0.0.1", port=5432, database="fastapi_turbo_bench", user="venky")
PSQLPY_DSN = "postgres://venky@127.0.0.1:5432/fastapi_turbo_bench"
SQL = "SELECT id, sku, name, qty FROM items WHERE id = $1"

SERIAL_N = 2000
GATHER_TASKS = 64
GATHER_OPS = 250          # per task -> 16k ops per rep
REPS = 5
EXPECT = {"id": 5, "sku": "SKU-5", "name": "item-5", "qty": 5}


async def apg_op(pool):
    async with pool.acquire() as conn:
        r = await conn.fetchrow(SQL, 5)
        return {"id": r["id"], "sku": r["sku"], "name": r["name"], "qty": r["qty"]}


def apg_held_op(conn):
    async def op():
        r = await conn.fetchrow(SQL, 5)
        return {"id": r["id"], "sku": r["sku"], "name": r["name"], "qty": r["qty"]}
    return op


async def psq_op(pool):
    async with pool.acquire() as conn:
        res = await conn.execute(SQL, [5])
        return res.result()[0]


def psq_held_op(conn):
    async def op():
        return (await conn.execute(SQL, [5])).result()[0]
    return op


async def serial(op, n=SERIAL_N):
    out = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        for _ in range(n):
            await op()
        out.append((time.perf_counter() - t0) / n * 1e6)
    return statistics.median(out)


async def bench(name, pool, op):
    for _ in range(300):
        await op()
    assert (await op()) == EXPECT

    s = await serial(op)

    async def task():
        for _ in range(GATHER_OPS):
            await op()

    gather = []
    for _ in range(REPS):
        t0 = time.perf_counter()
        await asyncio.gather(*[task() for _ in range(GATHER_TASKS)])
        gather.append(GATHER_TASKS * GATHER_OPS / (time.perf_counter() - t0))
    g = statistics.median(gather)

    print(f"{name:24s} serial {s:7.1f} µs/op   gather-64 {g:10,.0f} ops/s")
    return s, g


async def main():
    import asyncpg
    from psqlpy import ConnectionPool

    async def _no_reset(conn):
        return None

    out = {}
    for size in (2, 8):
        apg = await asyncpg.create_pool(min_size=size, max_size=size, reset=_no_reset, **DSN_KW)
        out[f"a{size}"] = await bench(f"asyncpg   pool={size}", apg, lambda: apg_op(apg))
        if size == 8:  # held-conn AFTER the pool benches so it can't shrink them
            conn = await apg.acquire()
            h = await serial(apg_held_op(conn))
            print(f"{'asyncpg   HELD conn':24s} serial {h:7.1f} µs/op")
            await apg.release(conn)
        await apg.close()

        psq = ConnectionPool(dsn=PSQLPY_DSN, max_db_pool_size=size)
        out[f"p{size}"] = await bench(f"psqlpy    pool={size}", psq, lambda: psq_op(psq))
        if size == 8:
            pconn = await psq.connection()
            h = await serial(psq_held_op(pconn))
            print(f"{'psqlpy    HELD conn':24s} serial {h:7.1f} µs/op")
            del pconn
        psq.close()

    print()
    for size in (2, 8):
        a_s, a_g = out[f"a{size}"]
        p_s, p_g = out[f"p{size}"]
        print(f"pool={size}: psqlpy serial {p_s / a_s:5.2f}x asyncpg µs/op | "
              f"gather-64 {p_g / a_g:5.2f}x asyncpg ops/s")


if __name__ == "__main__":
    uvloop.install()
    asyncio.run(main())
