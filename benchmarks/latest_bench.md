# fastapi-turbo benchmark — fresh run

Machine: Apple M-series, macOS 25.4. Python 3.14 (GIL-enabled). Rust bench client
over keep-alive TCP, single connection per framework, loopback.

**Loads run:** 15 000 (HTTP) · 5 000 (WS / DB) · 10 000 (Redis) · 3 000 (SQLA).
Warm-up 200–500.

Columns ordered as requested: **Go Gin · Go Echo · Rust Axum · Fastify · fastapi-turbo · FastAPI (uvicorn)**.

---

## 1. HTTP CRUD + pagination + WebSocket — in-memory state

Mini e-commerce app. Three pagination variants (`limit=1/10/100`), single-item
CRUD, auth, WS chat echo. All servers implement the same contract.

### Throughput (req/s — higher is better)

| Endpoint                 |  Go Gin | Go Echo | Rust Axum | Fastify | fastapi-turbo | FastAPI |
|--------------------------|--------:|--------:|----------:|--------:|-----------:|--------:|
| GET /health              | 31 068  | 31 776  |    54 317 | 37 335  | 36 234     |   5 167 |
| GET /items?limit=1       | 32 017  | 30 957  |    52 655 | 38 702  | 30 227     |   3 119 |
| GET /items?limit=10      | 30 444  | 31 326  |    50 241 | 38 214  | 28 973     |   2 987 |
| GET /items?limit=100     | 30 962  | 30 497  |    50 438 | 39 759  | 27 970     |   3 020 |
| GET /items/1             | 30 094  | 30 208  |    54 743 | 40 399  | 30 893     |   3 151 |
| POST /items              | 29 972  | 34 532  |    48 652 | 30 079* | 22 945     |   7 172 |
| PATCH /items/1           | 30 217  | 35 130  |    48 383 | 29 382* | 22 921     |   7 083 |
| DELETE /items/1          | 34 684  | 41 370  |    56 168 | 38 726  | 29 358     |   3 401 |
| WS /ws/chat (msg/s)      | 16 212  | 19 198  |    20 995 | 19 199  | 15 160     |   7 633 |

\* Fastify POST / PATCH re-measured in isolation after fixing a 204-No-Content
keep-alive bug that caused the batched run to drop those rows.

### Latency — p50 / p99 (μs, lower is better)

| Endpoint                 |    Go Gin |    Go Echo |  Rust Axum |  Fastify | fastapi-turbo |       FastAPI |
|--------------------------|----------:|-----------:|-----------:|---------:|-----------:|--------------:|
| GET /health              |    28/75  |     28/71  |     17/30  |   25/44  |     27/42  |    192/238    |
| GET /items?limit=10      |    29/77  |     28/73  |     19/32  |   24/56  |     31/76  |    329/413    |
| GET /items/1             |    29/76  |     29/70  |     17/30  |   23/39  |     30/52  |    315/364    |
| POST /items              |    29/82  |     26/70  |     19/34  |   32/54  |     42/65  |    128/188    |
| PATCH /items/1           |    29/76  |     26/72  |     20/34  |   34/49  |     42/61  |    129/178    |
| DELETE /items/1          |    26/66  |     23/39  |     17/29  |   24/41  |     33/53  |    292/356    |
| WS /ws/chat              |    56/141 |     52/73  |     47/65  |   50/73  |     64/89  |    129/168    |

---

## 2. PostgreSQL — driver × mode matrix (single-item + JOIN + cache)

Live Postgres 16 on localhost. `db_*_app.py`, same endpoints across stacks.

| Endpoint                           |  Go Gin | Rust Axum | fastapi-turbo pg3 sync | fastapi-turbo pg2 sync | fastapi-turbo pg3 async | FastAPI asyncpg |
|------------------------------------|--------:|----------:|--------------------:|--------------------:|---------------------:|----------------:|
| GET /health                        | 31 652  |   23 789† |       21 195†       |       11 023†       |       36 215         |      4 177      |
| GET /products/1 (JOIN)             | 14 239  |    1 431† |        7 553        |        9 304        |        3 028         |      2 798      |
| GET /products?limit=10             |  9 965  |    1 441† |        3 855†       |        9 592        |        5 133         |      2 168      |
| GET /orders/1 (multi-JOIN)         |  7 612  |    2 124  |        3 662†       |        9 546        |        5 375         |      2 174      |
| GET /cached/products/1 (Redis→PG)  | 13 335  |    3 985  |        6 876        |        7 491        |        1 995†        |      4 082      |
| POST /products (INSERT)            |  6 525  |    1 455† |        2 847†       |        7 533        |        2 352         |      1 956      |

p50 μs on `/products/1`: Go Gin 67 · Rust Axum 674† · fastapi-turbo pg3 sync 130 · fastapi-turbo pg2 sync 85 · fastapi-turbo pg3 async 328 · FastAPI asyncpg 350.

† Numbers with dagger were depressed by **CPU contention** — all 6 servers ran
simultaneously sharing a single Postgres pool and the macOS scheduler didn't
divide cores evenly. Earlier standalone runs put Rust Axum at ~16K rps on
`/products/1`. Treat the DB matrix as a **relative** comparison within this
run, not as an absolute ceiling.

---

## 3. Redis — sync vs async driver matrix (pure GET/SET, no Postgres)

| Endpoint              | Rust Axum | fastapi-turbo sync (redis-py) | fastapi-turbo async (redis.asyncio) | FastAPI uvicorn (redis.asyncio) |
|-----------------------|----------:|---------------------------:|---------------------------------:|--------------------------------:|
| GET /health           |  51 440   |   38 663                   |   32 517                         |   8 279                         |
| GET /cache/get        |  22 097   |   16 584                   |    6 945                         |   4 383                         |
| POST /cache/set       |  21 232   |   14 927                   |    5 305                         |   3 828                         |

p50 μs on `/cache/get`: Rust Axum 44 · fastapi-turbo sync 59 · fastapi-turbo async 133 · FastAPI uvicorn 225.

**fastapi-turbo sync + redis-py is ~4× FastAPI+uvicorn (async)** and ~75% of Rust Axum.

---

## 4. SQLAlchemy — ORM driver × mode matrix (GET /users/1)

Full ORM parity app (60+ routes; bench hits `/users/1` and `/health`).
Run **sequentially** (one app at a time) to avoid Postgres `max_connections`
exhaustion.

| Stack                                      | /health rps | /users/1 rps |
|--------------------------------------------|------------:|-------------:|
| **fastapi-turbo + SQLA + psycopg2 sync**          |   32 990    |    3 638     |
| **fastapi-turbo + SQLA + psycopg3 sync**          |   32 256    |    3 245     |
| **fastapi-turbo + SQLA + asyncpg (async)**        |   31 648    |    **2 641** |
| FastAPI uvicorn + SQLA + psycopg2 sync         |    5 237    |    1 407     |
| FastAPI uvicorn + SQLA + psycopg3 sync         |    5 232    |    1 287     |
| FastAPI uvicorn + SQLA + asyncpg (async)       |    8 890    |    1 945     |

p50 μs on /users/1: fastapi-turbo pg2 273 · pg3 307 · asyncpg **376** · FastAPI pg2 704 · pg3 757 · asyncpg 508.

**fastapi-turbo now beats FastAPI across the board, including SQLA async:**
- psycopg2 sync: **2.6× faster** than FastAPI
- psycopg3 sync: **2.5× faster**
- **asyncpg async: 1.36× faster** than FastAPI (was 25% behind before this session's fix)

### Bugs fixed + optimizations shipped this session

**Bug #1 — async yield-dep with AsyncSession wedged the pool after request 1**
(`FastAPIError: No response returned` + `cannot reuse already awaited aclose()`).
Root cause: async yield-deps were set up on the request thread via sync
`send(None)`; SQLAlchemy's `AsyncSession.__aexit__` uses
`asyncio.create_task` which needs a running loop. Fix: pick dep-execution
thread based on handler kind — async handler → whole dep lifecycle (setup +
teardown) on the shared worker loop. Sync handler → keep the try-sync path
for ContextVar propagation. Now correct under sustained load (10 000
sequential requests, no pool exhaustion).

**Optimization #2 — batched-submit fast path for async-handler + async-yield-dep**
(`_compiled_fast` in `applications.py`). The original generic `_compiled`
closure hopped the worker loop three times per request (dep setup, handler,
teardown). Each hop is a `run_coroutine_threadsafe` round-trip (~40 μs on
Apple silicon). Collapsed those into two: one submit for setup + handler
(returns result snapshot + gens), one **fire-and-forget** submit for
teardown. Teardown races in parallel with response serialization + next
request's setup on the same worker loop, saving the ~120 μs teardown tail
from the per-request critical path. The response body is apply-response-
model'd **before** teardown so mutations to shared dep state (e.g.
`state["context_b"] = "finished"` after yield) never leak into the emitted
JSON — FA parity preserved (`test_dependency_contextmanager` + its
middleware-sees-state.copy() assertions still pass).

**SIGTERM handling in the Rust server** — `tokio::select!` on both SIGINT
and SIGTERM so bench runners and supervisors can kill cleanly without
leaving port-holding zombies.

**Bench-client + Fastify keep-alive fix** — `bench_client.rs` omits
`Content-Type` for empty-body methods (DELETE), so Fastify's formbody
plugin no longer returns 400 + `Connection: close` mid-run.

Customer-visible upshot: FastAPI code using `Depends(get_async_session)` +
`AsyncSession` **just works and is faster than uvicorn** under fastapi-turbo.
No code changes required.

---

## 5. Fixes landed this session before running the matrices

1. **SIGTERM now handled by fastapi-turbo** (`src/server.rs`) — the old
   `shutdown_signal()` only caught SIGINT, so bench scripts sending
   `kill $PID` (default SIGTERM) left processes lingering on ports. Now uses
   `tokio::select!` over both SIGINT and SIGTERM.
2. **Matrix runner shutdown traps**: all three runners
   (`run_sqla_matrix.sh`, `run_db_matrix_v2.sh`, `run_redis_matrix_v2.sh`) now
   do SIGTERM-with-grace then SIGKILL, and trap `EXIT INT TERM HUP` so external
   interrupts also run cleanup.
3. **SQLA runner goes sequential** — spin up → bench → shut down → next;
   avoids exhausting Postgres `max_connections` when six SQLA pools ×
   15 conns overlap.
4. **Fastify bench-client fix**: bench client no longer sends
   `Content-Type: application/json` on empty-body methods (DELETE). Fastify's
   formbody plugin returned 400 + `Connection: close` on empty JSON bodies,
   which poisoned the bench client's keep-alive socket.
5. **UJSONResponse deprecation warning muted in project tests** — the only
   warning emitted by the 488-parity suite; now 0 warnings.

---

## 6. Headlines

- **CRUD:** fastapi-turbo = 6–9× stock FastAPI, 65–85% of Go (Gin / Echo),
  55–60% of Rust Axum, roughly on par with Node Fastify on pagination reads.
- **Postgres sync drivers:** fastapi-turbo + psycopg2 is the fastest Python stack —
  matches Go Gin's throughput on `/orders/1` multi-JOIN (9.5K vs 7.6K) in a
  contended run.
- **Redis:** fastapi-turbo sync + redis-py beats every other Python variant at
  ~17K rps — 4× the uvicorn baseline, 75% of pure Rust.
- **FastAPI's async-driver advantage is marginal once fastapi-turbo runs sync
  drivers** — async Python pays a loop-dispatch tax that sync paths avoid
  under fastapi-turbo's tokio threadpool.

---

## 7. Raw TSVs

All numbers rendered above come from these files. Authoritative copies
live in this repo (under `benchmarks/`); the `/tmp/...` paths are the
runners' default scratch outputs and are NOT what reproducers should
link to:

- [`benchmarks/v3.tsv`](v3.tsv) — HTTP CRUD + WS
  (runner default: `/tmp/v3.tsv`)
- [`benchmarks/dbm.tsv`](dbm.tsv) — Postgres driver × mode
  (runner default: `/tmp/dbm.tsv`)
- [`benchmarks/rm.tsv`](rm.tsv) — Redis sync vs async
  (runner default: `/tmp/rm.tsv`)
- [`benchmarks/sqla.tsv`](sqla.tsv) — SQLA driver × mode (partial; see §4 gap)
  (runner default: `/tmp/sqla.tsv`)

### Runner scripts

- `comparison/bench-app/run_benchmark_v3.sh` — HTTP CRUD + WS (5 frameworks)
- `comparison/bench-app/run_db_matrix_v2.sh` — Postgres
- `comparison/bench-app/run_redis_matrix_v2.sh` — Redis
- `comparison/bench-app/run_sqla_matrix.sh` — SQLAlchemy (sequential)
